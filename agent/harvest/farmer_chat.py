"""The farmer assistant: free text in ("will wheat go up in France by harvest?", "maize Spain", "help"), a short plain answer out.

Channel-agnostic: WhatsApp (Twilio, Meta Cloud API) and the in-app chat at /app/farmer/chat all call `reply(text)`.
The answer reads the live market book: the matching crop market's price is the crowd's probability, the AI forecaster
closest to that price gives the one plain reason, and how much the forecasters agree sets the confidence. No betting.
When farmer mode's own module (agent.harvest.farmer with `farmer_view(crop=, region=)`) is present, its answer is used.
"""

import re
from statistics import pstdev
from typing import Any
from urllib.parse import urlencode

from agent.harvest import market

MAX_TEXT = 400
CROPS = {  # keyword -> canonical crop name used in market titles
    "wheat": "wheat", "durum": "wheat", "maize": "maize", "corn": "maize", "olive": "olive oil", "olives": "olive oil",
    "coffee": "coffee", "cocoa": "cocoa", "sugar": "sugar", "beet": "sugar", "sunflower": "sunflower oil", "rice": "rice",
}
REGIONS = {
    "france": "France", "fr": "France", "french": "France", "spain": "Spain", "es": "Spain", "spanish": "Spain",
    "italy": "Italy", "it": "Italy", "italian": "Italy", "greece": "Greece", "gr": "Greece", "greek": "Greece",
    "romania": "Romania", "ro": "Romania", "romanian": "Romania", "eu": "EU", "europe": "EU", "european": "EU",
}
HELP = ("Hi, I'm The Crophecy farm assistant. Ask me about a crop and a country, for example "
        "\"will wheat go up in France?\" or \"maize Spain\". I know wheat, maize, olive oil, sunflower oil, sugar, rice, "
        "coffee and cocoa in France, Spain, Italy, Greece, Romania and the EU. I give you a chance, one reason and how sure "
        "the forecasters are. It's a forecast, not advice, and there is no betting here.")
UNKNOWN_CROP = "Which crop do you mean? Try wheat, maize, olive oil, sunflower oil, sugar, rice, coffee or cocoa, for example \"maize Spain\"."
JARGON = re.compile(r"\bt ?[+-] ?\d|\b[AB]\b|\b\d+\.\d\b(?![%\d])|\bBrier\b|\bLMSR\b")  # bot shorthand a farmer would not follow
GREETING = re.compile(r"^\s*(hi|hello|hey|hola|bonjour|ciao|start|help|menu|\?)\W*$", re.I)


def clean_text(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:MAX_TEXT]


def parse(text: str) -> tuple[str | None, str | None]:
    """(crop, region) named in the text, either may be None."""
    words = re.findall(r"[a-z]+", text.lower())
    crop = next((CROPS[w] for w in words if w in CROPS), None)
    region = next((REGIONS[w] for w in words if w in REGIONS and (len(w) > 2 or w in ("eu",))), None)
    if region is None:  # two-letter codes only when written in capitals ("FR"), so "it" stays a pronoun
        region = next((REGIONS[w.lower()] for w in re.findall(r"\b[A-Z]{2}\b", text) if w.lower() in REGIONS), None)
    return crop, region


def _pct(p: float) -> str:
    return f"{round(p * 100):d}%"


def confidence(forecasts: list[dict[str, Any]], volume: float) -> str:
    """'high' / 'medium' / 'low' from how much the AI forecasters agree (and, with few of them, how much was traded)."""
    ps = [float(f["p_yes"]) for f in forecasts if f.get("p_yes") is not None]
    if len(ps) >= 3:
        spread = pstdev(ps)
        return "high" if spread < 0.08 else "medium" if spread < 0.16 else "low"
    return "medium" if volume >= 500 else "low"


def _reason(view: dict[str, Any]) -> str:
    forecasts = [f for f in view.get("agent_forecasts") or [] if f.get("rationale") and f.get("p_yes") is not None]
    plain = [f for f in forecasts if not JARGON.search(str(f["rationale"]))]
    if not plain:
        return "There are few plain forecasts yet, so this mostly reflects the market's own estimate from recent prices."
    best = min(plain, key=lambda f: abs(float(f["p_yes"]) - float(view["p_yes"])))
    return " ".join(str(best["rationale"]).split())[:240]


