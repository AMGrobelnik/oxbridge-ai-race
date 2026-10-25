"""The grounded farm advisor behind WhatsApp and the in-app chat (/app/farmer/chat).

`advise(text, sender=)` answers the questions farmers actually ask: sell or store, price outlook, drought and heat
risk, what to plant, input costs, EU/CAP policy, hedging, "what does the crowd think", plus greetings and off-topic
chatter. Before answering it pulls context from our own system (`gather`): the matching live markets with their
probabilities and the AI agents' agreement, the climate fixture (Europe land temperature anomaly scaled per region,
El Nino index), the latest tagged news headlines and farmer mode's view. An LLM writes the answer from that context
only (Gemini 2.5 Flash, then Flash-Lite, through agent.harvest.models); offline or on failure a deterministic answer
built from the same context is used. Replies are capped (WhatsApp: about 1,200 characters) and end with a link.
A short per-sender memory (last 3 turns, 30-minute TTL, in process) makes follow-ups work.
"""

import json
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode

from . import climate, farmer_chat, fast_llm, market, models, news, world

DEFAULT_BASE = "https://crophecy.bid"
MAX_REPLY = 1200
TURNS = 3
MEMORY_TTL_S = 1800.0
MEMORY_MAX_SENDERS = 2000
MODELS = (("gemini-2.5-flash", 14.0), ("gemini-2.5-flash-lite", 7.0))  # (registry key, deadline in seconds)
DISCLAIMER = "Not financial advice."

INTENTS: list[tuple[str, re.Pattern[str]]] = [(name, re.compile(rx, re.I)) for name, rx in (
    ("sell_store", r"\b(sell|selling|store|storage|stor(e|ing) it|hold on|keep it|prodam|prodati|prodaja|skladišč|vender|almacen|vendere|stocc|conserv|vendre|stocker|verkauf|lager)"),
    ("hedging", r"\b(hedg|futures?|forward contract|options?\b|matif|euronext|zavarovanj|terminsk|cobertura|copertur|couverture|absicher|termingesch)"),
    ("inputs", r"\b(fertili[sz]|nitrogen|urea|ammonia|potash|phosphate|gas\b|natural gas|energy|diesel|fuel|electricity|gnojil|plin\b|fertilizant|abono|concim|fertilizzant|engrais|dünger|energie|energia|energía)"),
    ("policy", r"\b(cap\b|subsid|policy|grant|fund(s|ing)?\b|export ban|ban\b|bans\b|tariff|regulation|eu money|money|subvenc|ukrep|ayuda|pac\b|sussid|contribut|subvention|aide|förder|gap\b|politik|adaptation)"),
    ("plant", r"\b(plant|planting|sow|sowing|rotation|next season|grow instead|instead of|sejat|posadit|setev|sembrar|plantar|seminare|semina|semer|planter|anbauen|aussaat|säen)"),
    ("drought", r"\b(drought|heat|dry|rain|irrigat|water|el ni[nñ]o|la ni[nñ]a|climate|weather|temperature|suš|vroč|padavin|sequía|sequia|calor|lluvia|siccità|siccita|caldo|pioggia|sécheresse|secheresse|chaleur|pluie|dürre|hitze|regen|vreme|tiempo|meteo|wetter)"),
    ("price", r"\b(price|prices|outlook|go up|going up|rise|fall|forecast|market|cena|cene|precio|prezzo|prezzi|prix|preis|cours)"),
    ("crowd", r"\b(crowd|the ai|ai think|forecasters|agents|consensus|disagree|what do (you|they) think|množic|multitud|folla|foule|schwarm)"),
)]
WEATHER_SHORT = re.compile(r"\b(tomorrow|today|tonight|this weekend|jutri|danes|mañana|manana|domani|oggi|demain|aujourd|morgen|heute)\b", re.I)
GREETING = re.compile(r"^\s*(hi|hello|hey|hola|bonjour|salut|ciao|hallo|servus|zdravo|živjo|zivjo|pozdravljen\w*|dober dan|buenos d[ií]as|buongiorno|guten tag|good (morning|afternoon|evening)|start|help|menu|pomoč|pomoc|ayuda|aiuto|aide|hilfe|\?)\W*$", re.I)
CROP_WORDS: dict[str, str] = {
    "wheat": r"wheat|durum|pšenic|psenic|trigo|grano|frumento|blé\b|weizen",
    "maize": r"maize|corn\b|koruz|maíz|maiz\b|maïs|granturco",
    "barley": r"barley|ječmen|jecmen|cebada|orzo|orge\b|gerste",
    "rapeseed": r"rapeseed|canola|oljn\w* ogrščic|ogrščic|colza|raps\b",
    "sunflower": r"sunflower|sončnic|soncnic|girasol|tournesol|sonnenblum",
    "olive oil": r"olive|oliv|oljk|oljčn|olio\b|aceite",
    "sugar": r"sugar|beet|sladkor|azúcar|azucar|zucchero|sucre|zucker",
    "coffee": r"coffee|kav[aei]\b|café|cafe\b|caffè|caffe\b|kaffee",
    "cocoa": r"cocoa|cacao|kakav|kakao",
    "rice": r"\brice|riž\b|riz\b|arroz|riso\b|reis\b",
}
CROP_RX = {crop: re.compile(rf"\b({rx})", re.I) for crop, rx in CROP_WORDS.items()}
# How the canonical crop shows up in market titles/crops; barley and rapeseed have no market yet (closest proxy).
MARKET_CROP = {"wheat": "wheat", "maize": "maize", "barley": "wheat", "rapeseed": "sunflower", "sunflower": "sunflower",
               "olive oil": "olive", "sugar": "sugar", "coffee": "coffee", "cocoa": "cocoa", "rice": "rice"}
