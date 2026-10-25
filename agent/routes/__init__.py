"""HTTP endpoints. Every file in this folder adds its own with a `register(app)` function:

    from fastapi import FastAPI

    def register(app: FastAPI) -> None:
        @app.get("/api/hello")
        async def hello() -> dict[str, str]:
            return {"hello": "world"}
"""
