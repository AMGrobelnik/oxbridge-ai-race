"""Gemini forecaster with masked prompts, cache and deterministic fallback (SPEC.md section 5, addendum R2). Owner: agents-replay."""

import asyncio
import json
import math
import os
import random
import re
import statistics
from pathlib import Path
from typing import Any

from agent.sealed.hashing import sha256_hex, sha256_json

from . import prices
from .types import Forecast, Mask, Persona, PriceFixture, QuestionInstance

CACHE = Path(__file__).resolve().parent / "data" / "llm_cache.json"
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"forecasts": {"type": "array", "items": {"type": "object", "properties": {"qid": {"type": "string"}, "p_yes": {"type": "number"}, "rationale": {"type": "string"}}, "required": ["qid", "p_yes", "rationale"]}}},
    "required": ["forecasts"],
}
WINDOW_MONTHS = 13
"""t-12 .. t0: the only price history an agent sees at a cutoff."""
P_MIN, P_MAX = 0.01, 0.99
CALL_TIMEOUT_S = float(os.environ.get("HARVEST_LLM_TIMEOUT_S") or 60)
CONCURRENCY = int(os.environ.get("HARVEST_LLM_CONCURRENCY") or 4)
_LABEL_RE = re.compile(r"\bCommodity ([A-Z])\b")
_SEMS: dict[int, asyncio.Semaphore] = {}
_cache: dict[str, Any] | None = None


def _clamp(p: float, lo: float = P_MIN, hi: float = P_MAX) -> float:
    return min(max(float(p), lo), hi)


def _month_idx(month: str) -> int:
    y, m = (int(x) for x in month.split("-"))
    return y * 12 + m - 1


def offline_env() -> bool:
    return os.environ.get("HARVEST_OFFLINE", "") not in ("", "0", "false")


# ---------------------------------------------------------------- masking (R2)
def make_mask(series_ids: list[str], seed: int) -> Mask:
    """Seeded shuffle: each series gets a label 'Commodity A', 'Commodity B', ..."""
    ids = sorted(series_ids)
    letters = [chr(ord("A") + i) for i in range(len(ids))]
    random.Random(seed).shuffle(letters)
    return {"commodities": {sid: f"Commodity {letters[i]}" for i, sid in enumerate(ids)}, "seed": seed}


def mask_window(points: list[tuple[str, float]], cutoff: str) -> list[tuple[str, float]]:
    """Relative months t-11..t0 and prices rebased to 100 at the first point."""
    if not points:
        return []
    base = float(points[0][1]) or 1.0
    c = _month_idx(cutoff)
    out: list[tuple[str, float]] = []
    for month, value in points:
        d = _month_idx(month) - c
        label = "t0" if d == 0 else (f"t{d}" if d < 0 else f"t+{d}")
        out.append((label, round(float(value) / base * 100.0, 2)))
    return out


def visible_window(fixture: PriceFixture, series_id: str, cutoff: str) -> list[tuple[str, float]]:
    return prices.window(fixture, series_id, cutoff, WINDOW_MONTHS)


def window_sha256(fixture: PriceFixture, series_id: str, cutoff: str) -> str:
    """sha256_json of the masked window the agents saw at this cutoff (R2 data-window hash)."""
    return sha256_json([list(p) for p in mask_window(visible_window(fixture, series_id, cutoff), cutoff)])


def _names(mask: Mask) -> dict[str, str]:
    """label -> real commodity name."""
    return {label: prices.SERIES.get(sid, (sid, ""))[0] for sid, label in mask["commodities"].items()}


def mask_text(text: str, mask: Mask) -> str:
    """Replace real commodity names with their masked labels (inverse of unmask)."""
    for label, name in sorted(_names(mask).items(), key=lambda kv: -len(kv[1])):
        text = re.sub(rf"\b{re.escape(name)}\b", label, text)
    return text


def unmask(text: str, mask: Mask) -> str:
    names = _names(mask)
    return _LABEL_RE.sub(lambda m: names.get(m.group(0), m.group(0)), text)


# ---------------------------------------------------------------- prompt
def _hard(questions: list[QuestionInstance]) -> list[QuestionInstance]:
    return [q for q in questions if q["kind"] == "hard" and q.get("series") and q.get("rule") and q.get("target") is not None]