PROXY = {"barley": "wheat", "rapeseed": "sunflower oil"}
REGION_WORDS: dict[str, str] = {
    "France": r"france|french|francij|francia|frankreich|français|paris",
    "Spain": r"spain|spanish|španij|spanij|españa|espana|spagna|espagne|spanien|andaluc",
    "Italy": r"italy|italian|italij|italia|italie|italien|sicil|puglia",
    "Greece": r"greece|greek|grčij|grcij|grecia|grèce|grece|griechenland",
    "Romania": r"romania|romanian|romunij|rumania|roumanie|rumänien",
    "Germany": r"germany|german|nemčij|nemcij|alemania|germania|allemagne|deutschland",
    "Poland": r"poland|polish|poljsk|polonia|pologne|polen\b",
    "Slovenia": r"sloven|eslovenia|slowenien|slovénie",
    "Croatia": r"croatia|hrvašk|hrvatsk|croacia|croazia|croatie|kroatien",
    "Hungary": r"hungar|madžarsk|madzarsk|hungría|ungheria|hongrie|ungarn",
    "Austria": r"austria|avstrij|autriche|österreich",
    "Portugal": r"portug",
    "Bulgaria": r"bulgari|bolgarij",
    "Ukraine": r"ukrain|ucrania|ucraina",
    "EU": r"\beu\b|europe|european|evrop|europa",
}
REGION_RX = {name: re.compile(rf"\b({rx})", re.I) for name, rx in REGION_WORDS.items()}
EXTRA_EXPOSURE = {"Slovenia": 1.1, "Croatia": 1.2, "Hungary": 1.1, "Austria": 1.0, "Portugal": 1.3, "Bulgaria": 1.1, "Ukraine": 1.0, "EU": 1.0}
INTENT_TITLE = {  # market titles worth showing for an intent even without a crop
    "drought": re.compile(r"drought|water|rationing|heat|output|harvest", re.I),
    "policy": re.compile(r"\bCAP\b|subsid|export ban|restrict|adaptation|fund", re.I),
    "inputs": re.compile(r"export ban|shipping", re.I),
}
NEWS_WORDS = {
    "drought": re.compile(r"drought|heat|dry|rain|water|climate|el niño|el nino|wildfire", re.I),
    "policy": re.compile(r"\bCAP\b|subsid|policy|commission|export|ban|tariff|brussels|minister", re.I),
    "inputs": re.compile(r"fertili|nitrogen|urea|ammonia|gas|energy|diesel|potash", re.I),
    "hedging": re.compile(r"futures|euronext|matif|hedg", re.I),
}
LINKS = {"drought": "/app/map", "crowd": "/app/disagreement", "policy": "/app/markets", "inputs": "/app/markets"}
HELP = ("Hi, I'm The Crophecy farm advisor. Ask me about a crop and a country, in any language, for example:\n"
        "- Should I sell my wheat now or store it?\n- Drought risk in Spain this season?\n- Rapeseed or wheat next year in France?\n"
        "- Fertiliser prices if gas goes up?\n- Any CAP money for heat adaptation?\n"
        "I answer from our live forecasting markets, climate data and the latest farm news. " + DISCLAIMER)

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="advisor-llm")
_mem_lock = threading.Lock()
_memory: dict[str, list[dict[str, Any]]] = {}


