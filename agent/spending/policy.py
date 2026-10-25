"""Reads spending limits from env. Amounts are in US dollars (paid as USDC, 6 decimals).

Money is kept in whole micro-dollars (1 USD = 1_000_000), never floats, so totals are exact.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_BUDGET_USD = "1.00"
DEFAULT_MAX_PAYMENT_USD = "0.10"


@dataclass(frozen=True)
class SpendingPolicy:
    budget_micros: int
    """Total allowed spend, in micro-dollars."""
    max_payment_micros: int
    """Largest single payment, in micro-dollars."""


def usd_to_micros(usd: str) -> int:
    """"0.25" -> 250000. Rejects negatives, more than 6 decimals and non-numbers."""
    match = re.fullmatch(r"(\d+)(?:\.(\d{1,6}))?", usd.strip())
    if not match:
        raise ValueError(f'"{usd}" is not a valid dollar amount (e.g. 0.25)')
    whole, frac = match.group(1), match.group(2) or ""
    return int(whole) * 1_000_000 + int(frac.ljust(6, "0"))


def micros_to_usd(micros: int) -> str:
    """250000 -> "0.25" """
    whole, rest = divmod(micros, 1_000_000)
    frac = f"{rest:06d}".rstrip("0").ljust(2, "0")
    return f"{whole}.{frac}"


def read_spending_policy(env: Mapping[str, str] | None = None) -> SpendingPolicy:
    env = os.environ if env is None else env
    budget = usd_to_micros(env.get("AGENT_BUDGET_USD") or DEFAULT_BUDGET_USD)
    max_payment = usd_to_micros(env.get("MAX_PAYMENT_USD") or DEFAULT_MAX_PAYMENT_USD)
    if max_payment <= 0:
        raise ValueError("MAX_PAYMENT_USD must be greater than 0")
    if max_payment > budget:
        raise ValueError("MAX_PAYMENT_USD cannot be larger than AGENT_BUDGET_USD")
    return SpendingPolicy(budget_micros=budget, max_payment_micros=max_payment)
