"""Locked evaluation for the epidemic plug-in (SPEC.md section 4). Owner: W3.

SEALED: these bytes, SPEC included, are part of code_sha256.
"""

from agent.sealed.records import Candidate, EvalSpec, ScoreReport, Shift

SPEC: EvalSpec = {
    "version": "epidemic-eval/0",
    "metric": "score",
    "metric_label": "Hospital-days averted per GBP 1m, net of penalties",
    "higher_is_better": True,
    "dev_seeds": [],
    "gate": {"max_relative_gap": 0.25, "no_shift_worse_than_baseline": True},
    "notes": "Placeholder until W3 lands the locked eval.",
}


def evaluate(candidate: Candidate, *, seeds: list[int], shift: Shift) -> ScoreReport:
    """Mean score over seeds under one shift, with per-seed scores and a mean breakdown."""
    raise NotImplementedError("W3")
