"""Token design stress test: cohort sell-pressure over a constant-product AMM, pure stdlib (SPEC.md section 4).

SEALED: these bytes are part of code_sha256. Deterministic for (candidate, seed, shift).

Model: 1B tokens launch at $0.10 into one x*y=k pool funded by the treasury plus outside LPs, and
trade daily for 18 months. Team and investors vest (cliff, then linear, released every cadence
days) and sell part of each unlock, more when the price is above their cost basis. Emissions pay
farmers who sell most of them but bring buyers. Community holders follow the crypto market and
panic-sell once the price is 40% under fair value. The treasury pays opex, seeds the pool and buys back on
dips under its own rule. Every random draw comes up front from random.Random(seed), so every design
sees the same market path. Shifts add a market crash, a whale dump, an LP withdrawal or
correlated unlock selling.
"""

import math
import random
from typing import Any

from agent.sealed.records import Candidate, Shift

NAME = "tokenomics"
TITLE = "Token design stress test"
GOAL = "Design a token launch (vesting, emissions, float, pool depth, buybacks) that keeps price stable, drawdowns small and liquidity healthy for 18 months without draining the treasury."

DAYS = 540
SUPPLY = 1_000_000_000.0
P0 = 0.10  # launch price, so FDV is $100M
TEAM_SHARE, INVESTOR_SHARE, ECOSYSTEM_SHARE, TREASURY_SHARE = 0.18, 0.17, 0.30, 0.15
INVESTOR_COST, TEAM_COST = 0.02, 0.005  # cost basis per token
EXTERNAL_LP_USD = 4.0e6  # mercenary LPs' USD side at launch (matched in tokens); they leave when it pays elsewhere
BURN_USD_PER_DAY = 0.15e6 / 30  # treasury opex
RUNWAY_CAP_MONTHS = 72.0
LIQ_TARGET_USD = 3.0e6  # pool USD depth a $100k sell needs to stay under ~3% impact
DEMAND_USD = 10_000.0  # organic net buying per day before emissions
EMISSION_DEMAND = 0.25  # extra organic demand per 1%/month of emissions
SELL_SPEED = 0.15  # share of a cohort's sell queue sold per day
FARM_SELL = 0.6  # share of emissions farmers sell the day they get them
MARKET_MU, MARKET_SIGMA = 0.0003, 0.02  # sector index daily log-return; fair value tracks it
VALUE_USD = 150_000.0  # traders buy (sell) this many USD per day per unit log-gap below (above) fair value
PANIC_DD, PANIC_RATE = 0.40, 0.3  # once the price is 40% under fair value, holders dump this share per day per unit of extra gap
BUYBACK_LOOKBACK = 30
WEIGHTS = {"drawdown": 0.35, "stability": 0.15, "liquidity": 0.25, "runway": 0.25}
VOL_CAP = 2.0  # annualised volatility that scores zero stability

LIMITS: dict[str, tuple[float, float]] = {
    "team_cliff_months": (0, 12),
    "team_vest_months": (0, 48),
    "investor_cliff_months": (0, 12),
    "investor_vest_months": (0, 36),
    "unlock_cadence_days": (1, 90),
    "emission_pct_per_month": (0, 3),
    "initial_float_pct": (3, 25),
    "lp_depth_usd_m": (0.5, 12),
    "buyback_trigger_drawdown": (0.05, 0.8),
    "buyback_daily_pct": (0, 0.2),
    "treasury_reserve_months": (0, 24),
}
INTEGER_KEYS = {"team_cliff_months", "team_vest_months", "investor_cliff_months", "investor_vest_months", "unlock_cadence_days"}
KEY_DOCS = {
    "team_cliff_months": "Months before any team tokens (18% of supply) unlock; the accrued share unlocks at the cliff.",
    "team_vest_months": "Months of linear team vesting after the cliff (0 = everything at the cliff).",
    "investor_cliff_months": "Months before investor tokens (17%) unlock. Shorter locks raise more: raise = $6M + $8M x (1 - (cliff + vest/2)/36).",
    "investor_vest_months": "Months of linear investor vesting after the cliff.",
    "unlock_cadence_days": "Vested tokens are released every this many days (1 = daily drip, 90 = quarterly lumps).",
    "emission_pct_per_month": "Ecosystem emissions as % of supply per month; farmers sell 60% at once, but emissions add buyers.",
    "initial_float_pct": "Community float at launch, % of supply.",
    "lp_depth_usd_m": "USD the treasury puts in the AMM pool at launch (matched with treasury tokens), $M.",
    "buyback_trigger_drawdown": "Treasury buys back when the price is this far below its 30-day high (0.2 = 20% below).",
    "buyback_daily_pct": "Share of the treasury's USD spent per day while the buyback is triggered.",
    "treasury_reserve_months": "Months of opex the buyback may never touch.",
}

CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Token launch design",
    "properties": {
        key: {"type": "integer" if key in INTEGER_KEYS else "number", "minimum": lo, "maximum": hi, "description": KEY_DOCS[key]}
        for key, (lo, hi) in LIMITS.items()
    },
    "required": list(LIMITS),
    "additionalProperties": False,
}


def baseline() -> Candidate:
    """The naive first draft: short locks, quarterly lumps, high emissions, a thin pool, no buyback."""
    return {
        "team_cliff_months": 6,
        "team_vest_months": 12,
        "investor_cliff_months": 3,
        "investor_vest_months": 9,
        "unlock_cadence_days": 90,
        "emission_pct_per_month": 2.0,
        "initial_float_pct": 10.0,
        "lp_depth_usd_m": 1.0,
        "buyback_trigger_drawdown": 0.5,
        "buyback_daily_pct": 0.0,
        "treasury_reserve_months": 12.0,
    }


def raise_usd(candidate: Candidate) -> float:
    """Investor raise in USD: investors pay more for shorter average lockups."""
    lock = candidate["investor_cliff_months"] + candidate["investor_vest_months"] / 2
    return 6e6 + 8e6 * max(0.0, 1 - lock / 36)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(candidate: Candidate) -> list[str]:
    """Schema, range and budget errors; empty means safe to simulate."""
    if not isinstance(candidate, dict):
        return ["candidate must be a JSON object"]
    errors = [f"unknown key {k!r}" for k in candidate if k not in LIMITS]
    for key, (lo, hi) in LIMITS.items():
        value = candidate.get(key)
        if key not in candidate:
            errors.append(f"missing key {key!r}")
        elif not _is_number(value):
            errors.append(f"{key} must be a number")
        elif key in INTEGER_KEYS and float(value) != int(value):
            errors.append(f"{key} must be an integer")
        elif not lo <= value <= hi:
            errors.append(f"{key}={value} is outside [{lo}, {hi}]")
    if errors:
        return errors
    raised = raise_usd(candidate)
    if candidate["lp_depth_usd_m"] * 1e6 > raised - 1e6:
        errors.append(f"budget overrun: pool depth ${candidate['lp_depth_usd_m']}M leaves under $1M of the ${raised / 1e6:.2f}M raise")
    if candidate["lp_depth_usd_m"] * 1e6 / P0 > TREASURY_SHARE * SUPPLY:
        errors.append("pool depth needs more tokens than the 15% treasury allocation")
    return errors


