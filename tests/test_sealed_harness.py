"""W1 core loop: harness, proposer, store. Uses a toy environment and fake prereg/chain (no network)."""

import asyncio
import json
import re
import time
import types
from pathlib import Path
from typing import Any

import pytest

from agent.sealed import chain, harness, prereg, store
from agent.sealed.environment import Environment
from agent.sealed.proposer import GeminiProposer, ProposerError, ScriptedProposer, make_proposer, parse_proposal
from agent.sealed.records import RUN_ID_PATTERN
from agent.sealed.store import RunNotFoundError, RunStore

SECRET_SEED = 987654321
SECRET_LABEL = "SECRET-HARSH-SHIFT"


def _validate(c: dict[str, Any]) -> list[str]:
    errors = []
    for key in ("x", "y"):
        v = c.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            errors.append(f"{key} must be a number")
        elif not 0 <= v <= 10:
            errors.append(f"{key} out of range")
    return errors


def _evaluate(c: dict[str, Any], *, seeds: list[int], shift: dict[str, Any]) -> dict[str, Any]:
    if c.get("x") == 7:
        time.sleep(0.5)  # exercises the eval timeout
    penalty = shift["params"].get("penalty", 0.0)
    score = 10 + c["x"] + c["y"] - penalty * c["y"] ** 2
    return {"score": float(score), "per_seed": [float(score)] * len(seeds), "breakdown": {"x": c["x"], "y": c["y"]}, "seeds": list(seeds), "shift_id": shift["id"]}


TRAJECTORY = [
    {"hypothesis": "raise x", "rationale": "", "strategy": "continue", "candidate": {"x": 2, "y": 0}},  # 12 KEEP
    {"hypothesis": "lower x", "rationale": "", "strategy": "continue", "candidate": {"x": 1, "y": 0}},  # 11 REVERT
    {"hypothesis": "huge x", "rationale": "", "strategy": "widen", "candidate": {"x": 99, "y": 0}},  # INVALID schema
    {"hypothesis": "lean on y", "rationale": "", "strategy": "pivot", "candidate": {"x": 2, "y": 3}},  # 15 KEEP, collapses
    {"hypothesis": "not a dict", "rationale": "", "strategy": "continue", "candidate": "oops"},  # INVALID
]


@pytest.fixture
def toy_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    sim = types.ModuleType("toy_sim")
    sim.NAME, sim.TITLE, sim.GOAL = "toy", "Toy problem", "Maximise x + y."
    sim.CANDIDATE_SCHEMA = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}
    sim.baseline = lambda: {"x": 0, "y": 0}
    sim.validate = _validate
    sim.simulate = lambda c, *, seed, shift: {"value": 0.0}
    ev = types.ModuleType("toy_eval")
    ev.SPEC = {
        "version": "1",
        "metric": "value",
        "metric_label": "Toy value",
        "higher_is_better": True,
        "dev_seeds": [1, 2, 3],
        "gate": {"max_relative_gap": 0.5, "no_shift_worse_than_baseline": True},
        "notes": "",
    }
    ev.evaluate = _evaluate
    (tmp_path / "holdout.json").write_text(json.dumps({"seeds": [SECRET_SEED, 42], "shifts": [{"id": "harsh", "label": SECRET_LABEL, "params": {"penalty": 1.0}}]}))
    (tmp_path / "demo_trajectory.json").write_text(json.dumps(TRAJECTORY))
    (tmp_path / "handbook.md").write_text("Toy handbook.")
    env = Environment(name="toy", directory=tmp_path, sim=sim, evaluator=ev)
    monkeypatch.setattr(harness, "load_environment", lambda name: env)

    def pending(memo: str, error: str | None = None) -> dict[str, Any]:
        return {"status": "pending", "memo": memo, "signature": None, "explorer_url": None, "cluster": "devnet", "at": "2026-10-25T00:00:00Z", "error": error or "pending on-chain"}

    async def fake_anchor(memo: str, **_: Any) -> dict[str, Any]:
        return pending(memo, "pending on-chain: agent wallet not funded")

    monkeypatch.setattr(chain, "pending_receipt", pending)
    monkeypatch.setattr(chain, "anchor", fake_anchor)
    monkeypatch.setattr(prereg, "new_salt", lambda: "ab" * 32)

    def fake_prereg(env: Environment, run_id: str, salt: str, *, sealed_at: str) -> dict[str, Any]:
        memo = f"sealed:v1:commit:{run_id}:{'c' * 64}"
        return {"env": env.name, "run_id": run_id, "code_sha256": "0" * 64, "dev_seeds": list(env.spec["dev_seeds"]), "holdout_commitment": "1" * 64, "commitment": "c" * 64, "memo": memo, "sealed_at": sealed_at, "chain": pending(memo)}

    monkeypatch.setattr(prereg, "build_preregistration", fake_prereg)
    monkeypatch.setattr(prereg, "result_digest", lambda record: "d" * 64)
    monkeypatch.setattr(prereg, "reveal_memo", lambda run_id, salt, result, verdict, sel: f"sealed:v1:reveal:{run_id}:{salt}:{result}:{verdict}:{'none' if sel is None else sel}")
    return env


