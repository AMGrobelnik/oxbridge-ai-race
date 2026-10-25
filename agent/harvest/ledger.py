"""Hash-chained Harvest bet ledger in SQLite with batched memo anchors (SPEC.md section 7). Owner: market-engine.

Mirrors agent/challenge/ledger.py; anchors through agent.sealed.chain.anchor (pending while unfunded).
"""

import os
from pathlib import Path
from typing import Any

from agent.sealed.hashing import sha256_json

from .types import LedgerEntry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENESIS = sha256_json({"v": 1, "harvest": "ledger"})
MEMO_PREFIX = "harvest:v1:ledger"
USERS_MEMO_PREFIX = "harvest:v1:users"


def db_path() -> Path:
    return Path(os.environ.get("HARVEST_DB") or PROJECT_ROOT / ".harvest" / "harvest.sqlite3")


def entry_hash(entry: dict[str, Any]) -> str:
    keys = ("seq", "prev", "kind", "trader", "payload", "at")
    return sha256_json({"v": 1, **{k: entry[k] for k in keys}})


def memo_for(count: int, head: str) -> str:
    return f"{MEMO_PREFIX}:{count}:{head}"


def users_memo(n: int, head: str) -> str:
    return f"{USERS_MEMO_PREFIX}:{n}:{head[:16]}"


def append(*, kind: str, trader: str, payload: dict[str, Any]) -> LedgerEntry:
    raise NotImplementedError


def all_entries(limit: int = 100_000) -> list[LedgerEntry]:
    raise NotImplementedError


def anchors() -> list[dict[str, Any]]:
    raise NotImplementedError


def verify() -> dict[str, Any]:
    """{"ok", "count", "head", "broken_at", "anchors_ok"}."""
    raise NotImplementedError
