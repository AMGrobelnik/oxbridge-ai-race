"""The two endpoints the AI Race verifier calls, plus GET /race (what it will find, no secrets)."""

import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.race.adapter import handle_health, handle_task, implemented_task_types
from agent.race.protocol import TASK_TYPES


def register(app: FastAPI) -> None:
    @app.get("/agent-studio/v1/health")
    async def race_health(request: Request) -> JSONResponse:
        status, body = handle_health(request.headers.get("authorization"))
        return JSONResponse(body, status_code=status)

    @app.post("/agent-studio/v1/tasks")
    async def race_task(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        status, out = await handle_task(request.headers.get("authorization"), body)
        return JSONResponse(out, status_code=status)

    @app.get("/race")
    async def race_readiness() -> JSONResponse:
        built = implemented_task_types()
        return JSONResponse(
            {
                "verifierToken": "set" if os.environ.get("AGENT_STUDIO_VERIFIER_TOKEN") else "missing: copy it from your team page into AGENT_STUDIO_VERIFIER_TOKEN",
                "taskTypes": {t: ("handler written" if t in built else "not built yet") for t in TASK_TYPES},
            }
        )
