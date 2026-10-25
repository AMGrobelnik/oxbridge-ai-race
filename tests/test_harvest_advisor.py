"""The grounded farm advisor (agent/harvest/advisor.py) and its WhatsApp async path. Offline, LLM and Twilio mocked."""

import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from agent.harvest import advisor, market, news
from agent.routes import whatsapp as wa
from agent.server import app


def _q(qid: str, title: str, region: str, crop: str, kind: str = "hard") -> dict:
    return {"qid": qid, "spec_id": qid.split("@")[0], "kind": kind, "title": title, "region": region, "crop": crop, "adverse_if": "yes",
            "cutoff": "2026-09", "series": "PWHEAMTUSDM", "base_month": "2026-09", "base_value": 247.0, "resolve_month": "2026-12",
            "target": 222.0, "rule": None, "prior": 0.5}


QUESTIONS = [
    _q("wheat-down10-3m@2026-09", "Will wheat (now $247/mt) be below $222/mt by 2026-12?", "France", "wheat"),
    _q("wheat-up10-6m@2026-09", "Will wheat (now $247/mt) be above $272/mt by 2027-03?", "Italy", "durum wheat"),
    _q("sunoil-up10-6m@2026-09", "Will sunflower oil (now $1,677/mt) be above $1,844/mt by 2027-03?", "Romania", "sunflower"),
    _q("olive-up15-6m@2026-09", "Will olive oil (now $6,205/mt) be above $7,136/mt by 2027-03?", "Spain", "olive oil"),
    _q("eu-cap-heat-2028@2026-09", "Will the post-2027 EU CAP ring-fence at least EUR 10 bn for farm heat and drought adaptation?", "EU", "all crops", "soft"),
    _q("it-drought-2027@2026-09", "Will Italy declare a national drought emergency in 2027?", "Italy", "durum wheat", "soft"),
]
PRICES = {"wheat-down10-3m@2026-09": 0.45, "wheat-up10-6m@2026-09": 0.36, "sunoil-up10-6m@2026-09": 0.45, "olive-up15-6m@2026-09": 0.49,
          "eu-cap-heat-2028@2026-09": 0.61, "it-drought-2027@2026-09": 0.52}


