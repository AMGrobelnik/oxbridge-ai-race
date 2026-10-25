"""Run records as JSON files on disk (SPEC.md section 8). Owner: W1."""

import copy
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .records import RUN_ID_PATTERN, RunRecord, RunSummary

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = Path(__file__).resolve().parent / "demo"
SAMPLE_RUN = DEMO_DIR / "sample_run.json"

_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


class RunNotFoundError(LookupError):
    pass


def default_root() -> Path:
    return Path(os.environ.get("SEALED_RUNS_DIR") or REPO_ROOT / ".sealed_runs")


def utc_now() -> str:
    """ISO-8601 UTC with a trailing Z, second precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_json(path: Path, value: Any, *, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=indent, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()

    def path(self, run_id: str) -> Path:
        """<root>/<run_id>.json; raises ValueError when run_id fails RUN_ID_PATTERN."""
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"Invalid run id {run_id!r}: expected {RUN_ID_PATTERN}")
        return self.root / f"{run_id}.json"

    def save(self, record: RunRecord) -> None:
        """Atomic write (temp file in root, then os.replace); also stamps updated_at."""
        target = self.path(record["run_id"])
        record["updated_at"] = utc_now()
        _atomic_write_json(target, record)

    def load(self, run_id: str) -> RunRecord:
        """Raises RunNotFoundError for an unknown id."""
        target = self.path(run_id)
        try:
            return cast(RunRecord, json.loads(target.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise RunNotFoundError(f"Unknown run {run_id!r}") from exc

    def list_summaries(self) -> list[RunSummary]:
        """Newest first."""
        if not self.root.is_dir():
            return []
        summaries: list[RunSummary] = []
        for file in self.root.glob("*.json"):
            try:
                rec = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(rec, dict) or "run_id" not in rec:
                continue
            reveal = rec.get("reveal") or None
            summaries.append(
                {
                    "run_id": rec["run_id"],
                    "env": rec.get("env", ""),
                    "status": rec.get("status", "failed"),
                    "mode": rec.get("mode", "live"),
                    "created_at": rec.get("created_at", ""),
                    "iterations_done": sum(1 for it in rec.get("iterations", []) if it.get("decision") != "BASELINE"),
                    "verdict": reveal.get("verdict") if isinstance(reveal, dict) else None,
                }
            )
        summaries.sort(key=lambda s: (s["created_at"], s["run_id"]), reverse=True)
        return summaries


def demo_path(env: str) -> Path:
    if not _RUN_ID_RE.fullmatch(env):
        raise ValueError(f"Invalid environment name {env!r}")
    return DEMO_DIR / f"{env}_demo.json"


def load_demo(env: str = "epidemic") -> RunRecord:
    """demo/<env>_demo.json when frozen, else demo/sample_run.json."""
    frozen = demo_path(env)
    source = frozen if frozen.exists() else SAMPLE_RUN
    return cast(RunRecord, json.loads(source.read_text(encoding="utf-8")))


def freeze_demo(record: RunRecord) -> Path:
    """Writes a copy with mode="cached" to demo/<env>_demo.json and returns its path."""
    frozen = cast(RunRecord, copy.deepcopy(record))
    frozen["mode"] = "cached"
    target = demo_path(frozen["env"])
    _atomic_write_json(target, frozen, indent=2)
    return target
