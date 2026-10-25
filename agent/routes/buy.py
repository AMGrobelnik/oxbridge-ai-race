"""The service's own agent buys from its own paid endpoint.

The URL is fixed (this server, on the port it is listening on), never taken from the request,
so nobody can make your agent pay other sites.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.paidapi.buyer import buy, get_spending_tracker

logger = logging.getLogger("agent")


def register(app: FastAPI) -> None:
    @app.get("/api/buy")
    async def spending() -> JSONResponse:
        try:
            return JSONResponse({"spending": get_spending_tracker().snapshot()})
        except ValueError as err:
            return JSONResponse({"error": str(err)}, status_code=500)

    @app.post("/api/buy")
    async def buy_once(request: Request) -> JSONResponse:
        server = request.scope.get("server") or ("127.0.0.1", 3000)
        target = f"http://127.0.0.1:{server[1]}/premium-data"
        try:
            return JSONResponse((await buy(target)).to_json())
        except Exception as err:
            logger.warning("Purchase failed: %s", err)
            return JSONResponse({"error": str(err), "spending": _safe_snapshot()}, status_code=400)


def _safe_snapshot() -> dict[str, object] | None:
    try:
        return get_spending_tracker().snapshot()
    except ValueError:
        return None
