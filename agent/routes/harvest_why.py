"""GET /harvest/api/markets/{qid}/why?n=12: Croppy explains the market's latest price move (agent/harvest/why.py).

Cites the last n ledger bets (who bet what, how far each moved the price) and the news headlines tagged to the
market's question in that window. The explanation comes from the fast LLM within WHY_DEADLINE_S (default 8 s), else
a deterministic one; LLM answers are cached per (qid, last trade seq, n).
"""

import asyncio
import os
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.challenge.ratelimit import RateLimiter
from agent.harvest import fast_llm, market, news, why
from agent.routes import harvest as api

NO_STORE = {"Cache-Control": "no-store"}
CACHE_MAX = 256

why_limiter = RateLimiter(int(os.environ.get("WHY_PER_MIN") or 30))
ask = fast_llm.ask  # module attribute so tests can stub the model
_cache: "OrderedDict[tuple[str, int, int], str]" = OrderedDict()


def deadline_s() -> float:
    try:
        return float(os.environ.get("WHY_DEADLINE_S") or 8)
    except ValueError:
        return 8.0


def gather(book: market.LiveBook, qid: str, n: int) -> dict[str, Any] | None:
    with book.lock:
        if qid not in book.markets:
            return None
        meta = book.meta.get(qid) or {}
        trades = [t for t in book.trades if t["qid"] == qid]
        personas = {p["id"]: p["name"] for p in book.personas}
        mv = why.move(trades, book.traders, personas, n=n, price_now=book.price(qid))
        title = meta.get("title", qid)
    spec_id = str(meta.get("spec_id") or qid.split("@")[0])
    try:
        items, _ = news.query(spec=spec_id, limit=30)
    except Exception:  # noqa: BLE001 - news is optional context
        items = []
    return {"qid": qid, "title": title, "spec_id": spec_id, "move": mv, "news": why.news_in_window(items, mv["start"])}


async def explain(ctx: dict[str, Any], n: int) -> tuple[str, str]:
    mv = ctx["move"]
    last_seq = mv["bets"][-1]["seq"] if mv["bets"] else 0
    k = (ctx["qid"], last_seq, n)
    if k in _cache:
        return _cache[k], "cache"
    if mv["n_bets"] and fast_llm.available():
        try:
            text = await asyncio.wait_for(asyncio.to_thread(ask, why.build_prompt(ctx["title"], mv, ctx["news"])), deadline_s())
            if parsed := why.parse(text):
                _cache[k] = parsed
                while len(_cache) > CACHE_MAX:
                    _cache.popitem(last=False)
                return parsed, "llm"
        except Exception:  # noqa: BLE001 - any LLM failure falls back to the deterministic explanation
            pass
    return why.explain_fallback(ctx["title"], mv, ctx["news"]), "fallback"


def register(app: FastAPI) -> None:
    @app.get("/harvest/api/markets/{qid}/why")
    async def why_route(qid: str, request: Request, n: int = 12) -> JSONResponse:
        if (limited := api._limited(why_limiter, request, "explanations")) is not None:
            return limited
        n = max(3, min(int(n), 50))
        book = await asyncio.to_thread(market.live_book)
        ctx = await asyncio.to_thread(gather, book, qid, n)
        if ctx is None:
            return api._error(f"unknown market {qid!r}", 404)
        text, source = await explain(ctx, n)
        return JSONResponse({**ctx, "explanation": text, "source": source, "by": "Croppy"}, headers=NO_STORE)
