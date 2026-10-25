"""Sealed Verify: the agent-verified crypto merchant map under /map.

GET  /map                    the map page (Leaflet + OpenStreetMap)
GET  /map/static/<file>      its JS and CSS
GET  /map/api/state          pins with status, last probe and receipt; live feed; counters; sealed round
GET  /map/api/ledger         the full hash-chained probe ledger and every round (sealed ones redacted)
POST /map/api/onboard        {name, target, lat, lng, place?}: adds a merchant and probes it at once
POST /map/api/reverify       {merchant_id}: probes one merchant now (rate-limited)
GET  /map/verify             recomputes the ledger chain and every revealed schedule commitment

A background prober re-probes every merchant under a sealed schedule every VERIFY_INTERVAL_S
(default 150 s) while the app runs; VERIFY_PROBER=0 turns it off.
"""

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from agent.verify import merchants, prober
from agent.verify.ratelimit import RateLimiter, client_ip

STATIC_DIR = Path(__file__).resolve().parent.parent / "verify" / "static"
STATIC_FILES = {"map.js": "text/javascript", "map.css": "text/css"}
MERCHANT_ID = re.compile(r"^[a-z0-9-]{1,40}$")
MAX_BODY = 4096

logger = logging.getLogger("agent")
onboard_ip_limiter = RateLimiter(3, 600)
onboard_global_limiter = RateLimiter(30, 3600)
reverify_ip_limiter = RateLimiter(6, 60)
reverify_merchant_limiter = RateLimiter(1, 15)


def _error(message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _limited(wait: float) -> JSONResponse:
    return _error(f"Too many requests: try again in {int(wait) + 1} s.", 429, {"Retry-After": str(int(wait) + 1)})


async def _json_body(request: Request) -> dict[str, Any] | JSONResponse:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return _error("Request too large.", 413)
    try:
        import json

        body = json.loads(raw or b"{}")
    except ValueError:
        return _error("Send a JSON object.", 400)
    if not isinstance(body, dict):
        return _error("Send a JSON object.", 400)
    return body


def _wrap_lifespan(app: FastAPI) -> None:
    original = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        async with original(a) as state:
            task = None
            if os.environ.get("VERIFY_PROBER", "1") != "0":
                task = asyncio.get_running_loop().create_task(prober.get_service().loop())
            try:
                yield state
            finally:
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

    app.router.lifespan_context = lifespan


def register(app: FastAPI) -> None:
    _wrap_lifespan(app)

    @app.get("/map", include_in_schema=False)
    async def map_page() -> Response:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/map/static/{name}", include_in_schema=False)
    async def map_static(name: str) -> Response:
        if name not in STATIC_FILES:
            return _error("Not found.", 404)
        return FileResponse(STATIC_DIR / name, media_type=STATIC_FILES[name], headers={"Cache-Control": "no-cache"})

    @app.get("/map/api/state")
    async def map_state() -> dict[str, Any]:
        return prober.get_service().public_state()

    @app.get("/map/api/ledger")
    async def map_ledger() -> dict[str, Any]:
        service = prober.get_service()
        return {
            "entries": service.state["entries"],
            "rounds": [service._public_round(r) for r in service.state["rounds"]],
            "anchors": service.state["anchors"],
        }

    @app.get("/map/verify")
    async def map_verify() -> dict[str, Any]:
        return prober.get_service().verify()

    @app.post("/map/api/onboard")
    async def map_onboard(request: Request) -> Response:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        wait = onboard_ip_limiter.hit(client_ip(request)) or onboard_global_limiter.hit("all")
        if wait:
            return _limited(wait)
        service = prober.get_service()
        try:
            merchant = await service.add_merchant(body)
        except merchants.InvalidMerchant as err:
            return _error(str(err), 400)
        round_rec = await service.run_round(kind="onboard", merchant_ids=[merchant["id"]])
        pin = next(p for p in service.public_state()["merchants"] if p["id"] == merchant["id"])
        return JSONResponse({"merchant": pin, "payment_link": merchants.payment_link(merchant), "round": round_rec}, status_code=201)

    @app.post("/map/api/reverify")
    async def map_reverify(request: Request) -> Response:
        body = await _json_body(request)
        if isinstance(body, JSONResponse):
            return body
        merchant_id = body.get("merchant_id")
        if not isinstance(merchant_id, str) or not MERCHANT_ID.fullmatch(merchant_id):
            return _error("merchant_id is missing or invalid.", 400)
        service = prober.get_service()
        merchant = service.merchant(merchant_id)
        if merchant is None:
            return _error("Unknown merchant.", 404)
        if merchant.get("kind") == "physical":
            return _error("This is a physical-only listing: there is no crypto checkout to probe.", 400)
        wait = reverify_ip_limiter.hit(client_ip(request)) or reverify_merchant_limiter.hit(merchant_id)
        if wait:
            return _limited(wait)
        round_rec = await service.run_round(kind="reverify", merchant_ids=[merchant_id])
        pin = next(p for p in service.public_state()["merchants"] if p["id"] == merchant_id)
        return JSONResponse({"merchant": pin, "round": round_rec})
