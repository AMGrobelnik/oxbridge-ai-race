"""The waitlist landing page at /waitlist/: served with the server's Supabase config injected, assets whitelisted, public."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.routes import harvest_pages, public_mode
from agent.server import app


@pytest.fixture()
def client():
    return TestClient(app)


def test_waitlist_redirects_to_slash_keeping_the_query(client):
    r = client.get("/waitlist?ref=qr", follow_redirects=False)
    assert r.status_code == 308
    assert r.headers["location"] == "/waitlist/?ref=qr"


def test_waitlist_page_injects_supabase_config(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example-project.supabase.co/")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test</script>")
    r = client.get("/waitlist/")
    assert r.status_code == 200
    assert "Get in before <em>the harvest.</em>" in r.text
    assert 'const SUPABASE_URL = "https://example-project.supabase.co";' in r.text
    assert 'const SUPABASE_ANON_KEY = "sb_publishable_test\\u003c/script>";' in r.text


def test_waitlist_page_without_config_keeps_the_backend_fallback(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
    assert "const SUPABASE_ANON_KEY = '';" in harvest_pages.waitlist_html()


@pytest.mark.parametrize("name", sorted(harvest_pages.WAITLIST_ASSETS))
def test_waitlist_assets_served(client, name):
    r = client.get(f"/waitlist/{name}")
    assert r.status_code == 200
    assert r.headers["content-type"] == harvest_pages.WAITLIST_ASSETS[name]


@pytest.mark.parametrize("name", ["README.md", "supabase.sql", "hero-source.mp4", "..%2F.env"])
def test_waitlist_other_files_are_not_served(client, name):
    assert client.get(f"/waitlist/{name}").status_code == 404


@pytest.mark.parametrize("path", ["/waitlist", "/waitlist/", "/waitlist/hero.jpg"])
def test_waitlist_is_public(path):
    assert public_mode.is_public("GET", path)


def test_no_public_page_renders_the_waitlist_count():
    """The waitlist size stays private: neither the waitlist page nor /app shows or fetches it (the API keeps it)."""
    page = (harvest_pages.WAITLIST_DIR / "index.html").read_text()
    assert "waitlist_count" not in page and 'id="count"' not in page and "on the list ·" not in page
    app_js = (Path(harvest_pages.__file__).resolve().parents[1] / "harvest" / "static" / "app" / "app.js").read_text()
    assert "on the waitlist" not in app_js
