"""The Beat the Seal ledger: every submission is a link in a SHA-256 hash chain, and the chain
head is anchored on Solana devnet in batches as `sealed:v1:challenge:<count>:<chainhead>`.

entry hash = sha256(canonical_json({"v": 1, "seq", "prev", "nick_sha256", "params",
"dev_score", "sealed_score", "verdict", "at"})), with prev = GENESIS for seq 1. A memo for
count N commits to all N submissions, so a pending batch is superseded by the next one.

Storage is one SQLite file (CHALLENGE_DB, default <repo>/.challenge/challenge.sqlite3, gitignored).
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

from agent.config import PROJECT_ROOT
from agent.sealed import chain
from agent.sealed.hashing import sha256_json

GENESIS = sha256_json({"v": 1, "challenge": "beat-the-seal", "env": "strategy"})
MEMO_PREFIX = "sealed:v1:challenge"
LEADERBOARD_SIZE = 20
WALL_SIZE = 10

logger = logging.getLogger("agent")
_write_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
  seq INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  nickname TEXT NOT NULL,
  nick_sha256 TEXT NOT NULL,
  params TEXT NOT NULL,
  dev_score REAL NOT NULL,
  sealed_score REAL NOT NULL,
  verdict TEXT NOT NULL,
  relative_gap REAL NOT NULL,
  drop_score REAL NOT NULL,
  per_shift TEXT NOT NULL,
  prev TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS submissions_nick ON submissions(nick_sha256);
CREATE TABLE IF NOT EXISTS anchors (
  id INTEGER PRIMARY KEY,
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
    return Path(os.environ.get("CHALLENGE_DB") or PROJECT_ROOT / ".challenge" / "challenge.sqlite3")


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def entry_hash(entry: dict[str, Any]) -> str:
    keys = ("seq", "prev", "nick_sha256", "params", "dev_score", "sealed_score", "verdict", "at")
    return sha256_json({"v": 1, **{k: entry[k] for k in keys}})


def memo_for(count: int, head: str) -> str:
    return f"{MEMO_PREFIX}:{count}:{head}"


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


def _public(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "at": row["at"],
        "nickname": row["nickname"],
        "nick_sha256": row["nick_sha256"],
        "params": json.loads(row["params"]),
        "dev_score": row["dev_score"],
        "sealed_score": row["sealed_score"],
        "verdict": row["verdict"],
        "relative_gap": row["relative_gap"],
        "drop": row["drop_score"],
        "per_shift": json.loads(row["per_shift"]),
        "prev": row["prev"],
        "hash": row["hash"],
    }


def append(*, nickname: str, nick_sha256: str, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Adds one submission at the chain head and returns it as stored (with seq, prev, hash)."""
    with _write_lock, connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            last = conn.execute("SELECT seq, hash FROM submissions ORDER BY seq DESC LIMIT 1").fetchone()
            entry = {
                "seq": (last["seq"] + 1) if last else 1,
                "prev": last["hash"] if last else GENESIS,
                "nick_sha256": nick_sha256,
                "params": params,
                "dev_score": float(result["dev_score"]),
                "sealed_score": float(result["sealed_score"]),
                "verdict": result["verdict"],
                "at": now_iso(),
            }
            entry["hash"] = entry_hash(entry)
            conn.execute(
                "INSERT INTO submissions (seq, at, nickname, nick_sha256, params, dev_score, sealed_score, verdict,"
                " relative_gap, drop_score, per_shift, prev, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    entry["seq"], entry["at"], nickname, nick_sha256, json.dumps(params, sort_keys=True),
                    entry["dev_score"], entry["sealed_score"], entry["verdict"], float(result["relative_gap"]),
                    float(result["drop"]), json.dumps(result["per_shift"], sort_keys=True), entry["prev"], entry["hash"],
                ),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return {**entry, "nickname": nickname}