def _pick(views: list[dict[str, Any]], crop: str, region: str | None, direction: str | None) -> dict[str, Any] | None:
    def matches_crop(v: dict[str, Any]) -> bool:
        return crop in f"{v.get('crop', '')} {v.get('title', '')}".lower()

    cands = [v for v in views if v.get("status", "open") == "open" and matches_crop(v)]
    if not cands:
        return None

    def score(v: dict[str, Any]) -> tuple[int, int, int, int, float]:
        # A crop price is one world price, so "will my price go up" must get a price-rise market even when the only
        # market labelled with the farmer's region asks about a fall; the region breaks ties, then Europe-wide ones.
        title = str(v.get("title", "")).lower()
        moves = "above" if " above " in title else "below" if " below " in title else ""
        return (int(direction is not None and moves == direction), int(v.get("kind") == "hard"),
                int(bool(region) and v.get("region") == region), int(v.get("region") in ("EU", "EU importers")), float(v.get("volume") or 0))

    return max(cands, key=score)


def _direction(text: str) -> str | None:
    t = text.lower()
    if re.search(r"\b(up|rise|rising|higher|increase|above|climb|more)\b", t):
        return "above"
    if re.search(r"\b(down|fall|falling|drop|lower|decrease|below|cheaper|less)\b", t):
        return "below"
    return None


def farmer_link(base: str, crop: str, region: str | None) -> str:
    query = {"crop": crop} | ({"region": region} if region else {})
    return f"{base.rstrip('/')}/app/farmer?{urlencode(query)}"


def _farmer_mode(crop: str, region: str | None) -> dict[str, Any] | None:
    """Farmer mode's own answer when its module has landed, else None."""
    try:
        from agent.harvest import farmer  # type: ignore[attr-defined]  # optional: farmer mode
    except ImportError:
        return None
    view_fn = getattr(farmer, "farmer_view", None)
    if not callable(view_fn):
        return None
    try:
        data = view_fn(crop=crop, region=region)
    except (KeyError, ValueError, TypeError, LookupError):
        return None
    p = data.get("probability", data.get("p")) if isinstance(data, dict) else None
    if p is None:
        return None
    return {"p": float(p), "question": str(data.get("question") or data.get("title") or ""),
            "reason": str(data.get("reason") or ""), "confidence": str(data.get("confidence") or "medium")}


def _live(crop: str, region: str | None, direction: str | None) -> dict[str, Any] | None:
    book = market.live_book()
    with book.lock:
        views = [book.market_view(q) for q in book.markets]
    view = _pick(views, crop, region, direction)
    if view is None:
        return None
    return {"p": float(view["p_yes"]), "question": str(view.get("title", "")), "reason": _reason(view),
            "confidence": confidence(view.get("agent_forecasts") or [], float(view.get("volume") or 0)),
            "region": view.get("region") or ""}


def reply(text: str, *, base_url: str = "") -> dict[str, Any]:
    """{reply, crop, region, probability, confidence, link}; probability is None for help or an unknown crop."""
    text = clean_text(text)
    out: dict[str, Any] = {"crop": None, "region": None, "probability": None, "confidence": None, "link": None}
    if not text or GREETING.match(text):
        return out | {"reply": HELP}
    crop, region = parse(text)
    if crop is None:
        return out | {"reply": UNKNOWN_CROP}
    answer = _farmer_mode(crop, region) or _live(crop, region, _direction(text))
    if answer is None:
        return out | {"crop": crop, "region": region, "reply": f"I don't have a live forecast for {crop} right now. {UNKNOWN_CROP}"}
    link = farmer_link(base_url, crop, region)
    found = answer.get("region")
    note = f"No {region} market for {crop} asks this yet; the closest is {found}. " if region and found and found != region else ""
    lines = [
        f"{note}{answer['question'] or crop.capitalize()}".strip(),
        f"Chance: {_pct(answer['p'])}",
        f"Why: {answer['reason']}" if answer["reason"] else "",
        f"Confidence: {answer['confidence']}",
        f"More: {link}",
        "A forecast from The Crophecy's AI forecasters and players, not financial advice.",
    ]
    return out | {"crop": crop, "region": region, "probability": round(answer["p"], 3), "confidence": answer["confidence"],
                  "link": link, "reply": "\n".join(line for line in lines if line)}
