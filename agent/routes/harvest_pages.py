"""The Crophecy pages /harvest, /harvest/play, /harvest/board, /harvest/terms, /harvest/news, /harvest/qr.svg (SPEC.md section 12, addendum R3). Owner: UI.

/waitlist/ serves the static waitlist landing page in waitlist/ with SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY injected, so
its form inserts straight into the Supabase "waitlist" table (row-level security only lets the publishable key insert).

GET /harvest/api/story is a compact digest of the frozen replay for the pages (the full replay is ~2 MB of trades):
per-quarter Brier and PnL per world model, the commodity price paths, the sealed holdout with its commitment, and the
replay price path of every live market.
"""

import asyncio
import functools
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from agent.harvest import scoring
from agent.routes import harvest as api
from agent.routes.challenge import _qr_svg, public_base_url

STATIC_DIR = Path(__file__).resolve().parent.parent / "harvest" / "static"
PAGES = {"/harvest": "index.html", "/harvest/play": "play.html", "/harvest/board": "board.html", "/harvest/terms": "terms.html", "/harvest/news": "news.html"}
APP_PAGES = {"/app": "app/index.html", "/app/markets": "app/markets.html", "/app/live": "app/live.html",
             "/app/theory": "app/theory.html", "/app/leaderboard": "app/leaderboard.html"}
# /app/market/{qid} and /app/agent/{id} are served by harvest_growth.py, which adds Open Graph tags
PRICES_FROM = "2021-01"
WAITLIST_DIR = Path(__file__).resolve().parents[2] / "waitlist"
WAITLIST_ASSETS = {"hero-loop.mp4": "video/mp4", "hero.jpg": "image/jpeg"}
_WAITLIST_CONST = {"SUPABASE_URL": re.compile(r"const SUPABASE_URL = '[^']*';"), "SUPABASE_ANON_KEY": re.compile(r"const SUPABASE_ANON_KEY = '[^']*';")}
_story_cache: dict[str, Any] = {}


def _pid(trader: str) -> str:
    return trader.split(":", 1)[1] if trader.startswith("agent:") else trader


def _cutoff_digest(c: dict[str, Any], persona_ids: list[str]) -> dict[str, Any]:
    outcomes = {r["qid"]: int(r["outcome"]) for r in c.get("resolutions", []) if r.get("outcome") is not None}
    pairs: dict[str, list[tuple[float, int]]] = {pid: [] for pid in persona_ids}
    for f in c.get("forecasts", []):
        if f["qid"] in outcomes:
            pairs.setdefault(f["persona_id"], []).append((float(f["p_yes"]), outcomes[f["qid"]]))
    pnl: dict[str, float] = {pid: 0.0 for pid in persona_ids}
    volume = 0.0
    for t in c.get("trades", []):
        pnl[_pid(t["trader"])] = pnl.get(_pid(t["trader"]), 0.0) - float(t["spend"])
        volume += float(t["spend"])
    for p in c.get("payouts", []):
        pnl[_pid(p["trader"])] = pnl.get(_pid(p["trader"]), 0.0) + float(p["amount"])
    prices = c.get("final_prices", {})
    market = scoring.mean_brier([(float(prices[q]), o) for q, o in outcomes.items() if q in prices])
    return {
        "cutoff": c["cutoff"], "split": c["split"], "n_questions": len(c.get("questions", [])), "n_trades": len(c.get("trades", [])),
        "n_resolved": len(outcomes), "volume": round(volume, 2), "yes_rate": (sum(outcomes.values()) / len(outcomes)) if outcomes else None,
        "brier": {pid: scoring.mean_brier(v) for pid, v in pairs.items()}, "market_brier": market,
        "pnl": {pid: (round(v, 2) if outcomes else None) for pid, v in pnl.items()},
    }


