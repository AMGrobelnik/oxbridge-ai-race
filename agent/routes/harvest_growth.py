"""Growth and openness (SPEC.md section 16): share cards, the humans-vs-AI season, the bot SDK page and the open-data export.

GET /harvest/og/market/{qid}.png, /harvest/og/agent/{id}.png    1200x630 share cards (Open Graph / Twitter), needs Pillow
GET /harvest/api/card/market/{qid}, /harvest/api/card/agent/{id}  the card as JSON (headline, stats, urls)
GET /harvest/api/season?type=all|human|agent&limit=             humans vs AI agents on the live markets
GET /harvest/api/developers                                     API reference generated from the running app's routes
GET /harvest/api/export[/forecasts.csv|forecasts.json|resolutions.csv]   open data, CC-BY-4.0
Pages: /app/market/{qid} and /app/agent/{id} (with Open Graph tags), /app/season, /app/developers.
"""

import asyncio
import os
import time
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agent.challenge.ratelimit import RateLimiter
from agent.harvest import accounts, growth
from agent.routes import harvest as api
from agent.routes.challenge import public_base_url
from agent.routes.harvest_pages import STATIC_DIR, _page
from agent.routes.public_mode import request_limiter

PAGES = {"/app/season": "app/season.html", "/app/developers": "app/developers.html"}
NO_STORE = {"Cache-Control": "no-store"}
CARD_CACHE = {"Cache-Control": "public, max-age=300"}
EXPORT_TTL_S = 30
card_limiter = RateLimiter(int(os.environ.get("HARVEST_CARDS_PER_MIN") or 120))
_cache: dict[str, tuple[Any, Any]] = {}

REFERENCE = [  # (method, path, extra doc) in reading order; AGENT_API_DOC supplies the rest
    ("POST", "/harvest/api/agents/register", {}),
    ("GET", "/harvest/api/agents/markets", {}),
    ("POST", "/harvest/api/agents/bet", {}),
    ("GET", "/harvest/api/markets", {"returns": "every market: qid, title, kind, region, crop, p_yes, volume, status, outcome, history, agent_forecasts"}),
    ("GET", "/harvest/api/news", {}),
    ("POST", "/harvest/api/byo", {}),
    ("GET", "/harvest/api/agents/{agent_id}", {}),
    ("GET", "/harvest/api/leaderboard", {}),
    ("GET", "/harvest/api/season", {"returns": "headline, fao_bot, humans_ahead_of_fao, rows: humans and agents ranked on the live markets (Brier where resolved, else P&L)",
                                    "query": "type=all|human|agent, limit=1..1000"}),
    ("GET", "/harvest/api/export/forecasts.csv", {"returns": "every forecast and bet (backtest, live, ledger); also forecasts.json", "license": growth.LICENSE["name"]}),
    ("GET", "/harvest/api/export/resolutions.csv", {"returns": "every resolved question with its outcome", "license": growth.LICENSE["name"]}),
]


def _base(request: Request) -> str:
    return public_base_url(request)


async def _accounts(book: Any) -> list[dict[str, Any]]:
    """The leaderboard's accounts, cached for a few seconds (cards are fetched in bursts by link-preview crawlers)."""
    hit = _cache.get("accounts")
    if hit is not None and time.monotonic() - hit[0] < 10:
        return hit[1]
    def build() -> list[dict[str, Any]]:
        with book.lock:
            return list(api.leaderboard_view(book)["accounts"])
    rows = await asyncio.to_thread(build)
    _cache["accounts"] = (time.monotonic(), rows)
    return rows


async def market_card(request: Request, qid: str) -> dict[str, Any] | None:
    book = await asyncio.to_thread(api._book)
    with book.lock:
        if qid not in book.markets:
            return None
        return growth.market_card(book.market_view(qid), base_url=_base(request))


async def agent_card(request: Request, key: str) -> dict[str, Any] | None:
    book = await asyncio.to_thread(api._book)
    rows = await _accounts(book)
    account = growth.find_account(rows, key)
    if account is None:
        return None
    trader = growth.trader_id(account)
    with book.lock:
        take = growth.hottest_take(book, trader) if trader else None
    return growth.agent_card(account, take, rank_of=len(rows), base_url=_base(request))


def _unknown(what: str, key: str) -> JSONResponse:
    return api._error(f"unknown {what} {key!r}", 404)


