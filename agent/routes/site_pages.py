"""The Crophecy site: frontend/ (the teammates' design, copied verbatim from main) on clean routes, wired to the live market.

GET / (for browsers, agent/server.py), /home, /live, /markets, /market/{qid}, /leaderboard, /theory, /farmer serve frontend/index.html,
a single-page app with hash routes. The older landing/ page (agent/routes/landing.py) moved to /landing-alt.
frontend/ stays byte-identical to main so a later copy never conflicts; the wiring lives in frontend/js/api.js, and this module
injects it plus a few named hooks into the page as it is served:
  - boot: the page's first render waits for crophecyBoot (api.js), which swaps the demo data for the live market first;
  - sim:  the in-browser bet simulator only starts when api.js is absent (live trades drive the page instead);
  - let:  `money`, `kfmt`, `MAXV` and `NA` become `let`, so api.js can show play credits, rescale the globe to real volumes and
          re-index the agents (NA, EXT_I, HUM_I) to the live roster.
A hook whose anchor is gone (the page changed on main) is skipped and logged; api.js then labels the page as a preview.
Once main carries a hook itself (it mentions crophecyBoot / CROPHECY_LIVE), its patch is skipped.

GET /harvest/api/site[?since=<seq>] is the page's compact feed: the state summary without the 180 kB leaderboard, plus what the
design needs and /harvest/api/state lacks (each house world model's live positions, trade times for the price charts, geo points,
the latest Solana anchors). With `since` it returns only what changed, for a cheap poll.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from agent.harvest import ledger
from agent.routes import harvest as api

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
PAGE = FRONTEND_DIR / "index.html"
ROUTES = ("/home", "/live", "/markets", "/market/{qid:path}", "/leaderboard", "/theory", "/farmer")
API_JS = '<script src="/harvest/static/croppy.js" defer></script><script src="/frontend/js/api.js?v={v}"></script>'
HOOKS: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    ("boot", "crophecyBoot", re.compile(r"^go\(\(location\.hash\|\|'#(\w+)'\)\.slice\(1\),\{keep:true\}\);$", re.M),
     r"(window.crophecyBoot||(f=>f()))(()=>go((location.hash||'#\1').slice(1),{keep:true}));"),
    ("sim", "CROPHECY_LIVE", re.compile(r"^setInterval\(tick,1000\);$", re.M), "window.CROPHECY_LIVE||setInterval(tick,1000);"),
    ("let", "let money=", re.compile(r"\bconst (money|kfmt|MAXV|NA)="), r"let \1="),
)
HISTORY_POINTS = 120
POLL_TRADES = 200
logger = logging.getLogger("agent")
_page_cache: dict[str, Any] = {}


def wired_page(source: str, version: str) -> tuple[str, list[str]]:
    """frontend/index.html with api.js and the hooks applied; returns (html, names of the hooks in place)."""
    applied = []
    for name, marker, pattern, repl in HOOKS:
        if marker in source:
            applied.append(name)
            continue
        source, n = pattern.subn(repl, source)
        if n:
            applied.append(name)
        else:
            logger.warning("site: hook %r not found in frontend/index.html; the page will be labelled a preview", name)
    source = source.replace('href="waitlist/index.html"', 'href="/waitlist"')  # the bundled copy; /waitlist is the live one (Supabase)
    tag = f"<script>window.CROPHECY_HOOKS={json.dumps(applied)};</script>{API_JS.format(v=version)}"
    head = source.find("<script>")
    html = source[:head] + tag + "\n" + source[head:] if head >= 0 else source.replace("</body>", tag + "</body>")
    return html, applied


def _page_html() -> str:
    try:
        stat = PAGE.stat()
        js = (FRONTEND_DIR / "js" / "api.js").stat().st_mtime_ns
    except OSError:
        return "<!doctype html><title>The Crophecy</title><p>The Crophecy is being built.</p>"
    key = (stat.st_mtime_ns, js)
    if _page_cache.get("key") != key:
        _page_cache.update(key=key, html=wired_page(PAGE.read_text(), str(js))[0])
    return _page_cache["html"]


def page_response() -> HTMLResponse:
    return HTMLResponse(_page_html(), headers={"Cache-Control": "no-cache"})


def _series() -> dict[str, dict[str, Any]]:
    """Name, unit and source of each price series, for the resolution rules."""
    if "series" not in _page_cache:
        fixture = api.price_fixture() or {}
        _page_cache["series"] = {sid: {"name": s.get("name"), "unit": s.get("unit"), "source": fixture.get("source"), "source_url": s.get("source_url")}
                                 for sid, s in fixture.get("series", {}).items()}
    return _page_cache["series"]


def _changes(history: list[Any], at: dict[int, str]) -> dict[str, float]:
    """YES-price change over the last 1, 7 and 30 days, from the full history (since the open for a younger market)."""
    now = time.time()
    out = {}
    for label, days in (("1d", 1), ("7d", 7), ("30d", 30)):
        cut = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - days * 86400))
        before = history[0][1]
        for seq, p in history:
            stamp = at.get(int(seq))
            if stamp is not None and stamp[:19] > cut:
                break
            before = p
        out[label] = round(float(history[-1][1]) - float(before), 4)
    return out


def _market(book: Any, qid: str, at: dict[int, str], house: dict[str, dict[str, Any]], *, full: bool) -> dict[str, Any]:
    view = book.market_view(qid)
    meta = book.meta.get(qid) or {}
    if not full:
        return {k: view[k] for k in ("qid", "p_yes", "volume", "fees", "status", "outcome")}
    hist = [[int(s), round(float(p), 4), at.get(int(s))] for s, p in book.markets[qid]["history"][-HISTORY_POINTS:]]
    first: dict[str, int] = {}
    for t in book.trades:
        if t["qid"] == qid and t["trader"].startswith("agent:"):
            first.setdefault(_persona(t["trader"]), t["seq"])
    for pid, row in house.items():
        if pid in first:
            row.setdefault("first", {})[qid] = first[pid]
    view.update(history=hist, change=_changes(book.markets[qid]["history"], at), why=meta.get("why"), rule=meta.get("rule"), spec_id=meta.get("spec_id") or qid.split("@")[0],
                deadline=meta.get("deadline"), series_info=_series().get(str(view.get("series"))))
    return view


def _persona(trader: str) -> str:
    """agent:climate-pessimist--phi-4 -> climate-pessimist: the page shows one agent per persona, across its model runs."""
    return trader.removeprefix("agent:").split("--")[0]


def site_view(book: Any, since: int | None) -> dict[str, Any]:
    """The page feed (see the module docstring); house traders are grouped by persona. Call with book.lock held."""
    trades = book.trades
    full = since is None
    recent = trades[-60:] if full else [t for t in trades if t["seq"] > since][-POLL_TRADES:]
    traders = {t["trader"] for t in trades if t["trader"].startswith("agent:")}
    house: dict[str, dict[str, Any]] = {pid: {} for pid in sorted({str(p["id"]).split("--")[0] for p in book.personas} | {_persona(t) for t in traders})}
    at = {int(t["seq"]): t["at"] for t in trades} if full else {}
    markets = [_market(book, q, at, house, full=full) for q in book.markets]
    fund = api.fund_view(book)
    names = {tid: (info.get("nickname") or info.get("name")) for tid, info in book.traders.items() if info.get("kind") in ("human", "external")}
    body: dict[str, Any] = {
        "seq": trades[-1]["seq"] if trades else 0, "markets": markets, "recent_trades": recent,
        "names": {t["trader"]: names.get(t["trader"]) for t in recent if names.get(t["trader"])},
        "fund": fund if full else {"treasury": fund["treasury"], "allocations": fund["allocations"]},
        "users": book.users(), "ledger_head": {"seq": book.ledger_head[0], "hash": book.ledger_head[1]},
        "agents": sum(1 for t in book.traders.values() if t["kind"] == "external"),
    }
    bets: dict[str, int] = {}
    for t in trades:
        if t["trader"] in traders:
            bets[_persona(t["trader"])] = bets.get(_persona(t["trader"]), 0) + 1
    for pid, row in house.items():
        row.update(pnl=0.0, bets=bets.get(pid, 0))
    for tid in sorted(traders):
        row = house[_persona(tid)]
        row["pnl"] += book.equity(tid) - api.STARTING_CREDITS
        if full:
            held = row.setdefault("positions", {})
            for p in book.positions(tid):
                acc = held.setdefault(p["qid"], {k: 0.0 for k in ("yes", "no", "spent", "value", "pnl")})
                for k in acc:
                    acc[k] += float(p[k])
    body["house"] = house
    if full:
        body["personas"] = api._personas_view(book)
    return body


def _by_persona(world_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Holdout Brier per house persona, averaged over its model runs (for a replay whose leaderboard has no by_persona)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for w in world_models:
        if w.get("origin") == "house" and w.get("brier_holdout") is not None:
            groups.setdefault(str(w.get("persona") or w["persona_id"]).split("--")[0], []).append(w)
    rows = [{"key": key, "label": str(ws[0].get("persona_name") or ws[0].get("name")).split(" · ")[0], "origin": "house", "n_agents": len(ws),
             "brier_holdout": sum(float(w["brier_holdout"]) for w in ws) / len(ws)} for key, ws in groups.items()]
    return sorted(rows, key=lambda r: r["brier_holdout"])


def leaderboard_digest(book: Any) -> dict[str, Any]:
    lb = api.leaderboard_view(book)
    humans = sorted(lb.get("humans", []), key=lambda h: -(h.get("pnl") or 0))[:20]
    wm = lb.get("world_models", [])
    return {"by_persona": lb.get("by_persona") or _by_persona(wm), "baselines": lb.get("baselines", {}), "humans": humans, "insight": lb.get("insight"),
            "house_models": sum(1 for w in wm if w.get("origin") == "house"), "n_holdout": max((int(w.get("n_ci") or 0) for w in wm), default=0),
            "top": [{k: w.get(k) for k in ("persona_id", "name", "origin", "brier_holdout", "rank", "color", "owner")} for w in wm[:12]]}


def _anchors(limit: int = 8) -> list[dict[str, Any]]:
    try:
        rows = ledger.anchors()
    except Exception:  # noqa: BLE001 - the page shows the market without anchors rather than failing
        logger.exception("site: reading anchors failed")
        return []
    return [{k: a.get(k) for k in ("kind", "count", "memo", "status", "signature", "explorer_url", "at")} for a in rows[-limit:]][::-1]


def register(app: FastAPI) -> None:
    app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR, check_dir=False), name="frontend")
    for path in ROUTES:
        app.add_api_route(path, page_response, methods=["GET"], response_class=HTMLResponse, include_in_schema=False, name=f"site{path}")

    @app.get("/harvest/api/site")
    async def site_feed(since: int | None = None) -> JSONResponse:
        api._ensure_house_agents()
        book = await asyncio.to_thread(api._book)

        def build() -> dict[str, Any]:
            with book.lock:
                body = site_view(book, since)
                if since is None:
                    body["leaderboard"] = leaderboard_digest(book)
            if since is None:
                body["anchors"] = _anchors()
                body["geo"] = json.loads(api.GEO.read_text()) if api.GEO.exists() else {}
            return body

        return JSONResponse(await asyncio.to_thread(build), headers=api.NO_STORE)

    @app.get("/harvest/api/site/anchors")
    async def site_anchors() -> JSONResponse:
        return JSONResponse({"anchors": await asyncio.to_thread(_anchors, 20)}, headers=api.NO_STORE)
