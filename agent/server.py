"""The agent's HTTP service (FastAPI). Run it with `python -m agent serve`.

GET / lists every feature in this project (a page for browsers, JSON for code). Every file in agent/routes adds its own endpoints,
so adding a feature never means editing this file.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException

from . import routes
from .config import load_env
from .landing import render_landing
from .modules import modules
from .plugins import load_plugins

load_env()
logger = logging.getLogger("agent")

app = FastAPI(title="Superteam agent")


@app.get("/")
async def home(request: Request) -> Response:
    """Browsers get a readable page (agent/landing.py); code (curl, httpx, other agents) gets JSON."""
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(render_landing(modules, str(request.base_url).rstrip("/")))
    return JSONResponse({"service": "superteam-agent", "modules": modules})


for route in load_plugins(routes):
    route.register(app)


@app.exception_handler(HTTPException)
async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return JSONResponse({"error": "Not found. GET / lists the available endpoints."}, status_code=404)
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(Exception)
async def server_error(_request: Request, exc: Exception) -> JSONResponse:
    logger.error("Request failed", exc_info=exc)
    return JSONResponse({"error": "Internal server error. Check the server logs."}, status_code=500)
