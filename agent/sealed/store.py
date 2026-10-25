"""Run records as JSON files on disk (SPEC.md section 8). Owner: W1."""

import os
from pathlib import Path

from .records import RunRecord, RunSummary

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = Path(__file__).resolve().parent / "demo"
SAMPLE_RUN = DEMO_DIR / "sample_run.json"


class RunNotFoundError(LookupError):
    pass


def default_root() -> Path:
    return Path(os.environ.get("SEALED_RUNS_DIR") or REPO_ROOT / ".sealed_runs")


class RunStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_root()

    def path(self, run_id: str) -> Path:
        """<root>/<run_id>.json; raises ValueError when run_id fails RUN_ID_PATTERN."""
        raise NotImplementedError("W1")

    def save(self, record: RunRecord) -> None:
        """Atomic write (temp file in root, then os.replace); also stamps updated_at."""
        raise NotImplementedError("W1")

    def load(self, run_id: str) -> RunRecord:
        """Raises RunNotFoundError for an unknown id."""
        raise NotImplementedError("W1")

    def list_summaries(self) -> list[RunSummary]:
        """Newest first."""
        raise NotImplementedError("W1")


def load_demo(env: str = "epidemic") -> RunRecord:
    """demo/<env>_demo.json when frozen, else demo/sample_run.json."""
    raise NotImplementedError("W1")


def freeze_demo(record: RunRecord) -> Path:
    """Writes a copy with mode="cached" to demo/<env>_demo.json and returns its path."""
    raise NotImplementedError("W1")