def _png(card: dict[str, Any]) -> Response:
    from agent.harvest import ogcard

    return Response(ogcard.render(card), media_type="image/png", headers=CARD_CACHE)


async def _export_rows(kind: str) -> tuple[list[dict[str, Any]], str]:
    book = await asyncio.to_thread(api._book)
    result = await asyncio.to_thread(api.replay_result)
    key = (kind, id(result), book.version)
    hit = _cache.get(kind)
    if hit is not None and hit[0] == key:
        return hit[1]
    fn = growth.forecast_rows if kind == "forecasts" else growth.resolution_rows

    def build() -> list[dict[str, Any]]:
        with book.lock:
            return fn(book, result)

    rows = await asyncio.to_thread(build)
    cols = growth.FORECAST_COLUMNS if kind == "forecasts" else growth.RESOLUTION_COLUMNS
    _cache[kind] = (key, (rows, growth.to_csv(rows, cols)))
    return rows, _cache[kind][1][1]


def _download(body: str, name: str, media: str) -> Response:
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"', "X-License": growth.LICENSE["name"],
                                                     "Link": f'<{growth.LICENSE["url"]}>; rel="license"', "Cache-Control": f"public, max-age={EXPORT_TTL_S}"})


def reference(app: FastAPI, base: str) -> dict[str, Any]:
    """The API reference: AGENT_API_DOC and the extras above, each checked against the app's real routes."""
    routes = {(m, r.path) for r in app.routes if hasattr(r, "methods") for m in (r.methods or ())}  # type: ignore[attr-defined]
    doc = {(e["method"], e["path"].split("?")[0]): e for e in api.AGENT_API_DOC["endpoints"]}
    endpoints = []
    for method, path, extra in REFERENCE:
        entry = {**doc.get((method, path), {}), **extra, "method": method, "path": path, "available": (method, path) in routes}
        curl = f"curl -s {base}{path}" if method == "GET" else f"curl -s -X POST {base}{path} -H 'Content-Type: application/json'"
        if entry.get("body"):
            import json

            curl += f" -d '{json.dumps(entry['body'])}'"
        endpoints.append({**entry, "curl": curl})
    shown = {(e["method"], e["path"]) for e in endpoints}
    others = sorted({(m, p) for m, p in routes if p.startswith("/harvest/api/") and m in ("GET", "POST") and (m, p) not in shown})
    return {
        "name": "The Crophecy bot API", "base_url": base, "credits": api.STARTING_CREDITS, "credits_have_no_value": True,
        "auth": "No API key. POST /harvest/api/agents/register (a bot) or /harvest/api/join (a human) returns a token, shown once. Send it as `token` in the JSON "
                "body of POST /harvest/api/agents/bet. The server keeps only its hash. Every GET is public.",
        "rate_limits": {"register": "30/min per IP", "bet": "120/min per IP", "byo": "6/min per IP", "all public requests": f"{request_limiter.limit}/min per IP",
                        "on excess": "HTTP 429 with a Retry-After header"},
        "limits": {"spend": f"{api.SPEND_MIN:g}..{api.SPEND_MAX:g} credits per bet", "p_yes": "0.01..0.99", "world_model": f"at most {api.WORLD_MODEL_MAX} characters"},
        "errors": "Errors are JSON {\"error\": \"...\"} with a 4xx status.",
        "endpoints": endpoints, "other_endpoints": [{"method": m, "path": p} for m, p in others],
        "example_bot": {"url": f"{base}/harvest/static/sdk/example_bot.py", "run": f"CROPHECY_URL={base} python example_bot.py"},
        "open_data": {**growth.LICENSE, "files": [f"{base}/harvest/api/export/forecasts.csv", f"{base}/harvest/api/export/forecasts.json",
                                                   f"{base}/harvest/api/export/resolutions.csv"]},
    }


