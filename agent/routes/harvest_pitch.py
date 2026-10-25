"""Live audience market for the pitch: /pitch (projector) and /vote (phone).

GET  /pitch                      projector page: QR to /vote, the featured market's live chart, human vs swarm bet feed
GET  /vote                       phone page: a guest handle and PITCH_CREDITS play credits, YES/NO on the featured market
                                 and on a short list of headline events (EVENTS: heat, drought, wheat, olive oil, water, EU policy)
GET  /pitch/qr.svg, /pitch/qr.png   QR code of PITCH_VOTE_URL (default https://crophecy.bid/vote)
GET  /pitch/api/state?minutes=   featured market, price path, human vs bot tallies, the headline events with their own human
                                 vs bot tallies, and a feed of the latest bets across all of them tagged human or bot
POST /vote/api/guest {token?}    a guest account (no signup); with a known guest token it returns that guest instead
POST /vote/api/bet {token, side, spend, qid?}   a guest bet on the featured market or one of the events (qid), rate limited
                                 per device (guest token) and per IP; returns "vs", the guest's position next to the swarm's
POST /pitch/api/featured {qid}   admin only (X-Admin-Token): picks the featured market

Guests are ordinary human accounts in the hash-chained ledger (username guest-xxxx, payload via=vote). Their pitch wallet
is PITCH_CREDITS: /vote lets a guest spend at most that much in total across all events, while the book keeps the usual account so the
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

EVENTS: tuple[tuple[str, str, str, str], ...] = (
    ("wheat-up10-6m", "\U0001f33e", "Wheat price", "Will wheat cost over 10% more by {by}?"),
    ("eu-cap-heat-2028", "\U0001f525", "Heat", "Will the EU's next farm budget set aside \u20ac10 bn for heat and drought?"),
    ("it-drought-2027", "\U0001f3dc\ufe0f", "Drought", "Will Italy declare a national drought emergency in 2027?"),
    ("olive-up15-6m", "\U0001fad2", "Olive oil", "Will olive oil cost over 15% more by {by}?"),
    ("gr-water-rationing-2027", "\U0001f6b0", "Water", "Will Athens or a Greek island ration household water in 2027?"),
    ("eu-sovereign-land-2028", "\U0001f1ea\U0001f1fa", "EU policy", "Will an EU country stop foreign state funds buying its farmland by 2028?"),
)
"""Headline events on /vote and /pitch: (qid or qid prefix before '@cutoff', icon, tag, the question in plain words)."""
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


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


def _by(resolve_month: Any) -> str:
    try:
        y, m = str(resolve_month).split("-")[:2]
        return f"{_MONTHS[int(m) - 1]} {y}"
    except (ValueError, IndexError):
        return "the deadline"


def _match(book: LiveBook, key: str) -> str | None:
    """The open market for an EVENTS key: the exact qid, else the latest open cutoff of 'key@...'."""
    if key in book.markets and book.markets[key]["status"] == "open":
        return key
    hits = sorted(q for q, m in book.markets.items() if q.startswith(key + "@") and m["status"] == "open")
    return hits[-1] if hits else None


def events(book: LiveBook) -> list[dict[str, Any]]:
    """The featured market first, then the headline EVENTS that are open (each once): qid, icon, tag and plain question."""
    featured = featured_qid(book)
    out: list[dict[str, Any]] = []
    with book.lock:
        for key, icon, tag, plain in EVENTS:
            qid = _match(book, key)
            if qid and all(e["qid"] != qid for e in out):
                meta = book.meta.get(qid) or {}
                out.append({"qid": qid, "icon": icon, "tag": tag, "plain": plain.format(by=_by(meta.get("resolve_month")))})
        if featured:
            hit = next((e for e in out if e["qid"] == featured), None)
            if hit is None:
                hit = {"qid": featured, "icon": "\u2b50", "tag": "Featured", "plain": (book.meta.get(featured) or {}).get("title", featured)}
            else:
                out.remove(hit)
            out.insert(0, {**hit, "featured": True})
    return out


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
    evs = events(book)
    tags = {e["qid"]: e for e in evs}
    with book.lock:
        key = (qid, len(book.trades), book.version, minutes)
        if _state_cache.get("key") == key:
            return _state_cache["body"]
        horizon = time.time() * 1000 - minutes * 60_000
        path: list[list[Any]] = []
        feed: list[dict[str, Any]] = []
        tallies = {q: {w: {"bets": 0, "credits": 0.0, "yes_credits": 0.0, "traders": set()} for w in ("human", "bot")} for q in tags}
        tally = tallies[qid]
        for t in reversed(book.trades):
            if t["qid"] not in tags:
                continue
            who = _who(book, t["trader"])
            s = tallies[t["qid"]][who]
            s["bets"] += 1
            s["credits"] += t["spend"]
            s["yes_credits"] += t["spend"] if t["side"] == "YES" else 0.0
            s["traders"].add(t["trader"])
            if len(feed) < FEED_LEN:
                ev = tags[t["qid"]]
                feed.append({"seq": t["seq"], "at": t.get("at"), "who": who, "name": book.display_name(t["trader"]).removeprefix("\U0001f916").strip(),
                             "username": book.username_of(t["trader"]), "side": t["side"], "spend": round(t["spend"], 2),
                             "price_after": round(t["price_after"], 4), "qid": t["qid"], "icon": ev["icon"], "tag": ev["tag"]})
            if t["qid"] != qid:
                continue
            ms = _epoch_ms(str(t.get("at") or ""))
            if ms is not None and ms >= horizon:
                path.append([ms, round(t["price_after"], 4), "h" if who == "human" else "b"])
        path.reverse()
        view = book.market_view(qid)

        def summary(s: dict[str, Any]) -> dict[str, Any]:
            return {"bets": s["bets"], "credits": round(s["credits"], 2), "traders": len(s["traders"]),
                    "yes_share": round(s["yes_credits"] / s["credits"], 4) if s["credits"] else None}

        event_rows = [{**e, "p_yes": round(book.price(e["qid"]), 4), "ai_p": _ai_p(book, e["qid"]), "url": f"/app/market/{e['qid']}",
                       "human": summary(tallies[e["qid"]]["human"]), "bot": summary(tallies[e["qid"]]["bot"])} for e in evs]
        body = {
            "vote_url": vote_url(), "credits": PITCH_CREDITS,
            "featured": {k: view.get(k) for k in ("qid", "title", "kind", "region", "crop", "p_yes", "volume", "resolve_month")},
            "path": path, "feed": feed, "now": int(time.time() * 1000),
            "tally": {w: summary(s) for w, s in tally.items()}, "events": event_rows,
        }
        _state_cache.update(key=key, body=body)
        return body


def _ai_p(book: LiveBook, qid: str) -> float | None:
    """The swarm's average forecast of YES on a market (caller holds book.lock)."""
    ps = [float(f["p_yes"]) for f in book.agent_forecasts.get(qid, []) if f.get("p_yes") is not None]
    return round(sum(ps) / len(ps), 4) if ps else None