def _vested(day: int, cliff_m: float, vest_m: float, cadence: int) -> float:
    """Share of an allocation released by this day: cliff accrual at the cliff, then linear, released every cadence days."""
    cliff, vest = cliff_m * 30, vest_m * 30
    if day < cliff:
        return 0.0
    released_day = cliff + ((day - cliff) // cadence) * cadence
    if vest <= 0:
        return 1.0
    accrued = cliff / (cliff + vest) if cliff + vest > 0 else 1.0
    return min(1.0, accrued + (1 - accrued) * (released_day - cliff) / vest)


def _shift_params(shift: Shift) -> dict[str, float]:
    p = shift.get("params", {}) if isinstance(shift, dict) else {}
    defaults = {
        "crash_day": -1, "crash_depth": 0.0, "crash_days": 10,
        "whale_day": -1, "whale_pct": 0.0,
        "lp_withdraw_day": -1, "lp_withdraw_share": 0.0,
        "unlock_sell_mult": 1.0, "market_sigma_mult": 1.0,
    }
    return {k: float(p.get(k, v)) for k, v in defaults.items()}


def simulate(candidate: Candidate, *, seed: int, shift: Shift) -> dict[str, float]:
    """One 18-month launch; returns raw components (max_drawdown, volatility, min_pool_usd, runway_months, ...)."""
    sp = _shift_params(shift)
    rng = random.Random(seed)
    sigma = MARKET_SIGMA * sp["market_sigma_mult"]
    market = [rng.gauss(MARKET_MU, sigma) for _ in range(DAYS)]
    demand_noise = [rng.gauss(1.0, 0.5) for _ in range(DAYS)]
    crash_day, crash_days = int(sp["crash_day"]), max(1, int(sp["crash_days"]))
    if crash_day >= 0 and sp["crash_depth"] > 0:
        step = math.log(1 - sp["crash_depth"]) / crash_days
        for d in range(crash_day, min(DAYS, crash_day + crash_days)):
            market[d] += step

    c = candidate
    treasury = raise_usd(c) - c["lp_depth_usd_m"] * 1e6
    protocol_usd = c["lp_depth_usd_m"] * 1e6
    pool_usd = protocol_usd + EXTERNAL_LP_USD
    pool_tok = pool_usd / P0
    external_share = EXTERNAL_LP_USD / pool_usd
    community = c["initial_float_pct"] / 100 * SUPPLY
    eco_left = ECOSYSTEM_SHARE * SUPPLY
    released = {"team": 0.0, "investor": 0.0}
    alloc = {"team": TEAM_SHARE * SUPPLY, "investor": INVESTOR_SHARE * SUPPLY}
    cost = {"team": TEAM_COST, "investor": INVESTOR_COST}
    base_sell = {"team": 0.15, "investor": 0.30}
    schedule = {"team": (c["team_cliff_months"], c["team_vest_months"]), "investor": (c["investor_cliff_months"], c["investor_vest_months"])}
    cadence = int(c["unlock_cadence_days"])
    reserve = c["treasury_reserve_months"] * 30 * BURN_USD_PER_DAY
    emit_per_day = c["emission_pct_per_month"] / 100 * SUPPLY / 30
    demand = DEMAND_USD * (1 + EMISSION_DEMAND * c["emission_pct_per_month"])

    def sell(tokens: float) -> None:
        nonlocal pool_usd, pool_tok
        if tokens > 0:
            k = pool_usd * pool_tok
            pool_tok += tokens
            pool_usd = k / pool_tok

    def buy(usd: float) -> float:
        nonlocal pool_usd, pool_tok
        if usd <= 0:
            return 0.0
        k = pool_usd * pool_tok
        pool_usd += usd
        out = pool_tok - k / pool_usd
        pool_tok -= out
        return out

    prices = [P0]
    fair = P0
    rel = [1.0]
    min_pool_usd = pool_usd
    buyback_spent = 0.0
    unlock_sold = 0.0
    queue = {"team": 0.0, "investor": 0.0}
    for day in range(1, DAYS + 1):
        r = market[day - 1]
        # Vesting unlocks: each cohort queues a price-sensitive share of what was released today and sells
        # SELL_SPEED of its queue per day (faster when unlock selling is correlated).
        for cohort in ("team", "investor"):
            share = _vested(day, *schedule[cohort], cadence)
            new = share * alloc[cohort] - released[cohort]
            if new > 0:
                released[cohort] += new
                p = pool_usd / pool_tok
                frac = base_sell[cohort] + 0.12 * math.log(max(p, 1e-9) / cost[cohort])
                frac = min(0.95, max(0.02, frac * sp["unlock_sell_mult"]))
                queue[cohort] += new * frac
                community += new * (1 - frac)
            tokens = queue[cohort] * min(1.0, SELL_SPEED * sp["unlock_sell_mult"])
            queue[cohort] -= tokens
            sell(tokens)
            unlock_sold += tokens
        # Emissions: farmers sell most, keep the rest.
        emitted = min(emit_per_day, eco_left)
        eco_left -= emitted
        sell(emitted * FARM_SELL)
        community += emitted * (1 - FARM_SELL)
        # Organic flow: steady demand plus traders pulling the price toward fair value (which tracks the market).
        fair *= math.exp(r)
        p = pool_usd / pool_tok
        flow = demand * demand_noise[day - 1] + VALUE_USD * math.log(fair / p)
        if flow >= 0:
            community += buy(flow)
        else:
            tokens = min(community, -flow / p)
            community -= tokens
            sell(tokens)
        # Shocks.
        if day == int(sp["whale_day"]):
            whale = min(community, sp["whale_pct"] / 100 * SUPPLY)
            community -= whale
            sell(whale)
        if day == int(sp["lp_withdraw_day"]) and external_share > 0:
            pulled = external_share * sp["lp_withdraw_share"]
            pool_usd *= 1 - pulled
            pool_tok *= 1 - pulled
            external_share = (external_share - pulled) / (1 - pulled)
        # Panic selling past the drawdown threshold.
        p = pool_usd / pool_tok
        dd = 1 - p / fair
        if dd > PANIC_DD:
            tokens = community * min(0.5, PANIC_RATE * (dd - PANIC_DD))
            community -= tokens
            sell(tokens)
        # Treasury: opex, then the buyback rule (bought tokens are burned).
        treasury -= BURN_USD_PER_DAY
        p = pool_usd / pool_tok
        recent_high = max(prices[-BUYBACK_LOOKBACK:])
        if c["buyback_daily_pct"] > 0 and p < (1 - c["buyback_trigger_drawdown"]) * recent_high:
            spend = min(c["buyback_daily_pct"] * treasury, treasury - reserve)
            if spend > 0:
                treasury -= spend
                buyback_spent += spend
                buy(spend)
        p = pool_usd / pool_tok
        prices.append(p)
        rel.append(p / fair)
        min_pool_usd = min(min_pool_usd, pool_usd)

    # Stability and drawdown are measured against the market (price / fair value), so they score the design, not the cycle.
    max_dd, running = 0.0, rel[0]
    for x in rel:
        running = max(running, x)
        max_dd = max(max_dd, 1 - x / running)
    rets = [math.log(rel[i] / rel[i - 1]) for i in range(1, len(rel))]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - mean) ** 2 for x in rets) / len(rets)) * math.sqrt(365)
    runway = max(0.0, treasury) / (BURN_USD_PER_DAY * 30)
    return {
        "max_drawdown": max_dd,
        "volatility": vol,
        "min_pool_usd_m": min_pool_usd / 1e6,
        "runway_months": runway,
        "final_price": prices[-1],
        "buyback_spent_usd_m": buyback_spent / 1e6,
        "unlock_sold_pct": unlock_sold / SUPPLY * 100,
    }


def score_components(raw: dict[str, float]) -> dict[str, float]:
    """Maps raw outcomes to 0-1 components, higher is better."""
    return {
        "drawdown": 1 - raw["max_drawdown"],
        "stability": 1 - min(1.0, raw["volatility"] / VOL_CAP),
        "liquidity": min(1.0, raw["min_pool_usd_m"] * 1e6 / LIQ_TARGET_USD),
        "runway": min(1.0, raw["runway_months"] / RUNWAY_CAP_MONTHS),
    }


def score(raw: dict[str, float]) -> float:
    """Launch health 0-100: weighted drawdown, stability, liquidity and runway."""
    comp = score_components(raw)
    return 100 * sum(WEIGHTS[k] * comp[k] for k in WEIGHTS)
