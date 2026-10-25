"""The farmer assistant on WhatsApp and in the app (agent/harvest/farmer_chat.py answers all of them).

Answers come from the grounded farm advisor (agent/harvest/advisor.py).
POST /harvest/api/farmer/chat {message, session?} -> {reply, intents, crop, region, probability, confidence, link, source,
  model, latency_ms}: the in-app chat; `session` keys the short follow-up memory.
POST /harvest/api/whatsapp/twilio: Twilio WhatsApp webhook (form POST), answers with TwiML when the answer is ready within
  WHATSAPP_INLINE_S (default 9 s; the advisor asks only thinking-off Flash-Lite here); otherwise it replies "Thinking..."
  at once and sends the full answer through the Twilio REST API to the same sender (never anyone else), in a held task
  with 3 attempts. If the advisor fails outright the farmer still gets the deterministic answer, never nothing. When TWILIO_AUTH_TOKEN is
  set, the X-Twilio-Signature header must match the public URL (TWILIO_WEBHOOK_URL, the forwarded scheme and host,
  PUBLIC_BASE_URL or https://crophecy.bid), not the local one cloudflared forwards to.
GET  /harvest/api/whatsapp/meta: Meta WhatsApp Cloud API verify handshake against WHATSAPP_VERIFY_TOKEN.
POST /harvest/api/whatsapp/meta: Meta message webhook. Checks X-Hub-Signature-256 when WHATSAPP_APP_SECRET is set and
  sends the answers through the Graph API only when WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are both set.
GET  /app/farmer/chat: the in-app chat page.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

from agent.challenge.ratelimit import RateLimiter
from agent.harvest import advisor, farmer_chat
from agent.routes import harvest as api

PAGE = Path(__file__).resolve().parent.parent / "harvest" / "static" / "app" / "farmer-chat.html"
GRAPH_URL = "https://graph.facebook.com/v21.0/{phone_id}/messages"
TWILIO_MESSAGES_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
THINKING = "Thinking… (a few seconds)"
MAX_FORM = 16_000
NO_STORE = {"Cache-Control": "no-store"}
logger = logging.getLogger("uvicorn.error.whatsapp")  # a uvicorn.error child, so INFO lines land in the server log
SEND_ATTEMPTS = 3
SEND_TIMEOUT_S = 20.0
SORRY = "Sorry, I couldn't work that out just now. Please ask again in a minute."


chat_limiter = RateLimiter(int(os.environ.get("FARMER_CHAT_PER_MIN") or 300))
sender_limiter = RateLimiter(int(os.environ.get("WHATSAPP_PER_MIN") or 10))
_background: set[asyncio.Task[None]] = set()


def twiml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(text, quote=False)}</Message></Response>'


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64 HMAC-SHA1 of the URL followed by each sorted POST key and value."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


PUBLIC_HOSTS = ("https://crophecy.bid",)


def _candidate_urls(request: Request) -> list[str]:
    """Every URL Twilio may have signed: behind cloudflared the server sees http://127.0.0.1:3001, Twilio signed the public one."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    h = request.headers
    proto = (h.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (h.get("x-forwarded-host") or h.get("host") or "").split(",")[0].strip()
    urls = [u.strip() for u in os.environ.get("TWILIO_WEBHOOK_URL", "").split(",") if u.strip()]
    if host:
        urls.append(f"{proto}://{host}{path}")
    bases = [*PUBLIC_HOSTS, os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")]
    urls += [base + path for base in bases if base]
    urls.append(str(request.url))
    return list(dict.fromkeys(urls))


def twilio_ok(request: Request, params: dict[str, str]) -> bool:
    token = os.environ.get("TWILIO_AUTH_TOKEN", "")
    if not token:
        return True
    given = request.headers.get("x-twilio-signature", "")
    return any(hmac.compare_digest(given, twilio_signature(token, url, params)) for url in _candidate_urls(request))


def meta_signature_ok(secret: str, body: bytes, header: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header, expected)


def meta_messages(data: Any) -> list[tuple[str, str]]:
    """(sender wa_id, text) for every text message in a Meta webhook payload."""
    out = []
    for entry in data.get("entry") or [] if isinstance(data, dict) else []:
        for change in entry.get("changes") or [] if isinstance(entry, dict) else []:
            value = change.get("value") or {} if isinstance(change, dict) else {}
            for msg in value.get("messages") or [] if isinstance(value, dict) else []:
                if isinstance(msg, dict) and msg.get("type") == "text" and msg.get("from"):
                    out.append((str(msg["from"]), str((msg.get("text") or {}).get("body") or "")))
    return out


async def meta_send(to: str, text: str) -> None:
    token, phone_id = os.environ.get("WHATSAPP_TOKEN", ""), os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    if not (token and phone_id):
        return
    body = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text, "preview_url": True}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(GRAPH_URL.format(phone_id=phone_id), json=body, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 400:
            logger.warning("WhatsApp Cloud API send failed with HTTP %s", resp.status_code)
    except httpx.HTTPError as exc:
        logger.warning("WhatsApp Cloud API send failed: %s", type(exc).__name__)


def inline_s() -> float:
    try:
        return float(os.environ.get("WHATSAPP_INLINE_S") or 9)
    except ValueError:
        return 9.0


def _base() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/") or advisor.DEFAULT_BASE


def _answer(text: str, request: Request, sender: str | None = None, *, fast: bool = False) -> dict[str, Any]:
    return advisor.advise(text, sender=sender, base_url=_base(), fast=fast)


def _mask(sender: str) -> str:
    return f"...{sender[-4:]}" if sender else "anon"


def fallback_reply(text: str) -> str:
    """The deterministic answer when the advisor itself failed; a fixed apology only if even that fails."""
    try:
        ctx = advisor.gather(farmer_chat.clean_text(text))
        return advisor.fit(advisor.fallback(text, ctx), advisor.link_for(ctx, _base()))
    except Exception:  # noqa: BLE001 - the last resort must not raise
        logger.exception("WhatsApp deterministic fallback failed")
        return SORRY


async def twilio_send(to: str, text: str) -> bool:
    """Send one WhatsApp message through the Twilio REST API. Only ever called with the sender of an inbound message."""
    sid, token, from_ = (os.environ.get(k, "") for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM"))
    if not (sid and token and from_ and to.startswith("whatsapp:")):
        logger.warning("Twilio REST send skipped: credentials or a WhatsApp sender missing")
        return False
    from_ = from_ if from_.startswith("whatsapp:") else f"whatsapp:{from_}"
    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=SEND_TIMEOUT_S) as client:
                resp = await client.post(TWILIO_MESSAGES_URL.format(sid=sid), data={"From": from_, "To": to, "Body": text}, auth=(sid, token))
        except httpx.HTTPError as exc:
            logger.warning("Twilio REST send to %s attempt %d/%d failed: %s", _mask(to), attempt, SEND_ATTEMPTS, type(exc).__name__)
        else:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code < 400:
                logger.info("Twilio REST send to %s: HTTP %s sid=%s status=%s", _mask(to), resp.status_code, body.get("sid"), body.get("status"))
                return True
            logger.warning("Twilio REST send to %s attempt %d/%d failed: HTTP %s code=%s %s", _mask(to), attempt, SEND_ATTEMPTS,
                           resp.status_code, body.get("code"), str(body.get("message") or "")[:200])
            if resp.status_code < 500 and resp.status_code != 429:
                return False  # a 4xx (bad number, sandbox not joined) will not succeed on retry
        if attempt < SEND_ATTEMPTS:
            await asyncio.sleep(attempt)
    return False


async def _send_when_ready(job: "asyncio.Future[dict[str, Any]]", to: str, body: str) -> None:
    try:
        text = (await job)["reply"]
    except Exception:  # noqa: BLE001 - the advisor itself failed: still answer the farmer
        logger.exception("farm advisor failed for a WhatsApp message")
        text = await asyncio.to_thread(fallback_reply, body)
    ok = await twilio_send(to, text)
    logger.info("WhatsApp async answer to %s %s", _mask(to), "sent" if ok else "NOT sent")


def _done(task: "asyncio.Task[None]") -> None:
    _background.discard(task)
    if task.cancelled():
        logger.warning("WhatsApp background task cancelled (server shutting down?)")
    elif (exc := task.exception()) is not None:
        logger.error("WhatsApp background task failed: %s", exc, exc_info=exc)


def _spawn(coro: Any) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)  # held until done, so the task cannot be garbage-collected mid-send
    task.add_done_callback(_done)


def register(app: FastAPI) -> None:
    @app.get("/app/farmer/chat", response_model=None)
    async def farmer_chat_page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.post("/harvest/api/farmer/chat")
    async def farmer_chat_route(request: Request) -> JSONResponse:
        if (limited := api._limited(chat_limiter, request, "questions")) is not None:
            return limited
        data, bad = await api._body(request)
        if bad is not None or data is None:
            return bad or api._error("Bad request.", 400)
        message = farmer_chat.clean_text(data.get("message"))
        if not message:
            return api._error("message is required", 422)
        session = str(data.get("session") or "")[:64]
        out = await asyncio.to_thread(_answer, message, request, f"app:{session}" if session else None, fast=True)  # Flash took ~14 s
        return JSONResponse(out, headers=NO_STORE)

    @app.post("/harvest/api/whatsapp/twilio", response_model=None)
    async def twilio_route(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > MAX_FORM:
            return PlainTextResponse("Request body too large.", status_code=413)
        params = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
        if not twilio_ok(request, params):
            return PlainTextResponse("Bad signature.", status_code=403)
        sender = params.get("From") or ""
        if sender_limiter.hit(sender or "anon"):
            return Response(twiml("You're sending messages quickly; please wait a minute and ask again."), media_type="application/xml")
        body = params.get("Body", "")
        logger.info("WhatsApp inbound from %s (%d chars)", _mask(sender), len(body))
        job = asyncio.ensure_future(asyncio.to_thread(_answer, body, request, sender or None, fast=True))
        try:
            out = await asyncio.wait_for(asyncio.shield(job), inline_s())
            text = out["reply"]
            logger.info("WhatsApp inline answer to %s: source=%s model=%s %s ms", _mask(sender), out.get("source"), out.get("model"), out.get("latency_ms"))
        except TimeoutError:
            logger.info("WhatsApp answer to %s missed the %.0f s inline budget; sending Thinking and the answer via REST", _mask(sender), inline_s())
            _spawn(_send_when_ready(job, sender, body))  # the full answer follows through the REST API, to this sender only
            text = THINKING
        except Exception:  # noqa: BLE001 - the advisor itself failed: answer deterministically, never nothing
            logger.exception("farm advisor failed for a WhatsApp message; using the deterministic answer")
            text = await asyncio.to_thread(fallback_reply, body)
        return Response(twiml(text), media_type="application/xml")

    @app.get("/harvest/api/whatsapp/meta", response_model=None)
    async def meta_verify(request: Request) -> Response:
        q = request.query_params
        token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        given = q.get("hub.verify_token", "")
        if q.get("hub.mode") == "subscribe" and token and hmac.compare_digest(given.encode(), token.encode()):
            return PlainTextResponse(q.get("hub.challenge", ""))
        return PlainTextResponse("Verification failed.", status_code=403)

    @app.post("/harvest/api/whatsapp/meta")
    async def meta_webhook(request: Request) -> JSONResponse:
        raw = await request.body()
        if len(raw) > api.MAX_BODY * 4:
            return api._error("Request body too large.", 413)
        if (secret := os.environ.get("WHATSAPP_APP_SECRET", "")) and not meta_signature_ok(secret, raw, request.headers.get("x-hub-signature-256", "")):
            return api._error("Bad signature.", 403)
        try:
            data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return api._error("Body must be JSON.", 400)
        replies = []
        for sender, text in meta_messages(data):
            if sender_limiter.hit(sender):
                continue
            reply = (await asyncio.to_thread(_answer, text, request, f"meta:{sender}"))["reply"]
            replies.append({"to": sender, "text": reply})
            _spawn(meta_send(sender, reply))
        sending = bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))
        return JSONResponse({"ok": True, "sent": sending, "replies": replies}, headers=NO_STORE)
