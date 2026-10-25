"""SEIRV simulator for a 10M-person region over one winter, pure stdlib (SPEC.md section 4). Owner: W3.

SEALED: these bytes are part of code_sha256. Deterministic for (candidate, seed, shift).

Model: five age bands, daily steps over 180 days. Contacts come from a POLYMOD-style matrix split
into home, school, work and other settings; transmissibility is scaled so the basic reproduction
number (dominant eigenvalue of the next-generation matrix) matches a published-range R0 for a
winter respiratory virus. Infection to admission follows age-rising hospitalisation, ICU and
fatality rates; hospital and ICU occupancy drain with fixed lengths of stay. Every random draw
(seeding, R0 jitter, uptake noise, importations, demographic noise) is made up front from
random.Random(seed), so the policy and the no-intervention reference see the same epidemic.
"""

import math
import random
from functools import lru_cache
from typing import Any

from agent.sealed.records import Candidate, Shift

NAME = "epidemic"
TITLE = "Winter epidemic policy"
GOAL = "Minimise winter hospital-days for 10M people with GBP 40M, no school closures over 3 weeks, without widening the age gap."

BANDS = ("0-17", "18-39", "40-59", "60-74", "75+")
POPULATION = 10_000_000
POP_SHARE = (0.21, 0.28, 0.26, 0.16, 0.09)
N = tuple(POPULATION * s for s in POP_SHARE)
PER_100K = POPULATION / 100_000
DAYS = 180

# Contacts per day a person in row band has with people in column band (POLYMOD GB style).
HOME = ((2.0, 1.2, 0.6, 0.1, 0.05), (0.9, 1.0, 0.3, 0.1, 0.05), (0.5, 0.3, 0.9, 0.2, 0.1), (0.15, 0.15, 0.3, 0.9, 0.2), (0.1, 0.1, 0.2, 0.3, 0.6))
SCHOOL = ((6.0, 0.4, 0.3, 0.0, 0.0), (0.3, 0.1, 0.0, 0.0, 0.0), (0.3, 0.0, 0.1, 0.0, 0.0), (0.0,) * 5, (0.0,) * 5)
WORK = ((0.0, 0.2, 0.2, 0.0, 0.0), (0.1, 3.0, 2.0, 0.3, 0.0), (0.1, 2.0, 2.5, 0.4, 0.0), (0.0, 0.3, 0.4, 0.4, 0.0), (0.0,) * 5)
OTHER = ((2.0, 1.0, 1.0, 0.3, 0.2), (0.8, 3.0, 1.5, 0.6, 0.3), (0.8, 1.5, 2.0, 0.8, 0.4), (0.4, 0.8, 1.0, 1.5, 0.6), (0.3, 0.5, 0.7, 0.8, 1.0))
SUSCEPTIBILITY = (0.6, 1.0, 1.0, 1.0, 1.0)

R0 = 2.0  # mid-range of published estimates for a novel winter respiratory wave
LATENT_DAYS, INFECTIOUS_DAYS = 3.0, 5.0
PRIOR_IMMUNE = (0.25, 0.30, 0.30, 0.30, 0.30)
HOSP_RATE = (0.002, 0.006, 0.02, 0.06, 0.15)  # admissions per infection
ICU_SHARE = (0.05, 0.12, 0.18, 0.18, 0.06)  # share of admissions needing ICU
IFR = (0.00002, 0.0002, 0.002, 0.012, 0.06)
HOSP_LOS, ICU_LOS = 9.0, 12.0
ICU_BASE_BEDS = 300  # ICU beds free for the epidemic in a 10M region
OVERFLOW_DEATH_RATE = 0.05  # extra deaths per ICU patient-day without a bed
SEASON_PEAK_DAY, SEASON_AMPLITUDE = 75, 0.12

