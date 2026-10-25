"""ask_gemini timeout and retry. No network: the client is a fake."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agent.ai import gemini


def fake_client(handler):
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=handler)))


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(gemini, "RETRY_DELAY_S", 0)


def test_slow_client_times_out_after_one_retry(monkeypatch):
    calls = []

    async def slow(**kwargs):
        calls.append(1)
        await asyncio.sleep(10)

    monkeypatch.setattr(gemini, "_get_client", lambda: fake_client(slow))
    with pytest.raises(gemini.GeminiUnavailableError):
        asyncio.run(gemini.ask_gemini("hi", timeout_s=0.05))
    assert len(calls) == 2


def test_retry_recovers_from_transient_error(monkeypatch):
    calls = []

    async def flaky(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return SimpleNamespace(text="ok")

    monkeypatch.setattr(gemini, "_get_client", lambda: fake_client(flaky))
    assert asyncio.run(gemini.ask_gemini("hi")) == "ok"
    assert len(calls) == 2


def test_env_timeout(monkeypatch):
    async def slow(**kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(gemini, "_get_client", lambda: fake_client(slow))
    monkeypatch.setenv("GEMINI_TIMEOUT_S", "0.05")
    with pytest.raises(httpx.HTTPError):
        asyncio.run(gemini.ask_gemini("hi"))
