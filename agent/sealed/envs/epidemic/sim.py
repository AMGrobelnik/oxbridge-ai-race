"""SEIRV simulator for a 10M-person region over one winter, pure stdlib (SPEC.md section 4). Owner: W3.

SEALED: these bytes are part of code_sha256. Deterministic for (candidate, seed, shift).
"""

from typing import Any

from agent.sealed.records import Candidate, Shift

NAME = "epidemic"
TITLE = "Winter epidemic policy"
GOAL = "Minimise winter hospital-days for 10M people with GBP 40M, no school closures over 3 weeks, without widening the age gap."

CANDIDATE_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}
"""JSON Schema of a policy; W3 fills it in."""


def baseline() -> Candidate:
    """The naive starting policy."""
    raise NotImplementedError("W3")


def validate(candidate: Candidate) -> list[str]:
    """Schema, range and budget errors; empty means safe to simulate."""
    raise NotImplementedError("W3")


def simulate(candidate: Candidate, *, seed: int, shift: Shift) -> dict[str, float]:
    """One stochastic winter; returns raw outcome components (hospital_days, icu_overflow_days, ...)."""
    raise NotImplementedError("W3")