# ---------------------------------------------------------------- routing
def classify(text: str) -> list[str]:
    """Intents found in the text, most specific first; ['greeting'], ['offtopic'] or ['general'] when nothing fits."""
    if not text.strip() or GREETING.match(text):
        return ["greeting"]
    found = [name for name, rx in INTENTS if rx.search(text)]
    if "drought" in found and WEATHER_SHORT.search(text) and not parse(text)[0]:
        return ["offtopic"]  # "weather tomorrow in Paris": a day-ahead forecast is not something we have
    if found:
        return found
    return ["general"] if parse(text)[0] else ["offtopic"]


def parse(text: str) -> tuple[str | None, str | None]:
    """(crop, region) named in the text, in any of the supported languages; either may be None."""
    crop = next((c for c, rx in CROP_RX.items() if rx.search(text)), None)
    hits = [(m.start(), name) for name, rx in REGION_RX.items() if (m := rx.search(text))]
    region = min(hits)[1] if hits else None
    if region is None:
        region = farmer_chat.parse(text)[1]  # capital two-letter codes ("FR")
    return crop, region


# ---------------------------------------------------------------- grounding
def _views() -> list[dict[str, Any]]:
    book = market.live_book()
    with book.lock:
        return [book.market_view(q) for q in book.markets]


def _market_row(v: dict[str, Any]) -> dict[str, Any]:
    s = world.spread(v)
    row = {"qid": v["qid"], "title": v.get("title", ""), "region": v.get("region") or "", "crop": v.get("crop") or "",
           "p": round(float(v["p_yes"]), 3), "resolve": v.get("resolve_month"), "agents": None}
    if s:
        row["agents"] = {"n": s["n_agents"], "mean": s["mean"], "std": s["std"], "agreement": world.confidence(s)["level"],
                         "bull": {k: s["bull"][k] for k in ("name", "p_yes")} | {"reason": s["bull"]["reason"][:140]},
                         "bear": {k: s["bear"][k] for k in ("name", "p_yes")} | {"reason": s["bear"]["reason"][:140]}}
    return row


def pick_markets(views: list[dict[str, Any]], crop: str | None, region: str | None, intents: list[str], limit: int = 5) -> list[dict[str, Any]]:
    """The open markets most relevant to the question: crop first, then region, then the intent's topic."""
    want = MARKET_CROP.get(crop or "", "")
    scored = []
    for v in views:
        if v.get("status", "open") != "open":
            continue
        hay = f"{v.get('crop', '')} {v.get('title', '')}".lower()
        score = 3 * int(bool(want) and want in hay)
        if crop and not score:
            continue
        score += 2 * int(bool(region) and v.get("region") == region) + int(bool(region) and v.get("region") in world.EUROPE_WIDE)
        score += 2 * sum(1 for i in intents if (rx := INTENT_TITLE.get(i)) and rx.search(str(v.get("title", ""))))
        if score >= 2:
            scored.append((score, float(v.get("volume") or 0), v))
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [_market_row(v) for _, _, v in scored[:limit]]


