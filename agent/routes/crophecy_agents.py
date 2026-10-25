"""Crophecy agent features under /harvest/api/*: debate threads, resolution, markets drafted from the news.

GET  /harvest/api/debate?qid=&since=<id>&limit=   agent debate threads {threads: [{id, qid, posts: [...]}], latest_id}
GET  /harvest/api/resolutions                     per-market resolution status, proposals, live Brier scores
POST /harvest/api/resolve/{qid} {action: apply|reject, proposal_id?}          admin (X-Admin-Token)
GET  /harvest/api/markets/drafts                  markets drafted by the news agent
POST /harvest/api/markets/drafts/{id}/approve     admin (X-Admin-Token): opens the draft in the live book

This module's name sorts before `harvest`, so GET /harvest/api/markets/drafts registers before the
/harvest/api/markets/{qid} detail route would capture it.
"""

import asyncio
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.harvest import debate, market, market_gen, resolve
from agent.harvest.market import MarketError
from agent.routes.public_mode import admin_denial

NO_STORE = {"Cache-Control": "no-store"}


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def ensure_jobs() -> None:
    """Starts the debate, resolution and market-drafting loops (each a no-op offline or when disabled)."""
    debate.ensure_loop()
    resolve.ensure_loops()
    market_gen.ensure_loop()


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def register(app: FastAPI) -> None:
    @app.get("/harvest/api/debate")
    async def debate_route(qid: str = "", since: int | None = None, limit: int = 200) -> JSONResponse:
        ensure_jobs()
        book = await asyncio.to_thread(market.live_book)
        body = await asyncio.to_thread(debate.view, book, qid=qid or None, since=since, limit=limit)
        return JSONResponse(body, headers=NO_STORE)

    @app.get("/harvest/api/resolutions")
    async def resolutions_route() -> JSONResponse:
        ensure_jobs()
        book = await asyncio.to_thread(market.live_book)
        return JSONResponse(await asyncio.to_thread(resolve.view, book), headers=NO_STORE)

    @app.post("/harvest/api/resolve/{qid:path}")
    async def resolve_route(qid: str, request: Request) -> JSONResponse:
        denied = admin_denial(request)
        if denied is not None:
            return denied
        data = await _json(request)
        action = str(data.get("action") or "apply").lower()
        try:
            pid = int(data["proposal_id"]) if data.get("proposal_id") is not None else None
        except (TypeError, ValueError):
            return _error("proposal_id must be an integer", 400)
        book = await asyncio.to_thread(market.live_book)
        try:
            out = await asyncio.to_thread(resolve.decide, book, qid, action, proposal_id=pid)
        except LookupError as err:
            return _error(str(err), 404)
        except (ValueError, MarketError) as err:
            return _error(str(err), 409 if isinstance(err, MarketError) else 400)
        return JSONResponse(out)

    @app.get("/harvest/api/markets/drafts")
    async def drafts_route(status: str = "") -> JSONResponse:
        ensure_jobs()
        rows = await asyncio.to_thread(market_gen.drafts, status=status or None)
        return JSONResponse({"drafts": rows, "job": market_gen.job.status()}, headers=NO_STORE)

    @app.post("/harvest/api/markets/drafts/{draft_id}/approve")
    async def approve_route(draft_id: int, request: Request) -> JSONResponse:
        denied = admin_denial(request)
        if denied is not None:
            return denied
        book = await asyncio.to_thread(market.live_book)
        try:
            out = await asyncio.to_thread(market_gen.approve, book, draft_id)
        except LookupError as err:
            return _error(str(err), 404)
        except ValueError as err:
            return _error(str(err), 409)
        return JSONResponse(out)
