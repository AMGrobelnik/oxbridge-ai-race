"""Beat the Seal: the public challenge page, its JSON API, the projector board, QR and verify.

GET  /challenge                 phone page          GET /challenge/board   projector view
GET  /challenge/qr              QR page             GET /challenge/qr.svg  QR image of PUBLIC_BASE_URL/challenge
GET  /challenge/verify          recomputes the hash chain and checks every batch anchor
GET  /challenge/api/config      params schema, baseline scores, public URL
GET  /challenge/api/stats       counters, leaderboard, overfit wall, recent entries, anchor status
GET  /challenge/api/ledger      every chain entry and anchor (for independent re-hashing)
POST /challenge/api/score       {"params"} -> visible (dev) score
POST /challenge/api/parse       {"sentence", "params"?} -> params from one sentence (Gemini)
POST /challenge/api/submit      {"nickname", "params"} -> sealed reveal, verdict, chain entry
"""

import asyncio
import io
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from agent.challenge import game, ledger
from agent.challenge.ratelimit import RateLimiter, client_ip

STATIC_DIR = Path(__file__).resolve().parent.parent / "challenge" / "static"
MAX_BODY = 4096

submit_limiter = RateLimiter(int(os.environ.get("CHALLENGE_SUBMITS_PER_MIN") or 20))
parse_limiter = RateLimiter(int(os.environ.get("CHALLENGE_PARSES_PER_MIN") or 6))
score_limiter = RateLimiter(240)
global_submit_limiter = RateLimiter(600)
_background: set[asyncio.Task[Any]] = set()


def _error(message: str, status: int, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def _limited(limiter: RateLimiter, request: Request, what: str) -> JSONResponse | None:
    wait = limiter.hit(client_ip(request))
    if not wait and limiter is submit_limiter:
        wait = global_submit_limiter.hit("all")
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


def public_base_url(request: Request) -> str:
    return (os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)).strip().rstrip("/")


def _qr_svg(url: str, *, dark: str = "#0b0d12", light: str = "#ffffff") -> str:
    import segno

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=10, border=2, dark=dark, light=light, xmldecl=False, svgns=True, omitsize=True)
    return buf.getvalue().decode("utf-8")


def _schedule_anchor() -> None:
    ledger.anchorer.ensure_ticker()
    task = asyncio.get_running_loop().create_task(ledger.anchorer.maybe_anchor())
    _background.add(task)
    task.add_done_callback(_background.discard)


def _board_state() -> dict[str, Any]:
    return {
        "stats": ledger.stats(),
        "leaderboard": ledger.leaderboard(),
        "overfit_wall": ledger.overfit_wall(),
        "recent": ledger.recent(),
        "anchor_every": ledger.anchorer.every,
        "anchor_interval_s": ledger.anchorer.interval_s,
        "anchoring": ledger.anchoring_enabled(),
    }


def register(app: FastAPI) -> None:
    app.mount("/challenge/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="challenge-static")

    @app.get("/challenge", response_model=None)
    async def page() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/challenge/board", response_model=None)
    async def board() -> FileResponse:
        return FileResponse(STATIC_DIR / "board.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/challenge/qr.svg", response_model=None)
    async def qr_svg(request: Request) -> Response:
        try:
            svg = _qr_svg(f"{public_base_url(request)}/challenge")
        except ImportError:
            return _error("QR codes need the segno package (requirements.txt).", 501)
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "no-cache"})

    @app.get("/challenge/qr", response_model=None)
    async def qr_page(request: Request) -> Response:
        url = f"{public_base_url(request)}/challenge"
        try:
            svg = _qr_svg(url)
        except ImportError:
            return _error("QR codes need the segno package (requirements.txt).", 501)
        html = (STATIC_DIR / "qr.html").read_text(encoding="utf-8")
        safe_url = url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        return HTMLResponse(html.replace("{{QR_SVG}}", svg).replace("{{URL}}", safe_url), headers={"Cache-Control": "no-cache"})

    @app.get("/challenge/verify")
    async def verify(rescore: int = 0, onchain: int = 0) -> JSONResponse:
        report = await asyncio.to_thread(ledger.verify, rescore_last=max(0, min(rescore, 200)))
        if onchain:
            confirmed = [a for a in report["anchors"] if a["status"] == "confirmed" and a["signature"]][-3:]
            for a in confirmed:
                onchain_memo = await ledger.chain.fetch_memo(a["signature"])
                a["memo_onchain"] = onchain_memo
                a["onchain_match"] = onchain_memo == a["memo"] if onchain_memo is not None else None
                if onchain_memo is not None and onchain_memo != a["memo"]:
                    report["ok"] = False
                    report["reasons"].append(f"on-chain memo for count {a['count']} differs")
        return JSONResponse(report)

    @app.get("/challenge/api/config")
    async def config(request: Request) -> JSONResponse:
        return JSONResponse({
            **game.schema(),
            "baseline_scores": game.baseline_scores(),
            "public_url": f"{public_base_url(request)}/challenge",
            "gate": game.environment().spec["gate"],
            "shift_labels": game.shift_labels(),
        })

    @app.get("/challenge/api/stats")
    async def stats() -> JSONResponse:
        ledger.anchorer.ensure_ticker()
        return JSONResponse(await asyncio.to_thread(_board_state), headers={"Cache-Control": "no-store"})

    @app.get("/challenge/api/ledger")
    async def full_ledger() -> JSONResponse:
        entries = await asyncio.to_thread(ledger.all_entries)
        for e in entries:
            e.pop("nickname", None)  # the chain commits to nick hashes only
        return JSONResponse({"genesis": ledger.GENESIS, "entries": entries, "anchors": ledger.anchors()})

    @app.post("/challenge/api/score")
    async def score(request: Request) -> JSONResponse:
        if (limited := _limited(score_limiter, request, "score requests")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        try:
            params = game.validate_params(data.get("params"))
        except game.InvalidInput as err:
            return _error(str(err), 422)
        return JSONResponse({"params": params, **game.dev_report(params)})

    @app.post("/challenge/api/parse")
    async def parse(request: Request) -> JSONResponse:
        if (limited := _limited(parse_limiter, request, "sentence translations")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        try:
            sentence = game.clean_sentence(data.get("sentence"))
            base = game.validate_params(data["params"]) if data.get("params") is not None else game.environment().baseline()
        except game.InvalidInput as err:
            return _error(str(err), 422)
        out = await game.params_from_sentence(sentence, base)
        return JSONResponse({**out, **game.dev_report(out["params"])})

    @app.post("/challenge/api/submit")
    async def submit(request: Request) -> JSONResponse:
        if (limited := _limited(submit_limiter, request, "submissions")) is not None:
            return limited
        data, bad = await _body(request)
        if bad is not None or data is None:
            return bad or _error("Bad request.", 400)
        try:
            nickname = game.sanitise_nickname(data.get("nickname"))
            params = game.validate_params(data.get("params"))
        except game.InvalidInput as err:
            return _error(str(err), 422)
        result = await asyncio.to_thread(game.reveal, params)
        entry = await asyncio.to_thread(
            ledger.append, nickname=nickname, nick_sha256=game.nick_sha256(nickname), params=params, result=result
        )
        _schedule_anchor()
        rank = await asyncio.to_thread(ledger.rank_of, entry["seq"]) if result["verdict"] == "GENERALISES" else None
        return JSONResponse({"nickname": nickname, "params": params, "result": result, "entry": entry, "rank": rank, "stats": ledger.stats()})
