"""Bring your own world model (POST /harvest/api/byo) and the masked agent markets, offline."""

import asyncio
import re

import pytest
from fastapi.testclient import TestClient

from agent.harvest import byo, forecaster, ledger, market, prices, replay, scoring
from agent.routes import harvest as routes
from agent.routes import public_mode
from agent.server import app

THEORY = ("Heat is the binding constraint: hot dry summers in the Mediterranean cut yields faster than farmers adapt, "
          "so after a price spike stocks stay thin and prices keep rising for a season before they fall back.")


@pytest.fixture(scope="module")
def demo():
    return dict(replay.load_demo())


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch, demo):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.setattr(market, "load_replay", lambda: demo)
    monkeypatch.setattr(routes, "replay_result", lambda: demo)
    market.reset_live_book()
    for limiter in routes.LIMITERS:
        limiter.reset()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def calls(monkeypatch):
    """Spy on the house forecaster: (persona id, cutoff, qids, mask) per call."""
    seen: list[tuple[str, str, list[str], dict]] = []
    real = forecaster.forecast

    async def spy(persona, questions, fixture, cutoff, mask, *, offline=False):
        seen.append((persona["id"], cutoff, [q["qid"] for q in questions], mask))
        return await real(persona, questions, fixture, cutoff, mask, offline=offline)

    monkeypatch.setattr(forecaster, "forecast", spy)
    return seen


def test_byo_end_to_end_ranks_and_lands_on_leaderboard(client, demo, calls):
    r = client.post("/harvest/api/byo", json={"name": "Heat first", "world_model": THEORY, "nickname": "ada"})
    assert r.status_code == 200, r.text
    body = r.json()
    hold = scoring.scored_cutoffs(demo, "holdout")
    # exactly the house protocol: one batched call per sealed holdout cutoff, same instances, same mask
    assert [c for _, c, _, _ in calls] == [c["cutoff"] for c in hold]
    assert all(qids == [q["qid"] for q in c["questions"]] and mask == demo["mask"] for (_, _, qids, mask), c in zip(calls, hold, strict=True))
    assert body["model_id"].startswith("byo-") and body["origin"] == "human-model" and body["owner"] == "ada"
    assert body["world_model_sha256"] == byo.world_model_sha(THEORY)
    same = [p for p in demo["personas"] if p.get("model") == byo.model_key()] or demo["personas"]
    assert body["of"] == len(same) + 2 and 1 <= body["rank"] <= body["of"]  # house agents on the pinned model + market + you
    outcomes = scoring.holdout_outcomes(demo)
    assert {q for q, _, _ in body["per_question"]} == set(outcomes)
    assert all(o == outcomes[q] and 0 < p < 1 for q, p, o in body["per_question"])
    assert body["brier_holdout"] == pytest.approx(scoring.mean_brier([(p, o) for _, p, o in body["per_question"]]))
    assert body["source"] == "fallback" and len(body["rationales"]) >= 2 and body["cached"] is False

    got = client.get(f"/harvest/api/byo/{body['model_id']}").json()
    assert got["brier_holdout"] == body["brier_holdout"] and got["rank"] == body["rank"]
    assert client.get("/harvest/api/byo/byo-nope").status_code == 404

    rows = {m["persona_id"]: m for m in client.get("/harvest/api/leaderboard").json()["world_models"]}
    mine = rows[body["model_id"]]
    assert mine["origin"] == "human-model" and mine["brier_holdout"] == body["brier_holdout"] and mine["name"] == "Heat first"
    assert mine["brier_holdout_lo"] <= mine["brier_holdout"] <= mine["brier_holdout_hi"] and mine["n_ci"] == body["n"]
    assert "per_question" not in mine
    assert sum(1 for m in rows.values() if m["origin"] in ("house", "narrative")) == len(demo["personas"])
    kinds = [e["kind"] for e in ledger.all_entries() if e["trader"] == body["model_id"]]
    assert kinds == ["agent", "forecast"]  # committed before it was scored
    assert client.get("/harvest/api/state").json()["agents"] == 0


def test_byo_cache_hit_never_reruns(client, calls):
    first = client.post("/harvest/api/byo", json={"name": "Heat first", "world_model": THEORY}).json()
    n_calls, n_entries = len(calls), len(ledger.all_entries())
    again = client.post("/harvest/api/byo", json={"name": "Other name", "world_model": "  " + THEORY.replace(" ", "\n", 3) + " "})
    assert again.status_code == 200
    body = again.json()
    assert body["cached"] is True and body["model_id"] == first["model_id"] and body["brier_holdout"] == first["brier_holdout"]
    assert len(calls) == n_calls and len(ledger.all_entries()) == n_entries


def test_byo_times_out_into_the_deterministic_fallback(client, monkeypatch):
    async def hang(*_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(forecaster, "forecast", hang)
    monkeypatch.setattr(byo, "CUTOFF_TIMEOUT_S", 0.05)
    r = client.post("/harvest/api/byo", json={"name": "Slow", "world_model": THEORY + " Slow."})
    assert r.status_code == 200 and r.json()["source"] == "fallback" and r.json()["brier_holdout"] is not None


def test_byo_validation_and_rate_limit(client):
    assert client.post("/harvest/api/byo", json={"world_model": THEORY}).status_code == 422
    assert client.post("/harvest/api/byo", json={"name": "x", "world_model": "short"}).status_code == 422
    assert client.post("/harvest/api/byo", json={"name": "x", "world_model": "y" * 1201}).status_code == 422
    codes = [client.post("/harvest/api/byo", json={"name": "", "world_model": THEORY}).status_code for _ in range(5)]
    assert codes[:3] == [422, 422, 422] and codes[3] == 429  # 6 per minute per IP
    assert "Retry-After" in client.post("/harvest/api/byo", json={}).headers


def test_agent_markets_visible_history_is_masked(client, demo):
    fixture = prices.load_fixture()
    cutoff = market.live_cutoff(demo)["cutoff"]
    hard = [m for m in client.get("/harvest/api/agents/markets").json()["markets"] if m["kind"] == "hard"]
    assert hard
    for m in hard:
        hist = m["visible_history"]
        assert len(hist) == forecaster.WINDOW_MONTHS and hist[0][1] == 100.0 and hist[-1][0] == "t0"
        assert all(re.fullmatch(r"t0|t-\d{1,2}", label) for label, _ in hist)
        assert not re.search(r"(19|20)\d\d", str(hist))
        assert hist == [list(p) for p in forecaster.mask_window(prices.window(fixture, m["series"], cutoff, forecaster.WINDOW_MONTHS), cutoff)]
        assert m["target_index"] is not None and m["target_index"] > 0


def test_public_mode_allows_byo():
    assert public_mode.is_public("POST", "/harvest/api/byo")
    assert public_mode.is_public("GET", "/harvest/api/byo/byo-0123456789ab")
