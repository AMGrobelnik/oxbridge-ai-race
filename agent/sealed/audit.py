"""Per-round on-chain audit log: a hash chain over the iterations (SPEC.md section 13).

Each round's hash covers that iteration's proposal text, candidate, dev score and decision plus
the previous round's hash; the chain starts at the preregistration commitment, so the whole log
is bound to the sealed test. Every round is posted as a compact devnet memo
``sealed:v1:round:<run_id>:<index>:<sha256>``. Pure functions except anchor_round.
"""

import asyncio
import re
from typing import Any

from . import chain
from .hashing import sha256_json
from .records import AuditChain, AuditReport, AuditRound, AuditRoundCheck, ChainReceipt, IterationRecord, RunRecord

AUDIT_VERSION = 1
ZERO_HASH = "0" * 64
ROUND_MEMO_RE = re.compile(r"^sealed:v1:round:(?P<run_id>[A-Za-z0-9_-]{1,64}):(?P<index>\d+):(?P<sha256>[0-9a-f]{64})$")
COMMIT_PENDING = "pending on-chain: commit not anchored yet"


def round_payload(run_id: str, prev_sha256: str, iteration: IterationRecord | dict[str, Any]) -> dict[str, Any]:
    """The exact object round_digest hashes."""
    dev = iteration.get("dev")
    return {
        "v": AUDIT_VERSION,
        "run_id": run_id,
        "index": iteration["index"],
        "prev": prev_sha256,
        "hypothesis": iteration.get("hypothesis"),
        "rationale": iteration.get("rationale"),
        "strategy": iteration.get("strategy"),
        "candidate": iteration.get("candidate"),
        "dev_score": dev.get("score") if isinstance(dev, dict) else None,
        "decision": iteration.get("decision"),
    }


def round_digest(run_id: str, prev_sha256: str, iteration: IterationRecord | dict[str, Any]) -> str:
    return sha256_json(round_payload(run_id, prev_sha256, iteration))


def round_memo(run_id: str, index: int, sha256: str) -> str:
    """sealed:v1:round:<run_id>:<index>:<sha256>"""
    memo = f"sealed:v1:round:{run_id}:{int(index)}:{sha256}"
    if not ROUND_MEMO_RE.match(memo):
        raise ValueError(f"not a valid round memo: {memo!r}")
    return memo


def genesis_of(record: RunRecord | dict[str, Any]) -> str:
    """The chain's starting hash: the preregistration commitment (zeros if there is none)."""
    prereg = record.get("preregistration")
    commitment = prereg.get("commitment") if isinstance(prereg, dict) else None
    return str(commitment) if commitment else ZERO_HASH


def new_chain(record: RunRecord | dict[str, Any]) -> AuditChain:
    genesis = genesis_of(record)
    return {"v": AUDIT_VERSION, "genesis": genesis, "head": genesis, "rounds": []}


def append_round(record: RunRecord | dict[str, Any], iteration: IterationRecord | dict[str, Any]) -> AuditRound:
    """Hashes iteration onto record["audit"] (created on first use) with a not-yet-anchored
    receipt, and returns the new round. The caller anchors it with anchor_round."""
    audit = record.get("audit")
    if not isinstance(audit, dict):
        audit = record["audit"] = new_chain(record)  # type: ignore[typeddict-unknown-key]
    prev = audit["head"]
    sha = round_digest(str(record["run_id"]), prev, iteration)
    memo = round_memo(str(record["run_id"]), iteration["index"], sha)
    entry: AuditRound = {"index": int(iteration["index"]), "prev_sha256": prev, "sha256": sha, "memo": memo, "chain": chain.pending_receipt(memo, "pending on-chain: not anchored yet")}
    audit["rounds"].append(entry)
    audit["head"] = sha
    return entry


def commit_confirmed(record: RunRecord | dict[str, Any]) -> bool:
    prereg = record.get("preregistration")
    return isinstance(prereg, dict) and isinstance(prereg.get("chain"), dict) and prereg["chain"].get("status") == "confirmed"


async def anchor_round(record: RunRecord | dict[str, Any], entry: AuditRound, *, enabled: bool = True) -> ChainReceipt:
    """Anchors one round memo through chain.anchor. A round never lands before the commit it
    chains from: while the commit is pending the round stays pending with the commit's reason."""
    if not enabled:
        entry["chain"] = chain.pending_receipt(entry["memo"], "anchoring disabled")
    elif not commit_confirmed(record):
        prereg = record.get("preregistration") or {}
        reason = (prereg.get("chain") or {}).get("error") if isinstance(prereg, dict) else None
        entry["chain"] = chain.pending_receipt(entry["memo"], reason or COMMIT_PENDING)
    else:
        entry["chain"] = await chain.anchor(entry["memo"])
    return entry["chain"]


