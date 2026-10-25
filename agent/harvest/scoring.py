"""Brier, PnL and the world-model leaderboard (SPEC.md section 9). Owner: market-engine."""

import random
from functools import lru_cache
from typing import Any, cast

from .types import Leaderboard, ModelScore

COIN_BRIER = 0.25
CI_LEVEL = 0.90
CI_RESAMPLES = 2000
CI_SEED = 20261025


def brier(p: float, outcome: int) -> float:
    return (p - outcome) ** 2


def mean_brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum(brier(p, o) for p, o in pairs) / len(pairs) if pairs else None


def _outcomes(cutoff: dict[str, Any]) -> dict[str, int]:
    return {r["qid"]: int(r["outcome"]) for r in cutoff.get("resolutions", []) if r.get("outcome") is not None}


def scored_cutoffs(result_like: dict[str, Any], split: str) -> list[dict[str, Any]]:
    return [c for c in result_like.get("cutoffs", []) if c.get("split") == split]


def holdout_outcomes(result_like: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in scored_cutoffs(result_like, "holdout"):
        out.update(_outcomes(c))
    return out


def persona_briers(result_like: dict[str, Any], qids: set[str] | None = None, split: str = "holdout") -> dict[str, float | None]:
    """Mean Brier per persona over resolved forecasts in `split`, optionally restricted to `qids`."""
    pairs: dict[str, list[tuple[float, int]]] = {p["id"]: [] for p in result_like.get("personas", [])}
    for c in scored_cutoffs(result_like, split):
        outcomes = _outcomes(c)
        for f in c.get("forecasts", []):
            if f["qid"] in outcomes and (qids is None or f["qid"] in qids):
                pairs.setdefault(f["persona_id"], []).append((float(f["p_yes"]), outcomes[f["qid"]]))
    return {pid: mean_brier(v) for pid, v in pairs.items()}


def _split_stats(result_like: dict[str, Any], split: str) -> tuple[dict[str, list[tuple[float, int]]], dict[str, float]]:
    pairs: dict[str, list[tuple[float, int]]] = {}
    pnl: dict[str, float] = {}
    for c in scored_cutoffs(result_like, split):
        outcomes = _outcomes(c)
        for f in c.get("forecasts", []):
            if f["qid"] in outcomes:
                pairs.setdefault(f["persona_id"], []).append((float(f["p_yes"]), outcomes[f["qid"]]))
        for t in c.get("trades", []):
            pid = t["trader"].split(":", 1)[1] if t["trader"].startswith("agent:") else t["trader"]
            pnl[pid] = pnl.get(pid, 0.0) - float(t["spend"])  # spend already includes the fee
        for p in c.get("payouts", []):
            pid = p["trader"].split(":", 1)[1] if p["trader"].startswith("agent:") else p["trader"]
            pnl[pid] = pnl.get(pid, 0.0) + float(p["amount"])
    return pairs, pnl


def _baseline(result_like: dict[str, Any], split: str) -> tuple[float | None, float | None]:
    """(market Brier of final prices, climatology Brier of the dev base rate) on `split`."""
    market_pairs, outcomes_all = [], []
    for c in scored_cutoffs(result_like, split):
        outcomes = _outcomes(c)
        prices = c.get("final_prices", {})
        market_pairs += [(float(prices[q]), o) for q, o in outcomes.items() if q in prices]
        outcomes_all += list(outcomes.values())
    dev = [o for c in scored_cutoffs(result_like, "dev") for o in _outcomes(c).values()]
    base_rate = sum(dev) / len(dev) if dev else (sum(outcomes_all) / len(outcomes_all) if outcomes_all else None)
    clim = mean_brier([(base_rate, o) for o in outcomes_all]) if base_rate is not None else None
    return mean_brier(market_pairs), clim


def _holdout_question_briers(result_like: dict[str, Any]) -> tuple[dict[str, dict[str, float]], dict[str, float]]:
    """Per-question Brier on the holdout: ({persona: {qid: brier}}, {qid: market brier})."""
    per: dict[str, dict[str, float]] = {}
    market: dict[str, float] = {}
    for c in scored_cutoffs(result_like, "holdout"):
        outcomes = _outcomes(c)
        prices = c.get("final_prices", {})
        for q, o in outcomes.items():
            if q in prices:
                market[q] = brier(float(prices[q]), o)
        for f in c.get("forecasts", []):
            if f["qid"] in outcomes:
                per.setdefault(f["persona_id"], {})[f["qid"]] = brier(float(f["p_yes"]), outcomes[f["qid"]])
    return per, market


@lru_cache(maxsize=256)
def _bootstrap(values: tuple[float, ...]) -> tuple[float, float]:
    """Percentile CI (CI_LEVEL) of the mean of `values`; fixed seed so it is reproducible."""
    n = len(values)
    rng = random.Random(CI_SEED)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(CI_RESAMPLES))
    tail = (1 - CI_LEVEL) / 2
    return means[int(tail * CI_RESAMPLES)], means[min(CI_RESAMPLES - 1, int((1 - tail) * CI_RESAMPLES))]


