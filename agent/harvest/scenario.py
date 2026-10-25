"""Scenario sliders: the house world models re-forecast the open live markets under a what-if climate scenario.

A scenario is a temperature anomaly per European region (degrees C), a rainfall change (percent vs normal) and an
El Nino index (ENSO, -2 La Nina .. +2 strong El Nino). `deterministic` turns it into a climate stress per market
(region heat, dryness and ENSO, weighted by crop) and each persona moves its current forecast by that stress times
its own climate sensitivity, so the result is instant and offline. `llm_rows` asks a fast LLM to role-play every
persona on the most exposed markets in one batched call; its answers replace the deterministic ones there.
Scenario values are snapped to the slider steps so equal scenarios share a cache key.
"""

import json
import math
import re
from typing import Any

from agent.sealed.hashing import sha256_json

from .types import Persona

REGIONS = ("France", "Spain", "Italy", "Germany", "Poland", "Romania", "Greece")
TEMP_RANGE, TEMP_STEP = (0.0, 3.0), 0.5
RAIN_RANGE, RAIN_STEP = (-50.0, 50.0), 10.0
ENSO_RANGE, ENSO_STEP = (-2.0, 2.0), 0.5
LLM_MARKETS = 6
PRESETS: dict[str, dict[str, Any]] = {
    "Mediterranean heatwave": {"temp_anomaly": {"Spain": 2.5, "Italy": 2.0, "Greece": 2.5, "France": 1.0}, "rain_pct": -30, "enso": 0},
    "Central European drought": {"temp_anomaly": {"Germany": 2.0, "Poland": 2.0, "France": 1.5, "Romania": 1.5}, "rain_pct": -40, "enso": 0},
    "Strong El Niño": {"temp_anomaly": {"Spain": 0.5, "Italy": 0.5, "France": 0.5}, "rain_pct": 0, "enso": 2.0},
    "Cool, wet year": {"temp_anomaly": {}, "rain_pct": 30, "enso": -1.0},
    "Normal year": {"temp_anomaly": {}, "rain_pct": 0, "enso": 0},
}
P_MIN, P_MAX = 0.02, 0.98

# Logit shift per unit of driver, by crop: per degree C, per unit of dryness (-rain% / 50) and per ENSO unit.
CROP_WEIGHTS: dict[str, tuple[float, float, float]] = {
    "wheat": (0.35, 0.9, 0.15), "durum wheat": (0.4, 1.0, 0.1), "maize": (0.45, 1.1, 0.1), "feed maize": (0.45, 1.1, 0.1),
    "olive oil": (0.45, 1.2, 0.0), "sunflower": (0.35, 1.0, 0.05), "sunflower oil": (0.35, 1.0, 0.05),
    "sugar beet": (0.3, 0.8, 0.35), "rice": (0.3, 0.9, 0.4), "coffee": (0.05, 0.1, 0.45), "cocoa": (0.05, 0.1, 0.55),
    "water": (0.5, 1.4, 0.0), "all crops": (0.4, 0.9, 0.05), "farmland": (0.15, 0.3, 0.0),
}
DEFAULT_WEIGHTS = (0.3, 0.8, 0.1)
# How strongly each persona's world model lets climate stress move its forecast.
SENSITIVITY = {"climate-pessimist": 1.6, "agronomist": 1.3, "efficient-market-skeptic": 1.1, "geopolitics-hawk": 0.8,
               "land-grab-realist": 0.7, "macro-dollar": 0.6, "adaptation-optimist": 0.45, "crowd-follower": 0.5}
VOICE = {"climate-pessimist": "Heat compounds every shock", "agronomist": "In the field this means yield loss",
         "efficient-market-skeptic": "The market will underprice this", "geopolitics-hawk": "Stress feeds export politics",
         "land-grab-realist": "Scarcity pulls in land buyers", "macro-dollar": "Weather is second to the dollar",
         "adaptation-optimist": "Farmers adapt, so only a nudge", "crowd-follower": "I follow the crowd's reaction"}


