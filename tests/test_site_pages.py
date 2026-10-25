"""The Crophecy site: frontend/ served on clean routes with api.js and its hooks, and the /harvest/api/site feed it reads."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import market
from agent.routes import public_mode, site_pages
from agent.server import app

ROUTES = ["/home", "/live", "/markets", "/market/wheat-up10-6m%402026-09", "/leaderboard", "/theory", "/farmer"]
HTML = {"accept": "text/html"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    market.reset_live_book()
    yield TestClient(app)
    market.reset_live_book()


@pytest.mark.parametrize("path", ROUTES)
def test_routes_serve_the_wired_frontend(client, path):
    r = client.get(path, headers=HTML)
    assert r.status_code == 200
    assert '<script src="/frontend/js/api.js' in r.text
    assert 'window.CROPHECY_HOOKS=["boot", "sim", "let"]' in r.text
    assert r.text.index("/frontend/js/api.js") < r.text.index("const AGENTS=")


def test_api_js_is_served(client):
    r = client.get("/frontend/js/api.js")
    assert r.status_code == 200 and "crophecyBoot" in r.text


def test_hooks_apply_to_the_page_and_skip_when_main_has_them():
    page = site_pages.PAGE.read_text()
    html, applied = site_pages.wired_page(page, "1")
    assert applied == ["boot", "sim", "let"]
    assert "(window.crophecyBoot||(f=>f()))(()=>go(" in html
    assert "window.CROPHECY_LIVE||setInterval(tick,1000);" in html
    assert all(f"let {name}=" in html for name in ("money", "kfmt", "MAXV"))
    _, again = site_pages.wired_page(html, "1")  # a page that already carries the hooks is left alone
    assert again == ["boot", "sim", "let"]
    _, none = site_pages.wired_page("<html><script>go(1)</script></html>", "1")
    assert none == []


def test_site_feed_full_and_since(client):
    body = client.get("/harvest/api/site").json()
    assert body["markets"] and {"qid", "title", "p_yes", "history", "agent_forecasts", "rule", "why"} <= set(body["markets"][0])
    assert {"house", "personas", "leaderboard", "fund", "geo", "anchors", "seq"} <= set(body)
    assert all("--" not in pid for pid in body["house"])
    poll = client.get(f"/harvest/api/site?since={body['seq']}").json()
    assert poll["recent_trades"] == [] and "leaderboard" not in poll and set(poll["markets"][0]) == {"qid", "p_yes", "volume", "fees", "status", "outcome"}


def test_a_bet_shows_up_in_the_feed(client):
    qid = next(m["qid"] for m in client.get("/harvest/api/site").json()["markets"] if m["status"] == "open")
    seq = client.get("/harvest/api/site").json()["seq"]
    token = client.post("/harvest/api/join", json={"nickname": "sitefeed"}).json()["token"]
    assert client.post("/harvest/api/bet", json={"token": token, "qid": qid, "side": "YES", "spend": 5}).status_code == 200
    poll = client.get(f"/harvest/api/site?since={seq}").json()
    assert [t["qid"] for t in poll["recent_trades"]] == [qid]
    assert poll["names"][poll["recent_trades"][0]["trader"]] == "sitefeed"


@pytest.mark.parametrize("path", [*ROUTES, "/frontend/js/api.js", "/harvest/api/site"])
def test_site_routes_are_public(path):
    assert public_mode.is_public("GET", path)
