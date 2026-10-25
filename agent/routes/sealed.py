"""HTTP API and demo page for Sealed under /sealed (SPEC.md section 9). Owner: W4.

Skeleton: the page and env listing work; endpoints that need W1/W2 return 501 until they land.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent.sealed.environment import list_environments, load_environment

STATIC_DIR = Path(__file__).resolve().parent.parent / "sealed" / "static"


def register(app: FastAPI) -> None:
    app.mount("/sealed/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="sealed-static")

    @app.get("/sealed", response_model=None)
    async def sealed_page() -> FileResponse | JSONResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return JSONResponse({"error": "The Sealed page is not built yet."}, status_code=501)
        return FileResponse(index)

    @app.get("/sealed/api/envs")
    async def sealed_envs() -> JSONResponse:
        views = []
        for name in list_environments():
            try:
                views.append(load_environment(name).public_view())
            except (ImportError, AttributeError, KeyError) as err:
                views.append({"name": name, "error": f"plug-in not loadable: {type(err).__name__}"})
        return JSONResponse(views)