def you_vs_bots(book: LiveBook, trader: str, qid: str) -> dict[str, Any]:
    """The guest's credits on YES and NO in one market next to the bots': their credit split and average forecast."""
    with book.lock:
        you = {"YES": 0.0, "NO": 0.0}
        bots = {"YES": 0.0, "NO": 0.0}
        n_bots: set[str] = set()
        for t in book.trades:
            if t["qid"] != qid:
                continue
            if t["trader"] == trader:
                you[t["side"]] += t["spend"]
            elif _who(book, t["trader"]) == "bot":
                bots[t["side"]] += t["spend"]
                n_bots.add(t["trader"])
        bot_total = bots["YES"] + bots["NO"]
        return {"qid": qid, "p_yes": round(book.price(qid), 4), "url": f"/app/market/{qid}",
                "you": {"yes": round(you["YES"], 2), "no": round(you["NO"], 2)},
                "bots": {"agents": len(n_bots), "yes_share": round(bots["YES"] / bot_total, 4) if bot_total else None, "ai_p": _ai_p(book, qid)}}


def guest_spent(book: LiveBook, trader: str) -> float:
    with book.lock:
        return round(sum(t["spend"] for t in book.trades if t["trader"] == trader), 2)


def guest_view(book: LiveBook, trader: str, qid: str) -> dict[str, Any]:
    with book.lock:
        info = book.traders.get(trader) or {}
        everything = [t for t in book.trades if t["trader"] == trader]
        mine = [{"seq": t["seq"], "qid": t["qid"], "side": t["side"], "spend": round(t["spend"], 2), "price_after": round(t["price_after"], 4)}
                for t in everything if t["qid"] == qid]
        pos = next((p for p in book.positions(trader) if p.get("qid") == qid), None)
        per_qid: dict[str, dict[str, float]] = {}
        for t in everything:
            row = per_qid.setdefault(t["qid"], {"yes": 0.0, "no": 0.0})
            row[t["side"].lower()] = round(row[t["side"].lower()] + t["spend"], 2)
    spent = sum(t["spend"] for t in everything)
    return {"user_id": trader, "username": info.get("username"), "credits_left": round(max(PITCH_CREDITS - spent, 0.0), 2),
            "credits": PITCH_CREDITS, "bets": mine[-10:], "n_bets": len(everything), "position": pos, "mine": per_qid}


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
        allowed = [e["qid"] for e in await asyncio.to_thread(events, book)]
        qid = str(data.get("qid") or "") or (allowed[0] if allowed else "")
        if qid not in allowed:
            return api._error(f"{qid!r} is not one of the /vote events", 422)
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
                             "market": {"qid": qid, "p_yes": mv["p_yes"]}, "vs": you_vs_bots(book, trader, qid),
                             **guest_view(book, trader, qid)}, headers=NO_STORE)


def _page(app: FastAPI, path: str, name: str) -> None:
    @app.get(path, response_class=HTMLResponse, name=f"pitch-{name}")
    async def page() -> HTMLResponse:
        return HTMLResponse(versioned((STATIC_DIR / name).read_text()), headers={"Cache-Control": "no-cache"})
