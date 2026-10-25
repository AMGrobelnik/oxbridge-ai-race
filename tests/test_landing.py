"""GET / (the frontend/ site for browsers, JSON for code) and the landing/ page at /landing-alt with its live feed (agent/routes/landing.py)."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import market
from agent.routes import landing
from agent.server import app

HTML = {"accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    market.reset_live_book()
    landing._cache.clear()
    yield TestClient(app)
    market.reset_live_book()
    landing._cache.clear()


def test_root_serves_the_frontend_site_to_browsers(client):
    r = client.get("/", headers=HTML)
    assert r.status_code == 200
    page = r.text
    assert "/frontend/js/api.js" in page and 'class="brand"' in page  # frontend/, wired to the live market
    assert 'class="jp-seal"' not in page and 'href="/waitlist"' in page and 'href="waitlist/index.html"' not in page


def test_landing_alt_serves_the_landing_page(client):
    r = client.get("/landing-alt", headers=HTML)
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    page = r.text
    assert 'id="glyphs"' in page and "/waitlist/hero-loop.mp4" in page  # the waitlist hero
    assert 'id="g-live"' in page and "/harvest/api/landing" in page  # the live globe on real bets
    assert "data-fallback" not in page  # every site link resolved to a route that exists
    assert 'href="/waitlist"' in page
    assert 'href="/live"' in page or 'href="/app"' in page  # /live once agent/routes/site_pages.py serves it, else the /app fallback


def test_root_still_answers_code_with_json(client):
    r = client.get("/", headers={"accept": "application/json"})
    assert r.json()["service"] == "superteam-agent"


def test_classic_keeps_the_module_list(client):
    r = client.get("/classic")
    assert r.status_code == 200
    assert "is running" in r.text


def test_fallback_links_follow_registered_routes(tmp_path, monkeypatch):
    page = tmp_path / "index.html"
    page.write_text('<a href="/live" data-fallback="/app">x</a><a href="/markets" data-fallback="#markets">y</a>', encoding="utf-8")
    monkeypatch.setattr(landing, "PAGE", page)
    assert landing.landing_html({"/live"}) == '<a href="/live">x</a><a href="#markets">y</a>'


def test_feed_full_then_poll(client):
    qid = next(iter(market.live_book().markets))
    full = client.get("/harvest/api/landing").json()
    assert {"seq", "markets", "recent", "leaderboard", "fund", "geo", "personas", "volume", "anchor"} <= set(full)
    assert full["anchor"] is None  # HARVEST_ANCHOR=0: nothing confirmed on Solana, and the page says so
    assert any(m["qid"] == qid and len(m["split"]) == 2 for m in full["markets"])
    poll = client.get(f"/harvest/api/landing?since={full['seq']}").json()
    assert {"seq", "trades", "prices", "bets", "per_min", "volume", "treasury", "anchor"} <= set(poll)
    assert qid in poll["prices"]
    assert all(t["seq"] > full["seq"] for t in poll["trades"])
