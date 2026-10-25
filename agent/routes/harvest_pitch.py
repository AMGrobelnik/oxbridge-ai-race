"""Live audience market for the pitch: /pitch (projector) and /vote (phone).

GET  /pitch                      projector page: QR to /vote, the featured market's live chart, human vs swarm bet feed
GET  /vote                       phone page: a guest handle and PITCH_CREDITS play credits, YES/NO on the featured market
GET  /pitch/qr.svg, /pitch/qr.png   QR code of PITCH_VOTE_URL (default https://crophecy.bid/vote)
GET  /pitch/api/state?minutes=   featured market, price path, feed of the latest bets tagged human or bot, human vs bot tallies
POST /vote/api/guest {token?}    a guest account (no signup); with a known guest token it returns that guest instead
POST /vote/api/bet {token, side, spend}   a guest bet on the featured market, rate limited per device (guest token) and per IP
POST /pitch/api/featured {qid}   admin only (X-Admin-Token): picks the featured market

Guests are ordinary human accounts in the hash-chained ledger (username guest-xxxx, payload via=vote). Their pitch wallet
is PITCH_CREDITS: /vote lets a guest spend at most that much in total, while the book keeps the usual account so the
leaderboards' PnL stays comparable. The swarm (agent/harvest/swarm.py) bets on every open market through
/harvest/api/agents/bet, so the featured market already gets its bets; the default is the busiest one, wheat.
"""

import asyncio
import io
import json
import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agent.challenge.ratelimit import RateLimiter, client_ip
from agent.harvest import accounts, ledger, market
from agent.harvest.market import LiveBook, MarketError
from agent.routes import harvest as api
from agent.routes.harvest_pages import STATIC_DIR, versioned
from agent.routes.public_mode import admin_denial

DEFAULT_QID = "wheat-up10-6m@2026-09"
PITCH_CREDITS = 100.0
SPEND_MIN, SPEND_MAX = 1.0, 50.0
FEED_LEN = 40
NO_STORE = {"Cache-Control": "no-store"}

guest_limiter = RateLimiter(int(os.environ.get("PITCH_GUESTS_PER_MIN") or 60))
"""New guests per IP per minute (a venue NAT puts a room behind one IP)."""
device_bet_limiter = RateLimiter(int(os.environ.get("PITCH_BETS_PER_DEVICE_MIN") or 10))
"""Bets per guest (one guest token per device, kept in the phone's localStorage) per minute."""
ip_bet_limiter = RateLimiter(int(os.environ.get("PITCH_BETS_PER_IP_MIN") or 400))
LIMITERS = (guest_limiter, device_bet_limiter, ip_bet_limiter)
_state_cache: dict[str, Any] = {}


def vote_url() -> str:
    return (os.environ.get("PITCH_VOTE_URL") or "https://crophecy.bid/vote").strip()


def _featured_file() -> Path:
    return ledger.db_path().parent / "pitch_featured.json"


