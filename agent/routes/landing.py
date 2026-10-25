"""The Crophecy landing page at GET / (landing/index.html) and its compact live feed.

GET / for browsers is landing/index.html: the waitlist hero, a globe that plays the market's real bets, then the site's story
with live numbers (agent/server.py calls landing_response; code that asks / for JSON still gets the module list, and the old
module-list page moved to /classic). Nav links that point at site routes another module may not have registered yet
(href="/live" data-fallback="/app") are rewritten to their fallback when the route is missing, so no link 404s.

GET /harvest/api/landing is the page's feed: a small summary of /harvest/api/state (which is ~600 kB with the full
leaderboard). With ?since=<seq> it returns only the bets after that sequence number plus the current prices, for a cheap poll.
"""

import asyncio
import calendar
import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from agent.routes import harvest as api

LANDING_DIR = Path(__file__).resolve().parents[2] / "landing"
PAGE = LANDING_DIR / "index.html"
FALLBACK_LINK = re.compile(r'href="(/[\w/-]*)" data-fallback="([^"]*)"')
FULL_TTL_S = 15.0
POLL_TRADES = 150
SPLIT_TRADES = 3000  # recent bets counted for each market's YES/NO split
_cache: dict[str, Any] = {}


def landing_html(paths: set[str]) -> str:
    """landing/index.html with each fallback link resolved against the routes the app actually serves."""
    try:
        source = PAGE.read_text(encoding="utf-8")
    except OSError:
        return "<!doctype html><title>Crophecy</title><p>Crophecy is being built. <a href=\"/waitlist\">Join the waitlist</a>.</p>"
    return FALLBACK_LINK.sub(lambda m: f'href="{m.group(1) if m.group(1) in paths else m.group(2)}"', source)


def landing_response(request: Request) -> HTMLResponse:
    app = request.scope.get("app")
    paths = {getattr(r, "path", "") for r in app.routes} if app is not None else set()
    return HTMLResponse(landing_html(paths), headers={"Cache-Control": "no-cache"})


def _names(book: Any) -> dict[str, str]:
    names = {f"agent:{p['id']}": p.get("name") or p["id"] for p in book.personas}
    for tid, info in book.traders.items():
        if name := info.get("nickname") or info.get("name"):
            names[tid] = str(name)
    return names


def _trade(t: dict[str, Any]) -> dict[str, Any]:
    out = {k: t.get(k) for k in ("seq", "trader", "qid", "side", "spend", "price_after", "at")}
    out["why"] = str(t.get("rationale") or "")[:160]
    return out


def _rate(trades: list[dict[str, Any]], window_s: float = 300.0) -> float:
    """Bets per minute over the last `window_s` seconds (by the trades' own timestamps)."""
    now = time.time()
    n = 0
    for t in reversed(trades):
        at = _epoch(t.get("at"))
        if at is None or now - at > window_s:
            break
        n += 1
    return round(n * 60.0 / window_s, 1)


def _epoch(iso: Any) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return None


def _leaderboard(book: Any) -> dict[str, Any]:
    lb = api.leaderboard_view(book)
    wm = [w for w in lb.get("world_models", []) if w.get("brier_holdout") is not None]
    keep = ("persona_id", "name", "color", "origin", "model_label", "persona_name", "brier_holdout", "brier_holdout_lo", "brier_holdout_hi",
            "beats_market_ci", "rank")
    house = [{k: w.get(k) for k in keep} for w in wm if w.get("origin") == "house"]
    return {"baselines": lb.get("baselines", {}), "insight": lb.get("insight"), "n_holdout": max((int(w.get("n_ci") or 0) for w in wm), default=0),
            "n_models": len(wm), "house": house[:10], "best": {k: wm[0].get(k) for k in keep} if wm else None}


def _market(view: dict[str, Any], split: list[int]) -> dict[str, Any]:
    out = {k: view.get(k) for k in ("qid", "title", "kind", "region", "crop", "adverse_if", "p_yes", "volume", "status")}
    out["split"] = split
    return out


def full_view(book: Any) -> dict[str, Any]:
    """The first load. Call with book.lock held."""
    trades = book.trades
    fund = api.fund_view(book)
    split: dict[str, list[int]] = {}
    for t in trades[-SPLIT_TRADES:]:
        split.setdefault(t["qid"], [0, 0])[0 if t["side"] == "YES" else 1] += 1
    return {
        "seq": trades[-1]["seq"] if trades else 0, "now": int(time.time() * 1000), "bets": len(trades), "per_min": _rate(trades),
        "markets": [_market(book.market_view(q), split.get(q, [0, 0])) for q in book.markets],
        "recent": [_trade(t) for t in trades[-40:]], "names": _names(book),
        "personas": [{k: p.get(k) for k in ("id", "name", "color")} for p in book.personas],
        "agents": sum(1 for t in book.traders.values() if t["kind"] == "external"), "users": book.users(),
        "fund": {"treasury": fund.get("treasury"), "fee_rate": fund.get("fee_rate"), "allocations": fund.get("allocations", [])[:6]},
        "ledger_head": {"seq": book.ledger_head[0], "hash": book.ledger_head[1]}, "leaderboard": _leaderboard(book),
    }


def poll_view(book: Any, since: int) -> dict[str, Any]:
    """Bets after `since` and the live prices. Call with book.lock held."""
    trades = book.trades
    new = [t for t in trades[-POLL_TRADES * 4:] if t["seq"] > since][-POLL_TRADES:]
    names = _names(book)
    return {"seq": trades[-1]["seq"] if trades else 0, "now": int(time.time() * 1000), "bets": len(trades), "per_min": _rate(trades),
            "trades": [_trade(t) for t in new], "names": {t["trader"]: names[t["trader"]] for t in new if t["trader"] in names},
            "prices": {q: book.market_view(q)["p_yes"] for q in book.markets},
            "agents": sum(1 for t in book.traders.values() if t["kind"] == "external"),
            "ledger_head": {"seq": book.ledger_head[0], "hash": book.ledger_head[1]}}


def _geo() -> dict[str, Any]:
    try:
        return json.loads(api.GEO.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"regions": []}


def register(app: FastAPI) -> None:
    @app.get("/classic", include_in_schema=False)
    async def classic(request: Request) -> HTMLResponse:
        from agent.landing import render_landing
        from agent.modules import modules

        return HTMLResponse(render_landing(modules, str(request.base_url).rstrip("/")))

    @app.get("/harvest/api/landing")
    async def landing_feed(since: int | None = None) -> JSONResponse:
        api._ensure_house_agents()
        book = await asyncio.to_thread(api._book)

        def build() -> dict[str, Any]:
            if since is not None:
                with book.lock:
                    return poll_view(book, since)
            hit = _cache.get("full")
            if hit and time.monotonic() - hit[0] < FULL_TTL_S:
                return hit[1]
            with book.lock:
                body = full_view(book)
            body["geo"] = _geo().get("regions", [])
            _cache["full"] = (time.monotonic(), body)
            return body

        return JSONResponse(await asyncio.to_thread(build), headers=api.NO_STORE)
