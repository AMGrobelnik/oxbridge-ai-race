"""The hot GET cache: a burst computes once, bodies are gzipped, POSTs and other paths pass through. Offline."""

import asyncio

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent.routes import hot_cache


def _app(calls: list[str]) -> FastAPI:
    app = FastAPI()
    hot_cache.register(app)

    @app.get("/harvest/api/state")
    async def state(since: int | None = None) -> JSONResponse:
        calls.append(f"state:{since}")
        await asyncio.sleep(0.05)
        return JSONResponse({"since": since, "n": len(calls), "pad": "x" * 4000}, headers={"Cache-Control": "no-store"})

    @app.post("/harvest/api/bet")
    async def bet() -> JSONResponse:
        calls.append("bet")
        return JSONResponse({"n": len(calls)})

    @app.get("/harvest/api/me")
    async def me() -> JSONResponse:
        calls.append("me")
        return JSONResponse({"n": len(calls)})

    return app


async def _burst(monkeypatch) -> tuple[list[str], list[httpx.Response], httpx.Response]:
    monkeypatch.setenv("HARVEST_HOT_CACHE_TTL", "2")
    calls: list[str] = []
    transport = httpx.ASGITransport(app=_app(calls))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        burst = await asyncio.gather(*[c.get("/harvest/api/state", headers={"Accept-Encoding": "gzip"}) for _ in range(20)])
        other = await c.get("/harvest/api/state?since=5")
        await c.post("/harvest/api/bet")
        await c.post("/harvest/api/bet")
        await c.get("/harvest/api/me")
        await c.get("/harvest/api/me")
    return calls, burst, other


def test_burst_computes_once_and_gzips(monkeypatch):
    calls, burst, other = asyncio.run(_burst(monkeypatch))
    assert calls.count("state:None") == 1
    assert all(r.status_code == 200 and r.json()["n"] == 1 for r in burst)
    assert burst[0].headers["content-encoding"] == "gzip" and burst[0].headers["cache-control"] == "no-store"
    assert other.json()["since"] == 5  # keyed by path plus query
    assert calls.count("bet") == 2 and calls.count("me") == 2  # POSTs and other GETs are never cached


def test_ttl_zero_turns_it_off(monkeypatch):
    monkeypatch.setenv("HARVEST_HOT_CACHE_TTL", "0")
    calls: list[str] = []

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(calls)), base_url="http://t") as c:
            for _ in range(3):
                await c.get("/harvest/api/state")

    asyncio.run(run())
    assert calls.count("state:None") == 3

