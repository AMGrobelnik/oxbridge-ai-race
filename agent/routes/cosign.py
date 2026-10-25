"""Human co-sign guardrail endpoints under /api/cosign (agent/payments/cosign.py, SPEC.md "Human co-sign guardrail").

Agent payments over COSIGN_CAP_USD are queued, not sent; a human lists them here and approves
(sends with pay_usdc) or rejects them. Every decision is appended to the audit log.
When COSIGN_TOKEN is set, approve and reject need "Authorization: Bearer <COSIGN_TOKEN>".
"""

import json
import os
import secrets
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.payments import cosign

_STATUSES = ("open", "pending", "approved_pending_onchain", "sent", "rejected")


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _unauthorised(request: Request) -> JSONResponse | None:
    token = os.environ.get("COSIGN_TOKEN", "").strip()
    if not token:
        return None
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return None if secrets.compare_digest(given, token) else _error("Co-sign needs Authorization: Bearer <COSIGN_TOKEN>", 401)


async def _decision(request: Request) -> tuple[str, str | None]:
    try:
        body: Any = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = None
    body = body if isinstance(body, dict) else {}
    by = str(body.get("by") or "human")[:80]
    note = str(body["note"])[:500] if body.get("note") else None
    return by, note


def register(app: FastAPI) -> None:
    @app.get("/api/cosign/pending")
    async def cosign_pending() -> JSONResponse:
        return JSONResponse({"capUsd": cosign.cap_usd(), "pending": cosign.list_approvals("open")})

    @app.get("/api/cosign/approvals")
    async def cosign_approvals(status: str | None = None) -> JSONResponse:
        if status is not None and status not in _STATUSES:
            return _error(f"Unknown status: expected one of {', '.join(_STATUSES)}", 400)
        return JSONResponse({"capUsd": cosign.cap_usd(), "approvals": cosign.list_approvals(status)})

    @app.post("/api/cosign/{approval_id}/approve")
    async def cosign_approve(approval_id: str, request: Request) -> JSONResponse:
        if (denied := _unauthorised(request)) is not None:
            return denied
        by, note = await _decision(request)
        try:
            return JSONResponse(await cosign.approve(approval_id, by=by, note=note))
        except cosign.CosignError as err:
            return _error(str(err), err.status)

    @app.post("/api/cosign/{approval_id}/reject")
    async def cosign_reject(approval_id: str, request: Request) -> JSONResponse:
        if (denied := _unauthorised(request)) is not None:
            return denied
        by, note = await _decision(request)
        try:
            return JSONResponse(cosign.reject(approval_id, by=by, note=note))
        except cosign.CosignError as err:
            return _error(str(err), err.status)

    @app.get("/api/cosign/audit")
    async def cosign_audit(limit: int = 200) -> JSONResponse:
        return JSONResponse({"audit": cosign.read_audit(max(1, min(limit, 1000)))})
