"""The farmer assistant on WhatsApp and in the app (agent/harvest/farmer_chat.py answers all of them).

POST /harvest/api/farmer/chat {message} -> {reply, crop, region, probability, confidence, link}: the in-app chat.
POST /harvest/api/whatsapp/twilio: Twilio WhatsApp webhook (form POST), answers with TwiML. When TWILIO_AUTH_TOKEN is
  set, the X-Twilio-Signature header must match (URL from PUBLIC_BASE_URL or the forwarded host).
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
from agent.harvest import farmer_chat
from agent.routes import harvest as api
from agent.routes.challenge import public_base_url

PAGE = Path(__file__).resolve().parent.parent / "harvest" / "static" / "app" / "farmer-chat.html"
GRAPH_URL = "https://graph.facebook.com/v21.0/{phone_id}/messages"
MAX_FORM = 16_000
NO_STORE = {"Cache-Control": "no-store"}
logger = logging.getLogger(__name__)

chat_limiter = RateLimiter(int(os.environ.get("FARMER_CHAT_PER_MIN") or 20))
sender_limiter = RateLimiter(int(os.environ.get("WHATSAPP_PER_MIN") or 10))
_background: set[asyncio.Task[None]] = set()


def twiml(text: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Message>{escape(text, quote=False)}</Message></Response>'


def twilio_signature(token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's X-Twilio-Signature: base64 HMAC-SHA1 of the URL followed by each sorted POST key and value."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def _candidate_urls(request: Request) -> list[str]:
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    urls = [str(request.url)]
    if base := os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"):
        urls.append(base + path)
    if host := request.headers.get("x-forwarded-host") or request.headers.get("host"):
        urls.append(f"{request.headers.get('x-forwarded-proto', 'https')}://{host}{path}")
    return urls


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


def _answer(text: str, request: Request) -> dict[str, Any]:
    return farmer_chat.reply(text, base_url=public_base_url(request))


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
        return JSONResponse(await asyncio.to_thread(_answer, message, request), headers=NO_STORE)

    @app.post("/harvest/api/whatsapp/twilio", response_model=None)
    async def twilio_route(request: Request) -> Response:
        raw = await request.body()
        if len(raw) > MAX_FORM:
            return PlainTextResponse("Request body too large.", status_code=413)
        params = dict(parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True))
        if not twilio_ok(request, params):
            return PlainTextResponse("Bad signature.", status_code=403)
        if sender_limiter.hit(params.get("From") or "anon"):
            text = "You're sending messages quickly; please wait a minute and ask again."
        else:
            text = (await asyncio.to_thread(_answer, params.get("Body", ""), request))["reply"]
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
            reply = (await asyncio.to_thread(_answer, text, request))["reply"]
            replies.append({"to": sender, "text": reply})
            task = asyncio.get_running_loop().create_task(meta_send(sender, reply))
            _background.add(task)
            task.add_done_callback(_background.discard)
        sending = bool(os.environ.get("WHATSAPP_TOKEN") and os.environ.get("WHATSAPP_PHONE_NUMBER_ID"))
        return JSONResponse({"ok": True, "sent": sending, "replies": replies}, headers=NO_STORE)
