"""Accounts (usernames, type tags, verified badge, categories), baseline models, the agent view and the waitlist. Offline."""

import re

import pytest
from fastapi.testclient import TestClient

from agent.harvest import accounts, baseline_models, ledger, market, scoring, waitlist
from agent.routes import harvest as routes
from agent.routes import public_mode
from agent.server import app

PILOT = re.compile(r"^pilot-[0-9a-f]{4,}$")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    for limiter in routes.LIMITERS:
        limiter.reset()
    market.reset_live_book()
    yield TestClient(app)
    market.reset_live_book()


# ---------------------------------------------------------------- usernames
@pytest.mark.parametrize("raw, want", [("maria", "maria"), ("  @Maria_1 ", "maria_1"), ("a-b", "a-b"), ("x" * 24, "x" * 24)])
def test_normalise_username_accepts_clean_names(raw, want):
    assert accounts.normalise_username(raw) == want


@pytest.mark.parametrize("raw", ["ab", "x" * 25, "-lead", "has space", "émile", "a.b", "admin", "fao--gpt", "", None, 7])
def test_normalise_username_rejects_bad_names(raw):
    with pytest.raises(accounts.UsernameError):
        accounts.normalise_username(raw)


def test_join_defaults_to_a_generated_username(client):
    j = client.post("/harvest/api/join", json={"nickname": "Ana"}).json()
    assert PILOT.match(j["username"]) and j["type"] == "human" and j["nickname"] == "Ana"
    me = client.get("/harvest/api/me", params={"token": j["token"]}).json()
    assert me["username"] == j["username"] and me["type"] == "human"


def test_join_with_a_chosen_username_is_unique_case_insensitively(client):
    first = client.post("/harvest/api/join", json={"username": "Maria_1"})
    assert first.status_code == 200 and first.json()["username"] == "maria_1" and first.json()["nickname"] == "maria_1"
    again = client.post("/harvest/api/join", json={"username": "MARIA_1"})
    assert again.status_code == 409 and "taken" in again.json()["error"]
    assert client.post("/harvest/api/join", json={"username": "bad name"}).status_code == 422


def test_house_and_baseline_names_are_taken(client):
    house = client.get("/harvest/api/state").json()["personas"][0]["id"]
    assert client.post("/harvest/api/join", json={"username": house}).status_code in (409, 422)  # taken, or a '<persona>--<model>' id
    assert client.post("/harvest/api/join", json={"username": "momentum-bot"}).status_code == 409


def test_usernames_survive_a_restart_and_legacy_entries_get_one(client):
    j = client.post("/harvest/api/join", json={"username": "keeper"}).json()
    ledger.append(kind="join", trader="user:legacy", payload={"nickname": "Old timer"})
    market.reset_live_book()
    book = market.live_book()
    assert book.traders[j["user_id"]]["username"] == "keeper"
    legacy = book.traders["user:legacy"]["username"]
    assert PILOT.match(legacy)
    market.reset_live_book()
    assert market.live_book().traders["user:legacy"]["username"] == legacy  # deterministic


def test_agent_register_takes_or_generates_a_username(client):
    body = {"name": "Drought Bear", "world_model": "Heat cuts yields; prices lag the weather by a season."}
    named = client.post("/harvest/api/agents/register", json={**body, "username": "drought-bear"}).json()
    assert named["username"] == "drought-bear" and named["type"] == "agent"
    assert PILOT.match(client.post("/harvest/api/agents/register", json=body).json()["username"])
    assert client.post("/harvest/api/agents/register", json={**body, "username": "drought-bear"}).status_code == 409
    assert client.get(f"/harvest/api/agents/{named['agent_id']}").json()["username"] == "drought-bear"


# ---------------------------------------------------------------- one leaderboard, four categories
def test_leaderboard_lists_every_account_with_category_and_type(client):
    client.post("/harvest/api/join", json={"username": "human-one"})
    client.post("/harvest/api/agents/register", json={"name": "Bot", "world_model": "Prices trend.", "username": "bot-one"})
    lb = client.get("/harvest/api/leaderboard").json()
    rows = {r["username"]: r for r in lb["accounts"]}
    assert rows["human-one"]["category"] == "users" and rows["human-one"]["type"] == "human"
    assert rows["bot-one"]["category"] == "users" and rows["bot-one"]["type"] == "agent"
    assert {u for u, r in rows.items() if r["category"] == "baseline"} == set(baseline_models.USERNAMES)
    origins = [m["origin"] for m in lb["world_models"]]
    assert lb["categories"]["house"] == origins.count("house") and lb["categories"]["verified"] == origins.count("narrative")
    assert lb["categories"]["baseline"] == len(baseline_models.SPECS)
    scored = [r["brier_holdout"] for r in lb["accounts"] if r["brier_holdout"] is not None]
    assert scored == sorted(scored) and lb["accounts"][-1]["brier_holdout"] is None
    assert [r["rank"] for r in lb["accounts"]] == list(range(1, len(lb["accounts"]) + 1))
    assert lb["verified_label"] == "based on public statements; not endorsed"


