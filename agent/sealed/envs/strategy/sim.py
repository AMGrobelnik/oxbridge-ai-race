"""SOL/USDC strategy backtester on real daily SOL prices, pure stdlib (SPEC.md section 4).

SEALED: these bytes are part of code_sha256, and DATA_SHA256 below seals the price fixture by
reference, so editing sol_usd_daily.json breaks the import instead of silently moving scores.

A seed is a window start: the index of a day in the fixture (sol_usd_daily.json, Kraken daily
closes). The strategy trades for WINDOW_DAYS daily steps from that day, using only closes up to
the day it acts on, with the WARMUP_DAYS before the window available for its indicators. Dev
seeds start in 2025; the sealed holdout seeds start in 2026, a later, unseen time window.

Portfolio: $100 split between a SOL leg (part of it staked, earning STAKING_APY) and idle USDC.
Each day the rule sets a target SOL weight; the book trades to it only when the drift exceeds
the rebalance band, paying a swap fee plus slippage on the traded value. Shifts are stress
scenarios applied to the window: a SOL crash, a USDC depeg, a fee spike and a volatility spike.
"""

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from agent.sealed.records import Candidate, Shift

NAME = "strategy"
TITLE = "SOL/USDC strategy research"
GOAL = "Find a SOL/USDC/staking allocation rule that maximises risk-adjusted value after fees and slippage."

DATA_FILE = Path(__file__).with_name("sol_usd_daily.json")
DATA_SHA256 = "32066cdb75c3475613a47517302e5ddddda50cce20e1a8147c95510d8917dfea"

WINDOW_DAYS = 60
WARMUP_DAYS = 60
START_VALUE = 100.0
SWAP_FEE = 0.0010  # 10 bp per swap (DEX aggregator route)
SLIPPAGE = 0.0010  # 10 bp price impact per swap
STAKING_APY = 0.07  # native SOL staking yield on the staked part of the SOL leg
DRAWDOWN_PENALTY = 0.5  # score = end value minus this times the max drawdown in dollars
RULES = ("hold", "momentum", "mean_reversion")

LIMITS: dict[str, tuple[float, float]] = {
    "lookback_days": (2, WARMUP_DAYS),
    "entry_threshold_pct": (0, 50),
    "risk_on_sol_weight": (0, 1),
    "risk_off_sol_weight": (0, 1),
    "staked_share": (0, 1),
    "rebalance_band": (0, 0.5),
    "stop_loss_pct": (0, 80),
}
INTEGER_KEYS = {"lookback_days"}
KEY_DOCS = {
    "rule": "hold: always risk-on weight. momentum: risk-on after the price rose more than the threshold over the lookback, risk-off after it fell more. mean_reversion: risk-on when the price is more than the threshold below its lookback average, risk-off when above.",
    "lookback_days": "Days of history the rule looks at (2-60).",
    "entry_threshold_pct": "Signal threshold in percent (0-50); between the thresholds the previous target is kept.",
    "risk_on_sol_weight": "Share of the portfolio in SOL when the signal is on (0-1).",
    "risk_off_sol_weight": "Share of the portfolio in SOL when the signal is off (0-1).",
    "staked_share": "Share of the SOL leg that is staked and earns 7% a year (0-1).",
    "rebalance_band": "Trade only when the SOL weight drifts more than this from target (0-0.5).",
    "stop_loss_pct": "Force the risk-off weight while SOL is this many percent below its lookback high (0 = off).",
}

CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "SOL/USDC allocation strategy",
    "properties": {
        "rule": {"type": "string", "enum": list(RULES), "description": KEY_DOCS["rule"]},
        **{
            key: {"type": "integer" if key in INTEGER_KEYS else "number", "minimum": lo, "maximum": hi, "description": KEY_DOCS[key]}
            for key, (lo, hi) in LIMITS.items()
        },
    },
    "required": ["rule", *LIMITS],
    "additionalProperties": False,
}

SHIFT_PARAMS = ("crash_pct", "crash_day", "depeg_pct", "depeg_day", "depeg_days", "fee_mult", "vol_mult")


def baseline() -> Candidate:
    """The naive starting strategy: a 50/50 SOL/USDC book, nothing staked, loose rebalancing."""
    return {
        "rule": "hold",
        "lookback_days": 20,
        "entry_threshold_pct": 5,
        "risk_on_sol_weight": 0.5,
        "risk_off_sol_weight": 0.5,
        "staked_share": 0,
        "rebalance_band": 0.1,
        "stop_loss_pct": 0,
    }


@lru_cache(maxsize=1)
def load_prices() -> tuple[tuple[str, ...], tuple[float, ...]]:
    """(days, closes) from the sealed fixture; raises if its bytes differ from DATA_SHA256."""
    raw = DATA_FILE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATA_SHA256:
        raise RuntimeError(f"{DATA_FILE.name} does not match the sealed DATA_SHA256 ({digest[:12]}...)")
    bars = json.loads(raw)["bars"]
    return tuple(str(b[0]) for b in bars), tuple(float(b[1]) for b in bars)


def valid_seeds() -> range:
    """Window starts that leave room for the warm-up before and the full window after."""
    return range(WARMUP_DAYS, len(load_prices()[1]) - WINDOW_DAYS)


