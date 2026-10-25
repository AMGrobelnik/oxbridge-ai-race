"""Follow and copy: follows live in the hash chain, copies are tagged ledger bets sized by fraction and cap."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import follow, ledger, market
from agent.routes import harvest as routes
from agent.routes import public_mode
from agent.server import app
from tests.test_harvest_market import SOFT, synthetic_result

QID = "a@2026-06"
LEADER = "agent:climate-pessimist"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    result = synthetic_result()
    monkeypatch.setattr(market, "load_replay", lambda: result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: SOFT)
    monkeypatch.setattr(routes, "replay_result", lambda: result)
    market.reset_live_book()
    for limiter in routes.LIMITERS:
        limiter.reset()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


def _join(client, name="Ana"):
    return client.post("/harvest/api/join", json={"nickname": name}).json()


def test_copy_spend_respects_fraction_cap_balance_and_minimum():
    assert follow.copy_spend(40, 0.5, 25, 1000) == 20
    assert follow.copy_spend(100, 0.5, 25, 1000) == 25
    assert follow.copy_spend(100, 0.5, 25, 12.5) == 12.5
    assert follow.copy_spend(1, 0.5, 25, 1000) is None
    assert follow.settings("x", 10_000) == (follow.DEFAULT_FRACTION, follow.MAX_CAP)
    assert follow.settings(5, -1) == (1.0, follow.MIN_COPY)


def test_follow_copy_flow_through_the_api(client):
    me = _join(client)
    tok = me["token"]
    r = client.post("/harvest/api/follow", json={"token": tok, "agent": "climate-pessimist", "copy": True, "fraction": 0.5, "cap": 10})
    assert r.status_code == 200, r.text
    assert r.json()["agent"] == LEADER and r.json()["following"]["copy"] is True
    book = market.live_book()
    trade, entry = book.place_bet(LEADER, QID, "YES", 30)
    feed = client.get(f"/harvest/api/follow/feed?token={tok}").json()["items"]
    copied = [i for i in feed if i["trader"] == me["user_id"]]
    assert len(copied) == 1 and copied[0]["copied_from"] == LEADER and copied[0]["side"] == "YES"
    assert copied[0]["spend"] == pytest.approx(10) and copied[0]["copied_from_name"] == "Climate Pessimist"
    assert any(i["seq"] == trade["seq"] for i in feed)
    bet = next(e for e in ledger.all_entries() if e["kind"] == "bet" and e["trader"] == me["user_id"])
    assert bet["payload"]["copied_from"] == LEADER and bet["payload"]["copy_of"] == entry["seq"]
    assert "copied from Climate Pessimist" in bet["payload"]["rationale"]
    row = client.get(f"/harvest/api/follow?token={tok}").json()["following"][0]
    assert row["copied"] == 1 and row["bets"] == 1
    assert client.get(f"/harvest/api/me?token={tok}").json()["balance"] == pytest.approx(990)
    assert client.get("/harvest/api/verify").json()["chain_ok"] is True


def test_follow_without_copy_and_unfollow(client):
    me = _join(client)
    tok = me["token"]
    client.post("/harvest/api/follow", json={"token": tok, "agent": LEADER})
    market.live_book().place_bet(LEADER, QID, "NO", 20)
    feed = client.get(f"/harvest/api/follow/feed?token={tok}").json()["items"]
    assert [i["trader"] for i in feed] == [LEADER]
    client.post("/harvest/api/follow", json={"token": tok, "agent": LEADER, "follow": False})
    assert client.get(f"/harvest/api/follow?token={tok}").json()["following"] == []
    assert client.get(f"/harvest/api/follow/feed?token={tok}").json()["items"] == []


def test_copies_replay_from_the_ledger_and_never_cascade():
    book = market.live_book()
    for user in ("user:a", "user:b"):
        book.record(kind="join", trader=user, payload={"nickname": user[-1]})
    book.set_follow("user:a", LEADER, on=True, copy=True, fraction=1.0, cap=50)
    book.set_follow("user:b", "user:a", on=True, copy=True)
    book.place_bet(LEADER, QID, "YES", 40)
    assert book.balance("user:a") == pytest.approx(960)
    assert book.balance("user:b") == pytest.approx(1000)  # a copy is never copied again
    price = book.price(QID)
    market.reset_live_book()
    again = market.live_book()
    assert again.price(QID) == pytest.approx(price) and again.balance("user:a") == pytest.approx(960)
    assert again.follows["user:a"][LEADER]["copy"] is True
    assert [t.get("copied_from") for t in again.trades] == [None, LEADER]


def test_follow_errors(client):
    tok = _join(client)["token"]
    assert client.post("/harvest/api/follow", json={"token": "nope", "agent": LEADER}).status_code == 401
    assert client.post("/harvest/api/follow", json={"token": tok, "agent": "agent:nobody"}).status_code == 404
    me = client.get(f"/harvest/api/me?token={tok}").json()["user_id"]
    assert client.post("/harvest/api/follow", json={"token": tok, "agent": me}).status_code == 422
    assert client.get("/harvest/api/follow/feed?token=nope").status_code == 401
    assert public_mode.is_public("POST", "/harvest/api/follow")


def test_follow_an_external_agent_by_username_and_copy_its_api_bet(client):
    tok = _join(client)["token"]
    agent = client.post("/harvest/api/agents/register", json={"name": "Drought Bear", "world_model": "Heat cuts southern yields.", "username": "drought-bear"}).json()
    r = client.post("/harvest/api/follow", json={"token": tok, "agent": "@drought-bear", "copy": True})
    assert r.status_code == 200, r.text
    assert r.json()["agent"] == agent["agent_id"] and r.json()["following"]["username"] == "drought-bear"
    client.post("/harvest/api/agents/bet", json={"token": agent["token"], "qid": QID, "side": "NO", "spend": 20})
    items = client.get(f"/harvest/api/follow/feed?token={tok}").json()["items"]
    assert [(i["name"], i["side"], i["spend"]) for i in items] == [("You", "NO", 10.0), ("Drought Bear", "NO", 20.0)]
