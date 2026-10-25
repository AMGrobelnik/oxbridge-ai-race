"""W3 epidemic plug-in: simulator, locked eval, holdout and the cached demo trajectory."""

import copy
import json
import math
import time
from typing import Any

import pytest

from agent.sealed import gate
from agent.sealed.environment import Environment, load_environment
from agent.sealed.records import BASE_SHIFT

ONE_POINT_FOUR = "transmissibility_1_4x"


@pytest.fixture(scope="module")
def env() -> Environment:
    return load_environment("epidemic")


@pytest.fixture(scope="module")
def reveal(env: Environment) -> dict[str, Any]:
    """Replays the demo trajectory under keep-or-revert, then scores the finalists on the holdout."""
    champion, champion_score = env.baseline(), env.dev_report(env.baseline())["score"]
    decisions: list[str] = []
    keeps: list[tuple[int, dict[str, Any], float]] = []
    for index, proposal in enumerate(env.demo_trajectory(), 1):
        candidate = proposal["candidate"]
        if env.validate(candidate):
            decisions.append("INVALID")
            continue
        score = env.dev_report(candidate)["score"]
        if score > champion_score:
            champion, champion_score = candidate, score
            decisions.append("KEEP")
            keeps.append((index, candidate, score))
        else:
            decisions.append("REVERT")
    holdout = env.load_holdout()
    shifts = [BASE_SHIFT, *holdout["shifts"]]
    labels = {s["id"]: s["label"] for s in shifts}

    def per_shift(candidate: dict[str, Any]) -> dict[str, float]:
        return {s["id"]: env.evaluate(candidate, seeds=holdout["seeds"], shift=s)["score"] for s in shifts}

    baseline_dev = env.dev_report(env.baseline())["score"]
    baseline_hold = per_shift(env.baseline())
    verdicts = {
        index: (score, gate.judge(score, per_shift(candidate), baseline_hold, env.spec["gate"], labels))
        for index, candidate, score in keeps
    }
    return {"decisions": decisions, "keeps": keeps, "verdicts": verdicts, "baseline_dev": baseline_dev,
            "baseline_hold": baseline_hold, "per_shift": per_shift, "labels": labels}


def test_spec_and_holdout_shape(env: Environment) -> None:
    spec = env.spec
    assert spec["version"] == "epidemic-eval/1" and spec["higher_is_better"] is True
    assert 5 <= len(spec["dev_seeds"]) <= 8
    assert spec["gate"]["no_shift_worse_than_baseline"] is True
    holdout = env.load_holdout()
    assert len(holdout["seeds"]) == 20 and not set(holdout["seeds"]) & set(spec["dev_seeds"])
    ids = [s["id"] for s in holdout["shifts"]]
    assert ids[0] == ONE_POINT_FOUR and len(ids) == 4 and "base" not in ids
    assert env.candidate_schema["additionalProperties"] is False
    assert len(env.goal.split(". ")) == 1


def test_baseline_validates_and_scores_finite(env: Environment) -> None:
    assert env.validate(env.baseline()) == []
    report = env.dev_report(env.baseline())
    assert math.isfinite(report["score"]) and report["shift_id"] == "base"
    assert report["seeds"] == env.spec["dev_seeds"] and len(report["per_seed"]) == len(report["seeds"])
    assert report["breakdown"]["hospital_days"] > 0 and report["breakdown"]["cost_gbp_m"] <= 40
    json.dumps(report, allow_nan=False)


def test_evaluate_is_deterministic(env: Environment) -> None:
    shift = env.load_holdout()["shifts"][0]
    first = env.evaluate(env.baseline(), seeds=[7, 8, 9], shift=shift)
    again = env.evaluate(copy.deepcopy(env.baseline()), seeds=[7, 8, 9], shift=copy.deepcopy(shift))
    assert first == again
    assert first["per_seed"][0] != first["per_seed"][1]  # stochastic across seeds


