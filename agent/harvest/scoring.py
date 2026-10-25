"""Brier, PnL and the world-model leaderboard (SPEC.md section 9). Owner: market-engine."""

from typing import Any

from .types import Leaderboard

COIN_BRIER = 0.25


def brier(p: float, outcome: int) -> float:
    return (p - outcome) ** 2


def mean_brier(pairs: list[tuple[float, int]]) -> float | None:
    return sum(brier(p, o) for p, o in pairs) / len(pairs) if pairs else None


def leaderboard(result_like: dict[str, Any], *, external: list[dict[str, Any]] | None = None, humans: list[dict[str, Any]] | None = None) -> Leaderboard:
    raise NotImplementedError


def insight(lb: Leaderboard) -> str:
    raise NotImplementedError
