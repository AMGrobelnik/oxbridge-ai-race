"""Harvest pages /harvest, /harvest/play, /harvest/board, /harvest/terms, /harvest/qr.svg (SPEC.md section 12, addendum R3). Owner: UI.

GET /harvest/api/story is a compact digest of the frozen replay for the pages (the full replay is ~2 MB of trades):
per-quarter Brier and PnL per world model, the commodity price paths, the sealed holdout with its commitment, and the
replay price path of every live market.
"""

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from agent.harvest import scoring
from agent.routes import harvest as api
from agent.routes.challenge import _qr_svg, public_base_url

STATIC_DIR = Path(__file__).resolve().parent.parent / "harvest" / "static"
PAGES = {"/harvest": "index.html", "/harvest/play": "play.html", "/harvest/board": "board.html", "/harvest/terms": "terms.html"}
PRICES_FROM = "2021-01"
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


def register(app: FastAPI) -> None:
    app.mount("/harvest/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="harvest-static")
    for path, name in PAGES.items():
        _page(app, path, name)

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


def _page(app: FastAPI, path: str, name: str) -> None:
    @app.get(path, response_class=HTMLResponse, name=f"harvest-{name}")
    async def page() -> HTMLResponse:
        file = STATIC_DIR / name
        body = file.read_text() if file.exists() else "<!doctype html><title>Harvest</title><p>Harvest is being built.</p>"
        return HTMLResponse(body, headers={"Cache-Control": "no-cache"})
