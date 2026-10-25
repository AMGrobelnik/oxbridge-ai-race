"""Farmer mode, climate-stress map and disagreement index (agent/harvest/world.py, SPEC.md section 16).

GET /harvest/api/farmer?crop=&region=, GET /harvest/api/map, GET /harvest/api/disagreement?limit= and the pages
/app/farmer, /app/map and /app/disagreement. The farmer reason comes from the fast LLM (Croppy) within
FARMER_DEADLINE_S (default 6 s), cached per (crop, region, probability), else a deterministic one.
"""

import asyncio
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from agent.harvest import fast_llm, market, world
from agent.routes import harvest as api
from agent.routes.harvest_pages import versioned

NO_STORE = {"Cache-Control": "no-store"}
STATIC = Path(__file__).resolve().parent.parent / "harvest" / "static" / "app"
PAGES = {"/app/farmer": "farmer.html", "/app/map": "map.html", "/app/disagreement": "disagreement.html"}
CACHE_MAX = 128
ask = fast_llm.ask  # module attribute so tests can stub the model
_cache: "OrderedDict[tuple[str, str, float], str]" = OrderedDict()


def deadline_s() -> float:
    try:
        return float(os.environ.get("FARMER_DEADLINE_S") or 6)
    except ValueError:
        return 6.0


def views() -> list[dict[str, Any]]:
    book = market.live_book()
    with book.lock:
        return [book.market_view(qid) for qid in book.markets]


async def reason(ctx: dict[str, Any]) -> tuple[str, str]:
    k = (ctx["crop"], ctx["region"], round(ctx["probability"], 2))
    if k in _cache:
        return _cache[k], "cache"
    if fast_llm.available():
        try:
            text = await asyncio.wait_for(asyncio.to_thread(ask, world.build_prompt(ctx)), deadline_s())
            if parsed := world.parse(text):
                _cache[k] = parsed
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)
                return parsed, "llm"
        except Exception:  # noqa: BLE001 - any LLM failure falls back to the deterministic reason
            pass
    return world.farmer_reason(ctx), "fallback"


def _page(app: FastAPI, path: str, name: str) -> None:
    @app.get(path, response_class=HTMLResponse, name=f"harvest-world-{name}")
    async def page() -> HTMLResponse:
        return HTMLResponse(versioned((STATIC / name).read_text()), headers={"Cache-Control": "no-cache"})


def register(app: FastAPI) -> None:
    @app.get("/harvest/api/farmer")
    async def farmer_route(crop: str = "wheat", region: str = "France") -> JSONResponse:
        crop, region = crop.strip().lower(), region.strip().title()
        if crop not in world.CROPS:
            return api._error(f"crop must be one of {', '.join(world.CROPS)}", 400)
        if region not in world.REGIONS:
            return api._error(f"region must be one of {', '.join(world.REGIONS)}", 400)
        ctx = world.farmer_context(await asyncio.to_thread(views), crop, region)
        if ctx is None:
            return api._error(f"no open market for {crop}", 404)
        text, source = await reason(ctx)
        return JSONResponse({**ctx, "reason": text, "source": source, "by": "Croppy", "crops": list(world.CROPS),
                             "regions": list(world.REGIONS)}, headers=NO_STORE)

    @app.get("/harvest/api/map")
    async def map_route() -> JSONResponse:
        return JSONResponse(world.stress_map(await asyncio.to_thread(views)), headers=NO_STORE)

    @app.get("/harvest/api/disagreement")
    async def disagreement_route(limit: int = 50) -> JSONResponse:
        rows = world.disagreement(await asyncio.to_thread(views), max(1, min(int(limit), 100)))
        return JSONResponse({"markets": rows, "framing": "Where the AIs disagree most = real uncertainty.",
                             "metric": "std of the agents' latest p_yes (population standard deviation)"}, headers=NO_STORE)

    for path, name in PAGES.items():
        _page(app, path, name)