VACCINE_SUPPLY = 3_000_000  # single doses available this winter
VACCINE_RATE = 60_000  # doses delivered per day once the campaign starts
VE_INFECTION = 0.5  # leaky protection against infection
VE_SEVERE_GIVEN_INFECTION = 0.7  # further cut in admission and death risk for vaccinated cases
UPTAKE = (0.5, 0.6, 0.75, 0.85, 0.9)  # max share of each band willing to be vaccinated
TI_MAX_EFFECT = 0.35  # transmission cut at full test-and-isolate with spare testing capacity
TI_CAPACITY_PER_100K = 150.0  # daily incidence per 100k at which testing effectiveness halves
SCHOOL_CLOSURE_KEEP = 0.1  # share of school contacts that remain while closed
WORK_CLOSURE_KEEP, WORK_CLOSURE_OTHER_KEEP = 0.35, 0.7
REOPEN_FRACTION = 0.7  # reopen when occupancy falls below this share of the trigger

BUDGET_GBP_M = 40.0
COST_PER_DOSE_GBP = 4.0
EARLY_START_DAY = 45  # campaigns starting before this day pay an acceleration premium
EARLY_START_PREMIUM_GBP_M_PER_DAY = 0.2
TI_COST_GBP_M = 30.0  # at test_isolate_level 1
SURGE_BED_COST_GBP_M = 0.025  # per surge ICU bed staffed for the winter
WORK_CLOSURE_COST_GBP_M_PER_DAY = 0.4  # furlough top-up, outside the ex-ante budget

LIMITS: dict[str, tuple[float, float]] = {
    "vaccine_start_day": (0, 120),
    "school_close_trigger_per_100k": (5, 500),
    "school_close_max_days": (0, 60),
    "work_close_trigger_per_100k": (5, 500),
    "test_isolate_level": (0, 1),
    "surge_icu_beds": (0, 1000),
}
INTEGER_KEYS = {"vaccine_start_day", "school_close_max_days", "surge_icu_beds"}
KEY_DOCS = {
    "vaccine_share": "Share of the 3M-dose supply given to each age band (sum <= 1; unused doses are not bought).",
    "vaccine_start_day": "Day the vaccination campaign starts (0-120); starting before day 45 costs GBP 0.2M per day earlier.",
    "school_close_trigger_per_100k": "Close schools when hospital occupancy per 100k reaches this (reopen below 70% of it).",
    "school_close_max_days": "Most days schools may be closed in total over the winter.",
    "work_close_trigger_per_100k": "Close workplaces when hospital occupancy per 100k reaches this (reopen below 70%); GBP 0.4M per closed day.",
    "test_isolate_level": "Test-and-isolate intensity 0-1, GBP 30M at 1; it saturates when incidence is high.",
    "surge_icu_beds": "Extra ICU beds staffed for the winter on top of 300 (GBP 25k each).",
}

CANDIDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "title": "Winter epidemic policy",
    "properties": {
        "vaccine_share": {
            "type": "object",
            "description": KEY_DOCS["vaccine_share"],
            "properties": {b: {"type": "number", "minimum": 0, "maximum": 1} for b in BANDS},
            "required": list(BANDS),
            "additionalProperties": False,
        },
        **{
            key: {"type": "integer" if key in INTEGER_KEYS else "number", "minimum": lo, "maximum": hi, "description": KEY_DOCS[key]}
            for key, (lo, hi) in LIMITS.items()
        },
    },
    "required": ["vaccine_share", *LIMITS],
    "additionalProperties": False,
}

SHIFT_PARAMS = ("beta_mult", "uptake_mult", "start_delay_days", "escape")


def baseline() -> Candidate:
    """The naive starting policy: doses spread evenly, a late campaign, middling everything."""
    return {
        "vaccine_share": {"0-17": 0.2, "18-39": 0.2, "40-59": 0.2, "60-74": 0.2, "75+": 0.2},
        "vaccine_start_day": 50,
        "school_close_trigger_per_100k": 40,
        "school_close_max_days": 14,
        "work_close_trigger_per_100k": 60,
        "test_isolate_level": 0.3,
        "surge_icu_beds": 300,
    }


