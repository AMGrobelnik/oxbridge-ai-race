"""Croppy, the guide agent (agent/harvest/croppy.py, agent/routes/croppy.py). Offline: Gemini is stubbed."""

import json

import pytest
from fastapi.testclient import TestClient

from agent.challenge.ratelimit import RateLimiter
from agent.harvest import croppy, market
from agent.routes import croppy as croppy_route
from agent.routes import public_mode
from agent.server import app

HARD = {"qid": "a@2026-06", "spec_id": "a", "kind": "hard", "title": "Will wheat be more than 10% higher by 2026-12?", "region": "Italy",
        "crop": "durum wheat", "adverse_if": "yes", "cutoff": "2026-06", "series": "PWHEAMTUSDM", "base_month": "2026-06",
        "base_value": 300.0, "resolve_month": "2026-12", "target": 330.0, "rule": None, "prior": 0.5}
PERSONA = {"id": "climate-pessimist", "name": "Climate Pessimist", "color": "#d9480f", "world_model": "heat -> shocks",
           "beliefs": ["droughts cut yields"], "heuristic": {"type": "momentum"}, "soft_lean": 0.15, "risk": 0.25, "edge": 0.03}
SOFT = [{"id": "it-drought-2027", "kind": "soft", "title": "Will Italy declare a national drought emergency in 2027?", "deadline": "2027-12-31",
         "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "prior": 0.35, "why": ""}]


def _result():
    live = {"cutoff": "2026-06", "split": "live", "questions": [HARD], "windows": {}, "trades": [], "resolutions": [], "payouts": [],
            "forecasts": [], "final_prices": {HARD["qid"]: 0.4}}
    return {"personas": [PERSONA], "cutoffs": [live]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.setattr(market, "load_replay", _result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: SOFT)
    croppy_route.croppy_limiter.reset()
    market.reset_live_book()
    yield
    market.reset_live_book()
    croppy_route.croppy_limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


def test_offline_returns_friendly_fallback_with_actions(client, monkeypatch):
    async def boom(*args, **kwargs):
        pytest.fail("Gemini called while HARVEST_OFFLINE=1")

    monkeypatch.setattr(croppy_route, "ask", boom)
    res = client.post("/harvest/api/croppy", json={"message": "How do I bet?"})
    assert res.status_code == 200
    body = res.json()
    assert body["fallback"] is True
    assert "1,000" in body["reply"]
    assert body["actions"][0]["href"] == "/harvest/play"


def test_gemini_failure_falls_back(client, monkeypatch):
    monkeypatch.delenv("HARVEST_OFFLINE")

    async def down(*args, **kwargs):
        raise croppy_route.gemini.GeminiUnavailableError("down")

    monkeypatch.setattr(croppy_route, "ask", down)
    body = client.post("/harvest/api/croppy", json={"message": "What's a world model?"}).json()
    assert body["fallback"] is True
    assert any(a["href"] == "/harvest#models" for a in body["actions"])


def test_prompt_includes_live_context_and_actions_are_filtered(client, monkeypatch):
    monkeypatch.delenv("HARVEST_OFFLINE")
    monkeypatch.setattr(croppy, "news_headlines", lambda limit=4: ["Spain rations irrigation water in Andalusia"])
    seen = {}

    async def fake(prompt, *, json_output=False, timeout_s=None):
        seen["prompt"], seen["json"] = prompt, json_output
        return json.dumps({"reply": "Hop into Play!", "actions": [{"label": "Play", "href": "/harvest/play"},
                                                                  {"label": "Evil", "href": "https://evil.example"}]})

    monkeypatch.setattr(croppy_route, "ask", fake)
    history = [{"role": "user", "text": f"q{i}"} for i in range(12)]
    body = client.post("/harvest/api/croppy", json={"message": "x" * 900, "history": history, "page": "/harvest/play"}).json()
    assert body == {"reply": "Hop into Play!", "actions": [{"label": "Play", "href": "/harvest/play"}]}
    prompt = seen["prompt"]
    assert seen["json"] is True
    assert "LIVE CONTEXT" in prompt and "Will Italy declare a national drought emergency in 2027?: 35% YES" in prompt
    assert "Will wheat be more than 10% higher by 2026-12?: 40% YES" in prompt
    assert "Spain rations irrigation water in Andalusia" in prompt
    assert "play credits" in prompt.lower() and "/harvest/api/agents/register" in prompt
    assert "Visitor: q4" in prompt and "Visitor: q3" not in prompt  # last 8 turns only
    assert "x" * 500 in prompt and "x" * 501 not in prompt  # message capped at 500
    assert "The visitor is on page /harvest/play." in prompt


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(croppy_route, "croppy_limiter", RateLimiter(3))
    codes = [client.post("/harvest/api/croppy", json={"message": "hi"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_empty_message_rejected(client):
    assert client.post("/harvest/api/croppy", json={"message": "   "}).status_code == 422


def test_public_mode_allows_croppy(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("ADMIN_TOKEN", "secret")
    monkeypatch.setattr(public_mode, "request_limiter", RateLimiter(10_000))
    assert public_mode.is_public("POST", "/harvest/api/croppy")
    res = client.post("/harvest/api/croppy", json={"message": "Write my theory"})
    assert res.status_code == 200
    assert res.json()["actions"][0]["href"] == "/harvest/play#th"
