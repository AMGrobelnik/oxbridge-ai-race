"""Baseline models: very simple quant forecasters as leaderboard accounts, scored on the same sealed holdout.

No LLM. Momentum, Mean-reversion and Seasonal are the deterministic `forecaster.fallback` heuristics
run with a neutral persona (no tilt) over the same masked t-12..t0 window the agents see. Climatology
is the scoring baseline's base rate (the share of dev questions that resolved YES), and
Market-follower copies the market's closing price at each cutoff (the scoring "market" baseline).
"""

from typing import Any

from . import forecaster, scoring

SPECS: tuple[dict[str, Any], ...] = (
    {"id": "baseline:momentum", "username": "momentum-bot", "name": "Momentum", "heuristic": {"type": "momentum", "lookback": 6, "tilt": 0.0},
     "about": "Extrapolates the last 6 months' price trend."},
    {"id": "baseline:mean-reversion", "username": "mean-reversion-bot", "name": "Mean-reversion",
     "heuristic": {"type": "mean_revert", "lookback": 6, "tilt": 0.0}, "about": "Bets that prices drift back to their 6-month average."},
    {"id": "baseline:seasonal", "username": "seasonal-bot", "name": "Seasonal", "heuristic": {"type": "seasonal", "lookback": 12, "tilt": 0.0},
     "about": "Repeats the move of the same months a year earlier."},
    {"id": "baseline:climatology", "username": "climatology-bot", "name": "Climatology", "rule": "base_rate",
     "about": "Always says the base rate: the share of past questions that resolved YES."},
    {"id": "baseline:market-follower", "username": "market-follower-bot", "name": "Market-follower", "rule": "market",
     "about": "Copies the market's closing price."},
)
COLOR = "#868e96"
USERNAMES = frozenset(s["username"] for s in SPECS)
_cache: dict[str, Any] = {}


def _persona(spec: dict[str, Any]) -> dict[str, Any]:
    return {"id": spec["id"], "name": spec["name"], "color": COLOR, "world_model": spec["about"], "beliefs": [],
            "heuristic": dict(spec["heuristic"]), "soft_lean": 0.0, "risk": 0.0, "edge": 1.0}


def base_rate(result: dict[str, Any]) -> float | None:
    """The climatology rate used by scoring: dev YES share, else the holdout's own."""
    dev = [o for c in scoring.scored_cutoffs(result, "dev") for o in scoring._outcomes(c).values()]
    if dev:
        return sum(dev) / len(dev)
    hold = list(scoring.holdout_outcomes(result).values())
    return sum(hold) / len(hold) if hold else None


def per_question(spec: dict[str, Any], result: dict[str, Any], fixture: dict[str, Any] | None) -> list[list[Any]]:
    """[[qid, p_yes, outcome], ...] for every resolved holdout question this baseline can forecast."""
    rate = base_rate(result) if spec.get("rule") == "base_rate" else None
    persona = _persona(spec) if "heuristic" in spec else None
    rows: list[list[Any]] = []
    for cut in scoring.scored_cutoffs(result, "holdout"):
        outcomes = {r["qid"]: int(r["outcome"]) for r in cut.get("resolutions", []) if r.get("outcome") is not None}
        prices = cut.get("final_prices", {})
        for q in cut.get("questions", []):
            qid = q["qid"]
            if qid not in outcomes:
                continue
            if spec.get("rule") == "market":
                if qid not in prices:
                    continue
                p = float(prices[qid])
            elif rate is not None:
                p = rate
            elif persona is not None and fixture is not None:
                p = float(forecaster.fallback(persona, q, fixture, cut["cutoff"])["p_yes"])  # type: ignore[arg-type]
            else:
                continue
            rows.append([qid, p, outcomes[qid]])
    return rows


def rows(result: dict[str, Any] | None, fixture: dict[str, Any] | None) -> list[dict[str, Any]]:
    """One scored row per baseline: Brier on the sealed holdout with the same bootstrap interval as the models."""
    if not result:
        return []
    key = (id(result), id(fixture))
    if _cache.get("key") == key:
        return list(_cache["rows"])
    market = scoring._holdout_question_briers(result)[1]
    out = []
    for spec in SPECS:
        pq = per_question(spec, result, fixture)
        brier = scoring.mean_brier([(float(p), int(o)) for _, p, o in pq])
        row: dict[str, Any] = {"id": spec["id"], "username": spec["username"], "name": spec["name"], "color": COLOR, "about": spec["about"],
                               "brier_holdout": brier, "n": len(pq)}
        if pq:
            row.update(scoring._interval_row({str(q): scoring.brier(float(p), int(o)) for q, p, o in pq}, market))
        else:
            row.update(brier_holdout_lo=None, brier_holdout_hi=None, beats_market_ci=False)
        out.append(row)
    _cache.update(key=key, rows=out)
    return list(out)