def featured_qid(book: LiveBook) -> str:
    """The admin's pick if it is still an open market, else PITCH_QID, else wheat, else the first open market."""
    picks: list[str] = []
    try:
        picks.append(str(json.loads(_featured_file().read_text())["qid"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    picks += [os.environ.get("PITCH_QID", "").strip(), DEFAULT_QID]
    with book.lock:
        for qid in picks:
            if qid and qid in book.markets and book.markets[qid]["status"] == "open":
                return qid
        return next((q for q, m in book.markets.items() if m["status"] == "open"), "")


def _who(book: LiveBook, trader: str) -> str:
    return "human" if (book.traders.get(trader) or {}).get("kind") == "human" else "bot"


def _epoch_ms(at: str) -> int | None:
    try:
        return int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def pitch_state(book: LiveBook, minutes: float = 20.0) -> dict[str, Any]:
    """The featured market, its price path over the last `minutes`, the latest bets and human vs bot tallies."""
    qid = featured_qid(book)
    if not qid:
        return {"featured": None, "vote_url": vote_url()}
    with book.lock:
        key = (qid, len(book.trades), book.version, minutes)
        if _state_cache.get("key") == key:
            return _state_cache["body"]
        horizon = time.time() * 1000 - minutes * 60_000
        path: list[list[Any]] = []
        feed: list[dict[str, Any]] = []
        tally = {w: {"bets": 0, "credits": 0.0, "yes_credits": 0.0, "traders": set()} for w in ("human", "bot")}
        for t in reversed(book.trades):
            if t["qid"] != qid:
                continue
            who = _who(book, t["trader"])
            s = tally[who]
            s["bets"] += 1
            s["credits"] += t["spend"]
            s["yes_credits"] += t["spend"] if t["side"] == "YES" else 0.0
            s["traders"].add(t["trader"])
            ms = _epoch_ms(str(t.get("at") or ""))
            if ms is not None and ms >= horizon:
                path.append([ms, round(t["price_after"], 4), "h" if who == "human" else "b"])
            if len(feed) < FEED_LEN:
                feed.append({"seq": t["seq"], "at": t.get("at"), "who": who, "name": book.display_name(t["trader"]).removeprefix("\U0001f916").strip(),
                             "username": book.username_of(t["trader"]), "side": t["side"], "spend": round(t["spend"], 2),
                             "price_after": round(t["price_after"], 4)})
        path.reverse()
        view = book.market_view(qid)
        body = {
            "vote_url": vote_url(), "credits": PITCH_CREDITS,
            "featured": {k: view.get(k) for k in ("qid", "title", "kind", "region", "crop", "p_yes", "volume", "resolve_month")},
            "path": path, "feed": feed, "now": int(time.time() * 1000),
            "tally": {w: {"bets": s["bets"], "credits": round(s["credits"], 2), "traders": len(s["traders"]),
                          "yes_share": round(s["yes_credits"] / s["credits"], 4) if s["credits"] else None} for w, s in tally.items()},
        }
        _state_cache.update(key=key, body=body)
        return body


def guest_spent(book: LiveBook, trader: str) -> float:
    with book.lock:
        return round(sum(t["spend"] for t in book.trades if t["trader"] == trader), 2)


def guest_view(book: LiveBook, trader: str, qid: str) -> dict[str, Any]:
    with book.lock:
        info = book.traders.get(trader) or {}
        mine = [{"seq": t["seq"], "side": t["side"], "spend": round(t["spend"], 2), "price_after": round(t["price_after"], 4)}
                for t in book.trades if t["trader"] == trader and t["qid"] == qid]
        pos = next((p for p in book.positions(trader) if p.get("qid") == qid), None)
    spent = sum(b["spend"] for b in mine)
    return {"user_id": trader, "username": info.get("username"), "credits_left": round(max(PITCH_CREDITS - spent, 0.0), 2),
            "credits": PITCH_CREDITS, "bets": mine[-10:], "n_bets": len(mine), "position": pos}


def _new_guest(book: LiveBook, token: str) -> tuple[str, str]:
    for i in range(12):
        name = f"guest-{secrets.token_hex(2 if i < 8 else 3)}"
        try:
            _, trader, username = api.record_account(book, kind="join", trader=None, wanted=name,
                                                     payload={"nickname": name, "token_sha256": market.token_sha(token), "via": "vote"})
            return trader, username
        except accounts.UsernameTakenError:
            continue
    raise accounts.UsernameError("could not find a free guest name")


def _limited(limiter: RateLimiter, key: str, what: str) -> JSONResponse | None:
    wait = limiter.hit(key)
    if wait:
        return JSONResponse({"error": f"Too many {what}; try again in {int(wait) + 1} s."}, status_code=429,
                            headers={"Retry-After": str(int(wait) + 1)})
    return None


def qr_png(url: str) -> bytes:
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=16, border=2, dark="#0b0d12", light="#ffffff")
    return buf.getvalue()


def register(app: FastAPI) -> None:
    for path, name in (("/pitch", "pitch.html"), ("/vote", "vote.html")):
        _page(app, path, name)

    @app.get("/pitch/qr.svg", response_model=None)
    async def qr_svg() -> Response:
        from agent.routes.challenge import _qr_svg

        try:
            return Response(_qr_svg(vote_url()), media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})
        except ImportError:
            return JSONResponse({"error": "QR codes need the segno package (requirements.txt)."}, status_code=501)

    @app.get("/pitch/qr.png", response_model=None)
    async def qr_png_route() -> Response:
        try:
            return Response(qr_png(vote_url()), media_type="image/png", headers={"Cache-Control": "no-cache"})
        except ImportError:
            return JSONResponse({"error": "QR codes need the segno package (requirements.txt)."}, status_code=501)

    @app.get("/pitch/api/state")
    async def state(minutes: float = 20.0) -> JSONResponse:
        book = await asyncio.to_thread(api._book)
        body = await asyncio.to_thread(pitch_state, book, min(max(minutes, 1.0), 240.0))
        return JSONResponse(body, headers=NO_STORE)

    @app.post("/pitch/api/featured")
    async def set_featured(request: Request) -> JSONResponse:
        if (denied := admin_denial(request)) is not None:
            return denied
        data, bad = await api._body(request)
        if bad is not None or data is None:
            return bad or api._error("Bad request.", 400)
        book = await asyncio.to_thread(api._book)
        qid = str(data.get("qid") or "")
        if qid not in book.markets or book.markets[qid]["status"] != "open":
            return api._error(f"no open market {qid!r}", 404)
        _featured_file().write_text(json.dumps({"qid": qid}))
        return JSONResponse({"qid": featured_qid(book)})

    @app.post("/vote/api/guest")
    async def guest(request: Request) -> JSONResponse:
        data, bad = await api._body(request)
        if bad is not None or data is None:
            return bad or api._error("Bad request.", 400)
        book = await asyncio.to_thread(api._book)
        qid = featured_qid(book)
        token = str(data.get("token") or "")
        trader = book.trader_for(token) if token else None
        if trader is None:
            if (limited := _limited(guest_limiter, client_ip(request), "new guests")) is not None:
                return limited
            token = secrets.token_urlsafe(18)
            try:
                trader, _ = await asyncio.to_thread(_new_guest, book, token)
            except accounts.UsernameError as err:
                return api._error(str(err), 503)
            api._schedule_anchor()
        return JSONResponse({"token": token, **guest_view(book, trader, qid)}, headers=NO_STORE)

    @app.post("/vote/api/bet")
    async def bet(request: Request) -> JSONResponse:
        data, bad = await api._body(request)
        if bad is not None or data is None:
            return bad or api._error("Bad request.", 400)
        token = str(data.get("token") or "")
        if not token:
            return api._error("no guest token; reload /vote", 401)
        for limiter, key in ((ip_bet_limiter, client_ip(request)), (device_bet_limiter, market.token_sha(token))):
            if (limited := _limited(limiter, key, "bets")) is not None:
                return limited
        book = await asyncio.to_thread(api._book)
        trader = book.trader_for(token)
        if trader is None:
            return api._error("unknown guest; reload /vote", 401)
        side, spend = api._side(data.get("side")), api._num(data.get("spend"))
        if side is None:
            return api._error("side must be YES or NO", 422)
        if spend is None:
            return api._error("spend must be a number", 422)
        qid = featured_qid(book)
        left = PITCH_CREDITS - await asyncio.to_thread(guest_spent, book, trader)
        spend = round(min(market.clamp(spend, SPEND_MIN, SPEND_MAX), left), 2)
        if spend < SPEND_MIN:
            return api._error(f"you have used your {PITCH_CREDITS:.0f} pitch credits", 422)
        try:
            trade, _ = await asyncio.to_thread(book.place_bet, trader, qid, side, spend)  # type: ignore[arg-type]
        except MarketError as err:
            return api._error(str(err), 422)
        api._schedule_anchor()
        api._ensure_house_agents()
        with book.lock:
            mv = book.market_view(qid)
        return JSONResponse({"trade": {k: trade.get(k) for k in ("seq", "side", "spend", "price_before", "price_after")},
                             "market": {"qid": qid, "p_yes": mv["p_yes"]}, **guest_view(book, trader, qid)}, headers=NO_STORE)


def _page(app: FastAPI, path: str, name: str) -> None:
    @app.get(path, response_class=HTMLResponse, name=f"pitch-{name}")
    async def page() -> HTMLResponse:
        return HTMLResponse(versioned((STATIC_DIR / name).read_text()), headers={"Cache-Control": "no-cache"})
