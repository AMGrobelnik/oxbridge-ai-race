"""Farmer mode, the climate-stress map and the disagreement index (SPEC.md section 16).

All three read the live book's market views (`LiveBook.market_view`) and are pure functions of them, so they work
offline and in tests:
- `disagreement` ranks open markets by how far the AI agents' latest forecasts spread (standard deviation of p_yes),
  with the most bullish and most bearish agent and their one-line reasons.
- `stress_map` colours each producing country by heat stress: the recent NOAA Europe land temperature anomaly
  (data/climate.json, mean of the last three months) times a hand-set regional exposure (the Mediterranean heats
  and dries faster than the European mean), and links each country to the markets about it.
- `farmer_answer` answers "will my crop's price rise by harvest?" from the crop's price-rise market, with the
  agents' spread as the confidence and a plain-English reason (Croppy via the fast LLM, else `farmer_reason`).
"""

import json
import re
import statistics
from typing import Any

from . import climate

REGIONS: dict[str, dict[str, float]] = {
    # lat/lng: the main arable area; exposure: heat-stress multiplier vs the European land mean (hand-set, approximate).
    "Spain": {"lat": 40.0, "lng": -3.7, "exposure": 1.3},
    "Greece": {"lat": 39.3, "lng": 22.0, "exposure": 1.3},
    "Italy": {"lat": 42.5, "lng": 13.0, "exposure": 1.2},
    "Romania": {"lat": 45.0, "lng": 25.5, "exposure": 1.1},
    "France": {"lat": 47.5, "lng": 2.3, "exposure": 1.0},
    "Germany": {"lat": 51.2, "lng": 10.4, "exposure": 0.9},
    "Poland": {"lat": 52.1, "lng": 19.4, "exposure": 0.9},
}
EUROPE_WIDE = ("EU", "EU importers")
STRESS_MAX_C = 3.0  # a 3 degree C anomaly is full stress (1.0)
RECENT_MONTHS = 3
# The market that asks "will this crop's price rise?", and the crop markets its reason may cite. Barley and rapeseed
# have no market yet, so they use the closest traded proxy and say so.
CROPS: dict[str, dict[str, Any]] = {
    "wheat": {"market": "wheat-up10-6m", "crops": ("wheat", "durum wheat")},
    "maize": {"market": "maize-up10-6m", "crops": ("maize", "feed maize")},
    "barley": {"market": "wheat-up10-6m", "crops": ("wheat", "durum wheat"), "proxy": "wheat"},
    "rapeseed": {"market": "sunoil-up10-6m", "crops": ("sunflower", "sunflower oil"), "proxy": "sunflower oil"},
    "sunflower": {"market": "sunoil-up10-6m", "crops": ("sunflower", "sunflower oil")},
}


