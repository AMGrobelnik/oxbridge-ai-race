"""Harvest JSON API under /harvest/api/* incl. the external agent API (SPEC.md section 11, addendum R1). Owner: market-engine.

GET  /harvest/api/state?since=<seq>   markets, recent trades (newer than seq), leaderboard, fund, users, insight
GET  /harvest/api/markets[/{qid}]     live and soft markets          GET /harvest/api/replay   frozen ReplayResult
GET  /harvest/api/prices              price fixture                  GET /harvest/api/leaderboard
GET  /harvest/api/fund                Resilience Fund allocation     GET /harvest/api/geo      map regions and arcs
GET  /harvest/api/ledger              hash chain and anchors         GET|POST /harvest/api/verify
GET  /harvest/api/holdout             time-machine questions (no answers)
POST /harvest/api/join  {nickname}    POST /harvest/api/bet {token,qid,side,spend}
GET  /harvest/api/me?token=           POST /harvest/api/forecast {token,answers:[{qid,p_yes}]}
GET  /harvest/api/agents              self-description of the external agent API (R1)
POST /harvest/api/agents/register     GET /harvest/api/agents/markets
POST /harvest/api/agents/bet          GET /harvest/api/agents/{agent_id}
POST /harvest/api/byo {name, world_model, token?, nickname?}   GET /harvest/api/byo/{model_id}
"""

import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.challenge import game
from agent.challenge.ratelimit import RateLimiter, client_ip
from agent.harvest import byo, forecaster, ledger, market, scoring
from agent.harvest import fund as fund_mod
from agent.harvest.market import STARTING_CREDITS, LiveBook, MarketError, clamp
from agent.sealed.hashing import sha256_json

DATA_DIR = Path(__file__).resolve().parent.parent / "harvest" / "data"
GEO = DATA_DIR / "geo.json"
MAX_BODY = 8192
WORLD_MODEL_MAX = 1200
SPEND_MIN, SPEND_MAX = 1.0, 500.0
NO_STORE = {"Cache-Control": "no-store"}

join_limiter = RateLimiter(int(os.environ.get("HARVEST_JOINS_PER_MIN") or 10))
bet_limiter = RateLimiter(int(os.environ.get("HARVEST_BETS_PER_MIN") or 120))
register_limiter = RateLimiter(30)
agent_bet_limiter = RateLimiter(120)
forecast_limiter = RateLimiter(20)
verify_limiter = RateLimiter(10)
byo_limiter = RateLimiter(int(os.environ.get("HARVEST_BYO_PER_MIN") or 6))
LIMITERS = (join_limiter, bet_limiter, register_limiter, agent_bet_limiter, forecast_limiter, verify_limiter, byo_limiter)
_background: set[asyncio.Task[Any]] = set()
_house_task: dict[str, asyncio.Task[None]] = {}
_replay_cache: dict[str, Any] = {}
_byo_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}


