"""Farmer assistant (agent/harvest/farmer_chat.py) and its WhatsApp + in-app routes (agent/routes/whatsapp.py). Offline."""

import hashlib
import hmac
import json
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from agent.harvest import advisor, farmer_chat, market
from agent.routes import public_mode
from agent.routes import whatsapp as wa
from agent.server import app


def _hard(qid: str, title: str, region: str, crop: str) -> dict:
    return {"qid": qid, "spec_id": qid.split("@")[0], "kind": "hard", "title": title, "region": region, "crop": crop, "adverse_if": "yes",
            "cutoff": "2026-06", "series": "PWHEAMTUSDM", "base_month": "2026-06", "base_value": 247.0, "resolve_month": "2026-12",
            "target": 272.0, "rule": None, "prior": 0.5}


QUESTIONS = [_hard("wheat-up@2026-06", "Will wheat (now $247/mt) be above $272/mt by 2026-12?", "Italy", "durum wheat"),
             _hard("wheat-down@2026-06", "Will wheat (now $247/mt) be below $222/mt by 2026-12?", "France", "wheat"),
             _hard("maize-up@2026-06", "Will maize (now $237/mt) be above $250/mt by 2026-12?", "Spain", "feed maize")]


def _result():
    live = {"cutoff": "2026-06", "split": "live", "questions": QUESTIONS, "windows": {}, "trades": [], "resolutions": [], "payouts": [],
            "forecasts": [], "final_prices": {"wheat-up@2026-06": 0.4, "wheat-down@2026-06": 0.3, "maize-up@2026-06": 0.6}}
    return {"personas": [], "cutoffs": [live]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    for var in ("TWILIO_AUTH_TOKEN", "TWILIO_WEBHOOK_URL", "PUBLIC_BASE_URL", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(market, "load_replay", _result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: [])
    wa.chat_limiter.reset()
    wa.sender_limiter.reset()
    advisor.forget_all()
    market.reset_live_book()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


def test_parse_crop_and_region():
    assert farmer_chat.parse("will wheat go up in France by harvest?") == ("wheat", "France")
    assert farmer_chat.parse("maize Spain") == ("maize", "Spain")
    assert farmer_chat.parse("is it worth planting corn") == ("maize", None)  # lowercase "it" is not Italy
    assert farmer_chat.parse("olive oil IT") == ("olive oil", "Italy")


def test_help_and_unknown_crop():
    assert "Ask me about a crop" in farmer_chat.reply("help")["reply"]
    out = farmer_chat.reply("what about bananas in Peru")
    assert out["probability"] is None and "Which crop" in out["reply"]


def test_reply_prefers_region_and_gives_probability_reason_confidence_link():
    out = farmer_chat.reply("will wheat go up in France by harvest?", base_url="https://x.test")
    assert out["crop"] == "wheat" and out["region"] == "France"
    assert out["probability"] == pytest.approx(market.live_book().price("wheat-down@2026-06"), abs=1e-3)
    assert "below $222" in out["reply"] and "Chance:" in out["reply"] and "Why:" in out["reply"]
    assert out["confidence"] in ("high", "medium", "low") and "Confidence:" in out["reply"]
    assert out["link"] == "https://x.test/app/farmer?crop=wheat&region=France"


def test_reply_falls_back_to_closest_region():
    out = farmer_chat.reply("maize Romania")
    assert out["probability"] is not None and "No Romania market for maize yet; the closest is Spain" in out["reply"]


def test_confidence_from_forecaster_agreement():
    assert farmer_chat.confidence([{"p_yes": p} for p in (0.5, 0.52, 0.48)], 0) == "high"
    assert farmer_chat.confidence([{"p_yes": p} for p in (0.1, 0.5, 0.9)], 0) == "low"
    assert farmer_chat.confidence([], 0) == "low"


def test_in_app_chat_route_and_page(client):
    r = client.post("/harvest/api/farmer/chat", json={"message": "maize Spain"})
    assert r.status_code == 200 and r.json()["crop"] == "maize" and "Our forecasters put" in r.json()["reply"]
    assert client.post("/harvest/api/farmer/chat", json={}).status_code == 422
    page = client.get("/app/farmer/chat")
    assert page.status_code == 200 and "In-app assistant" in page.text and "/harvest/api/farmer/chat" in page.text


def _twilio_form(body: str) -> dict[str, str]:
    return {"From": "whatsapp:+15550001111", "To": "whatsapp:+14155238886", "Body": body, "MessageSid": "SM123", "NumMedia": "0"}


def test_twilio_webhook_returns_twiml(client):
    r = client.post("/harvest/api/whatsapp/twilio", data=_twilio_form("Will wheat go up in France?"))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert r.text.startswith('<?xml version="1.0" encoding="UTF-8"?><Response><Message>') and "Our forecasters put" in r.text
    assert "&amp;" in r.text  # the link's query is XML-escaped


def test_twilio_signature_checked_when_token_set(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "test-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://farm.example")
    form = _twilio_form("help")
    assert client.post("/harvest/api/whatsapp/twilio", data=form).status_code == 403
    sig = wa.twilio_signature("test-token", "https://farm.example/harvest/api/whatsapp/twilio", form)
    r = client.post("/harvest/api/whatsapp/twilio", content=urlencode(form), headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200 and "Ask me about a crop" in r.text


def _signed_post(client, url: str, form: dict[str, str], *, token: str = "dummy-token", headers: dict[str, str] | None = None):
    """POST the form to the local path, signed over `url` the way Twilio signs the public one."""
    path = "/" + url.split("/", 3)[3]
    sig = wa.twilio_signature(token, url, form)
    return client.post(path, content=urlencode(form), headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded", **(headers or {})})


def test_twilio_signature_over_public_url_behind_proxy(client, monkeypatch):
    """The server sees http://testserver (cloudflared -> 127.0.0.1); Twilio signed https://crophecy.bid/..."""
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy-token")
    form = _twilio_form("wheat France")
    url = "https://crophecy.bid/harvest/api/whatsapp/twilio"
    r = _signed_post(client, url, form)  # no forwarded headers at all: the crophecy.bid default
    assert r.status_code == 200 and "Our forecasters put" in r.text
    r = _signed_post(client, url, form, headers={"Host": "crophecy.bid", "X-Forwarded-Proto": "https"})
    assert r.status_code == 200 and "Our forecasters put" in r.text


def test_twilio_signature_from_forwarded_host_and_proto(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy-token")
    form = _twilio_form("help")
    r = _signed_post(client, "https://tunnel.example/harvest/api/whatsapp/twilio", form, headers={"X-Forwarded-Host": "tunnel.example", "X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    r = _signed_post(client, "http://tunnel.example/harvest/api/whatsapp/twilio", form, headers={"X-Forwarded-Host": "tunnel.example", "X-Forwarded-Proto": "http"})
    assert r.status_code == 200


def test_twilio_signature_over_configured_urls(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy-token")
    form = _twilio_form("help")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://farm.example")
    assert _signed_post(client, "https://farm.example/harvest/api/whatsapp/twilio", form).status_code == 200
    monkeypatch.setenv("TWILIO_WEBHOOK_URL", "https://hooks.example/harvest/api/whatsapp/twilio")
    assert _signed_post(client, "https://hooks.example/harvest/api/whatsapp/twilio", form).status_code == 200


def test_twilio_signature_rejects_wrong_token_url_and_params(client, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "dummy-token")
    form = _twilio_form("help")
    url = "https://crophecy.bid/harvest/api/whatsapp/twilio"
    assert _signed_post(client, url, form, token="other-token").status_code == 403
    assert _signed_post(client, "https://evil.example/harvest/api/whatsapp/twilio", form).status_code == 403
    sig = wa.twilio_signature("dummy-token", url, form)
    tampered = {**form, "Body": "wheat Spain"}
    r = client.post("/harvest/api/whatsapp/twilio", content=urlencode(tampered), headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
    assert client.post("/harvest/api/whatsapp/twilio", data=form).status_code == 403  # unsigned


def test_farmer_pages_have_whatsapp_button(client):
    for path in ("/app/farmer", "/app/farmer/chat"):
        html = client.get(path).text
        assert 'href="https://wa.me/14155238886?text=join%20globe-transportation"' in html
        assert "join globe-transportation" in html and "wheat France" in html


def test_twilio_signature_matches_twilio_docs_example():
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030", "To": "+18005551212"}
    assert wa.twilio_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="  # Twilio's docs example


def _meta_payload(text: str) -> dict:
    msg = {"from": "15550001111", "id": "wamid.X", "timestamp": "1", "type": "text", "text": {"body": text}}
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp", "messages": [msg]}}]}]}


def test_meta_verify_handshake(client, monkeypatch):
    params = {"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}
    assert client.get("/harvest/api/whatsapp/meta", params=params).status_code == 403  # no token configured
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "vt")
    r = client.get("/harvest/api/whatsapp/meta", params=params)
    assert r.status_code == 200 and r.text == "42"
    assert client.get("/harvest/api/whatsapp/meta", params=params | {"hub.verify_token": "no"}).status_code == 403


def test_meta_webhook_answers_without_sending(client):
    r = client.post("/harvest/api/whatsapp/meta", json=_meta_payload("maize Spain"))
    body = r.json()
    assert r.status_code == 200 and body["sent"] is False
    assert body["replies"][0]["to"] == "15550001111" and "Our forecasters put" in body["replies"][0]["text"]
    assert client.post("/harvest/api/whatsapp/meta", json={"object": "x", "entry": [{"changes": [{"value": {"statuses": []}}]}]}).json()["replies"] == []


def test_meta_webhook_checks_app_secret(client, monkeypatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3")
    raw = json.dumps(_meta_payload("help")).encode()
    assert client.post("/harvest/api/whatsapp/meta", content=raw, headers={"Content-Type": "application/json"}).status_code == 403
    sig = "sha256=" + hmac.new(b"s3", raw, hashlib.sha256).hexdigest()
    r = client.post("/harvest/api/whatsapp/meta", content=raw, headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})
    assert r.status_code == 200 and len(r.json()["replies"]) == 1


def test_routes_are_public():
    for path in ("/harvest/api/farmer/chat", "/harvest/api/whatsapp/twilio", "/harvest/api/whatsapp/meta"):
        assert public_mode.is_public("POST", path)
    assert public_mode.is_public("GET", "/harvest/api/whatsapp/meta") and public_mode.is_public("GET", "/app/farmer/chat")