def rank_of(seq: int) -> int | None:
    """1-based leaderboard rank of a submission, or None if it is not its player's best GENERALISES entry."""
    for i, row in enumerate(leaderboard(limit=10_000), start=1):
        if row["seq"] == seq:
            return i
    return None


def leaderboard(limit: int = LEADERBOARD_SIZE) -> list[dict[str, Any]]:
    """Each player's best GENERALISES entry, ranked by sealed score."""
    sql = """
      SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY nick_sha256 ORDER BY sealed_score DESC, seq ASC) AS rn
        FROM submissions WHERE verdict = 'GENERALISES'
      ) WHERE rn = 1 ORDER BY sealed_score DESC, seq ASC LIMIT ?"""
    with connect() as conn:
        return [_public(r) for r in conn.execute(sql, (limit,))]


def overfit_wall(limit: int = WALL_SIZE) -> list[dict[str, Any]]:
    """Each player's biggest dev -> sealed collapse among OVERFIT entries, biggest first."""
    sql = """
      SELECT * FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY nick_sha256 ORDER BY drop_score DESC, seq ASC) AS rn
        FROM submissions WHERE verdict = 'OVERFIT'
      ) WHERE rn = 1 ORDER BY drop_score DESC, seq ASC LIMIT ?"""
    with connect() as conn:
        return [_public(r) for r in conn.execute(sql, (limit,))]


def recent(limit: int = 8) -> list[dict[str, Any]]:
    with connect() as conn:
        return [_public(r) for r in conn.execute("SELECT * FROM submissions ORDER BY seq DESC LIMIT ?", (limit,))]


def stats() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS sealed, COUNT(DISTINCT nick_sha256) AS players,"
            " SUM(verdict = 'GENERALISES') AS generalises, SUM(verdict = 'OVERFIT') AS overfit FROM submissions"
        ).fetchone()
        head = conn.execute("SELECT hash FROM submissions ORDER BY seq DESC LIMIT 1").fetchone()
        anchor = conn.execute("SELECT * FROM anchors ORDER BY id DESC LIMIT 1").fetchone()
        confirmed = conn.execute("SELECT * FROM anchors WHERE status = 'confirmed' ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "players": row["players"] or 0,
        "sealed": row["sealed"] or 0,
        "generalises": row["generalises"] or 0,
        "overfit": row["overfit"] or 0,
        "chain_head": head["hash"] if head else GENESIS,
        "genesis": GENESIS,
        "last_anchor": _anchor_public(anchor) if anchor else None,
        "last_confirmed_anchor": _anchor_public(confirmed) if confirmed else None,
    }


def all_entries(limit: int = 100_000) -> list[dict[str, Any]]:
    with connect() as conn:
        return [_public(r) for r in conn.execute("SELECT * FROM submissions ORDER BY seq ASC LIMIT ?", (limit,))]


def anchors() -> list[dict[str, Any]]:
    with connect() as conn:
        return [_anchor_public(r) for r in conn.execute("SELECT * FROM anchors ORDER BY id ASC")]


def _anchor_public(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in ("count", "head", "memo", "status", "signature", "explorer_url", "error", "at")}


def record_anchor(count: int, head: str, receipt: dict[str, Any]) -> dict[str, Any]:
    with _write_lock, connect() as conn:
        conn.execute(
            "INSERT INTO anchors (count, head, memo, status, signature, explorer_url, error, at) VALUES (?,?,?,?,?,?,?,?)",
            (count, head, receipt["memo"], receipt["status"], receipt.get("signature"), receipt.get("explorer_url"), receipt.get("error"), now_iso()),
        )
    return {"count": count, "head": head, **{k: receipt.get(k) for k in ("memo", "status", "signature", "explorer_url", "error")}}


def unanchored() -> tuple[int, str, int] | None:
    """(count, head, last attempted count) when submissions exist beyond the last anchor attempt."""
    with connect() as conn:
        last = conn.execute("SELECT seq, hash FROM submissions ORDER BY seq DESC LIMIT 1").fetchone()
        tried = conn.execute("SELECT MAX(count) AS n FROM anchors").fetchone()["n"] or 0
    if not last or last["seq"] <= tried:
        return None
    return last["seq"], last["hash"], tried