def _run(env_store: RunStore, **kw: Any) -> dict[str, Any]:
    return asyncio.run(harness.run_sealed("toy", store=env_store, **kw))


def test_scripted_run_sequence_reveal_and_round_trip(toy_env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contexts: list[dict[str, Any]] = []
    real = ScriptedProposer(toy_env)

    class Recording:
        name = "scripted"

        async def propose(self, ctx: dict[str, Any]) -> dict[str, Any]:
            contexts.append(json.loads(json.dumps(ctx)))
            return await real.propose(ctx)

    monkeypatch.setattr(harness, "make_proposer", lambda kind, env: Recording())
    updates: list[str] = []

    async def on_update(rec: dict[str, Any]) -> None:
        updates.append(rec["status"])

    rs = RunStore(tmp_path / "runs")
    record = _run(rs, iterations=6, proposer="scripted", on_update=on_update)

    assert record["status"] == "done", record["error"]
    assert [it["decision"] for it in record["iterations"]] == ["BASELINE", "KEEP", "REVERT", "INVALID", "KEEP", "INVALID", "INVALID"]
    assert record["iterations"][0]["proposer"] == "baseline"
    assert record["iterations"][3]["errors"] and record["iterations"][3]["dev"] is None
    assert [it["champion_score"] for it in record["iterations"]] == [10.0, 12.0, 12.0, 12.0, 15.0, 15.0, 15.0]
    assert record["iterations"][4]["diff"] == [{"path": "y", "before": 0, "after": 3}]

    reveal = record["reveal"]
    verdicts = {r["iteration"]: r["verdict"] for r in reveal["results"]}
    assert verdicts == {0: "GENERALISES", 1: "GENERALISES", 4: "OVERFIT"}
    assert reveal["dev_champion_iteration"] == 4 and reveal["verdict"] == "OVERFIT"
    assert reveal["selected_iteration"] == 1
    assert reveal["results"][-1]["per_shift"] == {"base": 15.0, "harsh": 6.0}
    assert any("collapses" in r for r in reveal["results"][-1]["reasons"])
    assert reveal["memo"].endswith(":OVERFIT:1")
    assert reveal["chain"]["status"] == "pending"
    assert record["preregistration"]["chain"]["status"] == "pending"
    assert updates[-1] == "done" and "running" in updates and "revealing" in updates

    loaded = rs.load(record["run_id"])
    assert loaded == json.loads(json.dumps(record))
    assert loaded["updated_at"].endswith("Z")

    assert len(contexts) == 6
    for ctx in contexts:
        assert "holdout" not in ctx
        blob = json.dumps(ctx)
        assert str(SECRET_SEED) not in blob and SECRET_LABEL not in blob and "harsh" not in blob
        assert len(ctx["history"]) <= 6
    assert contexts[0]["iteration"] == 1 and contexts[0]["iterations_total"] == 6

    [summary] = rs.list_summaries()
    assert summary["verdict"] == "OVERFIT" and summary["iterations_done"] == 6


def test_raising_proposer_gives_invalid_not_crash(toy_env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        name = "gemini"

        async def propose(self, ctx: dict[str, Any]) -> dict[str, Any]:
            if ctx["iteration"] == 1:
                raise RuntimeError("model exploded")
            raise ProposerError("bad json")

    monkeypatch.setattr(harness, "make_proposer", lambda kind, env: Boom())
    record = _run(RunStore(tmp_path / "runs"), iterations=2)
    assert record["status"] == "done"
    assert [it["decision"] for it in record["iterations"]] == ["BASELINE", "INVALID", "INVALID"]
    assert "model exploded" in record["iterations"][1]["errors"][0]
    assert record["reveal"]["dev_champion_iteration"] == 0 and record["reveal"]["selected_iteration"] == 0


def test_eval_timeout_is_invalid(toy_env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "EVAL_TIMEOUT_S", 0.1)

    class Slow:
        name = "scripted"

        async def propose(self, ctx: dict[str, Any]) -> dict[str, Any]:
            return {"hypothesis": "slow", "rationale": "", "strategy": "continue", "candidate": {"x": 7, "y": 0}}

    monkeypatch.setattr(harness, "make_proposer", lambda kind, env: Slow())
    record = _run(RunStore(tmp_path / "runs"), iterations=1)
    assert record["status"] == "done"
    assert record["iterations"][1]["decision"] == "INVALID"
    assert "timed out" in record["iterations"][1]["errors"][0]


def test_unexpected_failure_is_saved_as_failed(toy_env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("seal broke")

    monkeypatch.setattr(prereg, "build_preregistration", broken)
    rs = RunStore(tmp_path / "runs")
    record = _run(rs, iterations=1, proposer="scripted")
    assert record["status"] == "failed" and "seal broke" in record["error"]
    assert rs.load(record["run_id"])["status"] == "failed"


def test_iterations_capped(toy_env: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _run(RunStore(tmp_path / "runs"), iterations=99, proposer="scripted", anchor=False)
    assert record["iterations_planned"] == harness.MAX_ITERATIONS
    assert len(record["iterations"]) == harness.MAX_ITERATIONS + 1
    assert record["preregistration"]["chain"]["error"] == "anchoring disabled"


def test_start_background_run_saves_immediately(toy_env: Environment, tmp_path: Path) -> None:
    rs = RunStore(tmp_path / "runs")

    async def go() -> tuple[str, str, dict[str, Any]]:
        run_id = harness.start_background_run("toy", iterations=2, proposer="scripted", store=rs)
        first = rs.load(run_id)["status"]
        await asyncio.gather(*list(harness._background))
        return run_id, first, rs.load(run_id)

    run_id, first, final = asyncio.run(go())
    assert first == "sealing"
    assert final["status"] == "done" and final["run_id"] == run_id
    assert not harness._background


@pytest.mark.parametrize("bad", ["../etc/passwd", "..", "a/b", "", "x" * 65, "a.b", "run id"])
def test_store_rejects_traversal_ids(tmp_path: Path, bad: str) -> None:
    rs = RunStore(tmp_path)
    with pytest.raises(ValueError):
        rs.path(bad)
    with pytest.raises(ValueError):
        rs.load(bad)


def test_store_missing_run_and_empty_listing(tmp_path: Path) -> None:
    rs = RunStore(tmp_path / "nope")
    assert rs.list_summaries() == []
    with pytest.raises(RunNotFoundError):
        rs.load("missing-run")


def test_freeze_and_load_demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sample = json.loads(store.SAMPLE_RUN.read_text())
    monkeypatch.setattr(store, "DEMO_DIR", tmp_path)
    monkeypatch.setattr(store, "SAMPLE_RUN", tmp_path / "sample_run.json")
    (tmp_path / "sample_run.json").write_text(json.dumps(sample))
    assert store.load_demo("toy")["run_id"] == sample["run_id"]
    live = dict(sample, env="toy", mode="live", run_id="frozen-1")
    path = store.freeze_demo(live)
    assert path == tmp_path / "toy_demo.json"
    assert live["mode"] == "live"
    demo = store.load_demo("toy")
    assert demo["mode"] == "cached" and demo["run_id"] == "frozen-1"
    assert path.read_text().startswith("{\n  ")


def test_new_run_id_and_diff() -> None:
    rid = harness.new_run_id("epidemic")
    assert re.fullmatch(RUN_ID_PATTERN, rid) and "-epidemic-" in rid
    assert re.fullmatch(RUN_ID_PATTERN, harness.new_run_id("weird env/../" * 10))
    before = {"a": 1, "b": {"c": 2, "d": 3}, "gone": 1}
    after = {"a": 1, "b": {"c": 5, "e": 4}, "new": {"k": 1}}
    changes = harness.diff_candidates(before, after)
    assert {"path": "b.c", "before": 2, "after": 5} in changes
    assert {"path": "b.d", "before": 3, "after": None} in changes
    assert {"path": "b.e", "before": None, "after": 4} in changes
    assert {"path": "gone", "before": 1, "after": None} in changes
    assert {"path": "new.k", "before": None, "after": 1} in changes
    assert len(changes) == 5


def test_parse_proposal_and_prompt(toy_env: Environment) -> None:
    text = '```json\n{"hypothesis": "raise x\\nmore", "rationale": "r", "strategy": "PIVOT", "candidate": {"x": 1, "y": 2}}\n```'
    p = parse_proposal(text)
    assert p["hypothesis"] == "raise x" and p["strategy"] == "pivot" and p["candidate"] == {"x": 1, "y": 2}
    for bad in ["not json", '{"hypothesis": "h"}', '{"candidate": {}}', "[1, 2]"]:
        with pytest.raises(ProposerError):
            parse_proposal(bad)
    ctx = {
        "env": "toy", "goal": "Maximise x + y.", "metric_label": "Toy value", "handbook": "Toy handbook.",
        "candidate_schema": toy_env.candidate_schema, "champion": {"x": 0, "y": 0},
        "champion_report": {"score": 10.0, "per_seed": [], "breakdown": {"x": 0}, "seeds": [], "shift_id": "base"},
        "history": [{"index": 0, "hypothesis": "baseline", "dev_score": 10.0, "decision": "BASELINE"}],
        "iteration": 1, "iterations_total": 8,
    }
    prompt = GeminiProposer().build_prompt(ctx)  # type: ignore[arg-type]
    assert "Maximise x + y." in prompt and "Toy handbook." in prompt and "1 of 8" in prompt
    with pytest.raises(ValueError):
        make_proposer("nope", toy_env)
    with pytest.raises(ProposerError):
        asyncio.run(ScriptedProposer(toy_env).propose(dict(ctx, iteration=99)))  # type: ignore[arg-type]


def test_gemini_proposer_missing_key_raises_proposer_error(toy_env: Environment, monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.ai.gemini as gemini

    async def no_key(prompt: str, *, json_output: bool = False) -> str:
        raise gemini.MissingGeminiKeyError()

    monkeypatch.setattr(gemini, "ask_gemini", no_key)
    ctx = {
        "env": "toy", "goal": "g", "metric_label": "m", "handbook": "", "candidate_schema": {}, "champion": {},
        "champion_report": {"score": 0.0, "per_seed": [], "breakdown": {}, "seeds": [], "shift_id": "base"},
        "history": [], "iteration": 1, "iterations_total": 1,
    }
    with pytest.raises(ProposerError):
        asyncio.run(GeminiProposer().propose(ctx))  # type: ignore[arg-type]
