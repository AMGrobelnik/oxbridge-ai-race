"""Agent swarm counter: GET /harvest/api/swarm.

Counts the AI agent accounts the swarm (agent/harvest/swarm.py) registered, identified by their
owner, and their bets in the last hour, plus the latest few swarm bets with each agent's name
(robot tag, persona and model) for the Live page.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent.harvest.market import LiveBook, live_book
from agent.harvest.swarm import SWARM_OWNER

NO_STORE = {"Cache-Control": "no-store"}


def swarm_view(book: LiveBook, now: datetime | None = None, recent: int = 12) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    with book.lock:
        ids = {tid: info for tid, info in book.traders.items() if info.get("owner") == SWARM_OWNER}
        hour = 0
        latest: list[dict[str, Any]] = []
        for t in reversed(book.trades):
            if str(t.get("at") or "")[:19] < since:
                break
            if t["trader"] in ids:
                hour += 1
                if len(latest) < recent:
                    latest.append({"name": ids[t["trader"]].get("name"), "agent_id": t["trader"], "qid": t["qid"], "side": t["side"],
                                   "spend": round(t["spend"], 2), "price_after": round(t["price_after"], 4), "at": t["at"]})
        all_hour = sum(1 for t in book.trades[-50000:] if str(t.get("at") or "")[:19] >= since)
    return {"agents": len(ids), "bets_last_hour": hour, "all_bets_last_hour": all_hour, "recent": latest}


def register(app: FastAPI) -> None:
    @app.get("/harvest/api/swarm")
    async def swarm_route() -> JSONResponse:
        book = await asyncio.to_thread(live_book)
        return JSONResponse(await asyncio.to_thread(swarm_view, book), headers=NO_STORE)