def planned_cost_gbp_m(candidate: Candidate) -> float:
    """Ex-ante spend in GBP millions, checked against the budget."""
    doses = VACCINE_SUPPLY * sum(float(v) for v in candidate["vaccine_share"].values())
    vaccines = doses * COST_PER_DOSE_GBP / 1e6
    premium = max(0.0, EARLY_START_DAY - float(candidate["vaccine_start_day"])) * EARLY_START_PREMIUM_GBP_M_PER_DAY
    testing = TI_COST_GBP_M * float(candidate["test_isolate_level"])
    beds = SURGE_BED_COST_GBP_M * float(candidate["surge_icu_beds"])
    return vaccines + premium + testing + beds


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(candidate: Candidate) -> list[str]:
    """Schema, range and budget errors; empty means safe to simulate."""
    if not isinstance(candidate, dict):
        return [f"policy must be a JSON object, got {type(candidate).__name__}"]
    errors: list[str] = []
    expected = {"vaccine_share", *LIMITS}
    errors += [f"missing key {k}" for k in sorted(expected - candidate.keys())]
    errors += [f"unknown key {k}" for k in sorted(set(candidate) - expected, key=str)]
    shares = candidate.get("vaccine_share")
    if "vaccine_share" in candidate:
        if not isinstance(shares, dict):
            errors.append("vaccine_share must be an object keyed by age band")
        else:
            errors += [f"missing vaccine_share.{b}" for b in BANDS if b not in shares]
            errors += [f"unknown age band vaccine_share.{b}" for b in shares if b not in BANDS]
            for band in BANDS:
                if band not in shares:
                    continue
                value = shares[band]
                if not _is_number(value):
                    errors.append(f"vaccine_share.{band} must be a number")
                elif not 0 <= value <= 1:
                    errors.append(f"vaccine_share.{band} must be between 0 and 1, got {value}")
            numbers = [v for v in shares.values() if _is_number(v)]
            if len(numbers) == len(shares) and sum(numbers) > 1 + 1e-9:
                errors.append(f"vaccine_share sums to {sum(numbers):.3f}; the shares of one dose supply must sum to at most 1")
    for key, (lo, hi) in LIMITS.items():
        if key not in candidate:
            continue
        value = candidate[key]
        if not _is_number(value):
            errors.append(f"{key} must be a number")
        elif key in INTEGER_KEYS and value != int(value):
            errors.append(f"{key} must be a whole number, got {value}")
        elif not lo <= value <= hi:
            errors.append(f"{key} must be between {lo:g} and {hi:g}, got {value}")
    if not errors:
        cost = planned_cost_gbp_m(candidate)
        if cost > BUDGET_GBP_M + 1e-9:
            errors.append(f"planned spend GBP {cost:.1f}M exceeds the GBP {BUDGET_GBP_M:.0f}M budget")
    return errors


def _dominant_eigenvalue(m: list[list[float]]) -> float:
    v = [1.0] * 5
    lam = 0.0
    for _ in range(200):
        w = [sum(m[i][j] * v[j] for j in range(5)) for i in range(5)]
        lam = max(w)
        v = [x / lam for x in w]
    return lam


ALL_CONTACTS = tuple(tuple(HOME[i][j] + SCHOOL[i][j] + WORK[i][j] + OTHER[i][j] for j in range(5)) for i in range(5))
BETA = R0 / _dominant_eigenvalue([[SUSCEPTIBILITY[i] * ALL_CONTACTS[i][j] * N[i] / N[j] * INFECTIOUS_DAYS for j in range(5)] for i in range(5)])


@lru_cache(maxsize=512)
def _draws(seed: int) -> dict[str, Any]:
    """Every random number one winter needs, independent of policy and shift."""
    rng = random.Random(seed)
    return {
        "r0_mult": math.exp(rng.gauss(0.0, 0.06)),
        "seed_day": rng.randint(5, 15),
        "seed_count": rng.uniform(150.0, 400.0),
        "uptake_noise": tuple(min(1.0, u * math.exp(rng.gauss(0.0, 0.06))) for u in UPTAKE),
        "imports": tuple(rng.expovariate(1 / 8.0) for _ in range(DAYS)),
        "noise": tuple(tuple(rng.gauss(0.0, 1.0) for _ in range(5)) for _ in range(DAYS)),
        "daily_beta": tuple(math.exp(rng.gauss(0.0, 0.04)) for _ in range(DAYS)),
    }


