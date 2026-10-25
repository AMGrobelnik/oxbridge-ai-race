"""POST /harvest/api/croppy {message, history?, page?} -> {reply, actions?, model?}: Croppy, the guide to The Crophecy.

Live chat, not the scored backtest, so the chat models are not in the backtest registry (training cutoffs do not matter
here). The newest DeepSeek on OpenRouter answers first, Gemini 2.5 Flash next, all within DEADLINE_S, with the live context
of the in-process book; a friendly canned reply stands in when neither answers or HARVEST_OFFLINE=1. CROPPY_MODEL overrides
the OpenRouter model id. Rate-limited per IP (CROPPY_PER_MIN) and public in PUBLIC_MODE.
"""

import asyncio
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.genai import errors as genai_errors

from agent.ai import gemini
from agent.challenge.ratelimit import RateLimiter
from agent.harvest import croppy, forecaster, market, models
from agent.routes import harvest as api
from agent.routes.harvest_swarm import swarm_view

DEADLINE_S = 11.0  # the whole answer, every model in CHAIN included
PRIMARY_TIMEOUT_S = 7.0  # the first model's share, so the fallback still has time
CONTEXT_TTL_S = 15.0
NO_STORE = {"Cache-Control": "no-store"}
CHAT_EXTRA = {"temperature": 0.4, "max_tokens": 600}
CHAIN: list[dict[str, Any]] = [
    {"key": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "provider": "gemini", "env": "GEMINI_API_KEY", "model_id": "gemini-2.5-flash"},
    {"key": "deepseek-v4.1-flash", "label": "DeepSeek V4.1 Flash", "provider": "openrouter", "env": "OPENROUTER_API_KEY",
     "model_id": os.environ.get("CROPPY_MODEL") or "deepseek/deepseek-v4.1-flash-1017",
     "extra": {**CHAT_EXTRA, "reasoning": {"enabled": False}}},
]

MODEL_ERRORS = (httpx.HTTPError, genai_errors.APIError, gemini.MissingGeminiKeyError, TimeoutError, ValueError, KeyError, IndexError)
croppy_limiter = RateLimiter(int(os.environ.get("CROPPY_PER_MIN") or 300))
ask = models.ask_once  # module attribute so tests can stub the model
_context: dict[str, Any] = {}


def live_context() -> str:
    """The live snapshot, rebuilt at most every CONTEXT_TTL_S (the full leaderboard is the expensive part)."""
    if _context.get("text") and time.monotonic() - _context["at"] < CONTEXT_TTL_S:
        return str(_context["text"])
    book = market.live_book()
    with book.lock:
        markets = [book.market_view(q) for q in book.markets]
        leaderboard = api.leaderboard_view(book)
        users, trades = book.users(), len(book.trades)
    text = croppy.snapshot(markets, leaderboard, users=users, news=croppy.news_headlines(), swarm=swarm_view(book), trades=trades)
    _context.update(text=text, at=time.monotonic())
    return text


async def answer(message: str, history: list[dict[str, str]], page: str, links: dict[str, str] | None = None) -> dict[str, object]:
    if forecaster.offline_env():
        return croppy.fallback(message)
    end = time.monotonic() + DEADLINE_S
    try:
        context = await asyncio.wait_for(asyncio.to_thread(live_context), DEADLINE_S / 2)
    except (TimeoutError, KeyError, ValueError):
        return croppy.fallback(message)
    prompt = croppy.build_prompt(message, history=history, page=page, context=context, links=links)
    for i, m in enumerate(CHAIN):
        left = end - time.monotonic()
        if left < 1.0:
            break
        try:
            text = await ask(m, prompt, timeout_s=min(left, PRIMARY_TIMEOUT_S) if i == 0 else left)
        except MODEL_ERRORS:  # any provider failure moves on to the next model
            continue
        if (parsed := croppy.parse_reply(text, links)) is not None:
            return parsed | {"model": m["label"]}
    return croppy.fallback(message)


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
        links = croppy.links_for(getattr(r, "path", "") for r in request.app.routes)
        body = await answer(message, croppy.clean_history(data.get("history")), croppy.clean_page(data.get("page")), links)
        return JSONResponse(body, headers=NO_STORE)