def register(app: FastAPI) -> None:
    for path, name in PAGES.items():
        _page(app, path, name)

    async def page_with_card(request: Request, name: str, card: dict[str, Any] | None) -> HTMLResponse:
        body = (STATIC_DIR / name).read_text()
        return HTMLResponse(growth.with_meta(body, card) if card else body, headers={"Cache-Control": "no-cache"})

    @app.get("/app/market/{qid:path}", response_class=HTMLResponse)
    async def market_page(request: Request, qid: str) -> HTMLResponse:
        return await page_with_card(request, "app/market.html", await market_card(request, qid))

    @app.get("/app/agent/{agent_id:path}", response_class=HTMLResponse)
    async def agent_page(request: Request, agent_id: str) -> HTMLResponse:
        return await page_with_card(request, "app/agent.html", await agent_card(request, agent_id))

    @app.get("/harvest/api/card/market/{qid:path}")
    async def card_market(request: Request, qid: str) -> JSONResponse:
        card = await market_card(request, qid)
        return JSONResponse(card, headers=NO_STORE) if card else _unknown("market", qid)

    @app.get("/harvest/api/card/agent/{agent_id:path}")
    async def card_agent(request: Request, agent_id: str) -> JSONResponse:
        card = await agent_card(request, agent_id)
        return JSONResponse(card, headers=NO_STORE) if card else _unknown("agent", agent_id)

    @app.get("/harvest/og/market/{qid:path}.png", response_model=None)
    async def og_market(request: Request, qid: str) -> Response:
        if (limited := api._limited(card_limiter, request, "card requests")) is not None:
            return limited
        card = await market_card(request, qid)
        return await _render(card, "market", qid)

    @app.get("/harvest/og/agent/{agent_id:path}.png", response_model=None)
    async def og_agent(request: Request, agent_id: str) -> Response:
        if (limited := api._limited(card_limiter, request, "card requests")) is not None:
            return limited
        card = await agent_card(request, agent_id)
        return await _render(card, "agent", agent_id)

    async def _render(card: dict[str, Any] | None, what: str, key: str) -> Response:
        if card is None:
            return _unknown(what, key)
        try:
            return await asyncio.to_thread(_png, card)
        except ImportError:
            return api._error("Share-card images need the Pillow package (requirements.txt).", 501)

    @app.get("/harvest/api/season")
    async def season_route(kind: str = Query("all", alias="type"), limit: int = 200) -> JSONResponse:
        if kind not in ("all", "human", "agent"):
            return api._error("type must be all, human or agent", 400)
        book = await asyncio.to_thread(api._book)
        verified = {p["id"] for p in book.personas if p.get("origin") == accounts.NARRATIVE}

        def build() -> dict[str, Any]:
            with book.lock:
                return growth.season(book, personas_verified=verified)

        body = await asyncio.to_thread(build)
        rows = [r for r in body["rows"] if kind == "all" or r["type"] == kind][: max(1, min(limit, 1000))]
        return JSONResponse({**body, "type": kind, "rows": rows, "shown": len(rows)}, headers=NO_STORE)

    @app.get("/harvest/api/developers")
    async def developers(request: Request) -> JSONResponse:
        return JSONResponse(reference(request.app, _base(request)), headers=NO_STORE)

    @app.get("/harvest/api/export")
    async def export_index(request: Request) -> JSONResponse:
        base = _base(request)
        return JSONResponse({**growth.LICENSE, "forecasts_csv": f"{base}/harvest/api/export/forecasts.csv", "forecasts_json": f"{base}/harvest/api/export/forecasts.json",
                             "resolutions_csv": f"{base}/harvest/api/export/resolutions.csv", "forecast_columns": growth.FORECAST_COLUMNS,
                             "resolution_columns": growth.RESOLUTION_COLUMNS, "excludes": "tokens, token hashes, emails and contact details are never exported"})

    @app.get("/harvest/api/export/forecasts.csv")
    async def export_forecasts_csv() -> Response:
        return _download((await _export_rows("forecasts"))[1], "crophecy-forecasts.csv", "text/csv; charset=utf-8")

    @app.get("/harvest/api/export/forecasts.json")
    async def export_forecasts_json() -> JSONResponse:
        rows = (await _export_rows("forecasts"))[0]
        return JSONResponse({"license": growth.LICENSE, "columns": growth.FORECAST_COLUMNS, "count": len(rows), "forecasts": rows},
                            headers={"X-License": growth.LICENSE["name"], "Cache-Control": f"public, max-age={EXPORT_TTL_S}"})

    @app.get("/harvest/api/export/resolutions.csv")
    async def export_resolutions_csv() -> Response:
        return _download((await _export_rows("resolutions"))[1], "crophecy-resolutions.csv", "text/csv; charset=utf-8")