def holdout_intervals(result_like: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per persona bootstrap intervals over holdout questions, computed at read time.

    brier_holdout_lo/hi bound the persona's mean Brier; diff_vs_market_lo/hi bound the paired
    difference to the market (persona minus market, negative is better) over the shared questions.
    beats_market_ci is true only when the whole difference interval is below 0.
    """
    per, market = _holdout_question_briers(result_like)
    out: dict[str, dict[str, Any]] = {}
    for pid, byq in per.items():
        if not byq:
            continue
        lo, hi = _bootstrap(tuple(byq[q] for q in sorted(byq)))
        row: dict[str, Any] = {"brier_holdout_lo": lo, "brier_holdout_hi": hi, "n_ci": len(byq),
                               "diff_vs_market": None, "diff_vs_market_lo": None, "diff_vs_market_hi": None, "beats_market_ci": False}
        shared = sorted(set(byq) & set(market))
        if shared:
            diffs = tuple(byq[q] - market[q] for q in shared)
            dlo, dhi = _bootstrap(diffs)
            row.update(diff_vs_market=sum(diffs) / len(diffs), diff_vs_market_lo=dlo, diff_vs_market_hi=dhi, beats_market_ci=dhi < 0)
        out[pid] = row
    return out


def _rank_key(s: dict[str, Any]) -> tuple[bool, float, float]:
    b = s.get("brier_holdout")
    return (b is None, b if b is not None else 0.0, -float(s.get("pnl_holdout") or 0.0))


def leaderboard(result_like: dict[str, Any], *, external: list[dict[str, Any]] | None = None, humans: list[dict[str, Any]] | None = None) -> Leaderboard:
    dev_pairs, dev_pnl = _split_stats(result_like, "dev")
    ho_pairs, ho_pnl = _split_stats(result_like, "holdout")
    rows: list[dict[str, Any]] = []
    for p in result_like.get("personas", []):
        pid = p["id"]
        rows.append({
            "persona_id": pid, "name": p.get("name", pid), "color": p.get("color"), "origin": "house",
            "brier_dev": mean_brier(dev_pairs.get(pid, [])), "brier_holdout": mean_brier(ho_pairs.get(pid, [])),
            "pnl_dev": dev_pnl.get(pid, 0.0), "pnl_holdout": ho_pnl.get(pid, 0.0),
            "n": len(dev_pairs.get(pid, [])) + len(ho_pairs.get(pid, [])), "rank": 0,
        })
    for e in external or []:
        rows.append({"brier_dev": None, "brier_holdout": None, "pnl_dev": 0.0, "pnl_holdout": 0.0, "n": 0, **e, "origin": "external", "rank": 0})
    intervals = holdout_intervals(result_like)
    for r in rows:
        if r["persona_id"] in intervals and r["origin"] == "house":
            r.update(intervals[r["persona_id"]])
        else:
            r.update({"brier_holdout_lo": None, "brier_holdout_hi": None, "n_ci": 0, "diff_vs_market": None,
                      "diff_vs_market_lo": None, "diff_vs_market_hi": None, "beats_market_ci": False})
    rows.sort(key=_rank_key)
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    split = "holdout" if scored_cutoffs(result_like, "holdout") else "dev"
    market, clim = _baseline(result_like, split)
    humans_sorted = sorted(humans or [], key=lambda h: (h.get("brier") is None, h.get("brier") or 0.0, -float(h.get("pnl") or 0.0)))
    n_questions = len(_holdout_question_briers(result_like)[1]) if split == "holdout" else 0
    return cast(Leaderboard, {
        "world_models": cast(list[ModelScore], rows),
        "baselines": {"coin": COIN_BRIER, "market": market, "climatology": clim},
        "humans": humans_sorted,
        "interval": {"level": CI_LEVEL, "resamples": CI_RESAMPLES, "n_questions": n_questions,
                     "note": f"{round(CI_LEVEL * 100)}% bootstrap interval over {n_questions} sealed questions"},
    })


def insight(lb: Leaderboard) -> str:
    scored = [m for m in lb.get("world_models", []) if m.get("brier_holdout") is not None]
    if not scored:
        return "No world model has been scored on the sealed holdout yet."
    best = min(scored, key=lambda m: m["brier_holdout"])  # type: ignore[arg-type,return-value]
    b = float(best["brier_holdout"])  # type: ignore[arg-type]
    market = lb.get("baselines", {}).get("market")
    ref, label = (market, "the market") if market is not None else (COIN_BRIER, "a coin flip")
    gap = float(ref) - b
    who = f"the {best['name']} world model" + (" (an outside agent)" if best.get("origin") == "external" else "")
    verb = "beat" if gap > 0 else "trailed"
    text = f"On the sealed holdout, {who} {verb} {label} by {abs(gap):.3f} Brier ({b:.3f} vs {float(ref):.3f})"
    lo, hi = best.get("diff_vs_market_lo"), best.get("diff_vs_market_hi")
    if market is not None and lo is not None and hi is not None:
        span = f"{round(CI_LEVEL * 100)}% interval on the gap {float(lo):+.3f} to {float(hi):+.3f}"
        if best.get("beats_market_ci"):
            text += f", clear of noise ({span})"
        elif float(lo) > 0:
            text += f", clearly behind ({span})"
        else:
            text = (f"On the sealed holdout, {who} scored {b:.3f} Brier against {float(ref):.3f} for {label}, "
                    f"within noise of {label} ({span})")
    if len(scored) > 1:
        worst = max(scored, key=lambda m: m["brier_holdout"])  # type: ignore[arg-type,return-value]
        text += f"; the {worst['name']} world model did worst ({float(worst['brier_holdout']):.3f})"  # type: ignore[arg-type]
    return text + "."
