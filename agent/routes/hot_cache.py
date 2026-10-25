"""A 2 s in-process cache for the public GETs that every live page polls, gzipped once per entry.

Under a burst (many tabs polling /harvest/api/state at once) each response used to be computed and sent uncompressed
(~600 kB) per request, and the tunnel returned 502s. Here the first request for a path plus query computes the body;
concurrent requests for the same key wait on it (single flight) and every request within the TTL gets the stored
bytes, gzipped for clients that accept it. Writes (bets, joins) are POSTs and never touch this cache, so a bettor's
own POST response is always fresh. HARVEST_HOT_CACHE_TTL sets the TTL in seconds; 0 turns the cache off.
"""

import asyncio
import gzip
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response
from starlette.middleware.base import RequestResponseEndpoint

HOT_PATHS = frozenset({
    "/harvest/api/state", "/harvest/api/leaderboard", "/harvest/api/swarm", "/harvest/api/landing",
    "/harvest/api/news", "/harvest/api/markets", "/harvest/api/fund", "/harvest/api/season", "/pitch/api/state",
})
MAX_ENTRIES = 512
GZIP_MIN_BYTES = 1024

_entries: dict[str, tuple[float, int, dict[str, str], bytes, bytes | None]] = {}
_locks: dict[str, asyncio.Lock] = {}


def ttl() -> float:
    try:
        return float(os.environ.get("HARVEST_HOT_CACHE_TTL") or 2.0)
    except ValueError:
        return 2.0


def _fresh(key: str, now: float, ttl_s: float) -> tuple[float, int, dict[str, str], bytes, bytes | None] | None:
    hit = _entries.get(key)
    return hit if hit is not None and now - hit[0] < ttl_s else None


def _prune(now: float, ttl_s: float) -> None:
    if len(_entries) < MAX_ENTRIES:
        return
    for key in [k for k, v in _entries.items() if now - v[0] >= ttl_s]:
        _entries.pop(key, None)
        _locks.pop(key, None)
    while len(_entries) >= MAX_ENTRIES:  # still full of live entries: drop the oldest
        oldest = min(_entries, key=lambda k: _entries[k][0])
        _entries.pop(oldest, None)
        _locks.pop(oldest, None)


def _reply(request: Request, entry: tuple[float, int, dict[str, str], bytes, bytes | None]) -> Response:
    _, status, headers, body, zipped = entry
    out = dict(headers)
    if zipped is not None and "gzip" in request.headers.get("accept-encoding", ""):
        out["Content-Encoding"] = "gzip"
        body = zipped
    out["Vary"] = "Accept-Encoding"
    return Response(body, status_code=status, headers=out)


async def hot_cache(request: Request, call_next: RequestResponseEndpoint) -> Response:
    ttl_s = ttl()
    if request.method != "GET" or request.url.path not in HOT_PATHS or ttl_s <= 0:
        return await call_next(request)
    key = request.url.path + "?" + request.url.query
    hit = _fresh(key, time.monotonic(), ttl_s)
    if hit is not None:
        return _reply(request, hit)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _fresh(key, time.monotonic(), ttl_s)  # computed by the request we waited on
        if hit is not None:
            return _reply(request, hit)
        response = await call_next(request)
        if response.status_code != 200 or response.headers.get("content-encoding"):
            return response
        body = b"".join([chunk if isinstance(chunk, bytes) else chunk.encode() async for chunk in response.body_iterator])  # type: ignore[attr-defined]
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ("content-length", "content-encoding", "vary")}
        zipped = await asyncio.to_thread(gzip.compress, body, 5) if len(body) >= GZIP_MIN_BYTES else None
        now = time.monotonic()
        _prune(now, ttl_s)
        entry = (now, response.status_code, headers, body, zipped)
        _entries[key] = entry
        return _reply(request, entry)


def register(app: FastAPI) -> None:
    app.middleware("http")(hot_cache)
