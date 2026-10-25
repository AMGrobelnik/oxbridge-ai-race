"""POST /harvest/api/croppy {message, history?, page?} -> {reply, actions?}: Croppy, the guide to The Crophecy.

Gemini answers with the live context of the in-process book; a friendly canned reply stands in when Gemini
is unavailable or HARVEST_OFFLINE=1. Rate-limited per IP (CROPPY_PER_MIN, default 10) and public in PUBLIC_MODE.
"""

import asyncio
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors

from agent.ai import gemini
from agent.challenge.ratelimit import RateLimiter
from agent.harvest import croppy, forecaster, market
from agent.routes import harvest as api

GEMINI_TIMEOUT_S = 12.0
TOTAL_TIMEOUT_S = 28.0
NO_STORE = {"Cache-Control": "no-store"}

croppy_limiter = RateLimiter(int(os.environ.get("CROPPY_PER_MIN") or 10))
ask = gemini.ask_gemini  # module attribute so tests can stub the model


def live_context() -> str:
    book = market.live_book()
    with book.lock:
        markets = [book.market_view(q) for q in book.markets]
        leaderboard = api.leaderboard_view(book)
        users = book.users()
    return croppy.snapshot(markets, leaderboard, users=users, news=croppy.news_headlines())


async def answer(message: str, history: list[dict[str, str]], page: str) -> dict[str, object]:
    if forecaster.offline_env():
        return croppy.fallback(message)
    try:
        context = await asyncio.to_thread(live_context)
        prompt = croppy.build_prompt(message, history=history, page=page, context=context)
        text = await asyncio.wait_for(ask(prompt, json_output=True, timeout_s=GEMINI_TIMEOUT_S), TOTAL_TIMEOUT_S)
    except (gemini.MissingGeminiKeyError, httpx.HTTPError, genai_errors.APIError, TimeoutError, ValueError):
        return croppy.fallback(message)
    return croppy.parse_reply(text) or croppy.fallback(message)


def register(app: FastAPI) -> None:
    @app.post("/harvest/api/croppy")
    async def croppy_route(request: Request) -> JSONResponse:
        if (limited := api._limited(croppy_limiter, request, "questions for Croppy")) is not None:
            return limited
        data, bad = await api._body(request)
        if bad is not None or data is None:
            return bad or api._error("Bad request.", 400)
        message = croppy.clean_message(data.get("message"))
        if not message:
            return api._error("message is required", 422)
        body = await answer(message, croppy.clean_history(data.get("history")), croppy.clean_page(data.get("page")))
        return JSONResponse(body, headers=NO_STORE)
