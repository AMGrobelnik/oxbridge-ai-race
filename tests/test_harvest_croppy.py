"""Croppy, the guide agent (agent/harvest/croppy.py, agent/routes/croppy.py). Offline: the chat models are stubbed."""

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
    croppy_route._context.clear()
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
        pytest.fail("a model was called while HARVEST_OFFLINE=1")

    monkeypatch.setattr(croppy_route, "ask", boom)
    res = client.post("/harvest/api/croppy", json={"message": "How do I bet?"})
    assert res.status_code == 200
    body = res.json()
    assert body["fallback"] is True
    assert "1,000" in body["reply"]
    assert body["actions"][0]["href"] == "/app"


def test_every_model_failing_falls_back(client, monkeypatch):
    monkeypatch.delenv("HARVEST_OFFLINE")
    tried = []

    async def down(m, prompt, *, timeout_s):
        tried.append((m["provider"], timeout_s))
        raise croppy_route.gemini.GeminiUnavailableError("down")

    monkeypatch.setattr(croppy_route, "ask", down)
    body = client.post("/harvest/api/croppy", json={"message": "What's a world model?"}).json()
    assert body["fallback"] is True
    assert any(a["href"] == "/app/live" for a in body["actions"])
    assert [p for p, _ in tried] == ["gemini", "openrouter"]
    assert tried[0][1] <= croppy_route.PRIMARY_TIMEOUT_S and tried[1][1] <= croppy_route.DEADLINE_S < 12


def test_gemini_first_then_deepseek(client, monkeypatch):
    monkeypatch.delenv("HARVEST_OFFLINE")
    assert croppy_route.CHAIN[0]["model_id"] == "gemini-2.5-flash" and croppy_route.CHAIN[1]["model_id"].startswith("deepseek/")

    async def flaky(m, prompt, *, timeout_s):
        if m["provider"] == "gemini":
            raise TimeoutError
        return json.dumps({"reply": "Hi from DeepSeek", "actions": []})

    monkeypatch.setattr(croppy_route, "ask", flaky)
    body = client.post("/harvest/api/croppy", json={"message": "hi"}).json()
    assert body == {"reply": "Hi from DeepSeek", "actions": [], "model": "DeepSeek V4.1 Flash"}


def test_links_only_to_served_pages():
    links = croppy.links_for(["/", "/app", "/harvest"])
    assert set(links) == {"/", "/app", "/harvest#holdout"}
    assert croppy.clean_actions([{"label": "x", "href": "/markets"}, {"label": "y", "href": "/app"}], links) == [{"label": "y", "href": "/app"}]
    assert croppy.clean_page("/app/markets") == "/app/markets" and croppy.clean_page("https://evil.example") == ""


def test_snapshot_cites_leaders_and_swarm():
    board = {"world_models": [{"rank": 1, "name": "Climate Pessimist · Phi-4", "brier_holdout": 0.16, "pnl_live": -30.0},
                              {"rank": 2, "name": "Macro / Dollar · Gemini", "brier_holdout": 0.2, "pnl_live": 51.4}]}
    text = croppy.snapshot([], board, swarm={"agents": 264, "bets_last_hour": 4621, "all_bets_last_hour": 5367}, trades=12345)
    assert "1. Macro / Dollar · Gemini +51 credits" in text
    assert "1. Climate Pessimist · Phi-4 0.160" in text
    assert "264 outside AI agents" in text and "5367 bets in total" in text and "12,345" in text


def test_prompt_includes_live_context_and_actions_are_filtered(client, monkeypatch):
    monkeypatch.delenv("HARVEST_OFFLINE")
    monkeypatch.setattr(croppy, "news_headlines", lambda limit=4: ["Spain rations irrigation water in Andalusia"])
    seen = {}

    async def fake(m, prompt, *, timeout_s):
        seen["prompt"], seen["model"] = prompt, m["model_id"]
        return json.dumps({"reply": "Hop into Play!", "actions": [{"label": "Play", "href": "/app"},
                                                                  {"label": "Evil", "href": "https://evil.example"}]})

    monkeypatch.setattr(croppy_route, "ask", fake)
    history = [{"role": "user", "text": f"q{i}"} for i in range(12)]
    body = client.post("/harvest/api/croppy", json={"message": "x" * 900, "history": history, "page": "/harvest/play"}).json()
    assert body == {"reply": "Hop into Play!", "actions": [{"label": "Play", "href": "/app"}], "model": "Gemini 2.5 Flash"}
    prompt = seen["prompt"]
    assert seen["model"] == "gemini-2.5-flash"
    assert "LIVE CONTEXT" in prompt and "Will Italy declare a national drought emergency in 2027?: 35% YES" in prompt
    assert "Will wheat be more than 10% higher by 2026-12?: 40% YES" in prompt
    assert "Spain rations irrigation water in Andalusia" in prompt
    assert "play credits" in prompt.lower() and "  /app/markets: " in prompt and "leakage guard" in prompt
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
    assert res.json()["actions"][0]["href"] == "/app/theory"


def test_prompt_carries_best_of_96_caveat():
    assert "best of 96 flatters it" in croppy.SYSTEM_PROMPT
    assert "clear of noise" in croppy.SYSTEM_PROMPT and "prefer the by-model result" in croppy.SYSTEM_PROMPT