def _holdout(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in scoring.scored_cutoffs(result, "holdout"):
        res = {r["qid"]: r for r in c.get("resolutions", [])}
        fc: dict[str, dict[str, float]] = {}
        for f in c.get("forecasts", []):
            fc.setdefault(f["qid"], {})[f["persona_id"]] = round(float(f["p_yes"]), 3)
        for q in c.get("questions", []):
            r = res.get(q["qid"]) or {}
            out.append({k: q.get(k) for k in ("qid", "spec_id", "title", "series", "region", "crop", "cutoff", "base_value", "target", "resolve_month")}
                       | {"outcome": r.get("outcome"), "resolved_value": r.get("resolved_value"),
                          "market_p": c.get("final_prices", {}).get(q["qid"]), "forecasts": fc.get(q["qid"], {})})
    return out


def _live_paths(result: dict[str, Any]) -> dict[str, list[float]]:
    lives = [c for c in result.get("cutoffs", []) if c.get("split") == "live"]
    if not lives:
        return {}
    paths: dict[str, list[float]] = {}
    for t in lives[-1].get("trades", []):
        path = paths.setdefault(t["qid"], [round(float(t["price_before"]), 4)])
        path.append(round(float(t["price_after"]), 4))
    return paths


def _prices(fixture: dict[str, Any] | None) -> dict[str, Any]:
    out = {}
    for sid, s in (fixture or {}).get("series", {}).items():
        out[sid] = {"name": s.get("name", sid), "unit": s.get("unit", ""), "source_url": s.get("source_url"),
                    "points": [[m, round(float(v), 2)] for m, v in s.get("points", []) if m >= PRICES_FROM]}
    return out


def story(result: dict[str, Any] | None, fixture: dict[str, Any] | None) -> dict[str, Any]:
    """Compact digest of the frozen replay for the pages."""
    if not result:
        return {"has_replay": False, "personas": [], "cutoffs": [], "holdout": [], "commitments": [], "prices": _prices(fixture), "live_paths": {}}
    personas = [{k: p.get(k) for k in ("id", "name", "color", "world_model", "beliefs", "heuristic")} for p in result.get("personas", [])]
    ids = [p["id"] for p in personas]
    scores = result.get("scores") or scoring.leaderboard(result)
    return {
        "has_replay": True, "model": result.get("model"), "generated_at": result.get("generated_at"), "mask": result.get("mask"),
        "personas": personas, "cutoffs": [_cutoff_digest(c, ids) for c in result.get("cutoffs", [])],
        "holdout": _holdout(result), "commitments": result.get("commitments", []), "scores": scores,
        "insight": scoring.insight(scores), "prices": _prices(fixture), "live_paths": _live_paths(result),
    }


def _story_cached() -> dict[str, Any]:
    result = api.replay_result()
    key = id(result)
    if _story_cache.get("key") != key:
        _story_cache.update(key=key, body=story(result, api.price_fixture()))
    return _story_cache["body"]


def _js_str(value: str) -> str:
    return json.dumps(value).replace("<", "\\u003c")


def waitlist_html() -> str:
    """The waitlist page with the server's Supabase project URL and publishable (browser-safe) key filled in."""
    body = (WAITLIST_DIR / "index.html").read_text()
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    if url and key:
        for name, value in (("SUPABASE_URL", url), ("SUPABASE_ANON_KEY", key)):
            body = _WAITLIST_CONST[name].sub(lambda _m, n=name, v=value: f"const {n} = {_js_str(v)};", body, count=1)
    return body


def register(app: FastAPI) -> None:
    app.mount("/harvest/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="harvest-static")
    for path, name in PAGES.items():
        _page(app, path, name)
    for path, name in APP_PAGES.items():  # the simple multi-page UI
        _page(app, path, name)

    @app.get("/waitlist", include_in_schema=False)
    async def waitlist_redirect(request: Request) -> RedirectResponse:  # the page loads hero.jpg etc. relative to /waitlist/
        query = request.url.query
        return RedirectResponse("/waitlist/" + (f"?{query}" if query else ""), status_code=308)

    @app.get("/waitlist/index.html", include_in_schema=False)
    async def waitlist_index_redirect(request: Request) -> RedirectResponse:  # old links
        query = request.url.query
        return RedirectResponse("/waitlist/" + (f"?{query}" if query else ""), status_code=308)

    @app.get("/waitlist/", response_class=HTMLResponse)
    async def waitlist_page() -> HTMLResponse:
        return HTMLResponse(waitlist_html(), headers={"Cache-Control": "no-cache"})

    @app.get("/waitlist/{name}", response_model=None)
    async def waitlist_asset(name: str) -> Response:
        if name not in WAITLIST_ASSETS:
            return JSONResponse({"error": "Not found."}, status_code=404)
        return FileResponse(WAITLIST_DIR / name, media_type=WAITLIST_ASSETS[name], headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/harvest/api/story")
    async def story_route() -> JSONResponse:
        return JSONResponse(await asyncio.to_thread(_story_cached), headers={"Cache-Control": "no-cache"})

    @app.get("/harvest/qr.svg", response_model=None)
    async def qr_svg(request: Request) -> Response:
        try:
            svg = _qr_svg(f"{public_base_url(request)}/harvest/play")
        except ImportError:
            return JSONResponse({"error": "QR codes need the segno package (requirements.txt)."}, status_code=501)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

    @app.get("/harvest/api/playurl")
    async def play_url(request: Request) -> JSONResponse:
        return JSONResponse({"url": f"{public_base_url(request)}/harvest/play"})


_ASSET_REF = re.compile(r'(["\'])/harvest/static/([\w./-]+\.(?:css|js))\1')


@functools.lru_cache(maxsize=256)
def _asset_version(rel: str, mtime_ns: int) -> str:
    """A short content hash; keyed on the mtime so a deploy that changes the file gets a new one."""
    return hashlib.sha256((STATIC_DIR / rel).read_bytes()).hexdigest()[:10]


def versioned(body: str) -> str:
    """Appends ?v=<content hash> to every /harvest/static css and js reference, so the CDN never serves a stale copy after a deploy."""
    def sub(m: re.Match[str]) -> str:
        file = STATIC_DIR / m.group(2)
        if not file.is_file():
            return m.group(0)
        return f"{m.group(1)}/harvest/static/{m.group(2)}?v={_asset_version(m.group(2), file.stat().st_mtime_ns)}{m.group(1)}"
    return _ASSET_REF.sub(sub, body)


def _page(app: FastAPI, path: str, name: str) -> None:
    @app.get(path, response_class=HTMLResponse, name=f"harvest-{name}")
    async def page() -> HTMLResponse:
        file = STATIC_DIR / name
        body = file.read_text() if file.exists() else "<!doctype html><title>The Crophecy</title><p>The Crophecy is being built.</p>"
        return HTMLResponse(versioned(body), headers={"Cache-Control": "no-cache"})
