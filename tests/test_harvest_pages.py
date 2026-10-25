"""Harvest pages, the story digest, the QR code and PUBLIC_MODE whitelisting of the Harvest routes."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.harvest import market
from agent.routes import harvest_pages, public_mode
from agent.server import app

GEO = Path(harvest_pages.STATIC_DIR).parent / "data" / "geo.json"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    market.reset_live_book()
    yield TestClient(app)
    market.reset_live_book()


@pytest.mark.parametrize("path, marker", [("/harvest", "Bets flow"), ("/harvest/play", "Can you beat"), ("/harvest/board", "Beat the world models"),
                                          ("/harvest/terms", "Terms of play")])
def test_pages_render(client, path, marker):
    r = client.get(path)
    assert r.status_code == 200
    assert marker in r.text
    assert "/harvest/static/common.js" in r.text


def test_static_assets_served(client):
    for name in ("common.js", "index.js", "play.js", "board.js", "harvest.css"):
        assert client.get(f"/harvest/static/{name}").status_code == 200


def test_qr_svg_points_at_play(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://harvest.example")
    r = client.get("/harvest/qr.svg")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/harvest/api/playurl").json()["url"] == "https://harvest.example/harvest/play"


def test_geo_has_regions_and_gulf_arcs(client):
    geo = client.get("/harvest/api/geo").json()
    assert len(geo["regions"]) >= 6
    assert len(geo["arcs"]) >= 3
    ids = {r["id"] for r in geo["regions"]}
    buyers = {b["id"] for b in geo["buyers"]}
    for arc in geo["arcs"]:
        assert arc["from"] in buyers
        assert arc.get("to") in ids or "to_point" in arc
    assert json.loads(GEO.read_text())["regions"] == geo["regions"]


def test_story_digest_of_synthetic_replay():
    personas = [{"id": "a", "name": "A", "color": "#000"}, {"id": "b", "name": "B", "color": "#111"}]
    q = {"qid": "x@2025-09", "title": "Will wheat (now $200/mt) be above $220/mt by 2026-03?", "series": "PWHEAMTUSDM", "cutoff": "2025-09"}
    result = {
        "personas": personas, "model": "m", "mask": {}, "commitments": [{"kind": "holdout", "sha256": "ab"}],
        "cutoffs": [
            {"cutoff": "2025-09", "split": "holdout", "questions": [q],
             "forecasts": [{"persona_id": "a", "qid": q["qid"], "p_yes": 0.8}, {"persona_id": "b", "qid": q["qid"], "p_yes": 0.2}],
             "trades": [{"trader": "agent:a", "qid": q["qid"], "spend": 10.0, "price_before": 0.5, "price_after": 0.6}],
             "final_prices": {q["qid"]: 0.6}, "resolutions": [{"qid": q["qid"], "outcome": 1, "resolved_value": 230.0}],
             "payouts": [{"trader": "agent:a", "qid": q["qid"], "amount": 16.0}]},
            {"cutoff": "2026-09", "split": "live", "questions": [], "forecasts": [], "final_prices": {}, "resolutions": [], "payouts": [],
             "trades": [{"trader": "agent:b", "qid": "y@2026-09", "spend": 5.0, "price_before": 0.5, "price_after": 0.4}]},
        ],
    }
    s = harvest_pages.story(result, None)
    hold = s["cutoffs"][0]
    assert hold["pnl"]["a"] == pytest.approx(6.0)
    assert hold["brier"]["a"] == pytest.approx(0.04)
    assert s["cutoffs"][1]["pnl"]["b"] is None  # live quarters are unresolved
    assert s["holdout"][0]["forecasts"] == {"a": 0.8, "b": 0.2}
    assert s["holdout"][0]["outcome"] == 1
    assert s["live_paths"] == {"y@2026-09": [0.5, 0.4]}


def test_story_route_without_replay_degrades(client, monkeypatch):
    monkeypatch.setattr(harvest_pages.api, "replay_result", lambda: None)
    harvest_pages._story_cache.clear()
    body = client.get("/harvest/api/story").json()
    assert body["has_replay"] is False
    harvest_pages._story_cache.clear()


@pytest.mark.parametrize("path", ["/harvest", "/harvest/play", "/harvest/board", "/harvest/terms", "/harvest/qr.svg", "/harvest/static/common.js",
                                  "/harvest/api/state", "/harvest/api/story", "/harvest/api/geo", "/harvest/api/holdout", "/harvest/api/playurl"])
def test_public_mode_allows_harvest_reads(path):
    assert public_mode.is_public("GET", path)


@pytest.mark.parametrize("path", ["/harvest/api/join", "/harvest/api/bet", "/harvest/api/forecast", "/harvest/api/verify"])
def test_public_mode_allows_play_posts(path):
    assert public_mode.is_public("POST", path)


@pytest.mark.parametrize("method, path", [("POST", "/harvest/api/reset"), ("POST", "/harvest/api/story"), ("DELETE", "/harvest/api/bet"), ("PUT", "/harvest/api/join")])
def test_public_mode_blocks_other_harvest_writes(method, path):
    assert not public_mode.is_public(method, path)


def test_public_mode_guard_serves_play_flow(client, monkeypatch):
    """In PUBLIC_MODE an anonymous phone can open /harvest/play, join and bet, and the join is rate limited per IP."""
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    for lim in harvest_pages.api.LIMITERS:
        lim.reset()
    assert client.get("/harvest/play").status_code == 200
    joined = client.post("/harvest/api/join", json={"nickname": "Phone Tester"})
    assert joined.status_code == 200
    token = joined.json()["token"]
    qid = client.get("/harvest/api/markets").json()["markets"][0]["qid"]
    bet = client.post("/harvest/api/bet", json={"token": token, "qid": qid, "side": "YES", "spend": 10})
    assert bet.status_code == 200, bet.text
    assert client.post("/harvest/api/reset", json={}).status_code == 401


@pytest.mark.parametrize("path", ["/harvest", "/harvest/play", "/harvest/board", "/harvest/terms"])
def test_pages_carry_play_credit_disclosure(client, path):
    text = client.get(path).text
    assert "Play credits · no real money · no cash-out" in text
    assert path == "/harvest/terms" or 'href="/harvest/terms"' in text


def test_index_and_play_pages_have_tournament_footer_and_no_cash_words(client):
    for path in ("/harvest", "/harvest/play"):
        text = client.get(path).text
        assert "A forecasting tournament, not a betting or investment service." in text
        for word in ("deposit", "withdraw ", "top-up", "buy credits", "winnings", "profit"):
            assert word not in text.lower(), (path, word)
    assert "Devnet test tokens with no value, used only to timestamp commitments" in client.get("/harvest").text
