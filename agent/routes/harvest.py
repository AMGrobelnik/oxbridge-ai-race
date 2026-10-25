"""Harvest JSON API under /harvest/api/* incl. the external agent API (SPEC.md section 11, addendum R1). Owner: market-engine.

GET  /harvest/api/state?since=<seq>   markets, recent trades (newer than seq), leaderboard, fund, users, insight
     ?lite=1: the same without leaderboard, forecasts and world-model text (~10 kB gzipped; what pollers use)
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
GET  /harvest/api/news?since=<id>&commodity=&qid=&limit=   live agri news, tagged with commodities, regions and market qids
GET  /harvest/api/markets/{qid}/agent   how an agent sees a market: masked window, news lines, latest forecasts, agents/markets entry
POST /harvest/api/waitlist {contact, note?}   GET /harvest/api/waitlist/count   early-access waitlist (count only is public)
GET  /harvest/api/follow?token=       POST /harvest/api/follow {token, agent, follow?, copy?, fraction?, cap?}
GET  /harvest/api/follow/feed?token=&limit=   followed agents' latest bets and your copies of them
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
from agent.harvest import accounts, baseline_models, byo, follow, forecaster, ledger, market, news, scoring, waitlist
from agent.harvest import fund as fund_mod
from agent.harvest.market import STARTING_CREDITS, LiveBook, MarketError, clamp
from agent.sealed.hashing import sha256_json

DATA_DIR = Path(__file__).resolve().parent.parent / "harvest" / "data"
GEO = DATA_DIR / "geo.json"
MAX_BODY = 8192
WORLD_MODEL_MAX = 1200
SPEND_MIN, SPEND_MAX = 1.0, 500.0
NO_STORE = {"Cache-Control": "no-store"}

join_limiter = RateLimiter(int(os.environ.get("HARVEST_JOINS_PER_MIN") or 100))
bet_limiter = RateLimiter(int(os.environ.get("HARVEST_BETS_PER_MIN") or 600))
register_limiter = RateLimiter(100)
agent_bet_limiter = RateLimiter(600)
forecast_limiter = RateLimiter(300)
verify_limiter = RateLimiter(300)
byo_limiter = RateLimiter(int(os.environ.get("HARVEST_BYO_PER_MIN") or 60))
waitlist_limiter = RateLimiter(int(os.environ.get("HARVEST_WAITLIST_PER_MIN") or 60))
follow_limiter = RateLimiter(600)
LIMITERS = (join_limiter, bet_limiter, register_limiter, agent_bet_limiter, forecast_limiter, verify_limiter, byo_limiter, waitlist_limiter, follow_limiter)
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
    """Starts the background loop where house world models trade live markets on their forecasts, and the news poller."""
    news.ensure_poller()
    from agent.routes import crophecy_agents

    crophecy_agents.ensure_jobs()  # debate, resolution and news-drafted markets
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
            "persona_id": tid, "name": info.get("name", tid), "username": info.get("username"), "owner": info.get("owner"),
            "world_model_sha256": info.get("world_model_sha256"),
            "brier_holdout": fc.get("brier"), "n": int(fc.get("n") or 0), "pnl_live": book.equity(tid) - STARTING_CREDITS,
            "trades": sum(1 for t in book.trades if t["trader"] == tid),
            **({"world_model": info.get("world_model"), "color": byo.COLOR, "per_question": fc.get("per_question"),
                "model": fc.get("model"), "model_label": fc.get("model_label") or "Gemini (legacy, before clean models)"} if byo.is_byo(tid) else {}),
        })
    return out


def _human_rows(book: LiveBook) -> list[dict[str, Any]]:
    out = []
    for tid, info in book.traders.items():
        if info["kind"] != "human":
            continue
        fc = book.forecasts.get(tid) or {}
        out.append({
            "user_id": tid, "nickname": info.get("nickname", tid), "username": info.get("username"), "brier": fc.get("brier"), "n": int(fc.get("n") or 0),
            "rank_vs_world_models": fc.get("rank_vs_world_models"), "balance": book.balance(tid),
            "pnl": book.equity(tid) - STARTING_CREDITS, "trades": sum(1 for t in book.trades if t["trader"] == tid),
        })
    return out


def leaderboard_view(book: LiveBook) -> dict[str, Any]:
    result = replay_result() or {"personas": book.personas, "cutoffs": []}
    lb = scoring.leaderboard(result, external=_external_scores(book), humans=_human_rows(book))
    from agent.harvest import resolve

    live = resolve.live_scores()  # house agents' Brier on resolved live markets
    for m in lb["world_models"]:
        if m["persona_id"] in live:
            m.update(live[m["persona_id"]])  # type: ignore[typeddict-item]
        if byo.is_byo(m["persona_id"]):
            m["origin"] = byo.ORIGIN  # type: ignore[typeddict-item]
        if m["origin"] in scoring.HOUSE_ORIGINS:
            m["pnl_live"] = book.equity(f"agent:{m['persona_id']}") - STARTING_CREDITS  # type: ignore[typeddict-unknown-key]
    board = accounts.board(list(lb["world_models"]), personas=list(result.get("personas") or book.personas), traders=book.traders,  # type: ignore[arg-type]
                           baselines=baseline_models.rows(replay_result(), price_fixture()), humans=lb["humans"])
    return {**lb, "insight": scoring.insight(lb), "accounts": board, "categories": accounts.category_counts(board),
            "category_labels": accounts.CATEGORY_LABELS, "verified_label": accounts.VERIFIED_LABEL}


def pilots(book: LiveBook) -> int:
    """Pilots: real humans who joined (never agents, swarm bots included)."""
    return book.users()


def record_account(book: LiveBook, *, kind: str, trader: str | None, payload: dict[str, Any], wanted: Any) -> tuple[Any, str, str]:
    """Checks or generates the username and appends the join / agent entry under the book lock, so two
    sign-ups can never take the same name. trader None means a human, whose id derives from the username.
    Returns (entry, trader, username); raises accounts.UsernameError (UsernameTakenError when taken)."""
    with book.lock:
        if wanted:
            username = accounts.normalise_username(wanted)
            if book.username_taken(username):
                raise accounts.UsernameTakenError(f"@{username} is taken; pick another username")
        else:
            username = accounts.generate(book.username_taken)
        trader = trader or accounts.user_id_for(username)
        payload = {**payload, "username": username}
        if kind == "join" and not payload.get("nickname"):
            payload["nickname"] = username
        entry = book.record(kind=kind, trader=trader, payload=payload)
        return entry, trader, username


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


LITE_TRADES = 40  # newest bets in a lite poll without `since`
LITE_TRADES_SINCE = 60  # most bets a lite poll with `since` returns
LITE_HISTORY = 40  # price points kept per market: the sparklines are 300 px wide
LITE_RATIONALE = 240


def _thin_history(history: list[list[Any]], keep: int = LITE_HISTORY) -> list[list[Any]]:
    """At most `keep` points spread evenly over the history, first and last included, prices to 4 places."""
    if len(history) > keep:
        history = [history[round(i * (len(history) - 1) / (keep - 1))] for i in range(keep)]
    return [[h[0], round(h[1], 4)] for h in history]


def lite_market(view: dict[str, Any]) -> dict[str, Any]:
    """A market for pollers: no per-agent forecasts (clients keep the ones from their last full load), thin history."""
    out = {k: v for k, v in view.items() if k not in ("agent_forecasts", "history")}
    out["history"] = _thin_history(view.get("history") or [])
    return out


def lite_trade(trade: dict[str, Any]) -> dict[str, Any]:
    rationale = trade.get("rationale")
    return {**trade, "rationale": rationale[:LITE_RATIONALE]} if isinstance(rationale, str) and len(rationale) > LITE_RATIONALE else trade


def state_view_lite(book: LiveBook, since: int | None) -> dict[str, Any]:
    """What the polling pages redraw from: the full state minus the leaderboard, forecasts and world-model text.

    The full /state is ~600 kB (~115 kB gzipped) because of the leaderboard and per-agent forecasts; every live page
    polling it from a laptop on venue wifi would saturate the uplink. Pages load the full state once, then poll this,
    and merge it over the last full load.
    """
    trades = book.trades
    recent = [t for t in trades if t["seq"] > since][-LITE_TRADES_SINCE:] if since is not None else trades[-LITE_TRADES:]
    return {
        "lite": True,
        "personas": [{k: p.get(k) for k in ("id", "name", "color")} for p in book.personas],
        "markets": [lite_market(book.market_view(q)) for q in book.markets],
        "recent_trades": [lite_trade(t) for t in recent],
        "seq": trades[-1]["seq"] if trades else 0,
        "fund": fund_view(book),
        "users": book.users(),
        "agents": sum(1 for tid, t in book.traders.items() if t["kind"] == "external" and not byo.is_byo(tid)),
        "human_models": sum(1 for tid in book.traders if byo.is_byo(tid)),
        "ledger_head": {"seq": book.ledger_head[0], "hash": book.ledger_head[1]},
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
        {"method": "POST", "path": "/harvest/api/agents/register", "body": {"name": "Drought Bear", "world_model": "Heat waves in southern Europe cut olive and durum yields; prices lag the weather by a season.", "owner": "team-x", "username": "drought-bear"},
         "returns": "agent_id, username (optional in the body: unique, 3-24 of a-z 0-9 - _; default pilot-xxxx), token (keep it secret), credits, world_model_sha256",
         "rate_limit": "30/min per IP"},
        {"method": "GET", "path": "/harvest/api/agents/markets",
         "returns": "open markets: qid, title, kind, p_yes, b, series, visible_history (masked: months t-12..t0, prices indexed to 100), target_index, closes"},
        {"method": "POST", "path": "/harvest/api/agents/bet", "body": {"token": "<token>", "qid": "<qid>", "side": "YES", "spend": 25, "p_yes": 0.7, "rationale": "short reason"},
         "returns": "trade, market, balance", "rate_limit": "120/min per IP", "limits": f"spend {SPEND_MIN:g}..{SPEND_MAX:g}, p_yes 0.01..0.99"},
        {"method": "POST", "path": "/harvest/api/forecast", "body": {"token": "<token>", "answers": [{"qid": "<holdout qid>", "p_yes": 0.6}]},
         "returns": "Brier on the sealed holdout vs the house world models (questions: GET /harvest/api/holdout)"},
        {"method": "GET", "path": "/harvest/api/agents/{agent_id}", "returns": "positions, PnL, Brier, world-model hash"},
        {"method": "GET", "path": "/harvest/api/markets/{qid}/agent",
         "returns": "how an agent sees one market: masked window (label, t-12..t0, index), masked question, the news lines it would get, "
                    "every agent's latest forecast (p_yes, rationale, model, timestamp) and the market's /agents/markets entry"},
        {"method": "GET", "path": "/harvest/api/leaderboard", "returns": "world_models (house, external and human-model), baselines, humans"},
        {"method": "POST", "path": "/harvest/api/byo", "body": {"name": "Heat first", "world_model": "<your theory, at most 1200 characters>"},
         "returns": "model_id, world_model_sha256, brier_holdout, rank vs the 8 house models and the market, per_question, rationales",
         "rate_limit": "6/min per IP", "note": "backtests your paragraph on the sealed holdout with the house forecaster; the same paragraph is never re-run"},
        {"method": "GET", "path": "/harvest/api/news?since=<id>&commodity=<name>&qid=<qid>&limit=50",
         "returns": "items (newest first): id, title, source, url, published_at, commodities, regions, qids (open markets it bears on), "
                    "summary and direction (up/down/neutral, LLM hint, may be null); latest_id. Poll with since=<latest_id> for new items.",
         "commodities": list(news.COMMODITIES), "note": "keyless RSS (Google News searches and agri trade feeds), refreshed every ~10 min"},
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


def agent_market_row(book: LiveBook, qid: str, fixture: dict[str, Any] | None) -> dict[str, Any]:
    """One market exactly as GET /harvest/api/agents/markets lists it."""
    m, q = book.markets[qid], book.meta.get(qid) or {}
    return {"qid": qid, "title": q.get("title", qid), "kind": q.get("kind", "hard"), "p_yes": book.price(qid), "b": m["b"],
            "series": q.get("series"), "target": q.get("target"), "region": q.get("region"), "crop": q.get("crop"),
            "visible_history": _visible_history(q.get("series"), fixture, q.get("cutoff")),
            "target_index": _target_index(q.get("series"), fixture, q.get("cutoff"), q.get("target")), "closes": q.get("resolve_month")}


NEWS_PER_MARKET = 6
"""Headlines per market in the live re-forecast prompt (live_forecast.NEWS_PER_MARKET)."""


def agent_view(book: LiveBook, qid: str, fixture: dict[str, Any] | None, result: dict[str, Any] | None, news_lines: list[str]) -> dict[str, Any]:
    """How an agent sees one market: the masked price window (label, t-12..t0, index), the masked question,
    the news lines it would get, each agent's latest forecast, and its exact /agents/markets entry."""
    q = book.meta.get(qid) or {}
    series, cutoff = q.get("series"), q.get("cutoff")
    mask = (result or {}).get("mask") or {}
    label = (mask.get("commodities") or {}).get(series) if series else None
    window = _visible_history(series, fixture, cutoff)
    question = None
    if window and label and q.get("rule") and q.get("target") is not None and fixture:
        raw = forecaster.visible_window(fixture, series, cutoff)  # type: ignore[arg-type]
        question = forecaster._masked_question(q, label, [tuple(p) for p in window], float(raw[0][1]) or 1.0)  # type: ignore[arg-type]
    names = {p["id"]: p.get("name", p["id"]) for p in book.personas}
    models = {p["id"]: p.get("model") for p in book.personas}  # persona x model agents run on one registry model each
    latest: dict[str, dict[str, Any]] = {}
    for f in book.agent_forecasts.get(qid, []):
        old = latest.get(f["persona_id"])
        if old is None or str(f.get("at") or "") >= str(old.get("at") or ""):
            latest[f["persona_id"]] = f
    model = (result or {}).get("model")
    forecasts = [{"agent": pid, "username": accounts.system_username(pid), "name": names.get(pid, f.get("name", pid)), "p_yes": f["p_yes"],
                  "rationale": f.get("rationale", ""), "model": f.get("model") or models.get(pid) or model, "source": f.get("source"),
                  "timestamp": f.get("at") or (result or {}).get("generated_at")} for pid, f in latest.items()]
    forecasts.sort(key=lambda f: -float(f["p_yes"]))
    return {
        "qid": qid, "title": q.get("title", qid), "kind": q.get("kind", "hard"), "status": book.markets[qid]["status"],
        "masked": {"label": label, "cutoff_hidden": True, "window": window, "question": question,
                   "note": "Commodity names and calendar dates are hidden; months are relative to now (t0) and prices are an index, 100 at t-12."},
        "news": news_lines,
        "forecasts": forecasts,
        "agents_markets_entry": agent_market_row(book, qid, fixture) if book.markets[qid]["status"] == "open" else None,
    }


