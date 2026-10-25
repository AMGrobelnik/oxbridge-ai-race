"""Shared test setup: the hot GET cache (agent/routes/hot_cache.py) is off unless a test turns it on, since each test
starts a fresh book and must never see a body cached by the one before."""

import pytest

from agent.routes import hot_cache


@pytest.fixture(autouse=True)
def _no_hot_cache(monkeypatch):
    monkeypatch.setenv("HARVEST_HOT_CACHE_TTL", "0")
    hot_cache._entries.clear()
    yield
    hot_cache._entries.clear()
