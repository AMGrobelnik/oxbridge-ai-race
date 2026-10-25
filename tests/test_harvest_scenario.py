"""Scenario sliders and 'why did this move?' (agent/harvest/scenario.py, why.py and their routes). Offline: the LLM is stubbed."""

import json

import pytest
from fastapi.testclient import TestClient

from agent.harvest import fast_llm, market, news, scenario, why
from agent.routes import harvest_scenario, harvest_why, public_mode
from agent.server import app

HARD = {"qid": "olive-up15-6m@2026-06", "spec_id": "olive-up15-6m", "kind": "hard", "title": "Will olive oil be more than 15% higher by 2026-12?",
        "region": "Spain", "crop": "olive oil", "adverse_if": "yes", "cutoff": "2026-06", "series": "POLVOILUSDM", "base_month": "2026-06",
        "base_value": 5000.0, "resolve_month": "2026-12", "target": 5750.0, "rule": {"type": "pct_change_gt", "threshold": 0.15, "horizon_months": 6},
        "prior": 0.5}
LOWER = {**HARD, "qid": "olive-down10-3m@2026-06", "spec_id": "olive-down10-3m", "title": "Will olive oil be more than 10% lower by 2026-09?",
         "adverse_if": "no", "rule": {"type": "pct_change_lt", "threshold": -0.10, "horizon_months": 3}}
COCOA = {**HARD, "qid": "cocoa-up15-6m@2026-06", "spec_id": "cocoa-up15-6m", "title": "Will cocoa be more than 15% higher?", "region": "EU importers",
         "crop": "cocoa", "series": "PCOCOUSDM"}
PERSONAS = [
    {"id": "climate-pessimist", "name": "Climate Pessimist", "color": "#d9480f", "world_model": "heat -> shocks", "beliefs": [],
     "heuristic": {"type": "momentum"}, "soft_lean": 0.15, "risk": 0.25, "edge": 0.03},
    {"id": "adaptation-optimist", "name": "Adaptation Optimist", "color": "#2b8a3e", "world_model": "farmers adapt", "beliefs": [],
     "heuristic": {"type": "mean_revert"}, "soft_lean": -0.1, "risk": 0.25, "edge": 0.03},
]
HEAT = {"temp_anomaly": {"Spain": 2.5, "italy": 2}, "rain_pct": -30, "enso": 0}


def _result():
    qs = [HARD, LOWER, COCOA]
    forecasts = [{"persona_id": p["id"], "qid": q["qid"], "p_yes": 0.4, "rationale": "base", "source": "fallback"} for p in PERSONAS for q in qs]
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
    harvest_scenario._llm_cache.clear()
    harvest_why._cache.clear()
    harvest_scenario.scenario_limiter.reset()
    harvest_why.why_limiter.reset()
    market.reset_live_book()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


def test_normalize_clamps_and_snaps():
    s = scenario.normalize({"temp_anomaly": {"spain": 2.26, "France": 9, "Mars": 3}, "rain_pct": -73, "enso": "x"})
    assert s["temp_anomaly"]["Spain"] == 2.5 and s["temp_anomaly"]["France"] == 3.0 and s["temp_anomaly"]["Germany"] == 0.0
    assert "Mars" not in s["temp_anomaly"]
    assert s["rain_pct"] == -50.0 and s["enso"] == 0.0
    assert scenario.key(s) == scenario.key(scenario.normalize({"temp_anomaly": {"Spain": 2.4, "France": 3}, "rain_pct": -50}))


def test_heat_raises_price_up_markets_and_lowers_price_down_markets(client):
    r = client.post("/harvest/api/scenario", json=HEAT)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "fallback" and body["scenario"]["temp_anomaly"]["Italy"] == 2.0
    by = {m["qid"]: m for m in body["markets"]}
    assert by[HARD["qid"]]["scenario"] > by[HARD["qid"]]["baseline"] + 0.05
    assert by[LOWER["qid"]]["scenario"] < by[LOWER["qid"]]["baseline"] - 0.05
    assert abs(by[COCOA["qid"]]["delta"]) < abs(by[HARD["qid"]]["delta"])
    up = by[HARD["qid"]]
    assert up["rationales"] and "Spain" in up["rationales"][0]["text"]
    agents = {a["persona_id"]: a for a in up["agents"]}
    assert agents["climate-pessimist"]["delta"] > agents["adaptation-optimist"]["delta"] > 0
    assert abs(body["markets"][0]["delta"]) >= abs(body["markets"][-1]["delta"])


def test_normal_year_moves_nothing_and_el_nino_moves_cocoa(client):
    calm = client.post("/harvest/api/scenario", json={}).json()
    assert all(abs(m["delta"]) < 1e-6 for m in calm["markets"])
    nino = {m["qid"]: m for m in client.post("/harvest/api/scenario", json={"enso": 2}).json()["markets"]}
    assert nino[COCOA["qid"]]["delta"] > 0.05 and abs(nino[HARD["qid"]]["delta"]) < 1e-6