def validate(candidate: Candidate) -> list[str]:
    if not isinstance(candidate, dict):
        return ["candidate must be a JSON object"]
    errors = [f"unknown key {key!r}" for key in candidate if key not in CANDIDATE_SCHEMA["properties"]]
    rule = candidate.get("rule")
    if rule not in RULES:
        errors.append(f"rule must be one of {', '.join(RULES)}, got {rule!r}")
    for key, (lo, hi) in LIMITS.items():
        value = candidate.get(key)
        if value is None:
            errors.append(f"missing {key}")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            errors.append(f"{key} must be a number")
        elif key in INTEGER_KEYS and value != int(value):
            errors.append(f"{key} must be an integer")
        elif not lo <= value <= hi:
            errors.append(f"{key} must be within [{lo:g}, {hi:g}], got {value}")
    return errors


def _window(seed: int, shift: Shift) -> tuple[list[float], list[float]]:
    """SOL and USDC prices from seed - WARMUP_DAYS to seed + WINDOW_DAYS with the shift applied."""
    _, closes = load_prices()
    if seed not in valid_seeds():
        raise ValueError(f"seed {seed} is not a valid window start ({valid_seeds().start}-{valid_seeds().stop - 1})")
    p = shift["params"]
    sol = list(closes[seed - WARMUP_DAYS : seed + WINDOW_DAYS + 1])
    vol_mult = float(p.get("vol_mult", 1.0))
    if vol_mult != 1.0:  # scale daily log returns inside the window around the window's drift
        rets = [math.log(sol[i] / sol[i - 1]) for i in range(WARMUP_DAYS + 1, len(sol))]
        drift = sum(rets) / len(rets)
        for k, r in enumerate(rets):
            sol[WARMUP_DAYS + 1 + k] = sol[WARMUP_DAYS + k] * math.exp(drift + vol_mult * (r - drift))
    crash = float(p.get("crash_pct", 0.0)) / 100
    if crash:  # a three-day slide, then the rest of the path continues from the lower level
        day = WARMUP_DAYS + int(p.get("crash_day", 20))
        for i in range(day, len(sol)):
            sol[i] *= (1 - crash) ** (min(i - day + 1, 3) / 3)
    usdc = [1.0] * len(sol)
    depeg = float(p.get("depeg_pct", 0.0)) / 100
    if depeg:  # USDC trades below $1, then re-pegs over the same number of days
        start, days = WARMUP_DAYS + int(p.get("depeg_day", 25)), int(p.get("depeg_days", 5))
        for i in range(start, min(start + 2 * days, len(usdc))):
            usdc[i] = 1 - depeg * (1 if i < start + days else (start + 2 * days - i) / (days + 1))
    return sol, usdc


def simulate(candidate: Candidate, *, seed: int, shift: Shift) -> dict[str, float]:
    """Backtests the strategy over one window; deterministic for (candidate, seed, shift)."""
    sol, usdc = _window(seed, shift)
    cost_rate = (SWAP_FEE + SLIPPAGE) * float(shift["params"].get("fee_mult", 1.0))
    rule, lookback = candidate["rule"], int(candidate["lookback_days"])
    threshold = float(candidate["entry_threshold_pct"]) / 100
    w_on, w_off = float(candidate["risk_on_sol_weight"]), float(candidate["risk_off_sol_weight"])
    stake_yield = float(candidate["staked_share"]) * STAKING_APY / 365
    band, stop = float(candidate["rebalance_band"]), float(candidate["stop_loss_pct"]) / 100

    sol_value, usdc_value = 0.0, START_VALUE  # dollars in each leg, starting all in USDC
    target = (w_on + w_off) / 2 if rule != "hold" else w_on
    values, fees, trades, weight_sum = [START_VALUE], 0.0, 0, 0.0
    for t in range(WARMUP_DAYS, WARMUP_DAYS + WINDOW_DAYS):
        price, past = sol[t], sol[t - lookback : t + 1]
        if rule == "hold":
            target = w_on
        elif rule == "momentum":
            move = price / past[0] - 1
            target = w_on if move > threshold else w_off if move < -threshold else target
        else:
            gap = price / (sum(past) / len(past)) - 1
            target = w_on if gap < -threshold else w_off if gap > threshold else target
        aim = w_off if stop and price < max(past) * (1 - stop) else target
        total = sol_value + usdc_value
        if t == WARMUP_DAYS or abs(sol_value / total - aim) > band:
            traded = abs(aim * total - sol_value)
            if traded > 1e-9:
                fee = traded * cost_rate
                fees, trades = fees + fee, trades + 1
                sol_value, usdc_value = aim * (total - fee), (1 - aim) * (total - fee)
        sol_value *= sol[t + 1] / price * (1 + stake_yield)
        usdc_value *= usdc[t + 1] / usdc[t]
        values.append(sol_value + usdc_value)
        weight_sum += sol_value / values[-1]

    peak, max_dd = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        max_dd = max(max_dd, (peak - v) / peak)
    rets = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    mean = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
    end = values[-1]
    return {
        "score": end - DRAWDOWN_PENALTY * max_dd * START_VALUE,
        "end_value": end,
        "return_pct": (end / START_VALUE - 1) * 100,
        "max_drawdown_pct": max_dd * 100,
        "sharpe": mean / sd * math.sqrt(365) if sd > 1e-12 else 0.0,
        "fees_usd": fees,
        "trades": float(trades),
        "avg_sol_weight": weight_sum / WINDOW_DAYS,
    }