def _snap(x: Any, lo: float, hi: float, step: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        v = 0.0
    if not math.isfinite(v):
        v = 0.0
    return round(min(hi, max(lo, round(v / step) * step)), 2)


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """The request body as a clean scenario: every region present, values clamped and snapped to the slider steps."""
    temps = raw.get("temp_anomaly") if isinstance(raw.get("temp_anomaly"), dict) else {}
    by_lower = {str(k).strip().lower(): v for k, v in temps.items()}
    return {"temp_anomaly": {r: _snap(by_lower.get(r.lower(), 0.0), *TEMP_RANGE, TEMP_STEP) for r in REGIONS},
            "rain_pct": _snap(raw.get("rain_pct", 0.0), *RAIN_RANGE, RAIN_STEP),
            "enso": _snap(raw.get("enso", 0.0), *ENSO_RANGE, ENSO_STEP)}


def key(scenario: dict[str, Any]) -> str:
    return sha256_json({"v": 1, "scenario": scenario})


def logit(p: float) -> float:
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def clamp(p: float) -> float:
    return min(P_MAX, max(P_MIN, p))


def direction(q: dict[str, Any]) -> int:
    """+1 when climate stress makes YES more likely (price above / rises, or a stress event), -1 for 'lower/below' prices."""
    rule = q.get("rule") or {}
    return -1 if str(rule.get("type", "")).endswith("_lt") else 1


def stress(q: dict[str, Any], scenario: dict[str, Any]) -> tuple[float, list[str]]:
    """Climate stress on market `q` in logit units (before direction and persona), plus the drivers that matter."""
    temps = scenario["temp_anomaly"]
    europe = sum(temps.values()) / len(temps)
    region = str(q.get("region") or "")
    local = temps.get(region)
    heat = europe if local is None else 0.6 * local + 0.4 * europe
    dry = -scenario["rain_pct"] / 50.0
    wt, wr, we = CROP_WEIGHTS.get(str(q.get("crop") or ""), DEFAULT_WEIGHTS)
    value = wt * heat + wr * dry + we * scenario["enso"]
    drivers = []
    if wt * heat >= 0.1:
        drivers.append(f"+{local:.1f}°C in {region}" if local else f"+{europe:.1f}°C across Europe")
    if abs(wr * dry) >= 0.05:
        drivers.append(f"{abs(scenario['rain_pct']):.0f}% {'less' if dry > 0 else 'more'} rain")
    if abs(we * scenario["enso"]) >= 0.05:
        drivers.append(f"{'El Niño' if scenario['enso'] > 0 else 'La Niña'} {abs(scenario['enso']):.1f}")
    return value, drivers


def latest_forecasts(agent_forecasts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for f in agent_forecasts:
        old = by.get(f["persona_id"])
        if old is None or str(f.get("at") or "") >= str(old.get("at") or ""):
            by[f["persona_id"]] = f
    return by


def _rationale(persona_id: str, drivers: list[str], delta: float) -> str:
    if not drivers or abs(delta) < 0.005:
        return "This scenario barely touches this market, so I hold my view."
    move = "raise" if delta > 0 else "cut"
    return f"{VOICE.get(persona_id, 'My world model reacts')}: {', '.join(drivers)}, so I {move} YES by {abs(delta) * 100:.0f} points."


def deterministic(markets: list[dict[str, Any]], personas: list[Persona], scenario: dict[str, Any]) -> list[dict[str, Any]]:
    """Per market: baseline vs scenario probability for every persona and the market, offline and instant."""
    out = []
    for m in markets:
        s, drivers = stress(m, scenario)
        signed = s * direction(m)
        latest = latest_forecasts(m.get("agent_forecasts") or [])
        agents = []
        for p in personas:
            base = float((latest.get(p["id"]) or {}).get("p_yes", m["p_yes"]))
            scen = clamp(sigmoid(logit(base) + signed * SENSITIVITY.get(p["id"], 1.0)))
            agents.append({"persona_id": p["id"], "name": p["name"], "color": p.get("color", "#888888"), "baseline": round(base, 4),
                           "scenario": round(scen, 4), "delta": round(scen - base, 4), "rationale": _rationale(p["id"], drivers, scen - base),
                           "source": "fallback"})
        out.append({"qid": m["qid"], "title": m["title"], "kind": m.get("kind", "hard"), "region": m.get("region", ""),
                    "crop": m.get("crop", ""), "stress": round(signed, 4), "drivers": drivers, "agents": agents})
    return out


def finish(rows: list[dict[str, Any]], prices: dict[str, float], top: int = 3) -> list[dict[str, Any]]:
    """Adds the market's baseline/scenario price (the market moves by the agents' mean logit shift) and the top rationales;
    sorts by the size of the move."""
    for r in rows:
        shifts = [logit(a["scenario"]) - logit(a["baseline"]) for a in r["agents"]]
        base = prices[r["qid"]]
        scen = clamp(sigmoid(logit(base) + (sum(shifts) / len(shifts) if shifts else 0.0)))
        r.update(baseline=round(base, 4), scenario=round(scen, 4), delta=round(scen - base, 4))
        movers = sorted(r["agents"], key=lambda a: -abs(a["delta"]))[:top]
        r["rationales"] = [{"persona_id": a["persona_id"], "name": a["name"], "delta": a["delta"], "text": a["rationale"], "source": a["source"]}
                           for a in movers]
    rows.sort(key=lambda r: -abs(r["delta"]))
    return rows


# ---------------------------------------------------------------- LLM (one batched call for the most exposed markets)
def describe(scenario: dict[str, Any]) -> str:
    temps = ", ".join(f"{r} +{t:.1f}°C" for r, t in scenario["temp_anomaly"].items())
    enso = scenario["enso"]
    phase = "neutral" if enso == 0 else ("El Niño" if enso > 0 else "La Niña")
    return f"Temperature anomaly this growing season: {temps}. Rainfall {scenario['rain_pct']:+.0f}% vs normal. ENSO index {enso:+.1f} ({phase})."


def build_prompt(rows: list[dict[str, Any]], personas: list[Persona], scenario: dict[str, Any]) -> str:
    lines = ["You simulate the forecasting agents of an agricultural prediction market, each reasoning only from its own world model.",
             "Agents:"]
    for p in personas:
        lines.append(f"- {p['id']}: {p['name']}. {str(p.get('world_model', ''))[:280]}")
    lines += ["", "Hypothetical scenario: " + describe(scenario), "", "Markets, with each agent's current probability of YES:"]
    for i, r in enumerate(rows, start=1):
        cur = ", ".join(f"{a['persona_id']}={a['baseline']:.2f}" for a in r["agents"])
        lines.append(f"M{i}: {r['title']} [{r['region']}, {r['crop']}] current: {cur}")
    lines += ["", "For every agent and every market, give the agent's new probability of YES if this scenario happens, as its world model sees it.",
              'Answer only with JSON: {"forecasts": [{"m": "M1", "agent": "<agent id>", "p_yes": 0.0-1.0, "why": "<=70 chars, in the agent\'s voice"}]}']
    return "\n".join(lines)


def parse_llm(text: str, rows: list[dict[str, Any]], persona_ids: set[str]) -> dict[tuple[str, str], tuple[float, str]]:
    """(qid, persona_id) -> (p_yes, why) from the model's JSON; skips malformed rows."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0) if match else text)
        items = data.get("forecasts", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, TypeError):
        items = []  # malformed reply: salvage the rows that are valid JSON on their own
        for raw in re.findall(r"\{[^{}]*\}", text or ""):
            try:
                items.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    out: dict[tuple[str, str], tuple[float, str]] = {}
    for f in items:
        if not isinstance(f, dict):
            continue
        m, agent = str(f.get("m", "")).strip(), str(f.get("agent", "")).strip()
        if not re.fullmatch(r"M\d+", m) or not 1 <= int(m[1:]) <= len(rows) or agent not in persona_ids:
            continue
        try:
            p = float(f["p_yes"])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(p):
            out[(rows[int(m[1:]) - 1]["qid"], agent)] = (clamp(p), str(f.get("why", ""))[:160])
    return out


def merge_llm(rows: list[dict[str, Any]], llm: dict[tuple[str, str], tuple[float, str]], source: str) -> int:
    """Replaces deterministic agent answers with the LLM's where it gave one. Returns how many were replaced."""
    n = 0
    for r in rows:
        for a in r["agents"]:
            hit = llm.get((r["qid"], a["persona_id"]))
            if hit:
                a.update(scenario=round(hit[0], 4), delta=round(hit[0] - a["baseline"], 4), rationale=hit[1] or a["rationale"], source=source)
                n += 1
    return n


def exposed(rows: list[dict[str, Any]], k: int = LLM_MARKETS) -> list[dict[str, Any]]:
    """The k markets the scenario stresses most (the ones worth an LLM's attention)."""
    return sorted((r for r in rows if abs(r["stress"]) > 0.01), key=lambda r: -abs(r["stress"]))[:k]
