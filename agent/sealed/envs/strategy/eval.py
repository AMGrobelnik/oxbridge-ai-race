"""Locked evaluation for the SOL/USDC strategy plug-in (SPEC.md section 4).

SEALED: these bytes, SPEC included, are part of code_sha256.

Dev seeds are 60-day windows starting in 2025 (fixture days 60 to 360, every 30 days); the sealed
holdout (holdout.json) is a set of later windows in 2026 plus stress scenarios.
"""

from agent.sealed.records import Candidate, EvalSpec, ScoreReport, Shift

from . import sim

SPEC: EvalSpec = {
    "version": "strategy-eval/1",
    "metric": "score",
    "metric_label": "Risk-adjusted value of $100 after fees and slippage (end value minus half the max drawdown)",
    "higher_is_better": True,
    "dev_seeds": list(range(60, 361, 30)),
    "gate": {"max_relative_gap": 0.05, "no_shift_worse_than_baseline": True},
    "notes": "Seed = window start day in Kraken SOL/USD daily closes (2024-11-04 to 2026-10-24). 60-day windows, 10 bp fee + 10 bp slippage per swap, 7% staking APY.",
}


def evaluate(candidate: Candidate, *, seeds: list[int], shift: Shift) -> ScoreReport:
    """Mean score over seeds under one shift, with per-seed scores and a mean breakdown."""
    if not seeds:
        raise ValueError("evaluate needs at least one seed")
    runs = [sim.simulate(candidate, seed=seed, shift=shift) for seed in seeds]
    per_seed = [round(r["score"], 4) for r in runs]
    breakdown = {key: round(sum(r[key] for r in runs) / len(runs), 4) for key in runs[0] if key != "score"}
    return {
        "score": round(sum(per_seed) / len(per_seed), 4),
        "per_seed": per_seed,
        "breakdown": breakdown,
        "seeds": list(seeds),
        "shift_id": shift["id"],
    }