def _result():
    live = {"cutoff": "2026-09", "split": "live", "questions": QUESTIONS, "windows": {}, "trades": [], "resolutions": [], "payouts": [],
            "forecasts": [], "final_prices": PRICES}
    return {"personas": [], "cutoffs": [live]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    for var in ("TWILIO_AUTH_TOKEN", "TWILIO_ACCOUNT_SID", "TWILIO_WHATSAPP_FROM", "TWILIO_WEBHOOK_URL", "PUBLIC_BASE_URL", "WHATSAPP_INLINE_S"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(market, "load_replay", _result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: [])
    wa.chat_limiter.reset()
    wa.sender_limiter.reset()
    advisor.forget_all()
    market.reset_live_book()
    yield
    market.reset_live_book()
    advisor.forget_all()


@pytest.fixture
def llm(monkeypatch):
    """A mocked LLM: records every (model, prompt); `answers` maps model key -> reply text, an Exception, or a callable."""
    calls: list[tuple[str, str]] = []
    answers: dict[str, object] = {}

    def fake(key: str, prompt: str, deadline_s: float) -> str:
        calls.append((key, prompt))
        a = answers.get(key, "Default answer from the model. Not financial advice.")
        if isinstance(a, Exception):
            raise a
        if callable(a):
            a = a(prompt)
        return json.dumps({"reply": a})

    monkeypatch.setattr(advisor, "llm_available", lambda: True)
    monkeypatch.setattr(advisor, "ask_model", fake)
    return {"calls": calls, "answers": answers}


# ---------------------------------------------------------------- routing
@pytest.mark.parametrize(("text", "intent"), [
    ("Should I sell my wheat now or store it until spring?", "sell_store"),
    ("olive oil price Spain", "price"),
    ("Kakšno sušo pričakujemo letos v Sloveniji?", "drought"),
    ("Is it worth planting rapeseed instead of wheat next season in northern France?", "plant"),
    ("What happens to fertiliser prices if gas goes up?", "inputs"),
    ("Any CAP money for heat adaptation?", "policy"),
    ("How do I hedge my maize with futures?", "hedging"),
    ("What does the crowd think about cocoa?", "crowd"),
])
def test_classify_farm_questions(text, intent):
    assert intent in advisor.classify(text)


def test_classify_greeting_and_offtopic():
    assert advisor.classify("hi") == ["greeting"] and advisor.classify("Zdravo!") == ["greeting"] and advisor.classify("") == ["greeting"]
    assert advisor.classify("what's the weather tomorrow in Paris") == ["offtopic"]
    assert advisor.classify("who won the football last night") == ["offtopic"]


def test_parse_multilingual_crop_and_region():
    assert advisor.parse("Kakšno sušo pričakujemo letos v Sloveniji?") == (None, "Slovenia")
    assert advisor.parse("¿Vendo el trigo ahora en España?") == ("wheat", "Spain")
    assert advisor.parse("colza dans le nord de la France") == ("rapeseed", "France")
    assert advisor.parse("Weizen oder Gerste in Deutschland?") == ("wheat", "Germany")
    assert advisor.parse("rapeseed instead of wheat in northern France")[1] == "France"
    assert advisor.parse("olive oil price Spain") == ("olive oil", "Spain")
    assert advisor.parse("Mais in Italia")[1] == "Italy"


# ---------------------------------------------------------------- grounding
def test_gather_pulls_markets_climate_farmer_mode_and_news():
    news.store([{"title": "Wheat prices slip as French harvest beats forecasts", "url": "https://example.com/w1", "source": "Wire", "published_at": "2026-10-20T10:00:00Z"},
                {"title": "Rice export ban lifted", "url": "https://example.com/r1", "source": "Wire", "published_at": "2026-10-21T10:00:00Z"}])
    ctx = advisor.gather("Should I sell my wheat now or store it until spring? I farm in France")
    assert ctx["crop"] == "wheat" and ctx["region"] == "France" and "sell_store" in ctx["intents"]
    qids = [m["qid"] for m in ctx["markets"]]
    assert qids[0] == "wheat-down10-3m@2026-09" and "wheat-up10-6m@2026-09" in qids and "olive-up15-6m@2026-09" not in qids
    assert ctx["markets"][0]["p"] == pytest.approx(market.live_book().price("wheat-down10-3m@2026-09"), abs=1e-3)
    assert ctx["climate"]["region"] == "France" and "europe_anomaly_c" in ctx["climate"] and "Nino" in ctx["climate"]["enso"]
    assert ctx["farmer_mode"] and ctx["farmer_mode"]["question"] == "Will my wheat price rise by harvest?"
    assert [n["title"] for n in ctx["news"]] == ["Wheat prices slip as French harvest beats forecasts"]


def test_gather_by_topic_without_crop():
    ctx = advisor.gather("Any CAP money for heat adaptation?")
    assert ctx["markets"] and ctx["markets"][0]["qid"] == "eu-cap-heat-2028@2026-09"
    ctx = advisor.gather("Kakšno sušo pričakujemo letos v Sloveniji?")
    assert ctx["climate"]["region"] == "Slovenia" and ctx["climate"]["heat_stress"] in ("low", "moderate", "high", "severe")
    assert "it-drought-2027@2026-09" in [m["qid"] for m in ctx["markets"]]
    assert advisor.gather("rapeseed France")["proxy"] == "sunflower oil"


def test_prompt_carries_numbers_rules_and_question():
    ctx = advisor.gather("olive oil price Spain")
    prompt = advisor.build_prompt("olive oil price Spain", ctx, [])
    assert "olive-up15-6m@2026-09" in prompt and f'"p": "{round(ctx["markets"][0]["p"] * 100)}%"' in prompt
    assert "Never invent prices" in prompt and "SAME LANGUAGE" in prompt and "not financial advice" in prompt
    assert prompt.rstrip().endswith('{"reply": "..."}') and "FARMER'S MESSAGE: olive oil price Spain" in prompt
    assert "REPLY LANGUAGE: English." in advisor.build_prompt("Should I sell my wheat in Italy?", ctx, [])
    sl = advisor.build_prompt("Kakšno sušo pričakujemo letos v Sloveniji?", ctx, [])
    assert "REPLY LANGUAGE: English" not in sl and "not the language of a country it mentions" in sl


# ---------------------------------------------------------------- LLM, language, fallback
def test_llm_answer_with_link_and_language_passthrough(llm):
    sl = "Letos pričakujemo vročino: zadnji trije meseci so bili +2,1 °C nad normalo. Ni finančni nasvet."
    llm["answers"]["gemini-2.5-flash"] = sl
    out = advisor.advise("Kakšno sušo pričakujemo letos v Sloveniji?", base_url="https://crophecy.bid")
    assert out["source"] == "llm" and out["model"] == "gemini-2.5-flash"
    assert out["reply"].startswith(sl) and out["reply"].endswith("\n\nMore: https://crophecy.bid/app/map")
    assert "Kakšno sušo pričakujemo letos v Sloveniji?" in llm["calls"][0][1]


def test_falls_back_to_flash_lite_then_deterministic(llm):
    llm["answers"]["gemini-2.5-flash"] = TimeoutError()
    llm["answers"]["gemini-2.5-flash-lite"] = "Lite answer."
    out = advisor.advise("olive oil price Spain")
    assert out["model"] == "gemini-2.5-flash-lite" and out["reply"].startswith("Lite answer.")
    assert out["reply"].endswith("Not financial advice.\n\nMore: https://crophecy.bid/app/market/olive-up15-6m@2026-09")  # added when missing
    llm["answers"]["gemini-2.5-flash-lite"] = RuntimeError("quota")
    out = advisor.advise("olive oil price Spain")
    assert out["source"] == "fallback" and "Our forecasters put 49% on" in out["reply"] and "Not financial advice" in out["reply"]
    assert [k for k, _ in llm["calls"]] == ["gemini-2.5-flash", "gemini-2.5-flash-lite"] * 2


def test_offline_fallbacks_for_each_kind():
    hi = advisor.advise("hi")
    assert hi["source"] == "help" and "Ask me about a crop" in hi["reply"]
    off = advisor.advise("what's the weather tomorrow in Paris")
    assert "can't help with that" in off["reply"] and "day-ahead weather" in off["reply"]
    inputs = advisor.advise("What happens to fertiliser prices if gas goes up?")
    assert "no fertiliser or energy price market" in inputs["reply"]
    sell = advisor.advise("Should I sell my wheat now or store it until spring?")
    assert "Our forecasters put 45% on" in sell["reply"] and "forward contract" in sell["reply"] and sell["probability"] is not None


def test_length_limit_and_markdown_stripped(llm):
    llm["answers"]["gemini-2.5-flash"] = "**Sell part now.** " + "Storage costs money and quality can slip over winter. " * 120
    out = advisor.advise("Should I sell my wheat now or store it until spring?", base_url="https://crophecy.bid")
    assert len(out["reply"]) <= advisor.MAX_REPLY and "**" not in out["reply"]
    assert out["reply"].endswith("More: https://crophecy.bid/app/farmer?crop=wheat")
    for text in ("x" * 5000, "a. " * 900):
        assert len(advisor.fit(text, "https://crophecy.bid/app/map")) <= advisor.MAX_REPLY
    long = "Option one is good. " * 100 + "\nNot financial advice."
    cut = advisor.fit(long, "https://crophecy.bid/app/map")
    assert len(cut) <= advisor.MAX_REPLY and cut.endswith("Not financial advice.\n\nMore: https://crophecy.bid/app/map")
    assert advisor.parse_reply(json.dumps({"reply": "-   one\n*  two"})) == "- one\n- two"


def test_memory_keeps_three_turns_and_inherits_crop(llm, monkeypatch):
    advisor.advise("Should I sell my wheat now or store it?", sender="whatsapp:+1555")
    out = advisor.advise("and in Spain?", sender="whatsapp:+1555")
    assert out["crop"] == "wheat" and out["region"] == "Spain"
    assert "Should I sell my wheat now or store it?" in llm["calls"][-1][1]  # earlier turn sent along
    for i in range(4):
        advisor.advise(f"wheat question {i}", sender="whatsapp:+1555")
    assert [h["q"] for h in advisor.history("whatsapp:+1555")] == ["wheat question 1", "wheat question 2", "wheat question 3"]
    assert advisor.history("whatsapp:+other") == []
    monkeypatch.setattr(advisor, "MEMORY_TTL_S", 0.0)
    assert advisor.history("whatsapp:+1555") == []


# ---------------------------------------------------------------- WhatsApp inline vs async
def _form(body: str, sender: str = "whatsapp:+15550001111") -> dict[str, str]:
    return {"From": sender, "To": "whatsapp:+14155238886", "Body": body, "MessageSid": "SM1", "NumMedia": "0"}


def test_twilio_inline_when_fast(llm, monkeypatch):
    sent: list = []

    async def fake_send(to, text):
        sent.append((to, text))
        return True

    monkeypatch.setattr(wa, "twilio_send", fake_send)
    llm["answers"]["gemini-2.5-flash"] = "Hold half, sell half."
    with TestClient(app) as client:
        r = client.post("/harvest/api/whatsapp/twilio", data=_form("Should I sell my wheat?"))
    assert r.status_code == 200 and "Hold half, sell half." in r.text and sent == []


def test_twilio_async_when_slow_replies_to_sender_only(llm, monkeypatch):
    sent: list = []

    async def fake_send(to, text):
        sent.append((to, text))
        return True

    def slow(prompt):
        time.sleep(0.6)
        return "Slow but thorough answer."

    monkeypatch.setattr(wa, "twilio_send", fake_send)
    monkeypatch.setenv("WHATSAPP_INLINE_S", "0.1")
    llm["answers"]["gemini-2.5-flash"] = slow
    with TestClient(app) as client:
        r = client.post("/harvest/api/whatsapp/twilio", data=_form("Should I sell my wheat?", "whatsapp:+15559990000"))
        assert r.status_code == 200 and "Thinking" in r.text and "Slow but" not in r.text
        deadline = time.monotonic() + 10
        while not sent and time.monotonic() < deadline:
            time.sleep(0.05)
    assert len(sent) == 1 and sent[0][0] == "whatsapp:+15559990000" and sent[0][1].startswith("Slow but thorough answer.")


def test_twilio_send_posts_to_rest_api(monkeypatch):
    import asyncio

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"sid": "SMx"})

    real = httpx.AsyncClient
    monkeypatch.setattr(wa.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(wa.twilio_send("whatsapp:+1555", "hi")) is False  # no credentials: nothing sent
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+14155238886")
    assert asyncio.run(wa.twilio_send("+1555", "hi")) is False  # only WhatsApp senders
    assert asyncio.run(wa.twilio_send("whatsapp:+1555", "hello farmer")) is True
    assert len(seen) == 1 and str(seen[0].url) == "https://api.twilio.com/2010-04-01/Accounts/AC123/Messages.json"
    form = parse_qs(seen[0].content.decode())
    assert form == {"From": ["whatsapp:+14155238886"], "To": ["whatsapp:+1555"], "Body": ["hello farmer"]}


def test_in_app_route_uses_advisor_with_session_memory(llm):
    with TestClient(app) as client:
        r = client.post("/harvest/api/farmer/chat", json={"message": "olive oil price Spain", "session": "abc"})
        body = r.json()
        assert r.status_code == 200 and body["source"] == "llm" and body["crop"] == "olive oil" and body["latency_ms"] >= 0
        assert body["link"] == "https://crophecy.bid/app/market/olive-up15-6m@2026-09"
        r = client.post("/harvest/api/farmer/chat", json={"message": "and in Greece?", "session": "abc"})
        assert r.json()["crop"] == "olive oil" and r.json()["region"] == "Greece"
        page = client.get("/app/farmer/chat").text
    assert "Any CAP money for heat adaptation?" in page and "session" in page