def _byo_view(book: LiveBook, model_id: str, *, cached: bool) -> dict[str, Any] | None:
    info, fc = book.traders.get(model_id), book.forecasts.get(model_id)
    if info is None or not fc or fc.get("origin") != byo.ORIGIN:
        return None
    keep = ("brier_holdout", "n", "rank", "of", "field", "per_question", "rationales", "source", "cutoffs", "model", "model_label")
    return {"model_id": model_id, "name": info.get("name"), "username": info.get("username"), "type": "agent", "owner": info.get("owner"),
            "origin": byo.ORIGIN, "world_model": info.get("world_model"),
            "world_model_sha256": info.get("world_model_sha256"), **{k: fc.get(k) for k in keep}, "scored_at": fc.get("at"), "cached": cached}


def leader_id(book: LiveBook, raw: Any) -> str:
    """The trader id behind what the UI sends: a trader id, an @username, or a house persona id (agent:<id> trades for it)."""
    target = str(raw or "").strip()
    if target.startswith("@") or (target and ":" not in target and target in book.usernames):
        return book.usernames.get(target.lstrip("@").lower(), target)
    if target and target not in book.traders and not target.startswith("agent:") and any(p["id"] == target for p in book.personas):
        return f"agent:{target}"
    return target


def follows_view(book: LiveBook, follower: str) -> list[dict[str, Any]]:
    rows = []
    for leader, s in book.follows.get(follower, {}).items():
        theirs = [t for t in book.trades if t["trader"] == leader]
        mine = [t for t in book.trades if t["trader"] == follower and t.get("copied_from") == leader]
        rows.append({"agent": leader, "name": book.display_name(leader), "username": book.username_of(leader), **s, "bets": len(theirs), "last_bet_at": theirs[-1]["at"] if theirs else None,
                     "copied": len(mine), "copied_spend": round(sum(t["spend"] for t in mine), 2)})
    return rows


