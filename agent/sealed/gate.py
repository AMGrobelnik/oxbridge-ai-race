"""The fixed holdout gate (SPEC.md section 6). Its parameters come from the sealed eval.SPEC."""

from typing import NamedTuple

from .records import GateSpec, Verdict


class GateOutcome(NamedTuple):
    verdict: Verdict
    reasons: list[str]
    holdout_mean: float
    worst_shift_id: str
    worst_score: float
    relative_gap: float


def judge(
    dev_score: float,
    per_shift: dict[str, float],
    baseline_per_shift: dict[str, float],
    gate: GateSpec,
    shift_labels: dict[str, str] | None = None,
) -> GateOutcome:
    """Judges one finalist from its dev score and its holdout score under every shift (base included)."""
    if not per_shift:
        raise ValueError("per_shift is empty: score the finalist under at least the base shift.")
    labels = shift_labels or {}
    holdout_mean = sum(per_shift.values()) / len(per_shift)
    relative_gap = (dev_score - holdout_mean) / max(abs(dev_score), 1e-9)
    worst_shift_id = min(per_shift, key=lambda s: per_shift[s])
    reasons: list[str] = []
    if relative_gap > gate["max_relative_gap"]:
        reasons.append(f"holdout mean {holdout_mean:.1f} is {relative_gap:.0%} below its dev score {dev_score:.1f} (limit {gate['max_relative_gap']:.0%})")
    if gate["no_shift_worse_than_baseline"]:
        for shift_id, score in per_shift.items():
            base = baseline_per_shift.get(shift_id)
            if base is not None and score < base - 1e-9:
                reasons.append(f"collapses under {labels.get(shift_id, shift_id)}: {score:.1f} vs naive baseline {base:.1f}")
    verdict: Verdict = "OVERFIT" if reasons else "GENERALISES"
    return GateOutcome(verdict, reasons, holdout_mean, worst_shift_id, per_shift[worst_shift_id], relative_gap)