def climate_context(region: str | None, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    data = climate.load_climate() if data is None else data
    anom = world.recent_anomaly(data)
    if not anom:
        return None
    out: dict[str, Any] = {"europe_anomaly_c": anom["value_c"], "months": f"{anom['from']} to {anom['to']}", "source": anom["source"]}
    if region:
        exposure = world.REGIONS.get(region, {}).get("exposure") or EXTRA_EXPOSURE.get(region, 1.0)
        heat = round(anom["value_c"] * exposure, 2)
        out |= {"region": region, "region_anomaly_c": heat, "heat_stress": world.level(min(1.0, max(0.0, heat / world.STRESS_MAX_C))),
                "exposure_note": "regional value = Europe land anomaly x a hand-set exposure factor (approximate)"}
    oni = ((data or {}).get("series") or {}).get("oni", {}).get("points") or []
    if oni:
        month, v = oni[-1]
        phase = "El Nino" if v >= 0.5 else "La Nina" if v <= -0.5 else "neutral"
        strength = " (strong)" if abs(v) >= 1.5 else ""
        out["enso"] = f"Oceanic Nino Index {v:+.2f} ({month}): {phase}{strength}"
    return out


def news_context(crop: str | None, region: str | None, intents: list[str], qids: list[str], limit: int = 4) -> list[dict[str, Any]]:
    try:
        rows, _ = news.query(limit=200)
    except (sqlite3.Error, OSError, ValueError):
        return []
    specs = {q.split("@")[0] for q in qids}
    crop_rx = CROP_RX.get(crop or "")
    scored = []
    for r in rows:
        title = str(r.get("title") or "")
        score = 2 * int(bool(specs & set(r.get("specs") or [])))
        score += 2 * int(bool(crop_rx) and (bool(crop_rx.search(title)) or MARKET_CROP.get(crop or "", "?") in " ".join(r.get("commodities") or []).lower()))
        score += sum(1 for i in intents if (rx := NEWS_WORDS.get(i)) and rx.search(title))
        score += int(bool(region) and (region in (r.get("regions") or []) or region.lower() in title.lower()))
        if score >= 2:
            scored.append((score, str(r.get("published_at") or r.get("fetched_at") or ""), r))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)  # most relevant, then newest
    return [{"title": r["title"], "source": r.get("source"), "date": str(r.get("published_at") or "")[:10], "direction": r.get("direction")}
            for _, _, r in scored[:limit]]


def farmer_view(views: list[dict[str, Any]], crop: str | None, region: str | None) -> dict[str, Any] | None:
    """Farmer mode's view (agent/harvest/world.py) for crops it covers."""
    if crop not in world.CROPS:
        return None
    ctx = world.farmer_context(views, crop, region or "EU")
    if ctx is None:
        return None
    return {"question": ctx["question"], "market": ctx["market"]["title"], "p": ctx["probability"], "proxy": ctx["proxy"],
            "confidence": ctx["confidence"]["text"]}