def test_twenty_holdout_seeds_under_one_shift_take_under_a_second(env: Environment) -> None:
    holdout = env.load_holdout()
    candidate = env.baseline()
    candidate["surge_icu_beds"] = 123  # a fresh candidate, so nothing is cached
    start = time.perf_counter()
    env.evaluate(candidate, seeds=holdout["seeds"], shift=holdout["shifts"][3])
    assert time.perf_counter() - start < 1.0


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"test_isolate_level": 1.0, "surge_icu_beds": 600}, "budget"),
        ({"isolation_pay": 5}, "unknown key isolation_pay"),
        ({"surge_icu_beds": -10}, "surge_icu_beds must be between"),
        ({"vaccine_share": {"0-17": 0.5, "18-39": 0.5, "40-59": 0.5, "60-74": 0, "75+": 0}}, "sum"),
        ({"vaccine_share": {"0-17": -0.1, "18-39": 0.2, "40-59": 0.2, "60-74": 0.2, "75+": 0.2}}, "vaccine_share.0-17"),
        ({"school_close_max_days": "14"}, "must be a number"),
        ({"vaccine_start_day": 30.5}, "whole number"),
        ({"test_isolate_level": True}, "must be a number"),
    ],
)
def test_validate_rejects_bad_candidates(env: Environment, change: dict[str, Any], fragment: str) -> None:
    candidate = env.baseline() | change
    errors = env.validate(candidate)
    assert any(fragment in e for e in errors), errors


def test_validate_rejects_missing_keys_and_non_objects(env: Environment) -> None:
    candidate = env.baseline()
    del candidate["surge_icu_beds"]
    assert "missing key surge_icu_beds" in env.validate(candidate)
    assert env.validate("oops")  # type: ignore[arg-type]
    assert env.validate({**env.baseline(), "vaccine_share": [0.2] * 5})


def test_every_trajectory_candidate_validates_except_one_deliberate(env: Environment) -> None:
    trajectory = env.demo_trajectory()
    assert len(trajectory) == 8
    for proposal in trajectory:
        assert proposal["hypothesis"] and "\n" not in proposal["hypothesis"]
        assert proposal["strategy"] in {"continue", "pivot", "widen"}
    invalid = [p for p in trajectory if env.validate(p["candidate"])]
    assert len(invalid) <= 1


def test_replay_keeps_and_reverts(reveal: dict[str, Any]) -> None:
    assert reveal["decisions"].count("KEEP") >= 4
    assert reveal["decisions"].count("REVERT") >= 2


def test_dev_champion_overfits_under_one_point_four_transmissibility(reveal: dict[str, Any]) -> None:
    index, _, _ = reveal["keeps"][-1]
    _, outcome = reveal["verdicts"][index]
    assert outcome.verdict == "OVERFIT"
    assert any(reveal["labels"][ONE_POINT_FOUR] in r for r in outcome.reasons), outcome.reasons


def test_an_earlier_keep_generalises(reveal: dict[str, Any]) -> None:
    last = reveal["keeps"][-1][0]
    generalising = [
        i for i, (score, outcome) in reveal["verdicts"].items()
        if i != last and score > reveal["baseline_dev"] and outcome.verdict == "GENERALISES"
    ]
    assert generalising


def test_robust_hand_made_policy_generalises(env: Environment, reveal: dict[str, Any]) -> None:
    robust = env.baseline() | {
        "vaccine_share": {"0-17": 0.1, "18-39": 0.2, "40-59": 0.25, "60-74": 0.25, "75+": 0.2},
        "test_isolate_level": 0.1,
        "surge_icu_beds": 250,
    }
    assert env.validate(robust) == []
    dev = env.dev_report(robust)["score"]
    outcome = gate.judge(dev, reveal["per_shift"](robust), reveal["baseline_hold"], env.spec["gate"], reveal["labels"])
    assert dev > reveal["baseline_dev"] and outcome.verdict == "GENERALISES", outcome.reasons
