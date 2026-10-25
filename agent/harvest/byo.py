"""Bring your own world model: backtest a human-written paragraph on the sealed holdout (SPEC.md R1/R2).

The paragraph becomes a persona with the house defaults (base-rate heuristic, median risk and edge),
is committed to the ledger, then forecasts the sealed holdout cutoffs with exactly the house protocol:
the same question instances, the same mask and masked windows, and the same `forecaster.forecast`
code path, one batched call per holdout cutoff. Any timeout or failure falls back to the
deterministic heuristic, so a backtest always finishes.
"""

import asyncio
import os
import statistics
from typing import Any

from agent.sealed.hashing import sha256_json

from . import forecaster, models, scoring
from .types import Forecast, Persona

ORIGIN = "human-model"
PREFIX = "byo-"
COLOR = "#f59f00"
CUTOFF_TIMEOUT_S = float(os.environ.get("HARVEST_BYO_TIMEOUT_S") or 45)
MAX_CONCURRENT = int(os.environ.get("HARVEST_BYO_CONCURRENCY") or 3)
HEURISTIC = {"type": "base_rate", "lookback": 12, "tilt": 0.0}
RATIONALES = 6


def normalise(world_model: str) -> str:
    return " ".join(str(world_model).split())


def model_key() -> str:
    """The one pinned, leakage-clean registry model every written theory runs on."""
    return models.byo_model()


def world_model_sha(world_model: str) -> str:
    """Hash of the paragraph and the pinned model, so the same theory is never re-run under another name."""
    return sha256_json({"harvest-byo-world-model": normalise(world_model), "model": model_key()})


def model_id_for(sha: str) -> str:
    return f"{PREFIX}{sha[:12]}"


def is_byo(trader_id: str | None) -> bool:
    return bool(trader_id) and str(trader_id).startswith(PREFIX)


def _median(house: list[Persona], key: str, default: float) -> float:
    vals = [float(p[key]) for p in house if p.get(key) is not None]  # type: ignore[literal-required]
    return round(statistics.median(vals), 4) if vals else default


def make_persona(model_id: str, name: str, world_model: str, house: list[Persona]) -> Persona:
    return {
        "id": model_id, "name": name, "color": COLOR, "world_model": normalise(world_model),
        "beliefs": ["Reason only from the world model above."], "heuristic": dict(HEURISTIC),  # type: ignore[typeddict-item]
        "soft_lean": 0.0, "risk": _median(house, "risk", 0.25), "edge": _median(house, "edge", 0.03),
        "model": model_key(), "model_label": models.get(model_key())["label"],
    }


async def _forecast_cutoff(persona: Persona, cut: dict[str, Any], fixture: dict[str, Any], mask: dict[str, Any], timeout_s: float) -> list[Forecast]:
    qs, cutoff = cut["questions"], cut["cutoff"]
    try:
        return await asyncio.wait_for(forecaster.forecast(persona, qs, fixture, cutoff, mask), timeout_s)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - a timeout or any failure falls back: a backtest must always finish
        return [forecaster.fallback(persona, q, fixture, cutoff) for q in qs]  # type: ignore[arg-type]


async def run_forecasts(persona: Persona, result: dict[str, Any], fixture: dict[str, Any], *, timeout_s: float | None = None) -> list[list[Forecast]]:
    """One batched forecaster call per sealed holdout cutoff, concurrently, each under a timeout."""
    hold = scoring.scored_cutoffs(result, "holdout")
    limit = CUTOFF_TIMEOUT_S if timeout_s is None else timeout_s
    return list(await asyncio.gather(*[_forecast_cutoff(persona, c, fixture, result["mask"], limit) for c in hold]))


def summarise(persona: Persona, result: dict[str, Any], forecasts: list[list[Forecast]]) -> dict[str, Any]:
    """Brier on the sealed holdout, rank among the house models and the market, per-question rows."""
    hold = scoring.scored_cutoffs(result, "holdout")
    per_question: list[list[Any]] = []
    rationales: list[dict[str, Any]] = []
    sources: dict[str, int] = {}
    for cut, fcs in zip(hold, forecasts, strict=True):
        outcomes = {r["qid"]: int(r["outcome"]) for r in cut.get("resolutions", []) if r.get("outcome") is not None}
        titles = {q["qid"]: q.get("title", q["qid"]) for q in cut.get("questions", [])}
        for f in fcs:
            sources[f["source"]] = sources.get(f["source"], 0) + 1
            if f["qid"] not in outcomes:
                continue
            per_question.append([f["qid"], float(f["p_yes"]), outcomes[f["qid"]]])
            rationales.append({"qid": f["qid"], "title": titles.get(f["qid"], f["qid"]), "cutoff": cut["cutoff"], "p_yes": float(f["p_yes"]),
                               "outcome": outcomes[f["qid"]], "rationale": f.get("rationale", ""), "source": f["source"]})
    brier = scoring.mean_brier([(p, o) for _, p, o in per_question])
    names = {p["id"]: p.get("name", p["id"]) for p in result.get("personas", [])}
    # Compare like with like: the house agents running on the same pinned model (all of them in a single-model replay).
    same = {p["id"] for p in result.get("personas", []) if p.get("model") == persona.get("model")} or set(names)
    field = [{"id": pid, "name": names.get(pid, pid), "brier": b, "kind": "house"} for pid, b in scoring.persona_briers(result).items() if b is not None and pid in same]
    market_brier = scoring.leaderboard(result)["baselines"].get("market")
    if market_brier is not None:
        field.append({"id": "market", "name": "Market (final prices)", "brier": market_brier, "kind": "market"})
    rank = 1 + sum(1 for f in field if brier is not None and f["brier"] < brier)
    field.append({"id": persona["id"], "name": persona["name"], "brier": brier, "kind": ORIGIN})
    field.sort(key=lambda f: (f["brier"] is None, f["brier"] or 0.0))
    rationales.sort(key=lambda r: -abs(r["p_yes"] - 0.5))
    if "gemini" in sources or "llm" in sources or "cache" in sources:
        source = "llm" if "fallback" not in sources else "mixed"
    else:
        source = "fallback"
    return {
        "origin": ORIGIN, "model": persona.get("model"), "model_label": persona.get("model_label"), "brier": brier, "brier_holdout": brier, "n": len(per_question), "rank": rank, "of": len(field),
        "field": field, "per_question": per_question, "rationales": rationales[:RATIONALES], "source": source,
        "cutoffs": [c["cutoff"] for c in hold],
    }
