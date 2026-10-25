"""Proof the scoring is real: GET /harvest/api/replay/timeline, /harvest/api/calibration, /harvest/api/impact and the
/app/replay and /app/impact pages (SPEC.md section 13). All read the current frozen backtest through
`harvest.replay_result()`, so a re-frozen backtest shows up without a code change.
"""

import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent.harvest import proof
from agent.routes import harvest as api
from agent.routes.harvest_pages import _page

PAGES = {"/app/replay": "app/replay.html", "/app/impact": "app/impact.html"}
SPLITS = ("all", "dev", "holdout")
NO_CACHE = {"Cache-Control": "no-cache"}
_cache: dict[Any, Any] = {}


def _cached(name: str, fn: Any, *args: Any, **kw: Any) -> Any:
    """Memoise per frozen replay object, so a re-frozen backtest invalidates everything."""
    result = api.replay_result()
    if _cache.get("result") is not result:  # identity, not id(): the cache holds the object, so a new replay always misses
        _cache.clear()
        _cache["result"] = result
    key = (name, args, tuple(sorted(kw.items())))
    if key not in _cache:
        _cache[key] = fn(result, *args, **kw)
    return _cache[key]


def register(app: FastAPI) -> None:
    for path, name in PAGES.items():
        _page(app, path, name)

    @app.get("/harvest/api/replay/timeline")
    async def timeline_route() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(_cached, "timeline", proof.timeline), headers=NO_CACHE)

    @app.get("/harvest/api/calibration")
    async def calibration_route(agent: str | None = None, split: str = "all", bins: int = 10) -> JSONResponse:
        if split not in SPLITS:
            return JSONResponse({"error": f"split must be one of {', '.join(SPLITS)}"}, status_code=400)
        bins = max(2, min(20, bins))
        if not agent:
            return JSONResponse(await asyncio.to_thread(_cached, "calsum", proof.calibration_summary, split=split, bins=bins), headers=NO_CACHE)
        body = await asyncio.to_thread(_cached, "cal", proof.calibration, agent, split=split, bins=bins)
        if body is None:
            return JSONResponse({"error": "Unknown agent. GET /harvest/api/calibration lists the agents."}, status_code=404)
        return JSONResponse(body, headers=NO_CACHE)

    @app.get("/harvest/api/impact")
    async def impact_route() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(_cached, "impact", proof.impact), headers=NO_CACHE)