def _masked_question(q: QuestionInstance, label: str, window: list[tuple[str, float]], ref: float) -> str:
    rule = q["rule"] or {}
    h = int(rule.get("horizon_months", 6))
    base_idx = round(float(q["base_value"] or window[-1][1]) / ref * 100.0, 1)
    target_idx = round(float(q["target"] or 0.0) / ref * 100.0, 1)
    direction = "above" if str(rule.get("type", "")).endswith("_gt") else "below"
    extra = ""
    if str(rule.get("type", "")).startswith("pct_change"):
        extra = f" (a change of {float(rule.get('threshold', 0.0)) * 100:+.0f}% from t0)"
    return f"Will {label} be {direction} index {target_idx}{extra} at t+{h}? It is at index {base_idx} at t0."


def build_prompt(persona: Persona, questions: list[QuestionInstance], fixture: PriceFixture, cutoff: str, mask: Mask) -> str:
    hard = _hard(questions)
    series = sorted({str(q["series"]) for q in hard}, key=lambda s: mask["commodities"].get(s, s))
    lines = [
        f"You are the forecasting agent \"{persona['name']}\" in an agricultural prediction market.",
        "Your world model (reason strictly from it):",
        persona["world_model"],
        "Your core beliefs:",
        *[f"- {b}" for b in persona["beliefs"]],
        "",
        "Commodity identities and calendar dates are hidden. Months are relative to now (t0). Each price is an index, 100 at the first month shown.",
    ]
    for sid in series:
        win = mask_window(visible_window(fixture, sid, cutoff), cutoff)
        label = mask["commodities"].get(sid, "Commodity ?")
        lines.append(f"{label}: " + ", ".join(f"{m}={v}" for m, v in win))
    lines += ["", "Questions:"]
    for i, q in enumerate(hard, start=1):
        sid = str(q["series"])
        raw = visible_window(fixture, sid, cutoff)
        ref = float(raw[0][1]) if raw else float(q["base_value"] or 1.0)
        lines.append(f"Q{i}: " + _masked_question(q, mask["commodities"].get(sid, "Commodity ?"), mask_window(raw, cutoff), ref or 1.0))
    lines += [
        "",
        "For every question give your probability that it resolves YES, as your world model sees it.",
        'Answer only with JSON: {"forecasts": [{"qid": "Q1", "p_yes": 0.0-1.0, "rationale": "<=200 chars, in your world model\'s voice"}]}',
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- fallback
def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _soft_forecast(persona: Persona, q: QuestionInstance) -> Forecast:
    lean = float(persona.get("soft_lean", 0.0))
    p = float(q.get("prior", 0.5)) + (lean if q["adverse_if"] == "yes" else -lean)
    return {"persona_id": persona["id"], "qid": q["qid"], "p_yes": round(_clamp(p), 4), "rationale": "World-model lean on the prior.", "source": "fallback"}


def fallback(persona: Persona, question: QuestionInstance, fixture: PriceFixture, cutoff: str) -> Forecast:
    """Deterministic forecast per heuristic over the visible window: lognormal move with a heuristic drift, then tilt."""
    if question["kind"] != "hard" or not question.get("rule") or question.get("target") is None:
        return _soft_forecast(persona, question)
    rule = question["rule"] or {}
    vals = [float(v) for _, v in visible_window(fixture, str(question["series"]), cutoff)]
    if len(vals) < 3:
        return {"persona_id": persona["id"], "qid": question["qid"], "p_yes": 0.5, "rationale": "Too little history; coin flip.", "source": "fallback"}
    heur = persona.get("heuristic", {})
    kind = heur.get("type", "base_rate")
    lookback = max(1, min(int(heur.get("lookback", 6)), len(vals) - 1))
    tilt = float(heur.get("tilt", 0.0))
    h = int(rule.get("horizon_months", 6))
    base = float(question["base_value"] or vals[-1])
    target = float(question["target"] or base)
    rets = [math.log(b / a) for a, b in zip(vals, vals[1:], strict=False) if a > 0 and b > 0]
    sigma = max(statistics.pstdev(rets) if len(rets) > 1 else 0.05, 0.02)
    trend = math.log(vals[-1] / vals[-1 - lookback]) / lookback
    x = math.log(target / base) if target > 0 and base > 0 else 0.0
    gt = str(rule.get("type", "")).endswith("_gt")
    mu, why, empirical = 0.0, "", None
    if kind == "momentum":
        mu, why = trend, f"{lookback}-month trend {trend * 100:+.1f}%/mo extrapolated"
    elif kind == "mean_revert":
        mean = statistics.fmean(vals[-1 - lookback:])
        mu, why = 0.5 * math.log(mean / base) / h, "fading the move back toward the recent average"
    elif kind == "shock":
        mu, sigma, why = 0.3 * trend, sigma * 1.5, "fat tails: volatility clusters after shocks"
    elif kind == "seasonal":
        mu = 0.7 * math.log(vals[min(h, len(vals) - 1)] / vals[0]) / h
        why = "same stretch of the crop calendar a year earlier"
    elif kind == "crowd":
        mu, why = 0.5 * trend, "close to consensus, leaning with the last few months"
    else:
        moves = [math.log(vals[i + h] / vals[i]) for i in range(len(vals) - h) if vals[i] > 0]
        if moves:
            empirical = sum(1 for m in moves if (m > x if gt else m < x)) / len(moves)
        why = "base rate of moves this size in the visible history"
    z = (x - mu * h) / (sigma * math.sqrt(h))
    p = 1.0 - _phi(z) if gt else _phi(z)
    if empirical is not None:
        p = 0.5 * p + 0.5 * empirical
    if kind == "crowd":
        p = 0.5 + 0.6 * (p - 0.5)
    p += tilt if gt else -tilt
    p = round(_clamp(p), 4)
    return {"persona_id": persona["id"], "qid": question["qid"], "p_yes": p, "rationale": f"{persona['name']}: {why}."[:200], "source": "fallback"}


# ---------------------------------------------------------------- cache + Gemini
def _load_cache() -> dict[str, Any]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE.read_text())
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is not None:
        try:  # merge what other processes wrote meanwhile, so parallel runs never drop entries
            _cache.update({k: v for k, v in json.loads(CACHE.read_text()).items() if k not in _cache})
        except (OSError, ValueError):
            pass
        CACHE.write_text(json.dumps(_cache, indent=0, sort_keys=True) + "\n")


