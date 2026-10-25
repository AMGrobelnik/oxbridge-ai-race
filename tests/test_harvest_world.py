"""Farmer mode, climate-stress map and disagreement index (agent/harvest/world.py, agent/routes/harvest_world.py). Offline."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import market, world
from agent.routes import harvest_world
from agent.server import app

WHEAT = {"qid": "wheat-up10-6m@2026-06", "spec_id": "wheat-up10-6m", "kind": "hard", "title": "Will wheat be above $272/mt by 2026-12?",
         "region": "France", "crop": "wheat", "adverse_if": "yes", "cutoff": "2026-06", "series": "PWHEAMTUSDM", "base_month": "2026-06",
         "base_value": 247.0, "resolve_month": "2026-12", "target": 271.7, "rule": {"type": "pct_change_gt", "threshold": 0.1, "horizon_months": 6},
         "prior": 0.5}
OLIVE = {**WHEAT, "qid": "olive-up15-6m@2026-06", "spec_id": "olive-up15-6m", "title": "Will olive oil be 15% higher?", "region": "Spain",
         "crop": "olive oil", "series": "POLVOILUSDM"}
COCOA = {**WHEAT, "qid": "cocoa-up15-6m@2026-06", "spec_id": "cocoa-up15-6m", "title": "Will cocoa be 15% higher?", "region": "EU importers",
         "crop": "cocoa", "series": "PCOCOUSDM"}
PERSONAS = [{"id": pid, "name": pid.title(), "color": "#123456", "world_model": "", "beliefs": [], "heuristic": {"type": "momentum"},
             "soft_lean": 0.0, "risk": 0.25, "edge": 0.03} for pid in ("pessimist", "optimist", "middle")]
P = {WHEAT["qid"]: (0.9, 0.1, 0.5), OLIVE["qid"]: (0.55, 0.45, 0.5), COCOA["qid"]: (0.4, 0.4, 0.4)}
CLIMATE = {"series": {"europe_land": {"name": "Europe land", "url": "u", "points": [("2026-06", 1.0), ("2026-07", 2.0), ("2026-08", 3.0)]}}}


def _result():
    qs = [WHEAT, OLIVE, COCOA]
    forecasts = [{"persona_id": p["id"], "qid": q["qid"], "p_yes": P[q["qid"]][i], "rationale": f"{p['id']} on {q['crop']}", "source": "llm"}
                 for q in qs for i, p in enumerate(PERSONAS)]
    live = {"cutoff": "2026-06", "split": "live", "questions": qs, "windows": {}, "trades": [], "resolutions": [], "payouts": [],
            "forecasts": forecasts, "final_prices": {q["qid"]: 0.4 for q in qs}}
    return {"personas": PERSONAS, "cutoffs": [live]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.setattr(market, "load_replay", _result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: [])
    monkeypatch.setattr(world.climate, "load_climate", lambda: CLIMATE)
    harvest_world._cache.clear()
    market.reset_live_book()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


def test_disagreement_ranks_by_spread_with_bull_and_bear(client):
    body = client.get("/harvest/api/disagreement").json()
    rows = body["markets"]
    assert [r["qid"] for r in rows] == [WHEAT["qid"], OLIVE["qid"], COCOA["qid"]]
    top = rows[0]
    assert top["rank"] == 1 and top["n_agents"] == 3 and top["range"] == pytest.approx(0.8)
    assert top["bull"]["persona_id"] == "pessimist" and top["bull"]["reason"] == "pessimist on wheat"
    assert top["bear"]["persona_id"] == "optimist" and rows[-1]["std"] == 0
    assert len(client.get("/harvest/api/disagreement?limit=1").json()["markets"]) == 1


def test_map_colours_regions_by_heat_and_links_markets(client):
    body = client.get("/harvest/api/map").json()
    assert body["anomaly"]["value_c"] == pytest.approx(2.0) and body["anomaly"]["to"] == "2026-08"
    by = {r["region"]: r for r in body["regions"]}
    assert set(by) == set(world.REGIONS)
    assert by["Spain"]["stress"] > by["Germany"]["stress"] and by["Spain"]["heat_anomaly_c"] == pytest.approx(2.6)
    assert by["France"]["qids"] == [WHEAT["qid"]] and by["Spain"]["qids"] == [OLIVE["qid"]] and by["Poland"]["qids"] == []
    assert [m["qid"] for m in body["europe_wide"]] == [COCOA["qid"]]
    assert body["regions"][0]["stress"] == max(r["stress"] for r in body["regions"])


def test_farmer_answers_with_probability_reason_and_confidence(client):
    r = client.get("/harvest/api/farmer?crop=Wheat&region=france")
    assert r.status_code == 200
    a = r.json()
    assert a["market"]["qid"] == WHEAT["qid"] and a["probability"] == pytest.approx(0.4, abs=0.01)
    assert a["source"] == "fallback" and "40%" in a["reason"] and "pessimist on wheat" in a["reason"]
    assert a["confidence"]["level"] == "low" and a["stress"]["level"] in ("moderate", "high", "severe")
    assert a["proxy"] is None and "betting" not in a


def test_farmer_proxy_crop_and_bad_input(client):
    a = client.get("/harvest/api/farmer?crop=barley&region=Spain").json()
    assert a["proxy"] == "wheat" and "no barley market" in a["reason"]
    assert client.get("/harvest/api/farmer?crop=cocoa").status_code == 400
    assert client.get("/harvest/api/farmer?crop=wheat&region=Mars").status_code == 400
    assert client.get("/harvest/api/farmer?crop=maize").status_code == 404  # no maize market in this book


def test_farmer_uses_croppy_when_available(client, monkeypatch):
    monkeypatch.setattr(harvest_world.fast_llm, "available", lambda: True)
    monkeypatch.setattr(harvest_world, "ask", lambda prompt: '{"reason": "Croppy says maybe."}')
    a = client.get("/harvest/api/farmer?crop=wheat&region=France").json()
    assert a["reason"] == "Croppy says maybe." and a["source"] == "llm"
    assert client.get("/harvest/api/farmer?crop=wheat&region=France").json()["source"] == "cache"


@pytest.mark.parametrize("path", ["/app/farmer", "/app/map", "/app/disagreement"])
def test_pages_serve(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "world.js" in r.text
