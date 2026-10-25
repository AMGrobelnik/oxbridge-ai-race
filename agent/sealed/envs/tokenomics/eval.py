"""Locked evaluation for the token design stress test (SPEC.md section 4).

SEALED: these bytes, SPEC included, are part of code_sha256.
"""

from agent.sealed.records import Candidate, EvalSpec, ScoreReport, Shift

from . import sim

SPEC: EvalSpec = {
    "version": "tokenomics-eval/1",
    "metric": "launch_health",
    "metric_label": "Launch health score (0-100): drawdown, stability, liquidity, runway",
    "higher_is_better": True,
    "dev_seeds": [1, 2, 3, 4, 5, 6, 7, 8],
    "gate": {"max_relative_gap": 0.20, "no_shift_worse_than_baseline": True},
    "notes": (
        "Score per seed = 100 x (0.35 x (1 - max drawdown vs market) + 0.15 x (1 - min(1, annual vol / 2)) "
        "+ 0.25 x min(1, worst pool USD depth / $3M) + 0.25 x min(1, treasury runway / 72 months)); "
        "mean over seeds. Dev scenario: calm market, 18 months."
    ),
}


def evaluate(candidate: Candidate, *, seeds: list[int], shift: Shift) -> ScoreReport:
    """Mean launch-health score over seeds under one shift, with per-seed scores and a mean breakdown."""
    if not seeds:
        raise ValueError("evaluate needs at least one seed")
    raws = [sim.simulate(candidate, seed=seed, shift=shift) for seed in seeds]
    per_seed = [round(sim.score(raw), 6) for raw in raws]
    keys = list(raws[0])
    breakdown = {key: round(sum(raw[key] for raw in raws) / len(raws), 6) for key in keys}
    for name, weight in sim.WEIGHTS.items():
        points = sum(sim.score_components(raw)[name] for raw in raws) / len(raws) * 100 * weight
        breakdown[f"points_{name}"] = round(points, 6)
    return {
        "score": round(sum(per_seed) / len(per_seed), 6),
        "per_seed": per_seed,
        "breakdown": breakdown,
        "seeds": list(seeds),
        "shift_id": shift["id"],
    }