# ---------------------------------------------------------------- verify

def verify(*, rescore_last: int = 0) -> dict[str, Any]:
    """Recomputes every link from GENESIS, checks each anchor memo against the recomputed head at
    its count, and optionally re-scores the last `rescore_last` entries from their params."""
    entries = all_entries()
    prev, heads, broken_at, reasons = GENESIS, {}, None, []
    for i, e in enumerate(entries, start=1):
        recomputed = entry_hash({**e, "prev": prev})
        if e["seq"] != i or e["prev"] != prev or recomputed != e["hash"]:
            broken_at = e["seq"]
            reasons.append(f"chain breaks at submission {e['seq']}")
            break
        heads[e["seq"]] = recomputed
        prev = recomputed
    anchor_checks = []
    for a in anchors():
        expected = heads.get(a["count"])
        ok = expected is not None and a["head"] == expected and a["memo"] == memo_for(a["count"], expected)
        anchor_checks.append({**a, "match": ok})
        if not ok:
            reasons.append(f"anchor for count {a['count']} does not match the chain")
    rescored = []
    if rescore_last > 0 and broken_at is None:
        from . import game

        for e in entries[-rescore_last:]:
            result = game.reveal(e["params"])
            ok = abs(result["dev_score"] - e["dev_score"]) < 1e-6 and abs(result["sealed_score"] - e["sealed_score"]) < 1e-6 and result["verdict"] == e["verdict"]
            rescored.append({"seq": e["seq"], "match": ok})
            if not ok:
                reasons.append(f"submission {e['seq']} does not re-score to its recorded result")
    return {
        "ok": not reasons,
        "length": len(entries),
        "genesis": GENESIS,
        "head_recomputed": prev if broken_at is None else None,
        "head_recorded": entries[-1]["hash"] if entries else GENESIS,
        "broken_at": broken_at,
        "anchors": anchor_checks,
        "rescored": rescored,
        "reasons": reasons,
        "hash_rule": 'sha256(canonical_json({"v":1,"seq","prev","nick_sha256","params","dev_score","sealed_score","verdict","at"}))',
    }


# ---------------------------------------------------------------- batch anchoring

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def anchoring_enabled() -> bool:
    return os.environ.get("CHALLENGE_ANCHOR", "1").strip() not in ("0", "false", "no", "off")


class Anchorer:
    """Posts the chain head every CHALLENGE_ANCHOR_EVERY submissions (default 10) or every
    CHALLENGE_ANCHOR_INTERVAL_S seconds (default 120) when there is anything new."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_attempt = time.monotonic()
        self._ticker: asyncio.Task[None] | None = None

    @property
    def every(self) -> int:
        return max(_env_int("CHALLENGE_ANCHOR_EVERY", 10), 1)

    @property
    def interval_s(self) -> int:
        return max(_env_int("CHALLENGE_ANCHOR_INTERVAL_S", 120), 10)

    def due(self, now: float | None = None) -> tuple[int, str] | None:
        pending = unanchored()
        if pending is None:
            return None
        count, head, tried = pending
        now = time.monotonic() if now is None else now
        if count - tried >= self.every or now - self._last_attempt >= self.interval_s:
            return count, head
        return None

    async def maybe_anchor(self) -> dict[str, Any] | None:
        if not anchoring_enabled() or self._lock.locked():
            return None
        async with self._lock:
            batch = self.due()
            if batch is None:
                return None
            self._last_attempt = time.monotonic()
            count, head = batch
            receipt = await chain.anchor(memo_for(count, head))
            logger.info("challenge anchor count=%s status=%s", count, receipt["status"])
            return record_anchor(count, head, dict(receipt))

    def ensure_ticker(self) -> None:
        """Starts the 2-minute background check once per event loop (lazily, from a request)."""
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
                logger.exception("challenge anchor tick failed")


anchorer = Anchorer()
