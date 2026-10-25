"""Token design stress test plug-in: simulator, locked eval, holdout and the overfit-then-rejected demo."""

import copy
import itertools
import time
from typing import Any

import pytest

from agent.sealed import gate
from agent.sealed.environment import Environment, list_environments, load_environment
from agent.sealed.envs.tokenomics import sim
from agent.sealed.records import BASE_SHIFT

# Hand-made conservative design: daily drip, $3M protocol pool, buyback with a 12-month reserve.
CONSERVATIVE: dict[str, Any] = {
    "team_cliff_months": 6, "team_vest_months": 36, "investor_cliff_months": 0, "investor_vest_months": 12,
    "unlock_cadence_days": 1, "emission_pct_per_month": 0.0, "initial_float_pct": 10.0, "lp_depth_usd_m": 3.0,
    "buyback_trigger_drawdown": 0.3, "buyback_daily_pct": 0.05, "treasury_reserve_months": 12.0,
}
OVERFIT_GRID: dict[str, list[Any]] = {
    "investor_vest_months": [6, 12, 24],
    "lp_depth_usd_m": [0.5, 1.0, 2.0, 3.0, 4.0, 6.0],
    "buyback_daily_pct": [0.0, 0.05],
    "emission_pct_per_month": [0.0, 1.0],
}


@pytest.fixture(scope="module")
def env() -> Environment:
    return load_environment("tokenomics")


@pytest.fixture(scope="module")
def holdout_judge(env: Environment) -> Any:
    """Scores a candidate on the sealed holdout and judges it with the sealed gate, as the reveal step does."""
    holdout = env.load_holdout()
    shifts = [BASE_SHIFT, *holdout["shifts"]]
    labels = {s["id"]: s["label"] for s in shifts}

    def per_shift(candidate: dict[str, Any]) -> dict[str, float]:
        return {s["id"]: env.evaluate(candidate, seeds=holdout["seeds"], shift=s)["score"] for s in shifts}

    baseline_hold = per_shift(env.baseline())

    def judge(candidate: dict[str, Any]) -> gate.GateOutcome:
        return gate.judge(env.dev_report(candidate)["score"], per_shift(candidate), baseline_hold, env.spec["gate"], labels)

    return judge


def test_registered_and_public_view(env: Environment) -> None:
    assert "tokenomics" in list_environments()
    view = env.public_view()
    assert view["name"] == "tokenomics" and view["title"] == sim.TITLE and view["goal"]
    assert set(view["candidate_schema"]["required"]) == set(sim.LIMITS)
    assert env.handbook() and env.spec["higher_is_better"] is True


def test_holdout_shape_and_seeds_disjoint_from_dev(env: Environment) -> None:
    holdout = env.load_holdout()
    ids = [s["id"] for s in holdout["shifts"]]
    assert ids == ["market_crash", "whale_dump", "liquidity_withdrawal", "correlated_unlocks"]
    assert all(s["label"] and s["params"] for s in holdout["shifts"])
    assert not set(holdout["seeds"]) & set(env.spec["dev_seeds"])


def test_baseline_validates_and_scores_in_range(env: Environment) -> None:
    assert env.validate(env.baseline()) == []
    report = env.dev_report(env.baseline())
    assert 0 <= report["score"] <= 100 and len(report["per_seed"]) == len(env.spec["dev_seeds"])
    assert report["shift_id"] == "base" and "max_drawdown" in report["breakdown"]


def test_evaluate_is_deterministic(env: Environment) -> None:
    shift = env.load_holdout()["shifts"][0]
    first = env.evaluate(CONSERVATIVE, seeds=[1, 2, 3], shift=shift)
    assert env.evaluate(copy.deepcopy(CONSERVATIVE), seeds=[1, 2, 3], shift=shift) == first


def test_one_design_scores_under_200ms_and_20_seeds_under_2s(env: Environment) -> None:
    start = time.perf_counter()
    env.dev_report(CONSERVATIVE)
    assert time.perf_counter() - start < 0.2
    start = time.perf_counter()
    env.evaluate(CONSERVATIVE, seeds=list(range(100, 120)), shift=env.load_holdout()["shifts"][0])
    assert time.perf_counter() - start < 2.0


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"lp_depth_usd_m": 13.0}, "outside"),
        ({"unlock_cadence_days": 7.5}, "integer"),
        ({"buyback_daily_pct": "lots"}, "must be a number"),
        ({"initial_float_pct": float("nan")}, "must be a number"),
        ({"surprise": 1}, "unknown key"),
        ({"lp_depth_usd_m": 12.0, "investor_cliff_months": 12, "investor_vest_months": 36}, "budget overrun"),
    ],
)
def test_validate_rejects_bad_candidates(env: Environment, change: dict[str, Any], fragment: str) -> None:
    errors = env.validate({**CONSERVATIVE, **change})
    assert any(fragment in e for e in errors), errors


def test_validate_rejects_missing_keys_and_non_objects(env: Environment) -> None:
    partial = dict(CONSERVATIVE)
    del partial["lp_depth_usd_m"]
    assert any("missing key" in e for e in env.validate(partial))
    assert env.validate([1, 2]) == ["candidate must be a JSON object"]  # type: ignore[arg-type]


def test_grid_search_overfitter_is_rejected_and_conservative_passes(env: Environment, holdout_judge: Any) -> None:
    """The demo moment: the dev-best design from a grid search fails the sealed stress test; the conservative one holds."""
    best_score, best = float("-inf"), CONSERVATIVE
    for values in itertools.product(*OVERFIT_GRID.values()):
        candidate = {**CONSERVATIVE, **dict(zip(OVERFIT_GRID, values))}
        if env.validate(candidate):
            continue
        score = env.dev_report(candidate)["score"]
        if score > best_score:
            best_score, best = score, candidate
    conservative_dev = env.dev_report(CONSERVATIVE)["score"]
    assert best_score > conservative_dev > env.dev_report(env.baseline())["score"]

    overfit = holdout_judge(best)
    assert overfit.verdict == "OVERFIT", overfit
    assert overfit.relative_gap > env.spec["gate"]["max_relative_gap"]

    robust = holdout_judge(CONSERVATIVE)
    assert robust.verdict == "GENERALISES", robust
    assert robust.holdout_mean > overfit.holdout_mean  # the holdout reverses the dev ranking


def test_demo_trajectory_ends_with_an_overfit_champion_and_a_generalising_pick(env: Environment, holdout_judge: Any) -> None:
    champion_score = env.dev_report(env.baseline())["score"]
    decisions, keeps = [], []
    for proposal in env.demo_trajectory():
        assert {"hypothesis", "rationale", "strategy", "candidate"} <= set(proposal)
        if env.validate(proposal["candidate"]):
            decisions.append("INVALID")
            continue
        score = env.dev_report(proposal["candidate"])["score"]
        if score > champion_score:
            champion_score = score
            decisions.append("KEEP")
            keeps.append((score, proposal["candidate"]))
        else:
            decisions.append("REVERT")
    assert decisions.count("INVALID") == 1 and "REVERT" in decisions
    verdicts = [(score, holdout_judge(candidate).verdict) for score, candidate in keeps]
    assert verdicts[-1][1] == "OVERFIT"  # the dev champion is rejected
    selected = max((s for s, v in verdicts if v == "GENERALISES"), default=None)
    assert selected is not None and selected < verdicts[-1][0]