def test_llm_answers_replace_fallback_and_are_cached(client, monkeypatch):
    monkeypatch.setattr(fast_llm, "available", lambda: True)
    calls = []

    def fake(prompt, timeout_s=20.0):
        calls.append(prompt)
        assert "Spain +2.5°C" in prompt and "M1:" in prompt
        return json.dumps({"forecasts": [{"m": "M1", "agent": "climate-pessimist", "p_yes": 0.9, "why": "Olive groves burn at +2.5C."},
                                         {"m": "M9", "agent": "climate-pessimist", "p_yes": 0.9, "why": "bad row"}]})

    monkeypatch.setattr(harvest_scenario, "ask", fake)
    first = client.post("/harvest/api/scenario", json=HEAT).json()
    assert first["source"] == "llm" and not first["llm_cached"]
    hit = [a for m in first["markets"] for a in m["agents"] if a["source"] == "llm"]
    assert len(hit) == 1 and hit[0]["scenario"] == 0.9 and "Olive" in hit[0]["rationale"]
    second = client.post("/harvest/api/scenario", json=HEAT).json()
    assert second["llm_cached"] and len(calls) == 1


def test_llm_failure_falls_back(client, monkeypatch):
    monkeypatch.setattr(fast_llm, "available", lambda: True)

    def boom(prompt, timeout_s=20.0):
        raise RuntimeError("down")

    monkeypatch.setattr(harvest_scenario, "ask", boom)
    body = client.post("/harvest/api/scenario", json=HEAT).json()
    assert body["source"] == "fallback" and body["markets"]


def test_scenario_spec_page_and_public(client, monkeypatch):
    spec = client.get("/harvest/api/scenario").json()
    assert "France" in spec["regions"] and spec["temp_anomaly"]["max"] == 3.0
    page = client.get("/app/scenario")
    assert page.status_code == 200 and "scenario.js" in page.text
    assert public_mode.PUBLIC_POST.match("/harvest/api/scenario")
    assert client.post("/harvest/api/scenario", content=b"[1]").status_code == 400


def test_why_cites_bets_and_news(client):
    book = market.live_book()
    book.place_bet("agent:climate-pessimist", HARD["qid"], "YES", 80, rationale="Heat in Andalusia")
    book.place_bet("agent:adaptation-optimist", HARD["qid"], "NO", 10)
    news.store([{"title": "Olive oil prices jump as Spain heatwave scorches groves", "source": "Reuters", "url": "https://example.com/o",
                 "published_at": "2099-01-01T00:00:00+00:00"}])
    r = client.get(f"/harvest/api/markets/{HARD['qid']}/why")
    assert r.status_code == 200
    body = r.json()
    mv = body["move"]
    assert mv["n_bets"] == 2 and mv["to"] > mv["from"]
    assert mv["top"][0]["name"] == "Climate Pessimist" and mv["top"][0]["side"] == "YES" and mv["top"][0]["delta"] > 0
    assert body["source"] == "fallback" and "Climate Pessimist bet 80 credits on YES" in body["explanation"]
    if body["news"]:
        assert body["news"][0]["in_window"] and "Olive oil" in body["explanation"]
    assert client.get("/harvest/api/markets/nope/why").status_code == 404


def test_why_llm_explanation_and_empty_market(client, monkeypatch):
    empty = client.get(f"/harvest/api/markets/{COCOA['qid']}/why").json()
    assert empty["move"]["n_bets"] == 0 and "Nobody has bet" in empty["explanation"]
    market.live_book().place_bet("agent:climate-pessimist", COCOA["qid"], "YES", 50)
    monkeypatch.setattr(fast_llm, "available", lambda: True)
    monkeypatch.setattr(harvest_why, "ask", lambda prompt, timeout_s=20.0: json.dumps({"explanation": "Climate Pessimist bought YES."}))
    body = client.get(f"/harvest/api/markets/{COCOA['qid']}/why").json()
    assert body["source"] == "llm" and body["explanation"] == "Climate Pessimist bought YES."
    assert client.get(f"/harvest/api/markets/{COCOA['qid']}/why").json()["source"] == "cache"


def test_news_window_marks_old_headlines():
    items = [{"title": "old", "published_at": "2020-01-01T00:00:00Z"}]
    out = why.news_in_window(items, "2026-10-25T10:00:00Z")
    assert out[0]["in_window"] is False


def test_persona_x_model_ids_keep_their_world_model_and_llm_takes_one_per_base():
    ps = [{**p, "id": f"{p['id']}--phi-4", "name": p["name"] + " · Phi-4"} for p in PERSONAS] + [{**PERSONAS[0], "id": "climate-pessimist--gemma"}]
    m = {"qid": "x", "title": "t", "region": "Spain", "crop": "olive oil", "rule": HARD["rule"], "p_yes": 0.4, "agent_forecasts": []}
    row = scenario.deterministic([m], ps, scenario.normalize(HEAT))[0]
    a = {x["persona_id"]: x for x in row["agents"]}
    assert a["climate-pessimist--phi-4"]["delta"] > a["adaptation-optimist--phi-4"]["delta"] > 0
    assert a["climate-pessimist--phi-4"]["rationale"].startswith("Heat compounds")
    chosen = scenario.llm_personas(ps)
    assert [p["id"] for p in chosen] == ["climate-pessimist--phi-4", "adaptation-optimist--phi-4"]
