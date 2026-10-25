"""Hash-chained probe ledger and sealed probe schedules for Sealed Verify.

Ledger entry hash = sha256(canonical_json({"v": 1, every field except "hash"})), with
prev = GENESIS for seq 1. The chain head is anchored on Solana devnet as the memo
`cmap:v1:probe:<n>:<chainhead>`, which commits to all n entries.

A probe round is sealed before it runs: commitment = sha256(canonical_json({"schedule", "salt"}))
where schedule = {v, round, merchants: [{id, target_sha256}], probes: [{merchant_id, at, type}]}.
The memo `cmap:v1:sched:<round>:<commitment>` goes on chain before the first probe; schedule and
salt are revealed after the round so anyone can recompute the commitment.
"""

import secrets
from typing import Any

from agent.sealed.hashing import sha256_hex, sha256_json

GENESIS = sha256_json({"v": 1, "product": "sealed-verify", "ledger": "cmap"})
PROBE_MEMO_PREFIX = "cmap:v1:probe"
SCHED_MEMO_PREFIX = "cmap:v1:sched"
ENTRY_FIELDS = ("seq", "prev", "at", "round", "merchant_id", "merchant_name", "probe", "status", "reason", "detail", "verdict", "verdict_by")


def entry_hash(entry: dict[str, Any]) -> str:
    return sha256_json({"v": 1, **{k: entry.get(k) for k in ENTRY_FIELDS}})


def append_entry(entries: list[dict[str, Any]], fields: dict[str, Any]) -> dict[str, Any]:
    """Links `fields` onto the chain (sets seq, prev, hash) and appends it. Returns the new entry."""
    prev = entries[-1]["hash"] if entries else GENESIS
    entry = {k: fields.get(k) for k in ENTRY_FIELDS if k not in ("seq", "prev")}
    entry.update(seq=len(entries) + 1, prev=prev)
    entry["hash"] = entry_hash(entry)
    entries.append(entry)
    return entry


def chain_head(entries: list[dict[str, Any]]) -> str:
    return entries[-1]["hash"] if entries else GENESIS


def verify_chain(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Recomputes every link. {ok, count, head, first_bad_seq, problem}."""
    prev = GENESIS
    for index, entry in enumerate(entries, start=1):
        problem = None
        if entry.get("seq") != index:
            problem = f"entry {index} has seq {entry.get('seq')}"
        elif entry.get("prev") != prev:
            problem = f"entry {index} does not link to entry {index - 1}"
        elif entry_hash(entry) != entry.get("hash"):
            problem = f"entry {index} was changed after it was recorded (hash mismatch)"
        if problem:
            return {"ok": False, "count": len(entries), "head": chain_head(entries), "first_bad_seq": index, "problem": problem}
        prev = entry["hash"]
    return {"ok": True, "count": len(entries), "head": prev, "first_bad_seq": None, "problem": None}


def probe_memo(count: int, head: str) -> str:
    return f"{PROBE_MEMO_PREFIX}:{count}:{head}"


def sched_memo(round_no: int, commitment: str) -> str:
    return f"{SCHED_MEMO_PREFIX}:{round_no}:{commitment}"


def target_digest(target: str | None) -> str:
    return sha256_hex((target or "").encode("utf-8"))


def build_schedule(round_no: int, merchants: list[dict[str, Any]], probes: list[dict[str, Any]]) -> dict[str, Any]:
    """The sealed content of a round: which merchants, which probe type, at what time."""
    return {
        "v": 1,
        "round": round_no,
        "merchants": sorted(({"id": m["id"], "target_sha256": target_digest(m.get("target"))} for m in merchants), key=lambda m: m["id"]),
        "probes": sorted(({"merchant_id": p["merchant_id"], "at": p["at"], "type": p["type"]} for p in probes), key=lambda p: (p["at"], p["merchant_id"])),
    }


def new_salt() -> str:
    return secrets.token_hex(16)


def schedule_commitment(schedule: dict[str, Any], salt: str) -> str:
    return sha256_json({"schedule": schedule, "salt": salt})


def verify_round(round_rec: dict[str, Any]) -> dict[str, Any]:
    """Recomputes one round's commitment from its revealed schedule and salt, and checks its memo."""
    out: dict[str, Any] = {"round": round_rec.get("round"), "kind": round_rec.get("kind"), "commitment": round_rec.get("commitment")}
    schedule, salt = round_rec.get("schedule"), round_rec.get("salt")
    if schedule is None or salt is None:
        return {**out, "ok": None, "status": "sealed (not revealed yet)"}
    recomputed = schedule_commitment(schedule, salt)
    memo_ok = round_rec.get("memo") == sched_memo(int(round_rec.get("round") or 0), recomputed)
    ok = recomputed == round_rec.get("commitment") and memo_ok and schedule.get("round") == round_rec.get("round")
    return {**out, "ok": ok, "recomputed": recomputed, "status": "match" if ok else "MISMATCH: schedule or salt changed after sealing"}


def probes_follow_schedule(round_rec: dict[str, Any], entries: list[dict[str, Any]]) -> bool | None:
    """True when every ledger entry of the round matches a sealed (merchant, type) pair."""
    schedule = round_rec.get("schedule")
    if schedule is None:
        return None
    planned = {(p["merchant_id"], p["type"]) for p in schedule.get("probes", [])}
    done = [(e["merchant_id"], e["probe"]) for e in entries if e.get("round") == round_rec.get("round")]
    return all(pair in planned for pair in done)
