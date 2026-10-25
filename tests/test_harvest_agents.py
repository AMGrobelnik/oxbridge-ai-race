"""Debate threads, resolution (FRED oracle, evidence proposals, admin confirm) and news-drafted markets. Offline:
the LLM and FRED are stubs; the background jobs are switched off."""

import json

import pytest
from fastapi.testclient import TestClient

from agent.harvest import debate, forecaster, ledger, live_forecast, market, market_gen, news, resolve
from agent.routes import harvest as harvest_routes
from agent.server import app

ADMIN = "test-admin-token"
P1 = {"id": "climate-pessimist", "name": "Climate Pessimist", "color": "#d9480f", "world_model": "heat -> shocks",
      "beliefs": ["droughts cut yields"], "heuristic": {"type": "momentum"}, "soft_lean": 0.15, "risk": 0.25, "edge": 0.03}
P2 = {**P1, "id": "adaptation-optimist", "name": "Adaptation Optimist", "soft_lean": -0.1}
P3 = {**P1, "id": "contrarian", "name": "Contrarian", "soft_lean": 0.0}
HARD = {"qid": "wheat-up10-6m@2026-06", "spec_id": "wheat-up10-6m", "kind": "hard", "title": "Will wheat be more than 10% higher by 2026-12?",
        "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "cutoff": "2026-06", "series": "PWHEAMTUSDM", "base_month": "2026-06",
        "base_value": 300.0, "resolve_month": "2026-12", "target": 330.0,
        "rule": {"type": "pct_change_gt", "threshold": 0.1, "horizon_months": 6}, "prior": 0.5}