def follow_feed(book: LiveBook, follower: str, limit: int) -> list[dict[str, Any]]:
    """Newest first: bets by the agents `follower` follows, and `follower`'s copies of them."""
    leaders = set(book.follows.get(follower, {}))
    out: list[dict[str, Any]] = []
    for t in reversed(book.trades):
        copied_from = t.get("copied_from")
        if t["trader"] in leaders or (t["trader"] == follower and copied_from):
            out.append({**t, "name": "You" if t["trader"] == follower else book.display_name(t["trader"]), "username": book.username_of(t["trader"]),
                        "title": (book.meta.get(t["qid"]) or {}).get("title", t["qid"]),
                        "copied_from_name": book.display_name(str(copied_from)) if copied_from else None})
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- routes

def register(app: FastAPI) -> None:  # noqa: C901 - one flat list of routes
    @app.get("/harvest/api/state")
    async def state(since: int | None = None, lite: bool = False) -> JSONResponse:
        ledger.anchorer.ensure_ticker()
        _ensure_house_agents()
        book = await asyncio.to_thread(_book)
        with book.lock:
            body = state_view_lite(book, since) if lite else state_view(book, since)
        return JSONResponse(body, headers=NO_STORE)

    @app.get("/harvest/api/markets")
    async def markets(lite: bool = False) -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            views = [book.market_view(q) for q in book.markets]
            return JSONResponse({"markets": [lite_market(v) for v in views] if lite else views}, headers=NO_STORE)

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
        nickname: str | None = None  # display name; the username stands in when none is given
        if data.get("nickname"):
            try:
                nickname = game.sanitise_nickname(data.get("nickname"))
            except game.InvalidInput as err:
                return _error(str(err), 422)
        token = secrets.token_urlsafe(18)
        book = await asyncio.to_thread(_book)
        try:
            entry, user_id, username = await asyncio.to_thread(record_account, book, kind="join", trader=None, wanted=data.get("username"),
                                                               payload={"nickname": nickname, "token_sha256": market.token_sha(token)})
        except accounts.UsernameError as err:
            return _error(str(err), 409 if isinstance(err, accounts.UsernameTakenError) else 422)
        _schedule_anchor()
        return JSONResponse({"user_id": user_id, "username": username, "type": "human", "nickname": nickname or username, "token": token,
                             "credits": book.balance(user_id), "users": book.users(), "entry": {"seq": entry["seq"], "hash": entry["hash"]}})

    @app.get("/harvest/api/me")
    async def me(token: str = "") -> JSONResponse:
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(token)
        if trader is None:
            return _error("unknown token; join first", 401)
        with book.lock:
            info = book.traders.get(trader, {})
            return JSONResponse({"user_id": trader, "username": info.get("username"), "type": accounts.account_type(info.get("kind")),
                                 "nickname": info.get("nickname") or info.get("name"), "balance": book.balance(trader),
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
        try:
            entry, _, username = await asyncio.to_thread(record_account, book, kind="agent", trader=agent_id, wanted=data.get("username"), payload={
                "name": name, "owner": owner, "world_model": world_model, "world_model_sha256": wm_sha, "token_sha256": market.token_sha(token)})
        except accounts.UsernameError as err:
            return _error(str(err), 409 if isinstance(err, accounts.UsernameTakenError) else 422)
        _schedule_anchor()
        return JSONResponse({"agent_id": agent_id, "username": username, "type": "agent", "token": token, "credits": book.balance(agent_id), "credits_have_no_value": True, "world_model_sha256": wm_sha,
                             "entry": {"seq": entry["seq"], "hash": entry["hash"]}, "next": "GET /harvest/api/agents/markets, then POST /harvest/api/agents/bet"})

    @app.get("/harvest/api/news")
    async def news_route(since: int | None = None, commodity: str = "", qid: str = "", limit: int = 50) -> JSONResponse:
        _ensure_house_agents()
        book = await asyncio.to_thread(_book)
        with book.lock:
            by_spec: dict[str, list[str]] = {}
            for q, m in book.markets.items():
                if m["status"] == "open":
                    by_spec.setdefault(str((book.meta.get(q) or {}).get("spec_id") or q.split("@")[0]), []).append(q)
            spec = str((book.meta.get(qid) or {}).get("spec_id") or qid.split("@")[0]) if qid else ""
        rows, latest = await asyncio.to_thread(news.query, since=since, commodity=commodity or None, spec=spec or None, limit=limit)
        items = [{k: r[k] for k in ("id", "title", "source", "url", "published_at", "commodities", "regions", "summary", "direction")}
                 | {"qids": [q for s in r["specs"] for q in by_spec.get(s, [])]} for r in rows]
        return JSONResponse({"items": items, "latest_id": latest, "poller": news.status()}, headers=NO_STORE)

    # ---- follow and copy (follow.py): play credits only
    @app.get("/harvest/api/follow")
    async def follow_list(token: str = "") -> JSONResponse:
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(token)
        if trader is None:
            return _error("unknown token; join first", 401)
        with book.lock:
            return JSONResponse({"following": follows_view(book, trader), "defaults": {"fraction": follow.DEFAULT_FRACTION, "cap": follow.DEFAULT_CAP},
                                 "credits_have_no_value": True}, headers=NO_STORE)

    @app.post("/harvest/api/follow")
    async def follow_toggle(request: Request) -> JSONResponse:
        if (limited := _limited(follow_limiter, request, "follow changes")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(str(data.get("token") or ""))
        if trader is None:
            return _error("unknown token; join first", 401)
        with book.lock:
            leader = leader_id(book, data.get("agent"))
            old = book.follows.get(trader, {}).get(leader) or {}
            copy = bool(data["copy"]) if "copy" in data else bool(old.get("copy"))
            on = bool(data.get("follow", True))
            try:
                entry = book.set_follow(trader, leader, on=on, copy=copy and on, fraction=data.get("fraction"), cap=data.get("cap"))
            except MarketError as err:
                return _error(str(err), 404 if "unknown agent" in str(err) else 422)
            row = next((r for r in follows_view(book, trader) if r["agent"] == leader), None)
        _schedule_anchor()
        return JSONResponse({"agent": leader, "following": row, "entry": {"seq": entry["seq"], "hash": entry["hash"]}}, headers=NO_STORE)

    @app.get("/harvest/api/follow/feed")
    async def follow_feed_route(token: str = "", limit: int = 30) -> JSONResponse:
        _ensure_house_agents()
        book = await asyncio.to_thread(_book)
        trader = book.trader_for(token)
        if trader is None:
            return _error("unknown token; join first", 401)
        with book.lock:
            return JSONResponse({"items": follow_feed(book, trader, max(1, min(limit, 200)))}, headers=NO_STORE)

    @app.get("/harvest/api/agents/markets")
    async def agents_markets() -> JSONResponse:
        book = await asyncio.to_thread(_book)
        fixture = await asyncio.to_thread(price_fixture)
        with book.lock:
            out = [agent_market_row(book, qid, fixture) for qid, m in book.markets.items() if m["status"] == "open"]
        return JSONResponse({"markets": out, "at": time.time()}, headers=NO_STORE)

    @app.get("/harvest/api/markets/{qid}/agent")
    async def agent_view_route(qid: str) -> JSONResponse:
        book = await asyncio.to_thread(_book)
        if qid not in book.markets:
            return _error(f"unknown market {qid!r}", 404)
        fixture = await asyncio.to_thread(price_fixture)
        result = await asyncio.to_thread(replay_result)
        spec = str((book.meta.get(qid) or {}).get("spec_id") or qid.split("@")[0])
        lines = (await asyncio.to_thread(news.headlines_for, [spec], NEWS_PER_MARKET)).get(spec, [])
        with book.lock:
            return JSONResponse(agent_view(book, qid, fixture, result, lines), headers=NO_STORE)

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
            return JSONResponse({"agent_id": agent_id, "username": info.get("username"), "type": "agent", "name": info.get("name"),
                                 "owner": info.get("owner"), "world_model": info.get("world_model"),
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
            await asyncio.to_thread(record_account, book, kind="agent", trader=model_id, wanted=None, payload={
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

    @app.post("/harvest/api/waitlist")
    async def waitlist_add(request: Request) -> JSONResponse:
        if (limited := _limited(waitlist_limiter, request, "sign-ups")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        contact = data.get("contact") or data.get("email") or data.get("handle")
        try:
            out = await asyncio.to_thread(waitlist.add, contact, data.get("note"))
        except waitlist.WaitlistError as err:
            return _error(str(err), 422)
        return JSONResponse({"ok": True, **out}, headers=NO_STORE)

    @app.get("/harvest/api/waitlist/count")
    async def waitlist_count() -> JSONResponse:
        book = await asyncio.to_thread(_book)
        n = await asyncio.to_thread(waitlist.count)
        with book.lock:
            return JSONResponse({"count": n, "waitlist": n, "pilots": pilots(book)}, headers=NO_STORE)

    @app.get("/harvest/api/byo/{model_id}")
    async def byo_detail(model_id: str) -> JSONResponse:
        book = await asyncio.to_thread(_book)
        with book.lock:
            view = _byo_view(book, model_id, cached=True)
        return JSONResponse(view, headers=NO_STORE) if view is not None else _error(f"unknown model {model_id!r}", 404)
