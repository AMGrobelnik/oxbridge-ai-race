"""Shared shapes for Harvest (SPEC.md). Architect-owned: import these, never redefine them."""

from typing import Any, Literal, NotRequired, TypedDict

from agent.sealed.records import ChainReceipt

Kind = Literal["hard", "soft"]
Side = Literal["YES", "NO"]
Split = Literal["dev", "holdout", "live"]
Adverse = Literal["yes", "no"]


class PriceSeries(TypedDict):
    id: str
    name: str
    unit: str
    source_url: str
    points: list[tuple[str, float]]
    """[["YYYY-MM", value], ...] ascending."""


class PriceFixture(TypedDict):
    v: int
    source: str
    fetched_at: str
    series: dict[str, PriceSeries]


class Rule(TypedDict):
    type: Literal["pct_change_gt", "pct_change_lt", "level_gt", "level_lt"]
    horizon_months: int
    threshold: NotRequired[float]
    level: NotRequired[float]


class QuestionSpec(TypedDict):
    id: str
    kind: Kind
    title: str
    region: str
    crop: str
    adverse_if: Adverse
    why: str
    series: NotRequired[str]
    rule: NotRequired[Rule]
    deadline: NotRequired[str]
    prior: NotRequired[float]


class QuestionInstance(TypedDict):
    qid: str
    spec_id: str
    kind: Kind
    title: str
    region: str
    crop: str
    adverse_if: Adverse
    cutoff: str | None
    series: str | None
    base_month: str | None
    base_value: float | None
    resolve_month: str | None
    target: float | None
    rule: Rule | None
    prior: float


class Resolution(TypedDict):
    qid: str
    outcome: int | None
    resolved_value: float | None
    resolve_month: str | None


class Heuristic(TypedDict, total=False):
    type: Literal["momentum", "mean_revert", "shock", "base_rate", "seasonal", "crowd"]
    lookback: int
    tilt: float


class Persona(TypedDict):
    id: str
    name: str
    color: str
    world_model: str
    beliefs: list[str]
    heuristic: Heuristic
    soft_lean: float
    risk: float
    edge: float


class Forecast(TypedDict):
    persona_id: str
    qid: str
    p_yes: float
    rationale: str
    source: Literal["gemini", "fallback", "cache"]


class BetIntent(TypedDict):
    side: Side
    spend: float


class Trade(TypedDict):
    seq: int
    trader: str
    qid: str
    side: Side
    spend: float
    fee: float
    shares: float
    price_before: float
    price_after: float
    at: str
    cutoff: str | None


class Payout(TypedDict):
    trader: str
    qid: str
    shares: float
    amount: float


class MarketState(TypedDict):
    qid: str
    b: float
    q_yes: float
    q_no: float
    volume: float
    fees: float
    status: Literal["open", "resolved"]
    outcome: int | None
    history: list[tuple[int, float]]


class LedgerEntry(TypedDict):
    seq: int
    prev: str
    kind: Literal["join", "agent", "bet", "resolve", "fund", "forecast"]
    trader: str
    payload: dict[str, Any]
    at: str
    hash: str


class Commitment(TypedDict):
    kind: Literal["models", "holdout"]
    sha256: str
    memo: str
    payload_ref: str
    receipt: ChainReceipt
    committed_at: str


class CutoffResult(TypedDict):
    cutoff: str
    split: Split
    questions: list[QuestionInstance]
    windows: dict[str, str]
    forecasts: list[Forecast]
    trades: list[Trade]
    final_prices: dict[str, float]
    resolutions: list[Resolution]
    payouts: list[Payout]


class ModelScore(TypedDict):
    persona_id: str
    name: str
    origin: Literal["house", "external"]
    brier_dev: float | None
    brier_holdout: float | None
    pnl_dev: float
    pnl_holdout: float
    n: int
    rank: int


class Leaderboard(TypedDict):
    world_models: list[ModelScore]
    baselines: dict[str, float | None]
    humans: list[dict[str, Any]]


class Mask(TypedDict):
    commodities: dict[str, str]
    seed: int


class ReplayResult(TypedDict):
    v: int
    generated_at: str
    model: str
    mask: Mask
    personas: list[Persona]
    cutoffs: list[CutoffResult]
    commitments: list[Commitment]
    scores: Leaderboard


class Allocation(TypedDict):
    region: str
    crop: str
    risk: float
    share: float
    amount: float
    questions: list[str]


class FundState(TypedDict):
    treasury: float
    fee_rate: float
    allocations: list[Allocation]
    memo: str
    receipt: ChainReceipt | None
