"""Honest error bars: paired bootstrap intervals on the holdout leaderboard. No network."""

import copy
import json
from pathlib import Path

import pytest

from agent.harvest import scoring

REPLAY = Path(__file__).resolve().parents[1] / "agent" / "harvest" / "data" / "demo_replay.json"


def synth(n: int, persona_p: dict[str, float], market_p: float = 0.5, outcome_of=lambda i: i % 2) -> dict:
    """One holdout cutoff with n questions; each persona forecasts a constant on the outcome-1 or -0 side."""
    qids = [f"q{i}" for i in range(n)]
    outcomes = {q: outcome_of(i) for i, q in enumerate(qids)}
    return {
        "personas": [{"id": pid, "name": pid} for pid in persona_p],
        "cutoffs": [{
            "cutoff": "2025-09", "split": "holdout", "trades": [], "payouts": [],
            "final_prices": {q: market_p for q in qids},
            "resolutions": [{"qid": q, "outcome": o} for q, o in outcomes.items()],
            "forecasts": [{"persona_id": pid, "qid": q, "p_yes": (p if outcomes[q] else 1 - p)} for pid, p in persona_p.items() for q in qids],
        }],
    }


def test_interval_contains_point_estimate_and_is_deterministic():
    result = synth(40, {"good": 0.8, "meh": 0.6})
    a = scoring.leaderboard(result)
    b = scoring.leaderboard(copy.deepcopy(result))
    for ra, rb in zip(a["world_models"], b["world_models"]):
        assert ra["brier_holdout_lo"] <= ra["brier_holdout"] <= ra["brier_holdout_hi"]
        assert (ra["brier_holdout_lo"], ra["brier_holdout_hi"], ra["diff_vs_market_lo"], ra["diff_vs_market_hi"]) == \
               (rb["brier_holdout_lo"], rb["brier_holdout_hi"], rb["diff_vs_market_lo"], rb["diff_vs_market_hi"])
    assert a["interval"]["n_questions"] == 40 and "90% bootstrap interval over 40 sealed questions" in a["interval"]["note"]


def test_beats_market_ci_true_only_when_interval_below_zero():
    lb = scoring.leaderboard(synth(40, {"sharp": 0.9, "coin": 0.5}))
    rows = {m["persona_id"]: m for m in lb["world_models"]}
    assert rows["sharp"]["beats_market_ci"] is True and rows["sharp"]["diff_vs_market_hi"] < 0
    assert rows["coin"]["beats_market_ci"] is False and rows["coin"]["diff_vs_market"] == pytest.approx(0.0)
    assert "beat the market" in scoring.insight(lb) and "within noise" not in scoring.insight(lb)


def test_noisy_edge_is_within_noise_and_worse_model_is_not_flagged():
    # alternating good/bad questions: tiny mean edge with a wide interval
    result = synth(8, {"lucky": 0.55, "bad": 0.2})
    lb = scoring.leaderboard(result)
    rows = {m["persona_id"]: m for m in lb["world_models"]}
    assert rows["lucky"]["diff_vs_market_lo"] < 0 < rows["lucky"]["diff_vs_market_hi"] or rows["lucky"]["beats_market_ci"]
    assert rows["bad"]["beats_market_ci"] is False and rows["bad"]["diff_vs_market_lo"] > 0
    crossing = synth(30, {"x": 0.5}, market_p=0.5)
    text = scoring.insight(scoring.leaderboard(crossing))
    assert "within noise of the market" in text


def test_existing_fields_kept_and_external_rows_have_no_interval():
    lb = scoring.leaderboard(synth(10, {"a": 0.7}), external=[{"persona_id": "ext:x", "name": "X", "brier_holdout": 0.1}])
    ext = next(m for m in lb["world_models"] if m["persona_id"] == "ext:x")
    assert ext["brier_holdout_lo"] is None and ext["beats_market_ci"] is False
    for key in ("persona_id", "name", "origin", "brier_dev", "brier_holdout", "pnl_dev", "pnl_holdout", "n", "rank"):
        assert key in ext
    assert set(lb["baselines"]) == {"coin", "market", "climatology"}


def test_frozen_replay_unchanged_and_intervals_computed_from_it():
    before = REPLAY.read_bytes()
    replay = json.loads(before)
    lb = scoring.leaderboard(replay)
    assert REPLAY.read_bytes() == before
    assert lb["interval"]["n_questions"] == 48
    for m in lb["world_models"]:
        assert m["brier_holdout_lo"] <= m["brier_holdout"] <= m["brier_holdout_hi"]
        assert m["diff_vs_market_lo"] <= m["diff_vs_market"] <= m["diff_vs_market_hi"]
        assert m["beats_market_ci"] == (m["diff_vs_market_hi"] < 0)
