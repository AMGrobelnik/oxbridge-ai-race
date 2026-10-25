"""Pure LMSR maths (SPEC.md section 6). Implemented by the architect; market-engine owns it now."""

import math

from .types import Side


def cost(q_yes: float, q_no: float, b: float) -> float:
    m = max(q_yes, q_no) / b
    return b * (m + math.log(math.exp(q_yes / b - m) + math.exp(q_no / b - m)))


def price_yes(q_yes: float, q_no: float, b: float) -> float:
    return 1.0 / (1.0 + math.exp((q_no - q_yes) / b))


def cost_to_buy(q_yes: float, q_no: float, b: float, side: Side, shares: float) -> float:
    if side == "YES":
        return cost(q_yes + shares, q_no, b) - cost(q_yes, q_no, b)
    return cost(q_yes, q_no + shares, b) - cost(q_yes, q_no, b)


def shares_for_spend(q_yes: float, q_no: float, b: float, side: Side, spend: float) -> float:
    """Closed form: shares s with cost_to_buy(...) == spend."""
    mine, other = (q_yes, q_no) if side == "YES" else (q_no, q_yes)
    c0 = cost(q_yes, q_no, b)
    # b*ln(e^((mine+s)/b) + e^(other/b)) = c0 + spend
    target = (c0 + spend) / b
    m = max(target, other / b)
    inner = math.exp(target - m) - math.exp(other / b - m)
    return b * (m + math.log(inner)) - mine


def initial_q(prior: float, b: float) -> tuple[float, float]:
    """q so that price_yes == prior (q_no = 0)."""
    p = min(max(prior, 0.01), 0.99)
    return b * math.log(p / (1 - p)), 0.0