async def fetch_round_memos(record: RunRecord | dict[str, Any]) -> dict[int, str | None]:
    """On-chain memo text for every confirmed round receipt, keyed by round index (None when a
    confirmed memo could not be fetched). Pending rounds are left out."""
    audit = record.get("audit") if isinstance(record, dict) else None
    rounds = audit.get("rounds") if isinstance(audit, dict) else None
    confirmed = [
        r for r in (rounds if isinstance(rounds, list) else [])
        if isinstance(r, dict) and isinstance(r.get("chain"), dict) and r["chain"].get("status") == "confirmed" and r["chain"].get("signature") and isinstance(r.get("index"), int)
    ]
    memos = await asyncio.gather(*(chain.fetch_memo(str(r["chain"]["signature"])) for r in confirmed))
    return {r["index"]: memo for r, memo in zip(confirmed, memos, strict=True)}


def verify_chain(record: RunRecord | dict[str, Any], onchain: dict[int, str | None] | None = None) -> AuditReport | None:
    """Recomputes the chain from record["iterations"] and compares every round with the recorded
    hashes and, when given, the on-chain round memos (keyed by round index). None when the bundle
    has no audit chain. Never raises on a tampered bundle."""
    audit = record.get("audit")
    if audit is None:
        return None
    onchain = onchain or {}
    report: AuditReport = {
        "ok": False,
        "length": 0,
        "genesis_match": False,
        "head_recomputed": None,
        "head_recorded": None,
        "broken_at": None,
        "onchain_status": "pending",
        "rounds": [],
        "reasons": [],
    }
    reasons = report["reasons"]
    try:
        if not isinstance(audit, dict) or not isinstance(audit.get("rounds"), list):
            reasons.append("audit chain is malformed")
            return report
        iterations = record.get("iterations") or []
        rounds = audit["rounds"]
        report["length"] = len(rounds)
        report["head_recorded"] = audit.get("head")
        genesis = genesis_of(record)
        report["genesis_match"] = audit.get("genesis") == genesis
        if not report["genesis_match"]:
            reasons.append("audit chain does not start at the preregistration commitment")
        if len(rounds) != len(iterations):
            reasons.append(f"audit chain has {len(rounds)} rounds for {len(iterations)} iterations")
        run_id = str(record.get("run_id"))
        prev = genesis
        statuses: list[str] = []
        for pos, iteration in enumerate(iterations):
            stored = rounds[pos] if pos < len(rounds) and isinstance(rounds[pos], dict) else {}
            sha = round_digest(run_id, prev, iteration)
            memo_onchain = onchain.get(int(iteration["index"]))
            check: AuditRoundCheck = {
                "index": int(iteration["index"]),
                "sha256_recomputed": sha,
                "sha256_recorded": stored.get("sha256"),
                "sha256_onchain": None,
                "match": stored.get("sha256") == sha and stored.get("prev_sha256") == prev and stored.get("index") == iteration["index"] and stored.get("memo") == round_memo(run_id, iteration["index"], sha),
                "onchain_status": "pending",
            }
            receipt = stored.get("chain")
            if memo_onchain is not None:
                check["onchain_status"] = "confirmed"
                m = ROUND_MEMO_RE.match(memo_onchain.strip())
                if m is None or m["run_id"] != run_id or int(m["index"]) != iteration["index"]:
                    check["match"] = False
                    reasons.append(f"on-chain memo for round {iteration['index']} is not this run's round memo")
                else:
                    check["sha256_onchain"] = m["sha256"]
                    if m["sha256"] != sha:
                        check["match"] = False
                        reasons.append(f"round {iteration['index']} does not match its on-chain memo")
            elif isinstance(receipt, dict) and receipt.get("status") == "confirmed":
                check["onchain_status"] = "unreachable"
            statuses.append(check["onchain_status"])
            if not check["match"] and report["broken_at"] is None:
                report["broken_at"] = check["index"]
                reasons.append(f"audit chain breaks at round {check['index']}: the round record was changed after it was hashed")
            report["rounds"].append(check)
            prev = sha
        report["head_recomputed"] = prev
        if report["head_recorded"] != prev and report["broken_at"] is None:
            reasons.append("audit chain head does not match the recomputed head")
        report["onchain_status"] = "unreachable" if "unreachable" in statuses else "pending" if "pending" in statuses or not statuses else "confirmed"  # type: ignore[typeddict-item]
        if report["onchain_status"] == "pending" and statuses:
            reasons.append("on-chain round memos pending: compared with the recorded chain")
        report["ok"] = (
            report["genesis_match"]
            and len(rounds) == len(iterations)
            and report["broken_at"] is None
            and report["head_recorded"] == prev
        )
    except Exception as err:  # a malformed bundle must give a report, never a 500
        report["ok"] = False
        reasons.append(f"audit chain is malformed: {type(err).__name__}: {err}"[:200])
    return report
