"""Locked evaluation for the epidemic plug-in (SPEC.md section 4). Owner: W3.

SEALED: these bytes, SPEC included, are part of code_sha256.

Per seed: net benefit = hospital-days averted against a no-intervention winter (same seed, same
shift) minus penalties expressed in hospital-day equivalents, divided by the total cost in GBP
millions (planned spend plus workplace-closure compensation). The score is that ratio in
thousands, averaged over seeds. Higher is better.
"""

from agent.sealed.records import Candidate, EvalSpec, ScoreReport, Shift

from . import sim

ICU_OVERFLOW_WEIGHT = 100.0  # hospital-day equivalents per ICU patient-day without a staffed bed
CLOSURE_CAP_DAYS = 21  # school closure days allowed before the penalty starts
CLOSURE_WEIGHT = 5_000.0  # hospital-day equivalents per school closure day over the cap
AGE_GAP_WEIGHT = 1_000_000.0  # hospital-day equivalents per unit rise in the 60+ share of hospital-days
MIN_COST_GBP_M = 1.0  # floor on the denominator so a do-nothing policy cannot divide by zero

SPEC: EvalSpec = {
    "version": "epidemic-eval/1",
    "metric": "score",
    "metric_label": "Thousand hospital-days averted per GBP 1m, net of penalties",
    "higher_is_better": True,
    "dev_seeds": [101, 102, 103, 104, 105, 106],
    "gate": {"max_relative_gap": 0.3, "no_shift_worse_than_baseline": True},
    "notes": (
        "Score per seed = (hospital-days averted vs no intervention - 100 x ICU overflow patient-days"
        " - 5,000 x school closure days over 21 - 1,000,000 x rise in the 60+ share of hospital-days)"
        " / total cost in GBP m (planned spend + GBP 0.4m per workplace-closure day), in thousands;"
        " the score is the mean over seeds."
    ),
}

OLDER_BANDS = ("60-74", "75+")


def _older_share(out: dict[str, float]) -> float:
    days = {b: out[f"hosp_days_per_1k_{b}"] * sim.N[i] for i, b in enumerate(sim.BANDS)}
    total = sum(days.values())
    return sum(days[b] for b in OLDER_BANDS) / total if total > 0 else 0.0


def score_one(candidate: Candidate, *, seed: int, shift: Shift) -> tuple[float, dict[str, float]]:
    """Score and raw components for one seed."""
    out = sim.simulate(candidate, seed=seed, shift=shift)
    ref = sim.no_intervention(seed=seed, shift=shift)
    averted = ref["hospital_days"] - out["hospital_days"]
    age_gap = _older_share(out) - _older_share(ref)
    excess_closure = max(0.0, out["closure_days"] - CLOSURE_CAP_DAYS)
    penalty = ICU_OVERFLOW_WEIGHT * out["icu_overflow_days"] + CLOSURE_WEIGHT * excess_closure + AGE_GAP_WEIGHT * max(0.0, age_gap)
    cost = max(out["cost_gbp_m"], MIN_COST_GBP_M)
    score = (averted - penalty) / cost / 1000.0
    components = {
        "hospital_days": out["hospital_days"],
        "hospital_days_averted": averted,
        "icu_overflow_days": out["icu_overflow_days"],
        "peak_icu": out["peak_icu"],
        "icu_beds": out["icu_beds"],
        "cost_gbp_m": out["cost_gbp_m"],
        "closure_days": out["closure_days"],
        "work_closure_days": out["work_closure_days"],
        "age_gap": age_gap,
        "deaths": out["deaths"],
        "penalty_hospital_days": penalty,
    }
    return score, components


def evaluate(candidate: Candidate, *, seeds: list[int], shift: Shift) -> ScoreReport:
    """Mean score over seeds under one shift, with per-seed scores and a mean breakdown."""
    if not seeds:
        raise ValueError("evaluate needs at least one seed")
    per_seed: list[float] = []
    sums: dict[str, float] = {}
    for seed in seeds:
        score, components = score_one(candidate, seed=seed, shift=shift)
        per_seed.append(round(score, 6))
        for key, value in components.items():
            sums[key] = sums.get(key, 0.0) + value
    breakdown = {key: round(value / len(seeds), 4) for key, value in sums.items()}
    return {
        "score": round(sum(per_seed) / len(per_seed), 6),
        "per_seed": per_seed,
        "breakdown": breakdown,
        "seeds": list(seeds),
        "shift_id": str(shift["id"]),
    }