def _shift_params(shift: Shift) -> tuple[float, float, int, float]:
    p = shift.get("params", {}) or {}
    return (
        float(p.get("beta_mult", 1.0)),
        float(p.get("uptake_mult", 1.0)),
        int(p.get("start_delay_days", 0)),
        float(p.get("escape", 0.0)),
    )


def simulate(candidate: Candidate, *, seed: int, shift: Shift) -> dict[str, float]:
    """One stochastic winter; returns raw outcome components (hospital_days, icu_overflow_days, ...)."""
    return dict(_run(_freeze(candidate), seed, _shift_params(shift)))


def no_intervention(*, seed: int, shift: Shift) -> dict[str, float]:
    """The reference winter with no policy at all, for hospital-days averted."""
    return dict(_run(None, seed, _shift_params(shift)))


def _freeze(c: Candidate) -> tuple[Any, ...]:
    return (
        tuple(float(c["vaccine_share"][b]) for b in BANDS),
        int(c["vaccine_start_day"]),
        float(c["school_close_trigger_per_100k"]),
        int(c["school_close_max_days"]),
        float(c["work_close_trigger_per_100k"]),
        float(c["test_isolate_level"]),
        int(c["surge_icu_beds"]),
        planned_cost_gbp_m(c),
    )


@lru_cache(maxsize=4096)
def _run(policy: tuple[Any, ...] | None, seed: int, shift: tuple[float, float, int, float]) -> tuple[tuple[str, float], ...]:
    beta_mult, uptake_mult, start_delay, escape = shift
    d = _draws(seed)
    if policy is None:
        shares, vacc_start, school_trig, school_max, work_trig, ti_level, surge, spend = (0.0,) * 5, DAYS, 1e9, 0, 1e9, 0.0, 0, 0.0
    else:
        shares, vacc_start, school_trig, school_max, work_trig, ti_level, surge, spend = policy

    ve = VE_INFECTION * (1.0 - escape)
    sev_keep = 1.0 - VE_SEVERE_GIVEN_INFECTION * (1.0 - 0.5 * escape)
    s = [N[i] * (1.0 - PRIOR_IMMUNE[i] * (1.0 - escape)) for i in range(5)]
    sv = [0.0] * 5
    e = [0.0] * 5
    inf = [0.0] * 5
    e_risk = [0.0] * 5  # severity-weighted E and I: vaccinated cases count sev_keep
    i_risk = [0.0] * 5
    unvaccinated = list(N)
    dose_cap = [N[i] * min(1.0, d["uptake_noise"][i] * uptake_mult) for i in range(5)]
    dose_target = [min(VACCINE_SUPPLY * shares[i], dose_cap[i]) for i in range(5)]
    doses_given = [0.0] * 5
    hosp = [0.0] * 5
    icu = 0.0
    hosp_days = [0.0] * 5
    deaths = 0.0
    icu_overflow = 0.0
    peak_icu = 0.0
    peak_hosp = 0.0
    school_closed = work_closed = False
    school_days = work_days = 0
    infections = [0.0] * 5
    beta0 = BETA * d["r0_mult"] * beta_mult
    seed_day = d["seed_day"] + start_delay
    icu_beds = ICU_BASE_BEDS + surge
    share_total = max(sum(shares), 1e-9)
    p_latent = 1.0 - math.exp(-1.0 / LATENT_DAYS)
    p_recover = 1.0 - math.exp(-1.0 / INFECTIOUS_DAYS)
    hosp_out, icu_out = 1.0 / HOSP_LOS, 1.0 / ICU_LOS

    for t in range(DAYS):
        occupancy = sum(hosp) / PER_100K
        if school_closed:
            school_closed = occupancy >= school_trig * REOPEN_FRACTION and school_days < school_max
        elif occupancy >= school_trig and school_days < school_max:
            school_closed = True
        if work_closed:
            work_closed = occupancy >= work_trig * REOPEN_FRACTION
        elif occupancy >= work_trig:
            work_closed = True
        school_days += school_closed
        work_days += work_closed
        sk = SCHOOL_CLOSURE_KEEP if school_closed else 1.0
        wk, ok = (WORK_CLOSURE_KEEP, WORK_CLOSURE_OTHER_KEEP) if work_closed else (1.0, 1.0)

        incidence = sum(inf) * p_recover / PER_100K
        ti_effect = TI_MAX_EFFECT * ti_level / (1.0 + incidence / TI_CAPACITY_PER_100K)
        season = 1.0 + SEASON_AMPLITUDE * math.cos(2 * math.pi * (t - SEASON_PEAK_DAY) / 365.0)
        beta_t = beta0 * season * d["daily_beta"][t] * (1.0 - ti_effect)
        prevalence = [inf[j] / N[j] for j in range(5)]
        if t >= seed_day:
            imports = d["imports"][t] + (d["seed_count"] if t == seed_day else 0.0)
        else:
            imports = 0.0

        noise = d["noise"][t]
        for i in range(5):
            contacts = sum((HOME[i][j] + sk * SCHOOL[i][j] + wk * WORK[i][j] + ok * OTHER[i][j]) * prevalence[j] for j in range(5))
            p_inf = 1.0 - math.exp(-beta_t * SUSCEPTIBILITY[i] * contacts)
            p_inf_v = 1.0 - math.exp(-beta_t * SUSCEPTIBILITY[i] * (1.0 - ve) * contacts)
            mean = s[i] * p_inf + sv[i] * p_inf_v
            new_inf = max(0.0, mean + math.sqrt(mean) * noise[i]) + imports * POP_SHARE[i]
            new_inf = min(new_inf, s[i] + sv[i])
            from_v = new_inf * (sv[i] * p_inf_v / mean) if mean > 0 else 0.0
            from_s = new_inf - from_v
            if from_s > s[i]:
                from_s, from_v = s[i], new_inf - s[i]
            risk = from_s + sev_keep * from_v
            new_sym = e[i] * p_latent
            sym_risk = e_risk[i] * p_latent
            recovered = inf[i] * p_recover
            rec_risk = i_risk[i] * p_recover
            s[i] -= from_s
            sv[i] -= from_v
            e[i] += new_inf - new_sym
            e_risk[i] += risk - sym_risk
            inf[i] += new_sym - recovered
            i_risk[i] += sym_risk - rec_risk
            infections[i] += new_inf
            deaths += risk * IFR[i]

            admissions = rec_risk * HOSP_RATE[i]
            hosp[i] += admissions - hosp[i] * hosp_out
            icu += admissions * ICU_SHARE[i] - icu * icu_out
            hosp_days[i] += hosp[i]

            if t >= vacc_start and doses_given[i] < dose_target[i]:
                today = min(dose_target[i] - doses_given[i], VACCINE_RATE * shares[i] / share_total)
                moved = min(s[i], today * s[i] / max(unvaccinated[i], 1.0))
                s[i] -= moved
                sv[i] += moved
                unvaccinated[i] -= today
                doses_given[i] += today

        over = max(0.0, icu - icu_beds)
        icu_overflow += over
        deaths += over * OVERFLOW_DEATH_RATE
        peak_icu = max(peak_icu, icu)
        peak_hosp = max(peak_hosp, sum(hosp) / PER_100K)

    per_capita = [hosp_days[i] / N[i] for i in range(5)]
    work_cost = work_days * WORK_CLOSURE_COST_GBP_M_PER_DAY
    out = {
        "hospital_days": sum(hosp_days),
        "icu_overflow_days": icu_overflow,
        "peak_icu": peak_icu,
        "icu_beds": float(icu_beds),
        "peak_hosp_per_100k": peak_hosp,
        "deaths": deaths,
        "infections_m": sum(infections) / 1e6,
        "closure_days": float(school_days),
        "work_closure_days": float(work_days),
        "doses_m": sum(doses_given) / 1e6,
        "cost_gbp_m": spend + work_cost,
        **{f"hosp_days_per_1k_{b}": 1000.0 * per_capita[i] for i, b in enumerate(BANDS)},
    }
    return tuple(out.items())
