"""Credits, LMSR order book and settlement (SPEC.md sections 5-6). Owner: market-engine."""

from typing import Any

from .types import BetIntent, MarketState, Payout, Persona, Side, Trade

DEFAULT_B = 100.0
FEE_RATE = 0.02
FUND_SHARE = 1.0
STARTING_CREDITS = 1000.0
FUND_TRADER = "fund:resilience"


class MarketError(ValueError):
    """Insufficient credits, unknown or closed market, bad side or spend."""


def decide(persona: Persona, p_yes: float, price: float, bankroll: float) -> BetIntent | None:
    """YES if p_yes - price > edge, NO if price - p_yes > edge; spend = min(risk*bankroll*|gap|, 0.2*bankroll)."""
    raise NotImplementedError


class Book:
    """In-memory LMSR book with credit accounts. Replay builds one per cutoff."""

    def __init__(self, *, starting_credits: float = STARTING_CREDITS) -> None:
        self.starting_credits = starting_credits
        self.markets: dict[str, MarketState] = {}
        self.accounts: dict[str, float] = {}
        self.holdings: dict[tuple[str, str], dict[Side, float]] = {}
        self.trades: list[Trade] = []
        self.fund: float = 0.0

    def open(self, qid: str, prior: float = 0.5, b: float = DEFAULT_B) -> MarketState:
        raise NotImplementedError

    def price(self, qid: str) -> float:
        raise NotImplementedError

    def balance(self, trader: str) -> float:
        raise NotImplementedError

    def buy(self, trader: str, qid: str, side: Side, spend: float, *, cutoff: str | None = None) -> Trade:
        raise NotImplementedError

    def resolve(self, qid: str, outcome: int) -> list[Payout]:
        raise NotImplementedError

    def positions(self, trader: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def snapshot(self) -> dict[str, Any]:
        raise NotImplementedError


def live_book() -> Book:
    """Process-wide live book: seeded from the replay's live cutoff + soft questions, then the SQLite ledger replayed."""
    raise NotImplementedError
