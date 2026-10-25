"""Harvest pages /harvest, /harvest/play, /harvest/board, /harvest/qr.svg (SPEC.md section 12, addendum R3). Owner: UI."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent.parent / "harvest" / "static"
PAGES = {"/harvest": "index.html", "/harvest/play": "play.html", "/harvest/board": "board.html"}


def register(app: FastAPI) -> None:
    app.mount("/harvest/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="harvest-static")
    for path, name in PAGES.items():
        _page(app, path, name)


def _page(app: FastAPI, path: str, name: str) -> None:
    @app.get(path, response_class=HTMLResponse, name=f"harvest-{name}")
    async def page() -> HTMLResponse:
        file = STATIC_DIR / name
        body = file.read_text() if file.exists() else "<!doctype html><title>Harvest</title><p>Harvest is being built.</p>"
        return HTMLResponse(body, headers={"Cache-Control": "no-cache"})
