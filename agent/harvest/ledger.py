"""Hash-chained Harvest bet ledger in SQLite with batched memo anchors (SPEC.md section 7). Owner: market-engine.

Mirrors agent/challenge/ledger.py; anchors through agent.sealed.chain.anchor (pending while unfunded).

entry hash = sha256_json({"v": 1, "seq", "prev", "kind", "trader", "payload", "at"}), prev = GENESIS for seq 1.
Anchor memos: harvest:v1:ledger:<count>:<head> every HARVEST_ANCHOR_EVERY entries (default 10) or
HARVEST_ANCHOR_INTERVAL_S seconds, and harvest:v1:users:<n>:<head[:16]> when users joined since the last one.
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.sealed import chain
from agent.sealed.hashing import sha256_json

from .types import LedgerEntry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENESIS = sha256_json({"v": 1, "harvest": "ledger"})
MEMO_PREFIX = "harvest:v1:ledger"
USERS_MEMO_PREFIX = "harvest:v1:users"
KINDS = ("join", "agent", "bet", "resolve", "fund", "forecast")

logger = logging.getLogger("agent")
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  seq INTEGER PRIMARY KEY,
  prev TEXT NOT NULL,
  kind TEXT NOT NULL,
  trader TEXT NOT NULL,
  payload TEXT NOT NULL,
  at TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anchors (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  count INTEGER NOT NULL,
  head TEXT NOT NULL,
  memo TEXT NOT NULL,
  status TEXT NOT NULL,
  signature TEXT,
  explorer_url TEXT,
  error TEXT,
  at TEXT NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("HARVEST_DB") or PROJECT_ROOT / ".harvest" / "harvest.sqlite3")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def entry_hash(entry: dict[str, Any]) -> str:
    keys = ("seq", "prev", "kind", "trader", "payload", "at")
    return sha256_json({"v": 1, **{k: entry[k] for k in keys}})


def memo_for(count: int, head: str) -> str:
    return f"{MEMO_PREFIX}:{count}:{head}"


def users_memo(n: int, head: str) -> str:
    return f"{USERS_MEMO_PREFIX}:{n}:{head[:16]}"


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def _row(row: sqlite3.Row) -> LedgerEntry:
    return {
        "seq": row["seq"],
        "prev": row["prev"],
        "kind": row["kind"],
        "trader": row["trader"],
        "payload": json.loads(row["payload"]),
        "at": row["at"],
        "hash": row["hash"],
    }


def append(*, kind: str, trader: str, payload: dict[str, Any]) -> LedgerEntry:
    """Adds one entry at the chain head and returns it as stored (with seq, prev, hash)."""
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind {kind!r}")
    payload = json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))
    with _write_lock, connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute("SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
            entry: dict[str, Any] = {
                "seq": (last["seq"] + 1) if last else 1,
                "prev": last["hash"] if last else GENESIS,
                "kind": kind,
                "trader": trader,
                "payload": payload,
                "at": now_iso(),
            }
            entry["hash"] = entry_hash(entry)
            conn.execute(
                "INSERT INTO entries (seq, prev, kind, trader, payload, at, hash) VALUES (?,?,?,?,?,?,?)",
                (entry["seq"], entry["prev"], kind, trader, json.dumps(payload, sort_keys=True), entry["at"], entry["hash"]),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return entry  # type: ignore[return-value]


def all_entries(limit: int = 100_000) -> list[LedgerEntry]:
    with connect() as conn:
        return [_row(r) for r in conn.execute("SELECT * FROM entries ORDER BY seq ASC LIMIT ?", (limit,))]


def recent_entries(limit: int = 50) -> list[LedgerEntry]:
    with connect() as conn:
        rows = [_row(r) for r in conn.execute("SELECT * FROM entries ORDER BY seq DESC LIMIT ?", (limit,))]
    return rows[::-1]


def head() -> tuple[int, str]:
    with connect() as conn:
        last = conn.execute("SELECT seq, hash FROM entries ORDER BY seq DESC LIMIT 1").fetchone()
    return (last["seq"], last["hash"]) if last else (0, GENESIS)


def user_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(DISTINCT trader) AS n FROM entries WHERE kind = 'join'").fetchone()["n"] or 0)


def _anchor_public(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in ("kind", "count", "head", "memo", "status", "signature", "explorer_url", "error", "at")}


def anchors() -> list[dict[str, Any]]:
    with connect() as conn:
        return [_anchor_public(r) for r in conn.execute("SELECT * FROM anchors ORDER BY id ASC")]


def last_anchor(kind: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM anchors WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
    return _anchor_public(row) if row else None


def record_anchor(kind: str, count: int, head_hash: str, receipt: dict[str, Any]) -> dict[str, Any]:
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO anchors (kind, count, head, memo, status, signature, explorer_url, error, at) VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, count, head_hash, receipt["memo"], receipt["status"], receipt.get("signature"), receipt.get("explorer_url"), receipt.get("error"), now_iso()),
        )
    return {"kind": kind, "count": count, "head": head_hash, **{k: receipt.get(k) for k in ("memo", "status", "signature", "explorer_url", "error")}}


# ---------------------------------------------------------------- verify

def verify() -> dict[str, Any]:
    """Recomputes every hash and prev link from GENESIS and checks each anchor memo against the
    recomputed head at its count. {"ok", "count", "head", "broken_at", "anchors_ok", ...}."""
    entries = all_entries()
    prev, heads, broken_at, reasons = GENESIS, {0: GENESIS}, None, []
    for i, e in enumerate(entries, start=1):
        recomputed = entry_hash({**e, "prev": prev})
        if e["seq"] != i or e["prev"] != prev or recomputed != e["hash"]:
            broken_at = e["seq"]
            reasons.append(f"chain breaks at entry {e['seq']}")
            break
        heads[e["seq"]] = recomputed
        prev = recomputed
    checks = []
    for a in anchors():
        expected = heads.get(a["count"])
        if a["kind"] == "ledger":
            ok = expected is not None and a["head"] == expected and a["memo"] == memo_for(a["count"], expected)
        elif a["kind"] == "users":
            ok = expected is not None and a["head"] == expected and a["memo"].startswith(USERS_MEMO_PREFIX + ":") and a["memo"].endswith(":" + expected[:16])
        else:
            ok = True  # fund memos commit to the allocation, not the chain
        checks.append({**a, "match": ok})
        if not ok:
            reasons.append(f"{a['kind']} anchor for count {a['count']} does not match the chain")
    anchors_ok = all(c["match"] for c in checks)
    return {
        "ok": broken_at is None and anchors_ok,
        "count": len(entries),
        "genesis": GENESIS,
        "head": prev if broken_at is None else None,
        "head_recorded": entries[-1]["hash"] if entries else GENESIS,
        "broken_at": broken_at,
        "anchors_ok": anchors_ok,
        "anchors": checks,
        "reasons": reasons,
        "hash_rule": 'sha256_json({"v":1,"seq","prev","kind","trader","payload","at"})',
    }


# ---------------------------------------------------------------- batch anchoring

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def anchoring_enabled() -> bool:
    return os.environ.get("HARVEST_ANCHOR", "1").strip().lower() not in ("0", "false", "no", "off")


class Anchorer:
    """Posts the chain head every HARVEST_ANCHOR_EVERY entries (default 10) or every
    HARVEST_ANCHOR_INTERVAL_S seconds (default 120) when there is anything new, and the user
    count whenever users joined since the last users memo. Runs in the background only."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_attempt = time.monotonic()
        self._ticker: asyncio.Task[None] | None = None

    @property
    def every(self) -> int:
        return max(_env_int("HARVEST_ANCHOR_EVERY", 10), 1)

    @property
    def interval_s(self) -> int:
        return max(_env_int("HARVEST_ANCHOR_INTERVAL_S", 120), 10)

    def due(self, now: float | None = None) -> dict[str, Any]:
        count, head_hash = head()
        tried = (last_anchor("ledger") or {}).get("count") or 0
        users_tried = last_anchor("users")
        users_n = user_count()
        now = time.monotonic() if now is None else now
        out: dict[str, Any] = {"count": count, "head": head_hash}
        if count > tried and (count - tried >= self.every or now - self._last_attempt >= self.interval_s):
            out["ledger"] = memo_for(count, head_hash)
        last_n = int(users_tried["memo"].split(":")[3]) if users_tried else 0
        if users_n > last_n:
            out["users"] = users_memo(users_n, head_hash)
        return out

    async def maybe_anchor(self) -> list[dict[str, Any]]:
        if not anchoring_enabled() or self._lock.locked():
            return []
        done: list[dict[str, Any]] = []
        async with self._lock:
            batch = await asyncio.to_thread(self.due)
            for kind in ("ledger", "users"):
                memo = batch.get(kind)
                if not memo:
                    continue
                self._last_attempt = time.monotonic()
                receipt = await chain.anchor(memo)
                logger.info("harvest %s anchor count=%s status=%s", kind, batch["count"], receipt["status"])
                done.append(await asyncio.to_thread(record_anchor, kind, batch["count"], batch["head"], dict(receipt)))
        return done

    def ensure_ticker(self) -> None:
        """Starts the background check once per event loop (lazily, from a request)."""
        if self._ticker is not None and not self._ticker.done():
            return
        try:
            self._ticker = asyncio.get_running_loop().create_task(self._tick())
        except RuntimeError:
            self._ticker = None

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.maybe_anchor()
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the ticker die on one bad round
                logger.exception("harvest anchor tick failed")


anchorer = Anchorer()
