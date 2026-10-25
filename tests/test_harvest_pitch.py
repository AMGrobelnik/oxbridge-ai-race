"""Live audience market: /vote guests with a 100-credit pitch wallet, per-device limits, /pitch state, admin featured pick."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import ledger, market
from agent.routes import harvest as routes
from agent.routes import harvest_pitch as pitch
from agent.routes import public_mode
from agent.server import app
from tests.test_harvest_market import SOFT, synthetic_result

HOUSE = "agent:climate-pessimist"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.delenv("PITCH_QID", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    result = synthetic_result()
    monkeypatch.setattr(market, "load_replay", lambda: result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: SOFT)
    monkeypatch.setattr(routes, "replay_result", lambda: result)
    market.reset_live_book()
    pitch._state_cache.clear()
    for limiter in (*routes.LIMITERS, *pitch.LIMITERS):
        limiter.reset()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


def test_guest_gets_a_handle_and_keeps_it_with_its_token(client):
    g = client.post("/vote/api/guest", json={}).json()
    assert g["username"].startswith("guest-") and g["credits_left"] == 100 and g["token"]
    again = client.post("/vote/api/guest", json={"token": g["token"]}).json()
    assert again["user_id"] == g["user_id"] and again["token"] == g["token"]
    join = next(e for e in ledger.all_entries() if e["kind"] == "join")
    assert join["payload"]["via"] == "vote"


def test_guest_bets_show_on_pitch_as_human_next_to_bots(client):
    book = market.live_book()
    qid = pitch.featured_qid(book)
    assert qid and book.markets[qid]["status"] == "open"
    book.place_bet(HOUSE, qid, "NO", 20)
    tok = client.post("/vote/api/guest", json={}).json()["token"]
    r = client.post("/vote/api/bet", json={"token": tok, "side": "yes", "spend": 30})
    assert r.status_code == 200, r.text
    assert r.json()["credits_left"] == 70 and r.json()["market"]["qid"] == qid
    s = client.get("/pitch/api/state").json()
    assert s["featured"]["qid"] == qid and s["vote_url"].endswith("/vote")
    assert [b["who"] for b in s["feed"][:2]] == ["human", "bot"]
    assert s["feed"][0]["side"] == "YES" and s["feed"][0]["username"].startswith("guest-")
    assert s["tally"]["human"] == {"bets": 1, "credits": 30.0, "traders": 1, "yes_share": 1.0}
    assert s["tally"]["bot"]["bets"] == 1 and s["tally"]["bot"]["yes_share"] == 0.0
    assert [p[2] for p in s["path"]] == ["b", "h"]


def test_pitch_wallet_caps_total_spend_at_100(client):
    tok = client.post("/vote/api/guest", json={}).json()["token"]
    for _ in range(2):
        assert client.post("/vote/api/bet", json={"token": tok, "side": "NO", "spend": 50}).status_code == 200
    r = client.post("/vote/api/bet", json={"token": tok, "side": "NO", "spend": 5})
    assert r.status_code == 422 and "100 pitch credits" in r.json()["error"]


def test_guest_bets_are_rate_limited_per_device(client, monkeypatch):
    monkeypatch.setattr(pitch.device_bet_limiter, "limit", 2)
    a = client.post("/vote/api/guest", json={}).json()["token"]
    b = client.post("/vote/api/guest", json={}).json()["token"]
    assert [client.post("/vote/api/bet", json={"token": a, "side": "YES", "spend": 1}).status_code for _ in range(3)] == [200, 200, 429]
    assert client.post("/vote/api/bet", json={"token": b, "side": "YES", "spend": 1}).status_code == 200


def test_unknown_guest_and_bad_side_are_rejected(client):
    assert client.post("/vote/api/bet", json={"token": "nope", "side": "YES", "spend": 5}).status_code == 401
    tok = client.post("/vote/api/guest", json={}).json()["token"]
    assert client.post("/vote/api/bet", json={"token": tok, "side": "maybe", "spend": 5}).status_code == 422


def test_admin_picks_the_featured_market(client, monkeypatch):
    book = market.live_book()
    other = [q for q, m in book.markets.items() if m["status"] == "open" and q != pitch.featured_qid(book)][0]
    assert client.post("/pitch/api/featured", json={"qid": other}).status_code == 401
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    h = {"X-Admin-Token": "s3cret"}
    assert client.post("/pitch/api/featured", json={"qid": "nope"}, headers=h).status_code == 404
    assert client.post("/pitch/api/featured", json={"qid": other}, headers=h).json() == {"qid": other}
    assert client.get("/pitch/api/state").json()["featured"]["qid"] == other


def test_pages_and_qr_codes(client):
    for path in ("/pitch", "/vote"):
        r = client.get(path)
        assert r.status_code == 200 and "beat the bots" in r.text
    assert client.get("/pitch/qr.svg").text.startswith("<svg")
    assert client.get("/pitch/qr.png").content[:8] == b"\x89PNG\r\n\x1a\n"


def test_public_mode_allows_the_audience_routes_only():
    assert public_mode.is_public("GET", "/pitch") and public_mode.is_public("GET", "/vote")
    assert public_mode.is_public("GET", "/pitch/api/state") and public_mode.is_public("GET", "/pitch/qr.png")
    assert public_mode.is_public("POST", "/vote/api/guest") and public_mode.is_public("POST", "/vote/api/bet")
    assert not public_mode.is_public("POST", "/pitch/api/featured")
