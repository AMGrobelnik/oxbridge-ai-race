"""Tests for the SOL/USDC strategy-research plug-in (agent/sealed/envs/strategy)."""

import asyncio
import hashlib
import itertools
import time
from pathlib import Path
from typing import Any

import pytest

from agent.sealed import gate, harness
from agent.sealed.environment import Environment, list_environments, load_environment
from agent.sealed.envs.strategy import sim
from agent.sealed.records import BASE_SHIFT, Candidate
from agent.sealed.store import RunStore


@pytest.fixture(scope="module")
def env() -> Environment:
    return load_environment("strategy")


def _holdout_judge(env: Environment, candidate: Candidate) -> tuple[float, gate.GateOutcome]:
    holdout = env.load_holdout()
    shifts = [BASE_SHIFT, *holdout["shifts"]]
    labels = {s["id"]: s["label"] for s in shifts}

    def per_shift(c: Candidate) -> dict[str, float]:
        return {s["id"]: env.evaluate(c, seeds=holdout["seeds"], shift=s)["score"] for s in shifts}

    dev = env.dev_report(candidate)["score"]
    return dev, gate.judge(dev, per_shift(candidate), per_shift(env.baseline()), env.spec["gate"], labels)


def _robust(env: Environment) -> Candidate:
    """A simple slow trend filter: 50% SOL in uptrends, 30% in downtrends, SOL leg staked."""
    return dict(env.baseline(), rule="momentum", lookback_days=60, entry_threshold_pct=0, risk_on_sol_weight=0.5, risk_off_sol_weight=0.3, staked_share=1.0)


def test_plugin_interface(env: Environment) -> None:
    assert "strategy" in list_environments()
    assert env.name == sim.NAME == "strategy"
    view = env.public_view()
    assert view["title"] and view["goal"] and view["metric_label"]
    assert env.validate(env.baseline()) == []
    assert env.spec["higher_is_better"] is True
    assert [p.name for p in env.code_files()] == ["sim.py", "eval.py"]
    assert env.handbook().startswith("# SOL/USDC")
    report = env.dev_report(env.baseline())
    assert report["seeds"] == env.spec["dev_seeds"] and report["shift_id"] == "base"
    assert len(report["per_seed"]) == len(report["seeds"])
    assert {"sharpe", "max_drawdown_pct", "fees_usd", "end_value"} <= set(report["breakdown"])


def test_fixture_is_real_sealed_data() -> None:
    raw = sim.DATA_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == sim.DATA_SHA256
    days, closes = sim.load_prices()
    assert days[0] == "2024-11-04" and days[-1] == "2026-10-24" and len(closes) == 720
    assert all(5 < c < 1000 for c in closes)


def test_holdout_is_later_than_dev(env: Environment) -> None:
    holdout = env.load_holdout()
    dev_end = max(env.spec["dev_seeds"]) + sim.WINDOW_DAYS
    assert min(holdout["seeds"]) > dev_end
    assert all(s in sim.valid_seeds() for s in [*env.spec["dev_seeds"], *holdout["seeds"]])
    assert {s["id"] for s in holdout["shifts"]} == {"crash", "usdc_depeg", "fee_spike", "vol_spike"}
    assert all(set(s["params"]) <= set(sim.SHIFT_PARAMS) for s in holdout["shifts"])


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"rule": "martingale"}, "rule must be one of"),
        ({"lookback_days": 1}, "lookback_days must be within"),
        ({"lookback_days": 7.5}, "must be an integer"),
        ({"risk_on_sol_weight": "all"}, "must be a number"),
        ({"staked_share": True}, "must be a number"),
        ({"leverage": 3}, "unknown key"),
    ],
)
def test_validate_rejects_bad_candidates(env: Environment, patch: dict[str, Any], needle: str) -> None:
    errors = env.validate(dict(env.baseline(), **patch))
    assert any(needle in e for e in errors), errors


def test_validate_reports_missing_keys(env: Environment) -> None:
    candidate = env.baseline()
    del candidate["rebalance_band"]
    assert "missing rebalance_band" in env.validate(candidate)


def test_deterministic_and_fast(env: Environment) -> None:
    candidate = _robust(env)
    first = env.dev_report(candidate)
    start = time.perf_counter()
    second = env.dev_report(candidate)
    assert time.perf_counter() - start < 0.1
    assert first == second


def test_stress_shifts_bite(env: Environment) -> None:
    base = env.baseline()
    seeds = env.spec["dev_seeds"]
    plain = env.evaluate(base, seeds=seeds, shift=BASE_SHIFT)
    for shift in env.load_holdout()["shifts"]:
        stressed = env.evaluate(base, seeds=seeds, shift=shift)["score"]
        # A rebalancer can profit from buying depegged USDC that re-pegs, so only "changes" there.
        assert stressed < plain["score"] if shift["id"] != "usdc_depeg" else stressed != plain["score"], shift["id"]


def test_grid_search_overfitter_fails_and_robust_passes(env: Environment) -> None:
    """The demo moment: the best in-sample grid strategy is rejected; the simple one generalises."""
    grid = itertools.product(("momentum", "mean_reversion"), (7, 14, 21, 30), (5, 10, 15, 20), (0.0, 0.2), (0.05, 0.1), (0, 10, 20))
    candidates = [
        {"rule": r, "lookback_days": lb, "entry_threshold_pct": th, "risk_on_sol_weight": 1.0, "risk_off_sol_weight": off, "staked_share": 1.0, "rebalance_band": band, "stop_loss_pct": stop}
        for r, lb, th, off, band, stop in grid
    ]
    overfit = max(candidates, key=lambda c: env.dev_report(c)["score"])
    robust = _robust(env)

    overfit_dev, overfit_gate = _holdout_judge(env, overfit)
    robust_dev, robust_gate = _holdout_judge(env, robust)
    baseline_dev = env.dev_report(env.baseline())["score"]

    assert overfit_dev > robust_dev > baseline_dev
    assert overfit_gate.verdict == "OVERFIT"
    assert overfit_gate.relative_gap > env.spec["gate"]["max_relative_gap"]
    assert any("collapses under" in r for r in overfit_gate.reasons)
    assert robust_gate.verdict == "GENERALISES", robust_gate.reasons
    assert robust_gate.holdout_mean > overfit_gate.holdout_mean


def test_scripted_demo_run_rejects_champion_and_selects_robust(env: Environment, tmp_path: Path) -> None:
    trajectory = env.demo_trajectory()
    assert len(trajectory) == 8 and all(env.validate(p["candidate"]) == [] for p in trajectory)
    record = asyncio.run(harness.run_sealed("strategy", iterations=8, proposer="scripted", anchor=False, store=RunStore(tmp_path)))
    assert record["status"] == "done", record["error"]
    decisions = [it["decision"] for it in record["iterations"]]
    assert decisions == ["BASELINE", "KEEP", "KEEP", "REVERT", "REVERT", "KEEP", "KEEP", "REVERT", "KEEP"]
    reveal = record["reveal"]
    assert reveal is not None
    assert reveal["dev_champion_iteration"] == 8 and reveal["verdict"] == "OVERFIT"
    assert reveal["selected_iteration"] == 2
    assert record["iterations"][2]["candidate"] == _robust(env)
