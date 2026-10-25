"""Proof APIs: tournament replay timeline, calibration and real-world impact, and their pages."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import market, proof, replay
from agent.routes import harvest as harvest_api
from agent.server import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    market.reset_live_book()
    yield TestClient(app)
    market.reset_live_book()


def _toy() -> dict:
    """Two agents, one dev quarter with two questions: a is perfect, b is a coin flip."""
    qs = [{"qid": f"wheat-up10-6m@2022-0{i}", "spec_id": "wheat-up10-6m", "crop": "wheat", "adverse_if": "yes",
           "rule": {"horizon_months": 6}, "resolve_month": "2022-09"} for i in (1, 2)]
    return {"personas": [{"id": "a", "name": "A", "color": "#000"}, {"id": "b", "name": "B", "color": "#111"}],
            "cutoffs": [{"cutoff": "2022-03", "split": "holdout", "questions": qs,
                         "forecasts": [{"persona_id": "a", "qid": qs[0]["qid"], "p_yes": 1.0}, {"persona_id": "a", "qid": qs[1]["qid"], "p_yes": 0.0},
                                       {"persona_id": "b", "qid": qs[0]["qid"], "p_yes": 0.5}, {"persona_id": "b", "qid": qs[1]["qid"], "p_yes": 0.5}],
                         "trades": [{"trader": "agent:a", "spend": 10.0}, {"trader": "agent:b", "spend": 10.0}],
                         "payouts": [{"trader": "agent:a", "amount": 30.0}],
                         "final_prices": {qs[0]["qid"]: 0.8, qs[1]["qid"]: 0.3},
                         "resolutions": [{"qid": qs[0]["qid"], "outcome": 1}, {"qid": qs[1]["qid"], "outcome": 0}]},
                        {"cutoff": "2026-06", "split": "live", "questions": [], "forecasts": [], "trades": [], "payouts": [], "final_prices": {}, "resolutions": []}]}


def test_timeline_toy():
    t = proof.timeline(_toy())
    assert len(t["steps"]) == 1  # the live quarter is not scored
    a, b = t["steps"][0]["agents"]["a"], t["steps"][0]["agents"]["b"]
    assert a["bankroll"] == 1020.0 and b["bankroll"] == 990.0
    assert a["brier_cum"] == 0.0 and b["brier_cum"] == 0.25
    assert [r["id"] for r in t["final"]] == ["a", "b"]


def test_calibration_toy():
    c = proof.calibration(_toy(), "b", bins=10)
    full = [x for x in c["bins"] if x["n"]]
    assert len(full) == 1 and full[0]["n"] == 2 and full[0]["freq"] == 0.5
    assert c["ece"] == 0.0 and c["brier"] == 0.25
    assert proof.calibration(_toy(), "nobody") is None
    assert proof.calibration(_toy(), "market")["n"] == 2


def test_impact_toy():
    i = proof.impact(_toy())
    assert i["holdout"]["n"] == 2
    assert i["holdout"]["base_rate"]["brier"] == 0.25  # no history yet, so 50%
    assert i["early_warning"]["holdout"]["market"] == {"events": 1, "flagged": 1, "hit_rate": 1.0, "false_alarms": 0,
                                                       "false_alarm_rate": 0.0, "lead_months_avg": 6.0}
    assert len(i["payers"]) >= 5


def test_apis_on_frozen_backtest(client, monkeypatch):
    demo = replay.load_demo()
    monkeypatch.setattr(harvest_api, "replay_result", lambda: demo)  # other tests may leave a toy replay cached
    t = client.get("/harvest/api/replay/timeline").json()
    assert t["has_replay"] and len(t["steps"]) >= 10 and t["final"][0]["rank_bankroll"] == 1
    assert {s["split"] for s in t["steps"]} <= {"dev", "holdout"}
    summary = client.get("/harvest/api/calibration").json()
    ids = [a["id"] for a in summary["agents"]]
    assert "market" in ids and len(ids) >= 3
    pid = demo["personas"][0]["id"]
    c = client.get(f"/harvest/api/calibration?agent={pid}&split=holdout").json()
    assert sum(b["n"] for b in c["bins"]) == c["n"] > 0 and 0 <= c["ece_lo"] <= c["ece"] <= c["ece_hi"]
    assert client.get("/harvest/api/calibration?agent=nobody").status_code == 404
    assert client.get("/harvest/api/calibration?split=bogus").status_code == 400
    i = client.get("/harvest/api/impact").json()
    assert i["has_replay"] and i["holdout"]["n"] > 0 and i["by_commodity"] and i["summary"]
    m = i["holdout"]["market"]
    assert m["lo"] <= m["brier"] <= m["hi"]


@pytest.mark.parametrize("path, marker", [("/app/replay", 'data-page="replay"'), ("/app/impact", 'data-page="impact"'), ("/app/leaderboard", "proof.js")])
def test_pages(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200 and marker in r.text
    assert client.get("/harvest/static/app/proof.js").status_code == 200
