"""POST /api/chat  { "prompt": "..." }  ->  { "text": "..." }"""

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.ai.gemini import MissingGeminiKeyError, ask_gemini

MAX_PROMPT_CHARS = 4000
logger = logging.getLogger("agent")


def register(app: FastAPI) -> None:
    @app.post("/api/chat")
    async def chat(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = None
        prompt = body.get("prompt") if isinstance(body, dict) else None
        prompt = prompt.strip() if isinstance(prompt, str) else ""
        if not prompt:
            return JSONResponse({"error": 'Send JSON like { "prompt": "Hello" }'}, status_code=400)
        if len(prompt) > MAX_PROMPT_CHARS:
            return JSONResponse({"error": f"Prompt is longer than {MAX_PROMPT_CHARS} characters"}, status_code=400)

        try:
            return JSONResponse({"text": await ask_gemini(prompt)})
        except MissingGeminiKeyError as err:
            return JSONResponse({"error": str(err)}, status_code=500)
        except Exception:
            logger.exception("Gemini request failed")
            return JSONResponse({"error": "Gemini request failed. Check the server logs."}, status_code=502)
