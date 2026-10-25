"""The simple multi-page UI under /app: every page renders offline and is reachable in PUBLIC_MODE."""

import pytest
from fastapi.testclient import TestClient

from agent.harvest import market
from agent.routes import public_mode
from agent.server import app

PAGES = ["/app", "/app/markets", "/app/market/wheat-up10-6m%402026-09", "/app/live", "/app/theory", "/app/leaderboard"]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "h.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    market.reset_live_book()
    yield TestClient(app)
    market.reset_live_book()


@pytest.mark.parametrize("path", PAGES)
def test_app_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "/harvest/static/app/app.js" in r.text


def test_app_assets_served(client):
    for name in ("app.js", "app.css"):
        assert client.get(f"/harvest/static/app/{name}").status_code == 200


@pytest.mark.parametrize("path", PAGES)
def test_app_pages_public(path):
    assert public_mode.is_public("GET", path)