def _error(message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _limited(limiter: RateLimiter, request: Request, what: str) -> JSONResponse | None:
    wait = limiter.hit(client_ip(request))
    if wait:
        return _error(f"Too many {what}; try again in {int(wait) + 1} s.", 429, {"Retry-After": str(int(wait) + 1)})
    return None


async def _body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return None, _error("Request body too large.", 413)
    try:
        data = json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _error("Body must be JSON.", 400)
    if not isinstance(data, dict):
        return None, _error("Body must be a JSON object.", 400)
    return data, None


# ---------------------------------------------------------------- data access (degrades while modules are stubs)

def replay_result() -> dict[str, Any] | None:
    """The frozen replay, cached by file mtime; None while it does not exist yet."""
    try:
        from agent.harvest import replay

        mtime = Path(replay.DEMO).stat().st_mtime if Path(replay.DEMO).exists() else None
    except (ImportError, OSError):
        return None
    if mtime is None:
        return None
    if _replay_cache.get("mtime") != mtime:
        _replay_cache.update(mtime=mtime, result=market.load_replay())
    return _replay_cache.get("result")


def price_fixture() -> dict[str, Any] | None:
    try:
        from agent.harvest import prices

        return dict(prices.load_fixture())
    except (ImportError, NotImplementedError, FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return None


def _book() -> LiveBook:
    return market.live_book()


def _schedule_anchor() -> None:
    ledger.anchorer.ensure_ticker()
    try:
        task = asyncio.get_running_loop().create_task(ledger.anchorer.maybe_anchor())
    except RuntimeError:
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def _ensure_house_agents() -> None:
    """Starts the background loop where house world models trade live markets on their forecasts."""
    interval = market.house_tick_s()
    if interval <= 0:
        return
    task = _house_task.get("t")
    if task is not None and not task.done():
        return
    try:
        _house_task["t"] = asyncio.get_running_loop().create_task(_house_loop(interval))
    except RuntimeError:
        return


async def _house_loop(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            trade = await asyncio.to_thread(_book().house_step)
            if trade is not None:
                _schedule_anchor()
        except asyncio.CancelledError:
            raise
        except Exception:  # one bad round never stops the loop
            import logging

            logging.getLogger("agent").exception("harvest house tick failed")


# ---------------------------------------------------------------- views

def _external_scores(book: LiveBook) -> list[dict[str, Any]]:
    out = []
    for tid, info in book.traders.items():
        if info["kind"] != "external":
            continue
        fc = book.forecasts.get(tid) or {}
        out.append({
            "persona_id": tid, "name": info.get("name", tid), "owner": info.get("owner"), "world_model_sha256": info.get("world_model_sha256"),
            "brier_holdout": fc.get("brier"), "n": int(fc.get("n") or 0), "pnl_live": book.equity(tid) - STARTING_CREDITS,
            "trades": sum(1 for t in book.trades if t["trader"] == tid),
            **({"world_model": info.get("world_model"), "color": byo.COLOR, "per_question": fc.get("per_question")} if byo.is_byo(tid) else {}),
        })
    return out


def _human_rows(book: LiveBook) -> list[dict[str, Any]]:
    out = []
    for tid, info in book.traders.items():
        if info["kind"] != "human":
            continue
        fc = book.forecasts.get(tid) or {}
        out.append({
            "user_id": tid, "nickname": info.get("nickname", tid), "brier": fc.get("brier"), "n": int(fc.get("n") or 0),
            "rank_vs_world_models": fc.get("rank_vs_world_models"), "balance": book.balance(tid),
            "pnl": book.equity(tid) - STARTING_CREDITS, "trades": sum(1 for t in book.trades if t["trader"] == tid),
        })
    return out


def leaderboard_view(book: LiveBook) -> dict[str, Any]:
    result = replay_result() or {"personas": book.personas, "cutoffs": []}
    lb = scoring.leaderboard(result, external=_external_scores(book), humans=_human_rows(book))
    for m in lb["world_models"]:
        if byo.is_byo(m["persona_id"]):
            m["origin"] = byo.ORIGIN  # type: ignore[typeddict-item]
        if m["origin"] == "house":
            m["pnl_live"] = book.equity(f"agent:{m['persona_id']}") - STARTING_CREDITS  # type: ignore[typeddict-unknown-key]
    return {**lb, "insight": scoring.insight(lb)}


def fund_view(book: LiveBook) -> dict[str, Any]:
    open_markets = [book.market_view(q) for q, m in book.markets.items() if m["status"] == "open"]
    state = dict(fund_mod.allocate(open_markets, book.fund))
    state["receipt"] = ledger.last_anchor("fund")
    state["earmark"] = "European farmers' heat and drought adaptation"
    return state


def _personas_view(book: LiveBook) -> list[dict[str, Any]]:
    return [{k: p.get(k) for k in ("id", "name", "color", "world_model", "beliefs", "heuristic")} for p in book.personas]  # type: ignore[misc]


def state_view(book: LiveBook, since: int | None) -> dict[str, Any]:
    trades = book.trades
    recent = [t for t in trades if t["seq"] > since][-200:] if since is not None else trades[-50:]
    lb = leaderboard_view(book)
    return {
        "personas": _personas_view(book),
        "markets": [book.market_view(q) for q in book.markets],
        "recent_trades": recent,
        "seq": trades[-1]["seq"] if trades else 0,
        "leaderboard": lb,
        "fund": fund_view(book),
        "users": book.users(),
        "agents": sum(1 for tid, t in book.traders.items() if t["kind"] == "external" and not byo.is_byo(tid)),
        "human_models": sum(1 for tid in book.traders if byo.is_byo(tid)),
        "ledger_head": {"seq": book.ledger_head[0], "hash": book.ledger_head[1]},
        "insight": lb["insight"],
        "has_replay": replay_result() is not None,
    }


def holdout_questions(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Holdout questions with what a human may see before answering (no outcome, no final price)."""
    out = []
    for c in scoring.scored_cutoffs(result or {}, "holdout"):
        for q in c.get("questions", []):
            out.append({k: q.get(k) for k in ("qid", "title", "kind", "region", "crop", "series", "base_month", "base_value", "resolve_month", "target", "cutoff")})
    return out


def score_answers(result: dict[str, Any] | None, answers: list[tuple[str, float]]) -> dict[str, Any]:
    outcomes = scoring.holdout_outcomes(result or {})
    pairs = [(p, outcomes[q]) for q, p in answers if q in outcomes]
    if not pairs:
        raise MarketError("no answered question is a resolved holdout question")
    qids = {q for q, _ in answers if q in outcomes}
    user = scoring.mean_brier(pairs)
    personas = scoring.persona_briers(result or {}, qids)
    names = {p["id"]: p.get("name", p["id"]) for p in (result or {}).get("personas", [])}
    models = sorted(({"persona_id": k, "name": names.get(k, k), "brier": v} for k, v in personas.items() if v is not None), key=lambda m: m["brier"])
    rank = 1 + sum(1 for m in models if m["brier"] < user)  # type: ignore[operator]
    reveal = []
    for c in scoring.scored_cutoffs(result or {}, "holdout"):
        reveal += [r for r in c.get("resolutions", []) if r["qid"] in qids]
    return {"brier": user, "n": len(pairs), "rank_vs_world_models": rank, "of": len(models) + 1, "world_models": models, "reveal": reveal}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:24] or "agent"


def _side(raw: Any) -> str | None:
    side = str(raw or "").strip().upper()
    return side if side in ("YES", "NO") else None


def _num(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value and abs(value) != float("inf") else None


AGENT_API_DOC: dict[str, Any] = {
    "name": "Harvest external agent API",
    "about": "Trade the European-agriculture climate-stress prediction market as an outside world model. "
             "Register a world model (committed to the hash-chained ledger before you trade), read open markets, bet with play credits. "
             "Your agent appears on the world-model leaderboard with origin 'external'.",
    "credits": STARTING_CREDITS,
    "credits_have_no_value": True,
    "credits_note": "Play credits have no monetary value and cannot be bought, sold, transferred or withdrawn. There is no deposit or cash-out.",
    "terms": "/harvest/terms",
    "fee_rate": market.FEE_RATE,
    "fee_goes_to": "Resilience Fund illustration (fees are play credits; real funding would come from sponsors and subscriptions, never player losses)",
    "endpoints": [
        {"method": "POST", "path": "/harvest/api/agents/register", "body": {"name": "Drought Bear", "world_model": "Heat waves in southern Europe cut olive and durum yields; prices lag the weather by a season.", "owner": "team-x"},
         "returns": "agent_id, token (keep it secret), credits, world_model_sha256", "rate_limit": "30/min per IP"},
        {"method": "GET", "path": "/harvest/api/agents/markets",
         "returns": "open markets: qid, title, kind, p_yes, b, series, visible_history (masked: months t-12..t0, prices indexed to 100), target_index, closes"},
        {"method": "POST", "path": "/harvest/api/agents/bet", "body": {"token": "<token>", "qid": "<qid>", "side": "YES", "spend": 25, "p_yes": 0.7, "rationale": "short reason"},
         "returns": "trade, market, balance", "rate_limit": "120/min per IP", "limits": f"spend {SPEND_MIN:g}..{SPEND_MAX:g}, p_yes 0.01..0.99"},
        {"method": "POST", "path": "/harvest/api/forecast", "body": {"token": "<token>", "answers": [{"qid": "<holdout qid>", "p_yes": 0.6}]},
         "returns": "Brier on the sealed holdout vs the house world models (questions: GET /harvest/api/holdout)"},
        {"method": "GET", "path": "/harvest/api/agents/{agent_id}", "returns": "positions, PnL, Brier, world-model hash"},
        {"method": "GET", "path": "/harvest/api/leaderboard", "returns": "world_models (house, external and human-model), baselines, humans"},
        {"method": "POST", "path": "/harvest/api/byo", "body": {"name": "Heat first", "world_model": "<your theory, at most 1200 characters>"},
         "returns": "model_id, world_model_sha256, brier_holdout, rank vs the 8 house models and the market, per_question, rationales",
         "rate_limit": "6/min per IP", "note": "backtests your paragraph on the sealed holdout with the house forecaster; the same paragraph is never re-run"},
    ],
}


def _visible_history(series: str | None, fixture: dict[str, Any] | None, cutoff: str | None) -> list[list[Any]]:
    """The masked window agents see at the cutoff (R2): relative months t-12..t0, prices rebased to 100."""
    if not series or not cutoff or not fixture or series not in fixture.get("series", {}):
        return []
    return [list(p) for p in forecaster.mask_window(forecaster.visible_window(fixture, series, cutoff), cutoff)]  # type: ignore[arg-type]


def _target_index(series: str | None, fixture: dict[str, Any] | None, cutoff: str | None, target: Any) -> float | None:
    """The target on the same rebased index as visible_history."""
    if not series or not cutoff or not fixture or series not in fixture.get("series", {}) or _num(target) is None:
        return None
    raw = forecaster.visible_window(fixture, series, cutoff)  # type: ignore[arg-type]
    return round(float(target) / float(raw[0][1]) * 100.0, 2) if raw and raw[0][1] else None


def _byo_view(book: LiveBook, model_id: str, *, cached: bool) -> dict[str, Any] | None:
    info, fc = book.traders.get(model_id), book.forecasts.get(model_id)
    if info is None or not fc or fc.get("origin") != byo.ORIGIN:
        return None
    keep = ("brier_holdout", "n", "rank", "of", "field", "per_question", "rationales", "source", "cutoffs")
    return {"model_id": model_id, "name": info.get("name"), "owner": info.get("owner"), "origin": byo.ORIGIN, "world_model": info.get("world_model"),
            "world_model_sha256": info.get("world_model_sha256"), **{k: fc.get(k) for k in keep}, "scored_at": fc.get("at"), "cached": cached}


# ---------------------------------------------------------------- routes

def register(app: FastAPI) -> None:  # noqa: C901 - one flat list of routes
    @app.get("/harvest/api/state")
    async def state(since: int | None = None) -> JSONResponse:
        ledger.anchorer.ensure_ticker()
        _ensure_house_agents()
        book = await asyncio.to_thread(_book)
        with book.lock:
            body = state_view(book, since)
        return JSONResponse(body, headers=NO_STORE)

    @app.get("/harvest/api/markets")
    async def markets() -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            return JSONResponse({"markets": [book.market_view(q) for q in book.markets]}, headers=NO_STORE)

    @app.get("/harvest/api/markets/{qid}")
    async def market_detail(qid: str) -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            if qid not in book.markets:
                return _error(f"unknown market {qid!r}", 404)
            return JSONResponse(book.market_view(qid, full=True), headers=NO_STORE)

    @app.get("/harvest/api/replay")
    async def replay_route() -> JSONResponse:
        result = await asyncio.to_thread(replay_result)
        if result is None:
            return JSONResponse({"error": "replay not frozen yet", "cutoffs": [], "personas": [], "commitments": []})
        return JSONResponse(result)

    @app.get("/harvest/api/prices")
    async def prices_route() -> JSONResponse:
        fixture = await asyncio.to_thread(price_fixture)
        if fixture is None:
            return JSONResponse({"error": "price fixture not available yet", "series": {}})
        return JSONResponse(fixture)

    @app.get("/harvest/api/leaderboard")
    async def leaderboard_route() -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            return JSONResponse(leaderboard_view(book), headers=NO_STORE)

    @app.get("/harvest/api/fund")
    async def fund_route() -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            return JSONResponse(fund_view(book), headers=NO_STORE)

    @app.get("/harvest/api/geo")
    async def geo() -> JSONResponse:
        try:
            return JSONResponse(json.loads(GEO.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return JSONResponse({"regions": [], "arcs": []})

    @app.get("/harvest/api/ledger")
    async def ledger_route(limit: int = 500) -> JSONResponse:
        entries = await asyncio.to_thread(ledger.recent_entries, max(1, min(limit, 5000)))
        return JSONResponse({"genesis": ledger.GENESIS, "entries": entries, "anchors": await asyncio.to_thread(ledger.anchors),
                             "anchor_every": ledger.anchorer.every, "anchoring": ledger.anchoring_enabled()}, headers=NO_STORE)

    async def _verify(tamper: bool) -> dict[str, Any]:
        chain_report = await asyncio.to_thread(ledger.verify)
        out: dict[str, Any] = {"chain_ok": chain_report["ok"], "commitments_ok": None, "windows_ok": None, "tamper": tamper, "ledger": chain_report, "details": []}
        result, fixture = replay_result(), price_fixture()
        if result is None or fixture is None:
            out["details"].append("replay or price fixture not available yet: commitments not checked")
            return out
        try:
            from agent.harvest import replay

            rep = await replay.verify_commitments(result, fixture, tamper=tamper)  # type: ignore[arg-type]
        except NotImplementedError:
            out["details"].append("replay.verify_commitments not implemented yet")
            return out
        out.update(commitments_ok=rep.get("commitments_ok"), windows_ok=rep.get("windows_ok"))
        out["details"] += list(rep.get("details", []))
        out["replay"] = {k: v for k, v in rep.items() if k not in ("details",)}
        return out

    @app.get("/harvest/api/verify")
    async def verify_get() -> JSONResponse:
        return JSONResponse(await _verify(False), headers=NO_STORE)

    @app.post("/harvest/api/verify")
    async def verify_post(request: Request) -> JSONResponse:
        if (limited := _limited(verify_limiter, request, "verify requests")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        return JSONResponse(await _verify(bool(data.get("tamper"))), headers=NO_STORE)

    @app.get("/harvest/api/holdout")
    async def holdout() -> JSONResponse:
        result = await asyncio.to_thread(replay_result)
        return JSONResponse({"questions": holdout_questions(result), "baselines": scoring.leaderboard(result or {})["baselines"]})

    @app.post("/harvest/api/join")
    async def join(request: Request) -> JSONResponse:
        if (limited := _limited(join_limiter, request, "joins")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        try:
            nickname = game.sanitise_nickname(data.get("nickname"))
        except game.InvalidInput as err:
            return _error(str(err), 422)
        user_id = f"user:{game.nick_sha256(nickname)[:12]}"
        token = secrets.token_urlsafe(18)
        book = await asyncio.to_thread(_book)
        entry = await asyncio.to_thread(book.record, kind="join", trader=user_id,
                                        payload={"nickname": nickname, "token_sha256": market.token_sha(token)})
        _schedule_anchor()
        return JSONResponse({"user_id": user_id, "nickname": nickname, "token": token, "credits": book.balance(user_id),
                             "users": book.users(), "entry": {"seq": entry["seq"], "hash": entry["hash"]}})

    @app.get("/harvest/api/me")
    async def me(token: str = "") -> JSONResponse:
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(token)
        if trader is None:
            return _error("unknown token; join first", 401)
        with book.lock:
            info = book.traders.get(trader, {})
            return JSONResponse({"user_id": trader, "nickname": info.get("nickname") or info.get("name"), "balance": book.balance(trader),
                                 "equity": book.equity(trader), "pnl": book.equity(trader) - STARTING_CREDITS,
                                 "positions": book.positions(trader), "forecast": book.forecasts.get(trader)}, headers=NO_STORE)

    async def _bet(request: Request, limiter: RateLimiter) -> JSONResponse:
        if (limited := _limited(limiter, request, "bets")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(str(data.get("token") or ""))
        if trader is None:
            return _error("unknown token; join or register first", 401)
        side, spend = _side(data.get("side")), _num(data.get("spend"))
        if side is None:
            return _error("side must be YES or NO", 422)
        if spend is None:
            return _error("spend must be a number", 422)
        p_yes = _num(data.get("p_yes"))
        rationale = str(data.get("rationale") or "")[:200] or None
        try:
            trade, entry = await asyncio.to_thread(book.place_bet, trader, str(data.get("qid") or ""), side,  # type: ignore[arg-type]
                                                   round(clamp(spend, SPEND_MIN, SPEND_MAX), 2),
                                                   p_yes=clamp(p_yes, 0.01, 0.99) if p_yes is not None else None, rationale=rationale)
        except MarketError as err:
            return _error(str(err), 422 if "unknown market" not in str(err) else 404)
        _schedule_anchor()
        _ensure_house_agents()
        with book.lock:
            return JSONResponse({"trade": trade, "market": book.market_view(trade["qid"]), "balance": book.balance(trader),
                                 "entry": {"seq": entry["seq"], "hash": entry["hash"]}})

    @app.post("/harvest/api/bet")
    async def bet(request: Request) -> JSONResponse:
        return await _bet(request, bet_limiter)

    @app.post("/harvest/api/forecast")
    async def forecast(request: Request) -> JSONResponse:
        if (limited := _limited(forecast_limiter, request, "forecasts")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(str(data.get("token") or ""))
        if trader is None:
            return _error("unknown token; join first", 401)
        raw = data.get("answers")
        if not isinstance(raw, list) or not raw:
            return _error("answers must be a non-empty list of {qid, p_yes}", 422)
        answers = []
        for a in raw[:64]:
            p = _num(a.get("p_yes")) if isinstance(a, dict) else None
            if p is None or not isinstance(a.get("qid"), str):
                return _error("each answer needs a qid and a numeric p_yes", 422)
            answers.append((a["qid"], clamp(p, 0.01, 0.99)))
        result = await asyncio.to_thread(replay_result)
        if result is None:
            return _error("the time machine is not ready yet (replay not frozen)", 503)
        try:
            scored = score_answers(result, answers)
        except MarketError as err:
            return _error(str(err), 422)
        payload = {"answers": [{"qid": q, "p_yes": p} for q, p in answers], "brier": scored["brier"], "n": scored["n"],
                   "rank_vs_world_models": scored["rank_vs_world_models"], "of": scored["of"]}
        entry = await asyncio.to_thread(book.record, kind="forecast", trader=trader, payload=payload)
        _schedule_anchor()
        return JSONResponse({**scored, "entry": {"seq": entry["seq"], "hash": entry["hash"]}})

    # ---- R1: external agents
    @app.get("/harvest/api/agents")
    async def agents_doc(request: Request) -> JSONResponse:
        base = (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).strip().rstrip("/")
        return JSONResponse({**AGENT_API_DOC, "base_url": base})

    @app.post("/harvest/api/agents/register")
    async def agents_register(request: Request) -> JSONResponse:
        if (limited := _limited(register_limiter, request, "registrations")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        name = " ".join(str(data.get("name") or "").split())[:40]
        world_model = str(data.get("world_model") or "").strip()
        if not name:
            return _error("name is required", 422)
        if not world_model:
            return _error("world_model is required: state your conviction about how climate, society and markets work", 422)
        if len(world_model) > WORLD_MODEL_MAX:
            return _error(f"world_model is at most {WORLD_MODEL_MAX} characters", 422)
        owner = " ".join(str(data.get("owner") or "").split())[:40] or None
        agent_id = f"ext:{_slug(name)}-{secrets.token_hex(3)}"
        token = secrets.token_urlsafe(18)
        wm_sha = sha256_json({"name": name, "world_model": world_model})
        book = await asyncio.to_thread(_book)
        entry = await asyncio.to_thread(book.record, kind="agent", trader=agent_id, payload={
            "name": name, "owner": owner, "world_model": world_model, "world_model_sha256": wm_sha, "token_sha256": market.token_sha(token)})
        _schedule_anchor()
        return JSONResponse({"agent_id": agent_id, "token": token, "credits": book.balance(agent_id), "credits_have_no_value": True, "world_model_sha256": wm_sha,
                             "entry": {"seq": entry["seq"], "hash": entry["hash"]}, "next": "GET /harvest/api/agents/markets, then POST /harvest/api/agents/bet"})

    @app.get("/harvest/api/agents/markets")
    async def agents_markets() -> JSONResponse:
        book = await asyncio.to_thread(_book)
        fixture = await asyncio.to_thread(price_fixture)
        with book.lock:
            out = []
            for qid, m in book.markets.items():
                if m["status"] != "open":
                    continue
                q = book.meta.get(qid) or {}
                out.append({"qid": qid, "title": q.get("title", qid), "kind": q.get("kind", "hard"), "p_yes": book.price(qid), "b": m["b"],
                            "series": q.get("series"), "target": q.get("target"), "region": q.get("region"), "crop": q.get("crop"),
                            "visible_history": _visible_history(q.get("series"), fixture, q.get("cutoff")),
                            "target_index": _target_index(q.get("series"), fixture, q.get("cutoff"), q.get("target")), "closes": q.get("resolve_month")})
        return JSONResponse({"markets": out, "at": time.time()}, headers=NO_STORE)

    @app.post("/harvest/api/agents/bet")
    async def agents_bet(request: Request) -> JSONResponse:
        return await _bet(request, agent_bet_limiter)

    @app.get("/harvest/api/agents/{agent_id}")
    async def agent_detail(agent_id: str) -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            info = book.traders.get(agent_id)
            if info is None or info["kind"] != "external":
                return _error(f"unknown agent {agent_id!r}", 404)
            fc = book.forecasts.get(agent_id) or {}
            return JSONResponse({"agent_id": agent_id, "name": info.get("name"), "owner": info.get("owner"), "world_model": info.get("world_model"),
                                 "world_model_sha256": info.get("world_model_sha256"), "balance": book.balance(agent_id),
                                 "equity": book.equity(agent_id), "pnl": book.equity(agent_id) - STARTING_CREDITS,
                                 "positions": book.positions(agent_id), "brier": fc.get("brier"), "n_scored": int(fc.get("n") or 0),
                                 "trades": [t for t in book.trades if t["trader"] == agent_id][-100:]}, headers=NO_STORE)

    # ---- bring your own world model: a human-written paragraph backtested on the sealed holdout
    async def _byo_backtest(model_id: str, name: str, world_model: str, wm_sha: str, owner: str | None) -> None:
        book = await asyncio.to_thread(_book)
        result = await asyncio.to_thread(replay_result)
        fixture = await asyncio.to_thread(price_fixture)
        if result is None or fixture is None or not scoring.scored_cutoffs(result, "holdout"):
            raise MarketError("the sealed holdout is not available yet")
        name = str((book.traders.get(model_id) or {}).get("name") or name)
        persona = byo.make_persona(model_id, name, world_model, list(result.get("personas", [])))
        if model_id not in book.traders:  # commit the world model before it sees a single question
            await asyncio.to_thread(book.record, kind="agent", trader=model_id, payload={
                "name": name, "owner": owner, "world_model": persona["world_model"], "world_model_sha256": wm_sha,
                "origin": byo.ORIGIN, "persona_sha256": sha256_json(persona)})
        await asyncio.to_thread(forecaster._load_cache)  # parse the LLM cache off the event loop
        forecasts = await byo.run_forecasts(persona, result, fixture)
        summary = await asyncio.to_thread(byo.summarise, persona, result, forecasts)
        await asyncio.to_thread(book.record, kind="forecast", trader=model_id, payload={**summary, "world_model_sha256": wm_sha})
        _schedule_anchor()

    @app.post("/harvest/api/byo")
    async def byo_create(request: Request) -> JSONResponse:
        if (limited := _limited(byo_limiter, request, "theories")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        name = " ".join(str(data.get("name") or "").split())[:40]
        world_model = byo.normalise(str(data.get("world_model") or ""))
        if not name:
            return _error("name is required", 422)
        if len(world_model) < 20:
            return _error("world_model is required: write a few sentences on how climate, politics and money move food prices", 422)
        if len(world_model) > WORLD_MODEL_MAX:
            return _error(f"world_model is at most {WORLD_MODEL_MAX} characters", 422)
        book = await asyncio.to_thread(_book)
        owner: str | None = None
        trader = book.trader_for(str(data.get("token") or "")) if data.get("token") else None
        if trader is not None:
            owner = (book.traders.get(trader) or {}).get("nickname")
        if owner is None and data.get("nickname"):
            try:
                owner = game.sanitise_nickname(data.get("nickname"))
            except game.InvalidInput:
                owner = None
        wm_sha = byo.world_model_sha(world_model)
        model_id = byo.model_id_for(wm_sha)
        with book.lock:
            hit = _byo_view(book, model_id, cached=True)
        if hit is not None:
            return JSONResponse(hit, headers=NO_STORE)
        task = _byo_inflight.get(wm_sha)
        if task is None:
            if len(_byo_inflight) >= byo.MAX_CONCURRENT:
                return _error("Backtests are busy; try again in a few seconds.", 503, {"Retry-After": "10"})
            task = asyncio.get_running_loop().create_task(_byo_backtest(model_id, name, world_model, wm_sha, owner))
            _byo_inflight[wm_sha] = task
            task.add_done_callback(lambda _t, key=wm_sha: _byo_inflight.pop(key, None))
        try:
            await asyncio.shield(task)  # a dropped client never cancels a running backtest
        except MarketError as err:
            return _error(str(err), 503)
        except Exception:  # noqa: BLE001 - report, never crash the route
            import logging

            logging.getLogger("agent").exception("harvest byo backtest failed")
            return _error("the backtest failed; try again", 500)
        with book.lock:
            view = _byo_view(book, model_id, cached=False)
        return JSONResponse(view, headers=NO_STORE) if view is not None else _error("the backtest did not record a result", 500)

    @app.get("/harvest/api/byo/{model_id}")
    async def byo_detail(model_id: str) -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            view = _byo_view(book, model_id, cached=True)
        return JSONResponse(view, headers=NO_STORE) if view is not None else _error(f"unknown model {model_id!r}", 404)