SOFT = [{"id": "it-drought-2027", "kind": "soft", "title": "Will Italy declare a national drought emergency in 2027?", "deadline": "2027-12-31",
         "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "prior": 0.35, "why": ""}]
ITEMS = [
    {"title": "Italy declares drought emergency as durum fields wither", "url": "https://example.com/italy-drought",
     "source": "Grain wire", "published_at": "2026-10-22T12:00:00Z"},
    {"title": "Spain olive oil harvest seen lower after drought in Jaen", "url": "https://example.com/olive",
     "source": "Olive Oil Times", "published_at": "2026-10-23T12:00:00Z"},
]


def _result():
    fc = [("climate-pessimist", 0.8), ("adaptation-optimist", 0.2), ("contrarian", 0.5)]
    live = {"cutoff": "2026-06", "split": "live", "questions": [HARD], "windows": {}, "trades": [], "resolutions": [], "payouts": [],
            "forecasts": [{"persona_id": p, "qid": HARD["qid"], "p_yes": v, "rationale": f"{p} replay belief", "source": "cache"} for p, v in fc],
            "final_prices": {HARD["qid"]: 0.5}}
    return {"personas": [P1, P2, P3], "cutoffs": [live]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    for name in ("NEWS_POLL_S", "LIVE_REFORECAST_S", "DEBATE_S", "RESOLVE_ORACLE_S", "RESOLVE_EVIDENCE_S", "MARKET_GEN_S"):
        monkeypatch.setenv(name, "0")  # no background loop may start even with the LLM switched on below
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setattr(market, "load_replay", _result)
    monkeypatch.setattr(harvest_routes, "replay_result", _result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: SOFT)
    monkeypatch.setattr(live_forecast, "gemini_ask", lambda prompt: pytest.fail("network LLM called in a test"))
    monkeypatch.setattr(resolve, "fetch_series", lambda sid: pytest.fail("network FRED called in a test"))
    market.reset_live_book()
    for job in (debate.job, market_gen.job):
        job.reset()
    resolve.reset()
    yield
    market.reset_live_book()
    resolve.reset()


@pytest.fixture
def llm_on(monkeypatch):
    """The LLM path enabled (the stubbed `ask` is the only model)."""
    monkeypatch.setattr(forecaster, "offline_env", lambda: False)


@pytest.fixture
def client():
    return TestClient(app)


def _json_ask(*answers):
    calls = []

    def ask(prompt):
        calls.append(prompt)
        return json.dumps(answers[min(len(calls), len(answers)) - 1])

    ask.calls = calls
    return ask


def _admin():
    return {"X-Admin-Token": ADMIN}


# ---------------------------------------------------------------- debate
def test_debate_round_stores_case_and_reply(llm_on, client):
    book = market.live_book()
    ask = _json_ask({"text": "Heat in the Po valley will squeeze durum supply; the market under-prices the tail."},
                    {"text": "Climate Pessimist, stocks are fine and farmers adapt; a 10% jump needs more than one hot summer."})
    first, second = debate.round_once(book, ask)
    assert len(ask.calls) == 2 and "Adaptation Optimist" in ask.calls[1] and first["text"] in ask.calls[1]
    assert first["qid"] == second["qid"] == HARD["qid"]
    assert {first["author"], second["author"]} == {"agent:climate-pessimist", "agent:adaptation-optimist"}
    assert first["reply_to"] is None and second["reply_to"] == first["id"]
    assert first["p_yes"] in (0.8, 0.2) and first["source"] == second["source"] == "llm"

    body = client.get(f"/harvest/api/debate?qid={HARD['qid']}").json()
    assert len(body["threads"]) == 1
    posts = body["threads"][0]["posts"]
    assert [p["id"] for p in posts] == [first["id"], second["id"]]
    assert set(posts[0]) >= {"id", "qid", "author", "reply_to", "text", "p_yes", "at", "name"}
    assert client.get(f"/harvest/api/debate?since={second['id']}").json()["threads"] == []
    assert client.get("/harvest/api/debate?qid=nope").json()["threads"] == []


def test_debate_falls_back_and_respects_the_cap(llm_on, monkeypatch):
    book = market.live_book()

    def broken(prompt):
        raise TimeoutError("slow model")

    posts = debate.round_once(book, broken)
    assert [p["source"] for p in posts] == ["fallback", "fallback"] and "replay belief" in posts[0]["text"]
    monkeypatch.setenv("DEBATE_MAX_CALLS", "2")
    assert debate.round_once(book, broken) == []  # the cap of 2 calls is spent


def test_debate_offline_makes_no_call():
    assert debate.round_once(market.live_book(), lambda p: pytest.fail("LLM called offline")) == []
    assert debate.add_post("x", "agent:a", "hello there")["reply_to"] is None
    with pytest.raises(ValueError):
        debate.add_post("x", "agent:b", "reply to nothing", reply_to=999)


# ---------------------------------------------------------------- hard auto-resolve
def _join_and_bet(client, side="YES"):
    tok = client.post("/harvest/api/join", json={"nickname": "ada"}).json()["token"]
    r = client.post("/harvest/api/bet", json={"token": tok, "qid": HARD["qid"], "side": side, "spend": 50})
    assert r.status_code == 200, r.text
    return tok


def test_hard_market_auto_resolves_from_new_fred_point(client):
    tok = _join_and_bet(client)
    book = market.live_book()
    user = next(t for t, i in book.traders.items() if i["kind"] == "human")
    shares = book.holdings[(user, HARD["qid"])]["YES"]
    before = book.balance(user)
    fetched = []

    def fred(sid, pts=(("2026-06", 300.0), ("2026-11", 320.0))):
        fetched.append(sid)
        return list(pts)

    # resolve month still in the future: nothing is fetched
    assert resolve.oracle_once(book, fetch=fred, month="2026-12") == [] and fetched == []
    # month over but the value is not out yet
    assert resolve.oracle_once(book, fetch=fred, month="2027-01", now=0.0) == [] and fetched == ["PWHEAMTUSDM"]
    # within the hour: no re-fetch even though a new point would be there
    assert resolve.oracle_once(book, fetch=lambda s: pytest.fail("re-fetched within the hour"), month="2027-01", now=60.0) == []

    def fred_new(sid):
        return [("2026-06", 300.0), ("2026-11", 320.0), ("2026-12", 340.0)]

    assert resolve.oracle_once(book, fetch=fred_new, month="2027-01", now=4000.0) == [HARD["qid"]]
    assert book.markets[HARD["qid"]]["status"] == "resolved" and book.markets[HARD["qid"]]["outcome"] == 1
    assert book.balance(user) == pytest.approx(before + shares)
    entry = [e for e in ledger.all_entries() if e["kind"] == "resolve"][-1]
    assert entry["trader"] == "oracle:fred" and entry["payload"]["outcome"] == 1 and entry["payload"]["value"] == 340.0
    assert entry["payload"]["agent_briers"]["climate-pessimist"] == pytest.approx(0.04)
    assert any(p["trader"] == user for p in entry["payload"]["payouts"])
    assert ledger.verify()["ok"]

    lb = client.get("/harvest/api/leaderboard").json()
    pess = next(m for m in lb["world_models"] if m["persona_id"] == "climate-pessimist")
    assert pess["brier_live"] == pytest.approx(0.04) and pess["n_live"] == 1
    st = next(s for s in client.get("/harvest/api/resolutions").json()["markets"] if s["qid"] == HARD["qid"])
    assert st["state"] == "resolved" and st["outcome"] == "YES"
    assert client.get(f"/harvest/api/me?token={tok}").json()["balance"] == pytest.approx(before + shares)

    market.reset_live_book()  # a restart replays the resolve entry
    again = market.live_book()
    assert again.markets[HARD["qid"]]["status"] == "resolved" and again.balance(user) == pytest.approx(before + shares)


def test_hard_status_waits_for_data(client):
    st = next(s for s in client.get("/harvest/api/resolutions").json()["markets"] if s["qid"] == HARD["qid"])
    assert st["state"] == "waiting" and st["mode"] == "auto" and st["resolve_month"] == "2026-12" and st["expected_by"] == "2027-01-15"


# ---------------------------------------------------------------- soft: proposal, admin confirm
def test_soft_proposal_is_not_applied_until_admin_confirms(llm_on, client):
    news.store(ITEMS)
    rows, _ = news.query(spec="it-drought-2027")
    nid = rows[0]["id"]
    book = market.live_book()
    ask = _json_ask({"verdict": "YES", "confidence": 0.8, "cited": [nid, 99999], "rationale": "Rome declared a national drought emergency."})
    p = resolve.evidence_once(book, ask, qid="it-drought-2027")
    assert p["status"] == "pending" and p["verdict"] == "YES" and [e["id"] for e in p["evidence"]] == [nid]
    assert book.markets["it-drought-2027"]["status"] == "open"  # never applied automatically
    assert not [e for e in ledger.all_entries() if e["kind"] == "resolve"]
    st = next(s for s in client.get("/harvest/api/resolutions").json()["markets"] if s["qid"] == "it-drought-2027")
    assert st["state"] == "proposed" and st["proposal"]["id"] == p["id"]

    assert client.post("/harvest/api/resolve/it-drought-2027", json={"action": "apply"}).status_code == 401
    assert client.post("/harvest/api/resolve/it-drought-2027", json={"action": "apply"}, headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert book.markets["it-drought-2027"]["status"] == "open"
    r = client.post("/harvest/api/resolve/it-drought-2027", json={"action": "apply"}, headers=_admin())
    assert r.status_code == 200, r.text
    assert book.markets["it-drought-2027"]["status"] == "resolved" and book.markets["it-drought-2027"]["outcome"] == 1
    assert [e["trader"] for e in ledger.all_entries() if e["kind"] == "resolve"] == ["admin"]
    assert resolve.proposals(qid="it-drought-2027")[0]["status"] == "applied"
    assert client.post("/harvest/api/resolve/it-drought-2027", json={"action": "apply"}, headers=_admin()).status_code == 404


def test_soft_reject_and_not_yet(llm_on, client):
    news.store(ITEMS)
    book = market.live_book()
    nid = news.query(spec="it-drought-2027")[0][0]["id"]
    resolve.evidence_once(book, _json_ask({"verdict": "NO", "confidence": 0.6, "cited": [nid], "rationale": "x"}), qid="it-drought-2027")
    r = client.post("/harvest/api/resolve/it-drought-2027", json={"action": "reject"}, headers=_admin())
    assert r.status_code == 200 and book.markets["it-drought-2027"]["status"] == "open"
    p = resolve.evidence_once(book, _json_ask({"verdict": "YES", "confidence": 0.9, "cited": [], "rationale": "rumour"}), qid="it-drought-2027")
    assert p["verdict"] == "NOT_YET" and p["status"] == "info"  # YES without a cited headline is not evidence
    assert client.post("/harvest/api/resolve/it-drought-2027", json={"action": "apply"}, headers=_admin()).status_code == 404


def test_evidence_offline_makes_no_call():
    news.store(ITEMS)
    assert resolve.evidence_once(market.live_book(), lambda p: pytest.fail("LLM called offline")) is None


# ---------------------------------------------------------------- news-drafted markets
SOFT_DRAFT = {"question": "Will Spain cap olive oil exports before 2027-06-30?", "kind": "soft", "deadline": "2027-06-30",
              "resolution_source": "Spanish agriculture ministry", "region": "Spain", "crop": "olive oil", "adverse_if": "yes", "prior": 0.2,
              "news_ids": [], "why": "A short harvest after drought."}
DUP_DRAFT = {**SOFT_DRAFT, "question": "Will Italy declare a national drought emergency in 2027?", "deadline": "2027-12-31"}
HARD_DRAFT = {"question": "Will olive oil fall 10% in 3 months?", "kind": "hard", "series": "POLVOILUSDM",
              "rule": {"type": "pct_change_lt", "threshold": -0.1, "horizon_months": 3}, "region": "Spain", "crop": "olive oil",
              "adverse_if": "no", "prior": 0.3, "news_ids": [], "why": "Bigger harvest."}


def test_drafts_created_deduped_and_approved(llm_on, client):
    news.store(ITEMS)
    ids = [r["id"] for r in news.query()[0]]
    book = market.live_book()
    out = market_gen.draft_once(book, _json_ask({"markets": [{**DUP_DRAFT, "news_ids": ids}, {**SOFT_DRAFT, "news_ids": ids + [4242]}]}))
    assert [d["question"] for d in out] == [SOFT_DRAFT["question"]]
    assert out[0]["status"] == "draft" and out[0]["news_ids"] == sorted(ids) and out[0]["kind"] == "soft"
    # no new news: no call
    assert market_gen.draft_once(book, lambda p: pytest.fail("called without new news")) == []
    # new news, but the same market again: deduped against the stored draft; a hard one is instantiated
    news.store([{"title": "Olive oil prices ease as Spain harvest recovers", "url": "https://example.com/o2", "source": "X",
                 "published_at": "2026-10-24T12:00:00Z"}])
    new_id = news.query()[1]
    out2 = market_gen.draft_once(book, _json_ask({"markets": [{**SOFT_DRAFT, "question": "Will Spain cap olive oil exports before mid 2027?", "news_ids": [new_id]},
                                                              {**HARD_DRAFT, "news_ids": [new_id]}]}))
    assert len(out2) == 1 and out2[0]["kind"] == "hard" and out2[0]["question"].startswith("Will olive oil (now")
    assert out2[0]["rule"]["type"] == "pct_change_lt"

    listed = client.get("/harvest/api/markets/drafts").json()["drafts"]
    assert [d["id"] for d in listed] == [out2[0]["id"], out[0]["id"]]

    url = f"/harvest/api/markets/drafts/{out[0]['id']}/approve"
    assert client.post(url).status_code == 401
    r = client.post(url, headers=_admin())
    assert r.status_code == 200, r.text
    qid, price = r.json()["qid"], r.json()["open_price"]
    assert price == pytest.approx((0.35 + 0.1 + 0.2) / 3, abs=1e-3)  # consensus of the agents' world-model leans on 0.2
    assert book.price(qid) == pytest.approx(price) and book.markets[qid]["status"] == "open"
    assert any(m["qid"] == qid for m in client.get("/harvest/api/markets").json()["markets"])
    assert client.post(url, headers=_admin()).status_code == 409
    assert client.post("/harvest/api/markets/drafts/999/approve", headers=_admin()).status_code == 404

    tok = client.post("/harvest/api/join", json={"nickname": "bo"}).json()["token"]
    assert client.post("/harvest/api/bet", json={"token": tok, "qid": qid, "side": "NO", "spend": 20}).status_code == 200
    after = book.price(qid)
    market.reset_live_book()  # a restart re-opens the approved market and replays its bet
    again = market.live_book()
    assert again.price(qid) == pytest.approx(after) and again.meta[qid]["title"] == SOFT_DRAFT["question"]

    h = client.post(f"/harvest/api/markets/drafts/{out2[0]['id']}/approve", headers=_admin()).json()
    assert again.meta[h["qid"]]["series"] == "POLVOILUSDM" and again.meta[h["qid"]]["target"] is not None


def test_draft_validation():
    ids = {1}
    assert market_gen.clean({**SOFT_DRAFT, "news_ids": [1]}, ids) is not None
    assert market_gen.clean({**SOFT_DRAFT, "news_ids": [2]}, ids) is None  # cites no given item
    assert market_gen.clean({**SOFT_DRAFT, "deadline": "2020-01-01", "news_ids": [1]}, ids) is None
    assert market_gen.clean({**HARD_DRAFT, "series": "NOPE", "news_ids": [1]}, ids) is None
    assert market_gen.clean({**HARD_DRAFT, "rule": {"type": "pct_change_lt", "threshold": 0.1, "horizon_months": 3}, "news_ids": [1]}, ids) is None
    assert market_gen.draft_once(market.live_book(), lambda p: pytest.fail("LLM called offline")) == []


def test_public_mode_allows_new_gets_and_guards_admin_posts(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    for path in ("/harvest/api/debate", "/harvest/api/resolutions", "/harvest/api/markets/drafts"):
        assert client.get(path).status_code == 200, path
    assert client.post("/harvest/api/resolve/it-drought-2027", json={}).status_code == 401
    assert client.post("/harvest/api/markets/drafts/1/approve").status_code == 401
    assert client.post("/harvest/api/markets/drafts/1/approve", headers=_admin()).status_code == 404
