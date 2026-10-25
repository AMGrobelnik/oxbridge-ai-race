"""Scenario sliders: POST /harvest/api/scenario, GET /harvest/api/scenario (slider spec) and the page /app/scenario.

The house world models re-forecast every open live market under a what-if climate scenario (agent/harvest/scenario.py).
The deterministic answer is instant; one batched fast-LLM call re-forecasts the most exposed markets and is awaited for
at most SCENARIO_DEADLINE_S (default 8 s). A late LLM answer is still cached, so asking again shows it. LLM answers are
cached per snapped scenario; baselines are always the live book's current prices.
"""

import asyncio
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from agent.challenge.ratelimit import RateLimiter
from agent.harvest import fast_llm, market, scenario
from agent.routes import harvest as api

NO_STORE = {"Cache-Control": "no-store"}
PAGE = Path(__file__).resolve().parent.parent / "harvest" / "static" / "app" / "scenario.html"
CACHE_MAX = 256
GROUP = 3  # personas per LLM call: smaller groups answer faster, in parallel

scenario_limiter = RateLimiter(int(os.environ.get("SCENARIO_PER_MIN") or 30))
ask = fast_llm.ask  # module attribute so tests can stub the model
_llm_cache: "OrderedDict[str, dict[tuple[str, str], tuple[float, str]]]" = OrderedDict()
_warming: list["asyncio.Task[None]"] = []  # the one preset warm-up task per process
_inflight: dict[str, "asyncio.Task[dict[tuple[str, str], tuple[float, str]]]"] = {}


def deadline_s() -> float:
    try:
        return float(os.environ.get("SCENARIO_DEADLINE_S") or 8)
    except ValueError:
        return 8.0


def snapshot(book: market.LiveBook) -> tuple[list[dict[str, Any]], list[Any], dict[str, float]]:
    with book.lock:
        views = [{**book.market_view(q), "rule": (book.meta.get(q) or {}).get("rule")} for q, m in book.markets.items() if m["status"] == "open"]
        return views, list(book.personas), {v["qid"]: v["p_yes"] for v in views}


def _remember(k: str, rows: dict[tuple[str, str], tuple[float, str]]) -> None:
    if rows:
        _llm_cache[k] = rows
        _llm_cache.move_to_end(k)
        while len(_llm_cache) > CACHE_MAX:
            _llm_cache.popitem(last=False)


def _llm_job(k: str, rows: list[dict[str, Any]], personas: list[Any], scen: dict[str, Any]) -> "asyncio.Task[dict[tuple[str, str], tuple[float, str]]]":
    """One background task per scenario: the personas in groups of GROUP, one LLM call per group, in parallel."""
    async def one(group: list[Any]) -> dict[tuple[str, str], tuple[float, str]]:
        sub = [{**r, "agents": [a for a in r["agents"] if a["persona_id"] in {p["id"] for p in group}]} for r in rows]
        text = await asyncio.to_thread(ask, scenario.build_prompt(sub, group, scen))
        return scenario.parse_llm(text, rows, {p["id"] for p in group})

    async def run() -> dict[tuple[str, str], tuple[float, str]]:
        try:
            parts = await asyncio.gather(*(one(personas[i:i + GROUP]) for i in range(0, len(personas), GROUP)), return_exceptions=True)
            merged = {kk: v for part in parts if isinstance(part, dict) for kk, v in part.items()}
            _remember(k, merged)
            return merged
        finally:
            _inflight.pop(k, None)
    task = _inflight.get(k)
    if task is None:
        task = _inflight[k] = asyncio.create_task(run())
    return task


async def run_scenario(raw: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    scen = scenario.normalize(raw)
    k = scenario.key(scen)
    book = await asyncio.to_thread(market.live_book)
    views, personas, prices = await asyncio.to_thread(snapshot, book)
    rows = scenario.deterministic(views, personas, scen)
    source, pending, cached = "fallback", False, k in _llm_cache
    llm = _llm_cache.get(k)
    exposed = scenario.exposed(rows)
    if llm is None and exposed and (k in _inflight or fast_llm.available()):
        task = _llm_job(k, exposed, scenario.llm_personas(personas), scen)
        try:
            llm = await asyncio.wait_for(asyncio.shield(task), max(0.5, deadline_s() - (time.monotonic() - started)))
        except TimeoutError:
            pending = True
        except Exception:  # noqa: BLE001 - any LLM failure keeps the deterministic answer
            llm = None
    if llm and scenario.merge_llm(rows, llm, "cache" if cached else "llm"):
        source = "llm"
    return {"scenario": scen, "key": k, "source": source, "llm_cached": cached, "llm_pending": pending,
            "llm_model": fast_llm.model() if source == "llm" else None, "elapsed_ms": int((time.monotonic() - started) * 1000),
            "markets": scenario.finish(rows, prices)}


async def warm() -> None:
    """Runs the LLM once for every preset (one after another), so the demo's first clicks are instant."""
    for raw in scenario.PRESETS.values():
        scen = scenario.normalize(raw)
        k = scenario.key(scen)
        if k in _llm_cache or not fast_llm.available():
            continue
        book = await asyncio.to_thread(market.live_book)
        views, personas, _ = await asyncio.to_thread(snapshot, book)
        exposed = scenario.exposed(scenario.deterministic(views, personas, scen))
        if exposed:
            try:
                await _llm_job(k, exposed, scenario.llm_personas(personas), scen)
            except Exception:  # noqa: BLE001 - warming is best effort
                continue


def spec() -> dict[str, Any]:
    return {"regions": list(scenario.REGIONS),
            "temp_anomaly": {"min": scenario.TEMP_RANGE[0], "max": scenario.TEMP_RANGE[1], "step": scenario.TEMP_STEP, "unit": "°C"},
            "rain_pct": {"min": scenario.RAIN_RANGE[0], "max": scenario.RAIN_RANGE[1], "step": scenario.RAIN_STEP, "unit": "% vs normal"},
            "enso": {"min": scenario.ENSO_RANGE[0], "max": scenario.ENSO_RANGE[1], "step": scenario.ENSO_STEP, "unit": "ONI-like index"},
            "presets": scenario.PRESETS, "example": {"temp_anomaly": {"Spain": 2.0, "Italy": 1.5}, "rain_pct": -30, "enso": 1.0}}


def register(app: FastAPI) -> None:
    @app.get("/harvest/api/scenario")
    async def scenario_spec() -> JSONResponse:
        if not _warming and fast_llm.available():
            _warming.append(asyncio.create_task(warm()))
        return JSONResponse(spec())

    @app.post("/harvest/api/scenario")
    async def scenario_route(request: Request) -> JSONResponse:
        if (limited := api._limited(scenario_limiter, request, "scenario runs")) is not None:
            return limited
        data, bad = await api._body(request)
        if bad is not None or data is None:
            return bad or api._error("Bad request.", 400)
        return JSONResponse(await run_scenario(data), headers=NO_STORE)

    @app.get("/app/scenario", response_model=None)
    async def scenario_page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html")
