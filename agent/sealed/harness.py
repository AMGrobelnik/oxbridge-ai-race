"""The seal, search, reveal loop (SPEC.md section 5). Owner: W1."""

import asyncio
import copy
import logging
import math
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import chain, gate, prereg
from .environment import Environment, load_environment
from .proposer import ProposerError, make_proposer
from .records import (
    BASE_SHIFT,
    RUN_SCHEMA,
    Candidate,
    ChainReceipt,
    FieldChange,
    HistoryItem,
    HoldoutResult,
    IterationRecord,
    Proposal,
    ProposerContext,
    Reveal,
    RunRecord,
    ScoreReport,
    Shift,
)
from .store import RunNotFoundError, RunStore, utc_now

MAX_ITERATIONS = 12
EVAL_TIMEOUT_S = 10.0
HISTORY_WINDOW = 6

OnUpdate = Callable[[RunRecord], Awaitable[None]]

_background: set[asyncio.Task[RunRecord]] = set()
_log = logging.getLogger(__name__)
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


def new_run_id(env: str) -> str:
    """<UTC %Y%m%dT%H%M%SZ>-<env>-<4 hex>, matching RUN_ID_PATTERN."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_env = _UNSAFE.sub("_", env)[:42] or "env"
    return f"{stamp}-{safe_env}-{secrets.token_hex(2)}"


def diff_candidates(before: Candidate, after: Candidate) -> list[FieldChange]:
    """Leaf-level changes with dotted paths (added, removed and changed keys)."""
    changes: list[FieldChange] = []

    def walk(a: Any, b: Any, prefix: str) -> None:
        if isinstance(a, dict) and isinstance(b, dict):
            for key in list(a) + [k for k in b if k not in a]:
                path = f"{prefix}.{key}" if prefix else str(key)
                if key not in b:
                    walk_one_side(a[key], path, removed=True)
                elif key not in a:
                    walk_one_side(b[key], path, removed=False)
                else:
                    walk(a[key], b[key], path)
        elif a != b:
            changes.append({"path": prefix, "before": copy.deepcopy(a), "after": copy.deepcopy(b)})

    def walk_one_side(value: Any, path: str, *, removed: bool) -> None:
        if isinstance(value, dict) and value:
            for key, sub in value.items():
                walk_one_side(sub, f"{path}.{key}", removed=removed)
        elif removed:
            changes.append({"path": path, "before": copy.deepcopy(value), "after": None})
        else:
            changes.append({"path": path, "before": None, "after": copy.deepcopy(value)})

    walk(before, after, "")
    return changes


def _new_record(env_name: str, run_id: str, proposer: str, iterations: int, env: Environment | None) -> RunRecord:
    now = utc_now()
    return {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "env": env_name,
        "title": env.title if env else "",
        "goal": env.goal if env else "",
        "metric_label": env.spec["metric_label"] if env else "",
        "mode": "live",
        "proposer": proposer,
        "iterations_planned": iterations,
        "status": "sealing",
        "created_at": now,
        "updated_at": now,
        "preregistration": None,
        "iterations": [],
        "reveal": None,
        "error": None,
    }


def _clamp_iterations(iterations: int) -> int:
    return max(0, min(int(iterations), MAX_ITERATIONS))


async def _evaluate(fn: Callable[[], ScoreReport]) -> ScoreReport:
    report = await asyncio.wait_for(asyncio.to_thread(fn), EVAL_TIMEOUT_S)
    score = report["score"]
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
        raise ValueError(f"evaluator returned a non-finite score {score!r}")
    return report


async def _anchor(memo: str, enabled: bool) -> ChainReceipt:
    if enabled:
        return await chain.anchor(memo)
    return chain.pending_receipt(memo, "anchoring disabled")


async def run_sealed(
    env_name: str,
    *,
    iterations: int = 8,
    proposer: str = "gemini",
    anchor: bool = True,
    store: RunStore | None = None,
    run_id: str | None = None,
    on_update: OnUpdate | None = None,
) -> RunRecord:
    """Seal, iterate on dev seeds with keep-or-revert, reveal and gate on the holdout, anchor the
    result. Saves the record after every step and returns the final record (status done or failed)."""
    store = store or RunStore()
    iterations = _clamp_iterations(iterations)
    run_id = run_id or new_run_id(env_name)
    store.path(run_id)  # ValueError on a bad id before anything else happens
    record = _new_record(env_name, run_id, proposer, iterations, None)
    try:
        record["created_at"] = store.load(run_id)["created_at"]
    except (RunNotFoundError, KeyError, ValueError):
        pass

    async def checkpoint() -> None:
        store.save(record)
        if on_update is not None:
            try:
                await on_update(record)
            except Exception:  # noqa: BLE001 - a broken listener must not kill the run
                _log.exception("sealed on_update callback failed")

    try:
        env = load_environment(env_name)
        record.update({"title": env.title, "goal": env.goal, "metric_label": env.spec["metric_label"]})
        prop = make_proposer(proposer, env)
        await checkpoint()

        # 1. Seal.
        salt = prereg.new_salt()
        registration = prereg.build_preregistration(env, run_id, salt, sealed_at=utc_now())
        registration["chain"] = await _anchor(registration["memo"], anchor)
        record["preregistration"] = registration
        await checkpoint()

        # 2. Baseline.
        record["status"] = "running"
        started = time.monotonic()
        champion = env.baseline()
        champion_report = await _evaluate(lambda: env.dev_report(champion))
        champion_index = 0
        record["iterations"].append(
            {
                "index": 0,
                "proposer": "baseline",
                "hypothesis": "Naive baseline policy",
                "rationale": "The starting point every experiment must beat on the dev seeds.",
                "strategy": "continue",
                "candidate": copy.deepcopy(champion),
                "diff": [],
                "dev": champion_report,
                "decision": "BASELINE",
                "errors": [],
                "champion_score": champion_report["score"],
                "duration_s": round(time.monotonic() - started, 3),
                "at": utc_now(),
            }
        )
        await checkpoint()

        # 3. Search on dev seeds only.
        for i in range(1, iterations + 1):
            started = time.monotonic()
            history: list[HistoryItem] = [
                {
                    "index": it["index"],
                    "hypothesis": it["hypothesis"],
                    "dev_score": it["dev"]["score"] if it["dev"] else None,
                    "decision": it["decision"],
                }
                for it in record["iterations"][-HISTORY_WINDOW:]
            ]
            ctx: ProposerContext = {
                "env": env.name,
                "goal": env.goal,
                "metric_label": env.spec["metric_label"],
                "handbook": env.handbook(),
                "candidate_schema": copy.deepcopy(env.candidate_schema),
                "champion": copy.deepcopy(champion),
                "champion_report": copy.deepcopy(champion_report),
                "history": history,
                "iteration": i,
                "iterations_total": iterations,
            }
            proposal: Proposal | None = None
            errors: list[str] = []
            report: ScoreReport | None = None
            try:
                proposal = await prop.propose(ctx)
                candidate = proposal["candidate"]
                if not isinstance(candidate, dict):
                    raise ProposerError("candidate is not a JSON object")
                errors = env.validate(candidate)
                if not errors:
                    report = await _evaluate(lambda c=candidate: env.dev_report(c))
            except ProposerError as exc:
                errors = [f"proposer: {exc}"]
            except TimeoutError:
                errors = [f"evaluation timed out after {EVAL_TIMEOUT_S:.0f} s"]
            except Exception as exc:  # noqa: BLE001 - one bad candidate never stops the loop
                errors = [f"{type(exc).__name__}: {exc}"]

            candidate = proposal["candidate"] if proposal and isinstance(proposal.get("candidate"), dict) else {}
            if errors or report is None:
                decision = "INVALID"
                report = None
                errors = errors or ["no score produced"]
            elif report["score"] > champion_report["score"]:
                decision = "KEEP"
            else:
                decision = "REVERT"
            entry: IterationRecord = {
                "index": i,
                "proposer": prop.name,
                "hypothesis": str(proposal["hypothesis"]) if proposal else "(no proposal)",
                "rationale": str(proposal.get("rationale", "")) if proposal else "",
                "strategy": str(proposal.get("strategy", "continue")) if proposal else "continue",
                "candidate": copy.deepcopy(candidate),
                "diff": diff_candidates(champion, candidate) if candidate else [],
                "dev": report,
                "decision": decision,  # type: ignore[typeddict-item]
                "errors": errors,
                "champion_score": champion_report["score"],
                "duration_s": 0.0,
                "at": utc_now(),
            }
            if decision == "KEEP" and report is not None:
                champion, champion_report, champion_index = copy.deepcopy(candidate), report, i
                entry["champion_score"] = report["score"]
            entry["duration_s"] = round(time.monotonic() - started, 3)
            record["iterations"].append(entry)
            await checkpoint()

        # 4. Reveal.
        record["status"] = "revealing"
        await checkpoint()
        holdout = env.load_holdout()
        shifts: list[Shift] = [BASE_SHIFT, *[s for s in holdout["shifts"] if s["id"] != BASE_SHIFT["id"]]]
        shift_labels = {s["id"]: s["label"] for s in shifts}
        seeds = list(holdout["seeds"])
        finalists = [it for it in record["iterations"] if it["decision"] in ("BASELINE", "KEEP")]

        per_shift_by_iter: dict[int, dict[str, float]] = {}
        for it in finalists:
            scores: dict[str, float] = {}
            for shift in shifts:
                rep = await _evaluate(lambda c=it["candidate"], s=shift: env.evaluate(c, seeds=seeds, shift=s))
                scores[shift["id"]] = float(rep["score"])
            per_shift_by_iter[it["index"]] = scores

        baseline_per_shift = per_shift_by_iter[0]
        results: list[HoldoutResult] = []
        for it in finalists:
            dev_score = float(it["dev"]["score"]) if it["dev"] else 0.0
            outcome = gate.judge(dev_score, per_shift_by_iter[it["index"]], baseline_per_shift, env.spec["gate"], shift_labels)
            results.append(
                {
                    "iteration": it["index"],
                    "dev_score": dev_score,
                    "per_shift": per_shift_by_iter[it["index"]],
                    "shift_labels": shift_labels,
                    "holdout_mean": outcome.holdout_mean,
                    "worst_shift_id": outcome.worst_shift_id,
                    "worst_score": outcome.worst_score,
                    "relative_gap": outcome.relative_gap,
                    "verdict": outcome.verdict,
                    "reasons": outcome.reasons,
                }
            )
        generalising = [r for r in results if r["verdict"] == "GENERALISES"]
        selected = max(generalising, key=lambda r: (r["dev_score"], r["iteration"]))["iteration"] if generalising else None
        champion_result = next(r for r in results if r["iteration"] == champion_index)
        reveal: Reveal = {
            "salt": salt,
            "holdout": holdout,
            "revealed_at": utc_now(),
            "results": results,
            "dev_champion_iteration": champion_index,
            "selected_iteration": selected,
            "verdict": champion_result["verdict"],
            "result_sha256": "",
            "memo": "",
            "chain": {"status": "pending", "memo": "", "signature": None, "explorer_url": None, "cluster": chain.CLUSTER, "at": utc_now(), "error": "not anchored yet"},
        }
        record["reveal"] = reveal
        reveal["result_sha256"] = prereg.result_digest(record)
        reveal["memo"] = prereg.reveal_memo(run_id, salt, reveal["result_sha256"], reveal["verdict"], selected)
        reveal["chain"] = await _anchor(reveal["memo"], anchor)

        # 5. Done.
        record["status"] = "done"
        await checkpoint()
    except Exception as exc:  # noqa: BLE001 - the run must end in a saved, inspectable state
        _log.exception("sealed run %s failed", run_id)
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        try:
            await checkpoint()
        except Exception:  # noqa: BLE001
            _log.exception("could not save failed run %s", run_id)
    return record


def start_background_run(env_name: str, *, iterations: int = 8, proposer: str = "gemini", anchor: bool = True, store: RunStore | None = None) -> str:
    """Schedules run_sealed on the running event loop and returns the run_id at once."""
    loop = asyncio.get_running_loop()
    store = store or RunStore()
    env = load_environment(env_name)  # UnknownEnvironmentError surfaces to the caller at once
    if proposer not in ("gemini", "scripted"):
        raise ValueError(f"Unknown proposer {proposer!r}: expected 'gemini' or 'scripted'")
    iterations = _clamp_iterations(iterations)
    run_id = new_run_id(env_name)
    store.save(_new_record(env_name, run_id, proposer, iterations, env))
    task = loop.create_task(run_sealed(env_name, iterations=iterations, proposer=proposer, anchor=anchor, store=store, run_id=run_id))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return run_id