def test_narrative_personas_are_verified_with_sources():
    personas = [{"id": "fao", "name": "FAO outlook", "origin": "narrative", "sources": [{"title": "Food Outlook", "url": "https://www.fao.org/x"}, "https://example.org/y",
                                                                                  {"url": "https://www.fao.org/z", "date": "2025-07", "note": "Price index brief"}]},
                {"id": "climate-pessimist", "name": "Climate Pessimist"}]
    wm = [{"persona_id": "fao--gpt-4o-mini", "name": "FAO outlook", "origin": "house", "brier_holdout": 0.2, "n": 3},
          {"persona_id": "climate-pessimist", "name": "Climate Pessimist", "origin": "house", "brier_holdout": 0.1, "n": 3}]
    rows = accounts.board(wm, personas=personas, traders={}, baselines=[], humans=[])
    fao = next(r for r in rows if r["id"].startswith("fao"))
    assert fao["category"] == "verified" and fao["verified"] and fao["verified_label"] == accounts.VERIFIED_LABEL
    assert fao["sources"] == [{"title": "Food Outlook", "url": "https://www.fao.org/x"}, {"title": "https://example.org/y", "url": "https://example.org/y"},
                              {"title": "Price index brief (2025-07)", "url": "https://www.fao.org/z"}]
    assert rows[0]["username"] == "climate-pessimist" and rows[0]["category"] == "house" and not rows[0]["verified"]
    assert accounts.category_counts(rows) == {"verified": 1, "house": 1, "baseline": 0, "users": 0}


def test_baselines_reuse_the_scoring_baselines(client):
    result = routes.replay_result()
    rows = {r["id"]: r for r in baseline_models.rows(result, routes.price_fixture())}
    base = scoring.leaderboard(result)["baselines"]
    assert rows["baseline:market-follower"]["brier_holdout"] == pytest.approx(base["market"])
    assert rows["baseline:climatology"]["brier_holdout"] == pytest.approx(base["climatology"])
    n = len(scoring.holdout_outcomes(result))
    for r in rows.values():
        assert r["n"] == n and r["brier_holdout"] is not None and r["brier_holdout_lo"] <= r["brier_holdout"] <= r["brier_holdout_hi"]


# ---------------------------------------------------------------- agent view
def test_agent_view_shows_what_an_agent_sees(client):
    listed = client.get("/harvest/api/agents/markets").json()["markets"]
    hard = next(m for m in listed if m["kind"] == "hard" and m["visible_history"])
    v = client.get(f"/harvest/api/markets/{hard['qid']}/agent").json()
    assert re.fullmatch(r"Commodity [A-Z]", v["masked"]["label"])
    assert v["masked"]["window"][0] == ["t-12", 100.0] and v["masked"]["window"][-1][0] == "t0"
    assert v["masked"]["label"] in v["masked"]["question"] and "20" not in v["masked"]["question"].split("index")[0]
    assert v["agents_markets_entry"] == hard
    assert isinstance(v["news"], list) and v["forecasts"]
    for f in v["forecasts"]:
        assert set(f) >= {"username", "p_yes", "rationale", "model", "timestamp"} and 0 < f["p_yes"] < 1
    assert client.get("/harvest/api/markets/nope/agent").status_code == 404


def test_agent_view_page_is_public(client):
    assert client.get("/app/market/wheat-up10-6m%402026-09?view=agent").status_code == 200
    assert public_mode.is_public("GET", "/harvest/api/markets/wheat-up10-6m@2026-09/agent")


# ---------------------------------------------------------------- waitlist
def test_waitlist_adds_dedupes_and_counts(client):
    r = client.post("/harvest/api/waitlist", json={"contact": "Ana@Example.org", "note": "farmer in Andalusia"})
    assert r.status_code == 200 and r.json() == {"ok": True, "already": False, "count": 1}
    assert client.post("/harvest/api/waitlist", json={"email": "ana@example.org"}).json()["already"] is True
    assert client.post("/harvest/api/waitlist", json={"handle": "@maria"}).json()["count"] == 2
    assert client.post("/harvest/api/waitlist", json={"contact": "not an email"}).status_code == 422
    assert client.post("/harvest/api/waitlist", json={"contact": "@x2", "note": "n" * 281}).status_code == 422
    client.post("/harvest/api/join", json={"username": "pilot-one"})
    assert client.get("/harvest/api/waitlist/count").json() == {"count": 2, "waitlist": 2, "pilots": 1}
    assert waitlist.count() == 2


def test_waitlist_is_rate_limited_and_public(client, monkeypatch):
    monkeypatch.setattr(routes.waitlist_limiter, "limit", 2)
    codes = [client.post("/harvest/api/waitlist", json={"contact": f"@user{i}"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    assert public_mode.is_public("POST", "/harvest/api/waitlist") and public_mode.is_public("GET", "/harvest/api/waitlist/count")
    assert client.get("/harvest/api/waitlist").status_code in (404, 405)