def latest_agents(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The latest forecast per agent on one market view."""
    by: dict[str, dict[str, Any]] = {}
    for f in view.get("agent_forecasts") or []:
        old = by.get(f["persona_id"])
        if old is None or str(f.get("at") or "") >= str(old.get("at") or ""):
            by[f["persona_id"]] = f
    return list(by.values())


def _agent(f: dict[str, Any]) -> dict[str, Any]:
    return {"persona_id": f["persona_id"], "name": f.get("name", f["persona_id"]), "color": f.get("color"),
            "p_yes": round(float(f["p_yes"]), 4), "reason": str(f.get("rationale") or "")[:200]}


def spread(view: dict[str, Any]) -> dict[str, Any] | None:
    """Spread of the agents' latest forecasts on one market, or None with fewer than two agents."""
    agents = latest_agents(view)
    if len(agents) < 2:
        return None
    ps = [float(f["p_yes"]) for f in agents]
    hi = max(agents, key=lambda f: float(f["p_yes"]))
    lo = min(agents, key=lambda f: float(f["p_yes"]))
    return {"n_agents": len(ps), "mean": round(statistics.fmean(ps), 4), "std": round(statistics.pstdev(ps), 4),
            "range": round(max(ps) - min(ps), 4), "bull": _agent(hi), "bear": _agent(lo)}


def disagreement(views: list[dict[str, Any]], limit: int = 50) -> list[dict[str, Any]]:
    """Open markets ranked by the standard deviation of the agents' probabilities (most disagreement first)."""
    rows = []
    for v in views:
        if v.get("status") != "open" or (s := spread(v)) is None:
            continue
        rows.append({"qid": v["qid"], "title": v["title"], "region": v.get("region", ""), "crop": v.get("crop", ""),
                     "kind": v.get("kind", "hard"), "price": round(float(v["p_yes"]), 4), **s})
    rows.sort(key=lambda r: (-r["std"], -r["range"], r["qid"]))
    for i, r in enumerate(rows[:limit], 1):
        r["rank"] = i
    return rows[:limit]


def recent_anomaly(data: dict[str, Any] | None, months: int = RECENT_MONTHS) -> dict[str, Any] | None:
    """Mean of the last `months` Europe land temperature anomalies, with the months it covers."""
    series = ((data or {}).get("series") or {}).get("europe_land")
    pts = (series or {}).get("points") or []
    if not pts:
        return None
    last = pts[-months:]
    return {"value_c": round(statistics.fmean(v for _, v in last), 3), "from": last[0][0], "to": last[-1][0],
            "source": series.get("name"), "url": series.get("url")}


def level(stress: float) -> str:
    return "severe" if stress >= 0.75 else "high" if stress >= 0.5 else "moderate" if stress >= 0.25 else "low"


def stress_map(views: list[dict[str, Any]], data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Producing countries with heat stress (0..1) and the qids of the open markets about them."""
    data = climate.load_climate() if data is None else data
    anom = recent_anomaly(data)
    base = anom["value_c"] if anom else 0.0
    open_views = [v for v in views if v.get("status") == "open"]
    regions = []
    for name, r in REGIONS.items():
        heat = round(base * r["exposure"], 2)
        stress = round(min(1.0, max(0.0, heat / STRESS_MAX_C)), 3)
        linked = [v for v in open_views if v.get("region") == name]
        regions.append({"region": name, "lat": r["lat"], "lng": r["lng"], "exposure": r["exposure"], "heat_anomaly_c": heat,
                        "stress": stress, "level": level(stress), "qids": [v["qid"] for v in linked],
                        "markets": [{"qid": v["qid"], "title": v["title"], "crop": v.get("crop", ""), "p_yes": round(float(v["p_yes"]), 4)}
                                    for v in linked]})
    regions.sort(key=lambda x: -x["stress"])
    wide = [{"qid": v["qid"], "title": v["title"], "crop": v.get("crop", ""), "p_yes": round(float(v["p_yes"]), 4)}
            for v in open_views if v.get("region") in EUROPE_WIDE]
    return {"anomaly": anom, "stress_max_c": STRESS_MAX_C, "regions": regions, "europe_wide": wide,
            "method": f"stress = (Europe land anomaly, mean of the last {RECENT_MONTHS} months) x regional exposure / {STRESS_MAX_C} C, "
                      "capped at 1; exposure is a hand-set approximation (Mediterranean heats faster than the European mean)."}


def confidence(s: dict[str, Any] | None) -> dict[str, Any]:
    """How much the agents agree: high below 8 points of standard deviation, medium below 15, else low."""
    if not s:
        return {"level": "low", "std": None, "n_agents": 0, "text": "Too few AI agents have forecast this yet."}
    std = s["std"]
    lvl = "high" if std < 0.08 else "medium" if std < 0.15 else "low"
    words = {"high": "the AI agents mostly agree", "medium": "the AI agents partly disagree", "low": "the AI agents disagree a lot"}
    return {"level": lvl, "std": std, "n_agents": s["n_agents"],
            "text": f"{lvl.capitalize()}: {words[lvl]} (spread {std * 100:.0f} points across {s['n_agents']} agents)."}


def farmer_context(views: list[dict[str, Any]], crop: str, region: str, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Everything the farmer answer needs, or None for an unknown crop or a missing market."""
    spec = CROPS.get(crop)
    if spec is None:
        return None
    by_spec = {str(v["qid"]).split("@")[0]: v for v in views}
    view = by_spec.get(spec["market"])
    if view is None:
        return None
    s = spread(view)
    regional = next((r for r in stress_map(views, data)["regions"] if r["region"] == region), None)
    related = [{"qid": v["qid"], "title": v["title"], "p_yes": round(float(v["p_yes"]), 4)} for v in views
               if v["qid"] != view["qid"] and v.get("status") == "open" and v.get("crop") in spec["crops"]
               and v.get("region") in (region, *EUROPE_WIDE)]
    return {"crop": crop, "region": region, "proxy": spec.get("proxy"),
            "question": f"Will my {crop} price rise by harvest?",
            "market": {"qid": view["qid"], "title": view["title"], "resolve_month": view.get("resolve_month"),
                       "base_value": view.get("base_value"), "target": view.get("target")},
            "probability": round(float(view["p_yes"]), 4), "agents": s, "confidence": confidence(s),
            "stress": {k: regional[k] for k in ("heat_anomaly_c", "stress", "level")} if regional else None, "related": related[:3]}


def farmer_reason(ctx: dict[str, Any]) -> str:
    """Deterministic plain-English reason (the offline fallback for Croppy)."""
    p, m = ctx["probability"], ctx["market"]
    parts = []
    if ctx["proxy"]:
        parts.append(f"There is no {ctx['crop']} market yet, so this uses {ctx['proxy']}, the closest traded crop.")
    parts.append(f"The market gives a {p * 100:.0f}% chance that the price ends {_rise(m)} by {m['resolve_month'] or 'harvest'}.")
    s = ctx["agents"]
    if s:
        parts.append(f"The most bullish AI ({s['bull']['name']}, {s['bull']['p_yes'] * 100:.0f}%) says: \"{s['bull']['reason']}\" "
                     f"The most bearish ({s['bear']['name']}, {s['bear']['p_yes'] * 100:.0f}%) says: \"{s['bear']['reason']}\"")
    if st := ctx["stress"]:
        parts.append(f"Heat stress in {ctx['region']} is {st['level']} right now (about {st['heat_anomaly_c']:+.1f} °C above normal).")
    return " ".join(parts)


def _rise(m: dict[str, Any]) -> str:
    if m.get("base_value") and m.get("target"):
        return f"more than {(float(m['target']) / float(m['base_value']) - 1) * 100:.0f}% higher than today"
    return "higher"


def build_prompt(ctx: dict[str, Any]) -> str:
    s, st, m = ctx["agents"], ctx["stress"], ctx["market"]
    lines = ["You are Croppy, the friendly guide of The Crophecy, a play-credit prediction market on European agriculture.",
             f"A {ctx['crop']} farmer in {ctx['region']} asks: will my {ctx['crop']} price rise by harvest?",
             f"Market used: {m['title']} (YES = price {_rise(m)}). Market probability: {ctx['probability'] * 100:.0f}%."]
    if ctx["proxy"]:
        lines.append(f"There is no {ctx['crop']} market; {ctx['proxy']} is the proxy. Say so.")
    if s:
        lines.append(f"{s['n_agents']} AI agents, mean {s['mean'] * 100:.0f}%, spread {s['std'] * 100:.0f} points.")
        lines.append(f"Most bullish: {s['bull']['name']} {s['bull']['p_yes'] * 100:.0f}%: {s['bull']['reason']}")
        lines.append(f"Most bearish: {s['bear']['name']} {s['bear']['p_yes'] * 100:.0f}%: {s['bear']['reason']}")
    if st:
        lines.append(f"Heat stress in {ctx['region']}: {st['level']} ({st['heat_anomaly_c']:+.1f} C above normal).")
    lines += ["", "In 2-3 plain sentences (under 70 words, no jargon, no betting advice), explain the probability to the farmer. "
              "Do not invent facts.", 'Answer only with JSON: {"reason": "..."}']
    return "\n".join(lines)


def parse(text: str) -> str | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0) if match else text)
    except (json.JSONDecodeError, TypeError):
        return None
    out = data.get("reason") if isinstance(data, dict) else None
    return str(out).strip()[:700] if out and str(out).strip() else None