def cache_key(persona: Persona, cutoff: str, prompt: str) -> str:
    return sha256_json({"persona": persona["id"], "cutoff": cutoff, "prompt_sha": sha256_hex(prompt.encode("utf-8"))})


def _sem() -> asyncio.Semaphore:
    loop = id(asyncio.get_running_loop())
    if loop not in _SEMS:
        _SEMS[loop] = asyncio.Semaphore(CONCURRENCY)
    return _SEMS[loop]


def _parse(text: str, n: int) -> dict[str, tuple[float, str]]:
    data = json.loads(text)
    rows = data.get("forecasts", []) if isinstance(data, dict) else data
    out: dict[str, tuple[float, str]] = {}
    for r in rows:
        qid = str(r.get("qid", "")).strip()
        if re.fullmatch(r"Q\d+", qid) and 1 <= int(qid[1:]) <= n:
            p = float(r["p_yes"])
            if math.isfinite(p):
                out[qid] = (_clamp(p), str(r.get("rationale", ""))[:200])
    return out


async def _ask(prompt: str) -> str:
    from agent.ai.gemini import ask_gemini

    async with _sem():
        return await asyncio.wait_for(ask_gemini(prompt, json_output=True, timeout_s=CALL_TIMEOUT_S), CALL_TIMEOUT_S * 2 + 5)


async def forecast(persona: Persona, questions: list[QuestionInstance], fixture: PriceFixture, cutoff: str, mask: Mask, *, offline: bool = False) -> list[Forecast]:
    offline = offline or offline_env()
    hard = _hard(questions)
    by_q: dict[str, Forecast] = {}
    if hard:
        prompt = build_prompt(persona, hard, fixture, cutoff, mask)
        key = cache_key(persona, cutoff, prompt)
        cache = _load_cache()
        rows: list[dict[str, Any]] | None = cache.get(key)
        source = "cache"
        if rows is None and not offline:
            try:
                parsed = _parse(await _ask(prompt), len(hard))
                rows = [{"qid": q["qid"], "p_yes": parsed[f"Q{i}"][0], "rationale": unmask(parsed[f"Q{i}"][1], mask)} for i, q in enumerate(hard, start=1) if f"Q{i}" in parsed]
                if rows:
                    cache[key] = rows
                    _save_cache()
                source = "gemini"
            except Exception:  # noqa: BLE001 - any LLM failure falls back to the deterministic heuristic
                rows = None
        for r in rows or []:
            by_q[r["qid"]] = {"persona_id": persona["id"], "qid": r["qid"], "p_yes": round(_clamp(r["p_yes"]), 4), "rationale": str(r["rationale"])[:200], "source": source}  # type: ignore[typeddict-item]
    out: list[Forecast] = []
    for q in questions:
        out.append(by_q.get(q["qid"]) or fallback(persona, q, fixture, cutoff))
    return out
