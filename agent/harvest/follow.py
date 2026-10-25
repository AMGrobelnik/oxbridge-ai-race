"""Follow and copy: a player follows any agent (or account) and may copy its bets, with play credits only.

A follow is a `follow` ledger entry {agent, follow, copy, fraction, cap}, so replaying the ledger restores it.
When a followed agent bets, every follower with copy on buys the same side for `fraction` of the agent's
stake, at most `cap` and at most its balance, as an ordinary `bet` entry tagged copied_from=<agent> and
copy_of=<the agent's entry seq>. A copied bet is never copied again, so copies cannot cascade or loop.
"""

import math
from typing import Any

DEFAULT_FRACTION = 0.5
DEFAULT_CAP = 25.0
MAX_CAP = 500.0
MIN_COPY = 1.0
"""Copies smaller than this many credits are skipped."""


def _finite(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def settings(fraction: Any = None, cap: Any = None) -> tuple[float, float]:
    """(fraction of the agent's stake in 0.01..1, per-bet cap in MIN_COPY..MAX_CAP), defaults for missing values."""
    f = min(max(_finite(fraction, DEFAULT_FRACTION), 0.01), 1.0)
    c = min(max(_finite(cap, DEFAULT_CAP), MIN_COPY), MAX_CAP)
    return round(f, 4), round(c, 2)


def copy_spend(stake: float, fraction: float, cap: float, balance: float) -> float | None:
    """Credits a follower spends copying a `stake`-credit bet, or None when that is under MIN_COPY."""
    spend = round(min(stake * fraction, cap, balance), 2)
    return spend if spend >= MIN_COPY else None
