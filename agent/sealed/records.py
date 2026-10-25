"""Shared, JSON-serialisable record types for Sealed (see SPEC.md sections 3 and 8).

Architect-owned contract: workstreams import these and never change their shape.
"""

from typing import Any, Literal, TypedDict

Candidate = dict[str, Any]
"""The editable artifact: data validated against the plug-in's CANDIDATE_SCHEMA, never code."""

Decision = Literal["BASELINE", "KEEP", "REVERT", "INVALID"]
Verdict = Literal["GENERALISES", "OVERFIT"]
RunStatus = Literal["sealing", "running", "revealing", "done", "failed"]
ChainStatus = Literal["confirmed", "pending"]

RUN_SCHEMA = "sealed.run/v1"
RUN_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class Shift(TypedDict):
    """A scenario perturbation. params are plug-in specific multipliers or offsets."""

    id: str
    label: str
    params: dict[str, float]


BASE_SHIFT: Shift = {"id": "base", "label": "Base scenario", "params": {}}


class GateSpec(TypedDict):
    max_relative_gap: float
    """Reject when (dev - holdout_mean) / |dev| exceeds this."""
    no_shift_worse_than_baseline: bool
    """Reject when any holdout shift scores below the naive baseline under that shift."""


class EvalSpec(TypedDict):
    version: str
    metric: str
    metric_label: str
    higher_is_better: bool
    """Always True: every plug-in scores so that higher is better."""
    dev_seeds: list[int]
    gate: GateSpec
    notes: str


class Holdout(TypedDict):
    seeds: list[int]
    shifts: list[Shift]
    """Excludes BASE_SHIFT; the harness always scores the base scenario too."""


class ScoreReport(TypedDict):
    score: float
    """Mean over seeds; higher is better."""
    per_seed: list[float]
    breakdown: dict[str, float]
    """Mean raw components (for example hospital_days, cost_gbp_m) for the UI."""
    seeds: list[int]
    shift_id: str


class Proposal(TypedDict):
    hypothesis: str
    rationale: str
    strategy: str
    """continue | pivot | widen"""
    candidate: Candidate


class HistoryItem(TypedDict):
    index: int
    hypothesis: str
    dev_score: float | None
    decision: Decision


class ProposerContext(TypedDict):
    """Everything a proposer may see. There is deliberately no holdout field."""

    env: str
    goal: str
    metric_label: str
    handbook: str
    candidate_schema: dict[str, Any]
    champion: Candidate
    champion_report: ScoreReport
    history: list[HistoryItem]
    iteration: int
    iterations_total: int


class FieldChange(TypedDict):
    path: str
    """Dotted path to the changed leaf, for example vaccine_share.75+"""
    before: Any
    after: Any


class ChainReceipt(TypedDict):
    status: ChainStatus
    memo: str
    signature: str | None
    explorer_url: str | None
    cluster: str
    at: str
    """ISO-8601 UTC time of the last anchor attempt."""
    error: str | None
    """Human reason when pending, for example "pending on-chain: agent wallet not funded"."""


class Preregistration(TypedDict):
    env: str
    run_id: str
    code_sha256: str
    dev_seeds: list[int]
    holdout_commitment: str
    commitment: str
    memo: str
    sealed_at: str
    chain: ChainReceipt


class IterationRecord(TypedDict):
    index: int
    proposer: str
    hypothesis: str
    rationale: str
    strategy: str
    candidate: Candidate
    diff: list[FieldChange]
    """Changes against the champion this proposal started from."""
    dev: ScoreReport | None
    decision: Decision
    errors: list[str]
    champion_score: float
    """Champion dev score after this iteration's decision."""
    duration_s: float
    at: str


class HoldoutResult(TypedDict):
    iteration: int
    dev_score: float
    per_shift: dict[str, float]
    shift_labels: dict[str, str]
    holdout_mean: float
    worst_shift_id: str
    worst_score: float
    relative_gap: float
    verdict: Verdict
    reasons: list[str]


class Reveal(TypedDict):
    salt: str
    holdout: Holdout
    revealed_at: str
    results: list[HoldoutResult]
    dev_champion_iteration: int
    selected_iteration: int | None
    verdict: Verdict
    """Verdict on the dev champion."""
    result_sha256: str
    memo: str
    chain: ChainReceipt


class RunRecord(TypedDict):
    schema: str
    run_id: str
    env: str
    title: str
    goal: str
    metric_label: str
    mode: Literal["live", "cached"]
    proposer: str
    iterations_planned: int
    status: RunStatus
    created_at: str
    updated_at: str
    preregistration: Preregistration | None
    iterations: list[IterationRecord]
    reveal: Reveal | None
    error: str | None


class RunSummary(TypedDict):
    run_id: str
    env: str
    status: RunStatus
    mode: str
    created_at: str
    iterations_done: int
    verdict: Verdict | None


class EnvPublicView(TypedDict):
    name: str
    title: str
    goal: str
    metric_label: str
    candidate_schema: dict[str, Any]
    dev_seeds: list[int]


class VerifyReport(TypedDict):
    ok: bool
    code_match: bool
    commitment_recomputed: str
    commitment_recorded: str
    commitment_onchain: str | None
    commit_match: bool
    result_recomputed: str | None
    result_recorded: str | None
    result_onchain: str | None
    result_match: bool | None
    onchain_status: Literal["confirmed", "pending", "unreachable"]
    reasons: list[str]