def gather(text: str, *, prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the answer may cite. Follow-ups inherit the crop and region of the previous turn."""
    intents = classify(text)
    crop, region = parse(text)
    if prior and intents == ["offtopic"] and not WEATHER_SHORT.search(text) and (region or len(text) < 60):
        intents = ["followup"]  # "and in Spain?" after a wheat question
    if prior and "greeting" not in intents and "offtopic" not in intents:
        crop, region = crop or prior.get("crop"), region or prior.get("region")
    ctx: dict[str, Any] = {"intents": intents, "crop": crop, "region": region, "proxy": PROXY.get(crop or "")}
    if intents == ["greeting"]:
        return ctx
    views = _views()
    ctx["markets"] = pick_markets(views, crop, region, intents)
    ctx["farmer_mode"] = farmer_view(views, crop, region)
    ctx["climate"] = climate_context(region)
    ctx["news"] = news_context(crop, region, intents, [m["qid"] for m in ctx["markets"]])
    return ctx


# ---------------------------------------------------------------- the answer
SYSTEM = """You are the farm advisor of The Crophecy (crophecy.bid): a practical, plain-spoken European agronomist and \
agricultural market analyst talking to a farmer on WhatsApp.

Rules:
- Use ONLY the facts in CONTEXT below plus general agronomy and market know-how. Never invent prices, numbers, dates, \
policies or amounts of money. If CONTEXT has no data for what they ask, say plainly that we don't know or have no \
market on it, then give general practical guidance.
- Quote probabilities as given (whole percents). Cite our numbers when you use them: "our forecasters put 45% on wheat being below $222/mt by Dec", "headline: ...", \
"temperatures in the last 3 months ran +2.1 C above normal". Say whether the AI agents agree or disagree.
- Give 1-3 concrete options with their trade-offs (e.g. sell part now / store and sell in spring / hedge with a forward \
contract). Be specific to the crop and region asked about.
- Off-topic or day-ahead weather: say politely what you can't do, then offer what you can (season-level climate, prices).
- Reply in the SAME LANGUAGE as the farmer's latest message (Slovenian, Spanish, Italian, French, German, ...).
- Short paragraphs or "- " bullets, no markdown headers, no bold, no links, under {limit} characters.
- End with a brief "not financial advice" note in the reply language."""


def _readable(obj: Any) -> Any:
    """Probabilities as whole percents ("34%") and spreads as points, so the model quotes them that way."""
    if isinstance(obj, dict):
        return {k: f"{round(float(v) * 100)}%" if k in ("p", "p_yes", "mean") and isinstance(v, (int, float))
                else f"{round(float(v) * 100)} points" if k == "std" and isinstance(v, (int, float)) else _readable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_readable(v) for v in obj]
    return obj


def build_prompt(text: str, ctx: dict[str, Any], history: list[dict[str, Any]], *, limit: int = MAX_REPLY) -> str:
    facts = _readable({k: v for k, v in ctx.items() if v not in (None, [], {})})
    parts = [SYSTEM.format(limit=limit - 400), "", "CONTEXT (from The Crophecy's live system):", json.dumps(facts, ensure_ascii=False, indent=1)]
    if history:
        parts += ["", "EARLIER IN THIS CHAT:"] + [f"Farmer: {h['q']}\nYou: {h['a'][:400]}" for h in history]
    parts += ["", f"FARMER'S MESSAGE: {text}", "", 'Answer only with JSON: {"reply": "..."}']
    return "\n".join(parts)


def llm_available() -> bool:
    return fast_llm.available()


def ask_model(key: str, prompt: str, deadline_s: float) -> str:
    """One registry model's JSON text within `deadline_s`, through fast_llm's synchronous client (the shared async
    Gemini client is bound to the server's event loop). A call past the deadline is abandoned; the client's own HTTP
    timeout ends it."""
    return _pool.submit(fast_llm.ask, prompt, model_id=models.get(key)["model_id"]).result(timeout=deadline_s)


def parse_reply(text: str) -> str | None:
    try:
        data = json.loads(models.extract_json(text or ""))
    except (json.JSONDecodeError, TypeError):
        return None
    out = str(data.get("reply") or "").strip() if isinstance(data, dict) else ""
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"(?m)^\s*[-*•]\s+", "- ", out)
    return out if out and not world.looping(out) else None


def llm_answer(prompt: str) -> tuple[str, str] | None:
    """(reply, model key) from the first model that answers in time, else None."""
    if not llm_available():
        return None
    for key, deadline in MODELS:
        try:
            if reply := parse_reply(ask_model(key, prompt, deadline)):
                return reply, key
        except Exception:  # noqa: BLE001 - any model failure (timeout, quota, bad JSON) moves to the next model
            continue
    return None


def _pct(p: float) -> str:
    return f"{round(p * 100):d}%"


def fallback(text: str, ctx: dict[str, Any]) -> str:
    """The deterministic answer from the same context (offline, or every model failed)."""
    intents = ctx["intents"]
    if intents == ["greeting"]:
        return HELP
    lines: list[str] = []
    if "offtopic" in intents:
        lines.append("I'm a farm advisor, so I can't help with that (and I don't do day-ahead weather). "
                     "I can tell you about crop prices, drought and heat risk this season, what to plant, input costs, CAP policy and hedging.")
    if ctx.get("proxy"):
        lines.append(f"We have no {ctx['crop']} market yet, so I use {ctx['proxy']}, the closest traded crop.")
    for m in ctx.get("markets", [])[:3]:
        a = m.get("agents")
        agree = f" ({a['n']} AI agents, they {'mostly agree' if a['agreement'] == 'high' else 'partly disagree' if a['agreement'] == 'medium' else 'disagree a lot'})" if a else ""
        lines.append(f"- Our forecasters put {_pct(m['p'])} on: {m['title']}{agree}")
    if c := ctx.get("climate"):
        where = f"{c['region']} about {c['region_anomaly_c']:+.1f} C (heat stress {c['heat_stress']})" if c.get("region") else f"Europe {c['europe_anomaly_c']:+.1f} C"
        if "drought" in intents or "plant" in intents or "offtopic" in intents:
            lines.append(f"- Climate: the last 3 months ran {where} above normal. {c.get('enso', '')}".strip())
    for n in ctx.get("news", [])[:2]:
        lines.append(f"- Headline ({n['date']}): {n['title']}")
    if "inputs" in intents:
        lines.append("We have no fertiliser or energy price market, so I can't put a number on it. Nitrogen fertiliser is made from "
                     "natural gas, so dearer gas usually means dearer fertiliser some weeks later: buying part of your need early spreads the risk.")
    if "policy" in intents and not ctx.get("markets"):
        lines.append("I have no live market on that policy question yet; check your national paying agency for current CAP schemes.")
    if "sell_store" in intents or "hedging" in intents:
        lines.append("Options: sell part now and store the rest, or fix a price for part with a forward contract. Storing pays only if "
                     "the expected rise beats storage, interest and quality-loss costs.")
    if len(lines) <= int("offtopic" in intents):
        lines.append("I don't have data on that in our system yet. Try asking about a crop and a country, e.g. \"wheat France\".")
    lines.append(f"A forecast from our AI forecasters and players. {DISCLAIMER}")
    return "\n".join(lines)


def link_for(ctx: dict[str, Any], base: str = DEFAULT_BASE) -> str:
    base = base.rstrip("/")
    intents, crop, region = ctx.get("intents") or [], ctx.get("crop"), ctx.get("region")
    if crop in world.CROPS and ("drought" not in intents or intents[0] != "drought"):
        query = {"crop": crop} | ({"region": region} if region in world.REGIONS else {})
        return f"{base}/app/farmer?{urlencode(query)}"
    if (markets := ctx.get("markets")) and intents[0] not in ("drought", "crowd"):
        return f"{base}/app/market/{markets[0]['qid']}"
    return base + next((LINKS[i] for i in intents if i in LINKS), "/app/farmer/chat")


def fit(text: str, link: str, limit: int = MAX_REPLY) -> str:
    """The answer cut at a paragraph or sentence boundary so that it plus the link stays within `limit` characters."""
    tail = f"\n\nMore: {link}"
    room = limit - len(tail)
    text = text.strip()
    if len(text) > room:
        paras = text.rsplit("\n", 1)
        keep = paras[1].strip() if len(paras) == 2 and 0 < len(paras[1].strip()) <= 120 else ""  # the closing disclaimer
        room -= len(keep) + 2 if keep else 0
        cut = text[:room]
        stop = max(cut.rfind("\n"), cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = (cut[: stop + 1] if stop > room // 2 else cut[: room - 1].rsplit(" ", 1)[0] + "…").strip()
        text += f"\n\n{keep}" if keep else ""
    return text + tail


# ---------------------------------------------------------------- memory
def history(sender: str | None) -> list[dict[str, Any]]:
    if not sender:
        return []
    now = time.monotonic()
    with _mem_lock:
        turns = [t for t in _memory.get(sender, []) if now - t["at"] < MEMORY_TTL_S]
        if turns:
            _memory[sender] = turns
        else:
            _memory.pop(sender, None)
        return list(turns[-TURNS:])


def remember(sender: str | None, q: str, a: str, ctx: dict[str, Any]) -> None:
    if not sender:
        return
    with _mem_lock:
        if sender not in _memory and len(_memory) >= MEMORY_MAX_SENDERS:
            _memory.pop(next(iter(_memory)))
        turns = _memory.setdefault(sender, [])
        turns.append({"at": time.monotonic(), "q": q, "a": a, "crop": ctx.get("crop"), "region": ctx.get("region")})
        del turns[:-TURNS]


def forget_all() -> None:
    with _mem_lock:
        _memory.clear()


# ---------------------------------------------------------------- entry point
def advise(text: str, *, sender: str | None = None, base_url: str = DEFAULT_BASE, limit: int = MAX_REPLY) -> dict[str, Any]:
    """{reply, intents, crop, region, probability, confidence, link, source, model, latency_ms}."""
    started = time.monotonic()
    text = farmer_chat.clean_text(text)
    past = history(sender)
    ctx = gather(text, prior=past[-1] if past else None)
    link = link_for(ctx, base_url)
    answer, source, model = None, "fallback", None
    if ctx["intents"] == ["greeting"]:
        answer, source = HELP, "help"
    elif got := llm_answer(build_prompt(text, ctx, past, limit=limit)):
        (answer, model), source = got, "llm"
    if answer is None:
        answer = fallback(text, ctx)
    reply = fit(answer, link, limit)
    remember(sender, text, answer, ctx)
    fm = ctx.get("farmer_mode") or {}
    top = (ctx.get("markets") or [{}])[0]
    p = fm.get("p", top.get("p"))
    conf = ((top.get("agents") or {}).get("agreement")) if top else None
    return {"reply": reply, "intents": ctx["intents"], "crop": ctx.get("crop"), "region": ctx.get("region"),
            "probability": round(float(p), 3) if p is not None else None, "confidence": conf, "link": link, "source": source,
            "model": model, "latency_ms": round((time.monotonic() - started) * 1000)}
