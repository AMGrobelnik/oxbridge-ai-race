"""The seal, search, reveal loop (SPEC.md section 5). Owner: W1."""

import asyncio
from collections.abc import Awaitable, Callable

from .records import Candidate, FieldChange, RunRecord
from .store import RunStore

MAX_ITERATIONS = 12
EVAL_TIMEOUT_S = 10.0

OnUpdate = Callable[[RunRecord], Awaitable[None]]

_background: set[asyncio.Task[RunRecord]] = set()


def new_run_id(env: str) -> str:
    """<UTC %Y%m%dT%H%M%SZ>-<env>-<4 hex>, matching RUN_ID_PATTERN."""
    raise NotImplementedError("W1")


def diff_candidates(before: Candidate, after: Candidate) -> list[FieldChange]:
    """Leaf-level changes with dotted paths (added, removed and changed keys)."""
    raise NotImplementedError("W1")


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
    raise NotImplementedError("W1")


def start_background_run(env_name: str, *, iterations: int = 8, proposer: str = "gemini", anchor: bool = True, store: RunStore | None = None) -> str:
    """Schedules run_sealed on the running event loop and returns the run_id at once."""
    raise NotImplementedError("W1")
