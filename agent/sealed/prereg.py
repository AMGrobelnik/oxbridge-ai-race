"""Pre-registration hashes, memos and bundle verification (SPEC.md section 7). Owner: W2.

Pure functions, no network: chain.py does the devnet side.
"""

import re
import secrets
from datetime import UTC, datetime
from typing import Any

from .environment import Environment
from .hashing import sha256_hex, sha256_json
from .records import Holdout, Preregistration, RunRecord, Verdict, VerifyReport

MEMO_PREFIX = "sealed:v1"
COMMIT_MEMO_RE = re.compile(r"^sealed:v1:commit:(?P<run_id>[A-Za-z0-9_-]{1,64}):(?P<commitment>[0-9a-f]{64})$")
REVEAL_MEMO_RE = re.compile(
    r"^sealed:v1:reveal:(?P<run_id>[A-Za-z0-9_-]{1,64}):(?P<salt>[0-9a-f]{64}):(?P<result>[0-9a-f]{64})"
    r":(?P<verdict>GENERALISES|OVERFIT):(?P<selected>\d+|none)$"
)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

PENDING_REASON = "on-chain record pending: compared with the recorded commitment"
UNREACHABLE_REASON = "on-chain memo could not be fetched: compared with the recorded values"


def new_salt() -> str:
    """32 random bytes as 64 lowercase hex characters (secrets.token_hex)."""
    return secrets.token_hex(32)


def code_digest(env: Environment) -> str:
    """sha256 over name + NUL + bytes + NUL for each of env.code_files(), in order."""
    return sha256_hex(b"".join(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0" for path in env.code_files()))


def holdout_commitment(holdout: Holdout, salt: str) -> str:
    """sha256_json({"salt": salt, "holdout": holdout})."""
    return sha256_json({"salt": salt, "holdout": holdout})


def commitment_digest(*, run_id: str, env: str, code_sha256: str, dev_seeds: list[int], holdout_commitment: str) -> str:
    """sha256_json({"v": 1, "run_id", "env", "code_sha256", "dev_seeds", "holdout_commitment"})."""
    return sha256_json(
        {"v": 1, "run_id": run_id, "env": env, "code_sha256": code_sha256, "dev_seeds": list(dev_seeds), "holdout_commitment": holdout_commitment}
    )


def commit_memo(run_id: str, commitment: str) -> str:
    """sealed:v1:commit:<run_id>:<commitment>"""
    _check_run_id(run_id)
    return f"{MEMO_PREFIX}:commit:{run_id}:{commitment}"


def reveal_memo(run_id: str, salt: str, result_sha256: str, verdict: Verdict, selected_iteration: int | None) -> str:
    """sealed:v1:reveal:<run_id>:<salt>:<result_sha256>:<verdict>:<selected or none>"""
    _check_run_id(run_id)
    selected = "none" if selected_iteration is None else str(int(selected_iteration))
    return f"{MEMO_PREFIX}:reveal:{run_id}:{salt}:{result_sha256}:{verdict}:{selected}"


def build_preregistration(env: Environment, run_id: str, salt: str, *, sealed_at: str) -> Preregistration:
    """Everything for the commit memo. Its chain receipt starts pending (memo set, no signature);
    the harness replaces it with chain.anchor(memo)."""
    from . import chain  # chain imports nothing from prereg; kept local so prereg stays import-light

    code_sha256 = code_digest(env)
    dev_seeds = [int(s) for s in env.spec["dev_seeds"]]
    holdout_sha = holdout_commitment(env.load_holdout(), salt)
    commitment = commitment_digest(run_id=run_id, env=env.name, code_sha256=code_sha256, dev_seeds=dev_seeds, holdout_commitment=holdout_sha)
    memo = commit_memo(run_id, commitment)
    return {
        "env": env.name,
        "run_id": run_id,
        "code_sha256": code_sha256,
        "dev_seeds": dev_seeds,
        "holdout_commitment": holdout_sha,
        "commitment": commitment,
        "memo": memo,
        "sealed_at": sealed_at,
        "chain": chain.pending_receipt(memo, "pending on-chain: not anchored yet"),
    }


def result_payload(record: RunRecord) -> dict[str, Any]:
    """The exact object result_digest hashes (SPEC.md section 7)."""
    prereg = record["preregistration"]
    reveal = record["reveal"]
    if prereg is None or reveal is None:
        raise ValueError("the record has no preregistration or reveal")
    return {
        "v": 1,
        "run_id": record["run_id"],
        "commitment": prereg["commitment"],
        "iterations": [
            {
                "index": it["index"],
                "candidate": it["candidate"],
                "dev_score": it["dev"]["score"] if it.get("dev") else None,
                "decision": it["decision"],
            }
            for it in record["iterations"]
        ],
        "results": [{"iteration": r["iteration"], "per_shift": r["per_shift"], "verdict": r["verdict"]} for r in reveal["results"]],
        "selected_iteration": reveal["selected_iteration"],
    }


def result_digest(record: RunRecord) -> str:
    """sha256_json of the result payload in SPEC.md section 7 (needs record["reveal"]["results"])."""
    return sha256_json(result_payload(record))


def verify_bundle(
    record: RunRecord,
    env: Environment,
    *,
    commit_memo_onchain: str | None,
    reveal_memo_onchain: str | None,
    round_memos_onchain: dict[int, str | None] | None = None,
) -> VerifyReport:
    """Recomputes code, holdout commitment, commitment, result hash and the per-round audit chain
    and compares them with the recorded values and, when given, the on-chain memos. Never raises
    on a tampered bundle: it returns ok=False with reasons."""
    report: VerifyReport = {
        "ok": False,
        "code_match": False,
        "commitment_recomputed": "",
        "commitment_recorded": "",
        "commitment_onchain": None,
        "commit_match": False,
        "result_recomputed": None,
        "result_recorded": None,
        "result_onchain": None,
        "result_match": None,
        "onchain_status": "pending",
        "reasons": [],
    }
    try:
        _verify_into(report, record, env, commit_memo_onchain, reveal_memo_onchain)
    except Exception as err:  # a malformed bundle must give a report, never a 500
        report["ok"] = False
        report["reasons"].append(f"bundle is malformed: {type(err).__name__}: {err}"[:200])
    _verify_audit_into(report, record, round_memos_onchain)
    return report


def _verify_audit_into(report: VerifyReport, record: Any, round_memos_onchain: dict[int, str | None] | None) -> None:
    """Folds the audit chain check (SPEC.md section 13) into the report; a bundle without an
    audit chain (made before it existed) reports audit_match=None and is not failed for it."""
    from . import audit  # audit imports chain; kept local so prereg stays import-light

    audit_report = audit.verify_chain(record, round_memos_onchain) if isinstance(record, dict) else None
    report["audit"] = audit_report
    report["audit_match"] = None if audit_report is None else audit_report["ok"]
    if audit_report is not None and not audit_report["ok"]:
        report["ok"] = False
        report["reasons"].extend(r for r in audit_report["reasons"] if not r.startswith("on-chain round memos pending"))


def _verify_into(report: VerifyReport, record: Any, env: Environment, commit_onchain: str | None, reveal_onchain: str | None) -> None:
    reasons = report["reasons"]
    failures = 0

    def fail(reason: str) -> None:
        nonlocal failures
        failures += 1
        reasons.append(reason)

    prereg = record.get("preregistration") if isinstance(record, dict) else None
    if not isinstance(prereg, dict):
        fail("bundle has no preregistration")
        return
    reveal = record.get("reveal")
    reveal = reveal if isinstance(reveal, dict) else None

    # 1. Sealed code: the installed plug-in files must hash to the recorded code_sha256.
    try:
        installed = code_digest(env)
    except OSError as err:
        installed = ""
        fail(f"could not read the installed evaluation code: {err}")
    report["code_match"] = bool(installed) and installed == prereg.get("code_sha256")
    if installed and not report["code_match"]:
        fail("evaluation code does not match the sealed code hash")

    # 2. Commitment from the recorded fields; the holdout commitment from the revealed salt.
    commit_ok = True
    if record.get("run_id") != prereg.get("run_id") or record.get("env") != prereg.get("env"):
        commit_ok = False
        fail("run id or environment differs from the preregistration")
    recomputed = commitment_digest(
        run_id=str(record.get("run_id")),
        env=str(record.get("env")),
        code_sha256=str(prereg["code_sha256"]),
        dev_seeds=list(prereg["dev_seeds"]),
        holdout_commitment=str(prereg["holdout_commitment"]),
    )
    report["commitment_recomputed"] = recomputed
    report["commitment_recorded"] = str(prereg.get("commitment", ""))
    if recomputed != report["commitment_recorded"]:
        commit_ok = False
        fail("commitment does not match the recorded preregistration")
    if reveal is not None:
        if holdout_commitment(reveal["holdout"], str(reveal["salt"])) != prereg["holdout_commitment"]:
            commit_ok = False
            fail("revealed salt and holdout do not match the sealed holdout commitment")

    # 3. Result hash from the bundle.
    if reveal is not None:
        result = result_digest(record)
        report["result_recomputed"] = result
        report["result_recorded"] = str(reveal.get("result_sha256", ""))
        report["result_match"] = result == report["result_recorded"]
        if not report["result_match"]:
            fail("result hash does not match the recorded reveal")

    # 4. On-chain memos.
    receipts: list[tuple[dict[str, Any] | None, str | None]] = [(prereg.get("chain"), commit_onchain)]
    if reveal is not None:
        receipts.append((reveal.get("chain"), reveal_onchain))
    statuses = []
    for receipt, onchain in receipts:
        if onchain is not None:
            statuses.append("confirmed")
        elif isinstance(receipt, dict) and receipt.get("status") == "confirmed":
            statuses.append("unreachable")
        else:
            statuses.append("pending")
    report["onchain_status"] = "unreachable" if "unreachable" in statuses else "pending" if "pending" in statuses else "confirmed"

    if commit_onchain is not None:
        m = COMMIT_MEMO_RE.match(commit_onchain.strip())
        if m is None:
            commit_ok = False
            fail("on-chain commit memo is not a Sealed commit memo")
        else:
            report["commitment_onchain"] = m["commitment"]
            if m["run_id"] != record.get("run_id"):
                commit_ok = False
                fail("run id does not match the on-chain commit memo")
            if m["commitment"] != recomputed:
                commit_ok = False
                fail("commitment does not match the on-chain commit memo")
    elif statuses[0] == "pending":
        reasons.append(PENDING_REASON)
    else:
        reasons.append(UNREACHABLE_REASON)

    if reveal is not None:
        if reveal_onchain is not None:
            m = REVEAL_MEMO_RE.match(reveal_onchain.strip())
            if m is None:
                report["result_match"] = False
                fail("on-chain reveal memo is not a Sealed reveal memo")
            else:
                report["result_onchain"] = m["result"]
                if m["run_id"] != record.get("run_id"):
                    report["result_match"] = False
                    fail("run id does not match the on-chain reveal memo")
                if m["salt"] != reveal.get("salt"):
                    commit_ok = False
                    fail("revealed salt does not match the on-chain reveal memo")
                if m["result"] != report["result_recomputed"]:
                    report["result_match"] = False
                    fail("result hash does not match the on-chain reveal memo")
                selected = reveal.get("selected_iteration")
                if m["verdict"] != reveal.get("verdict") or m["selected"] != ("none" if selected is None else str(selected)):
                    report["result_match"] = False
                    fail("verdict or selected candidate does not match the on-chain reveal memo")
        elif statuses[-1] == "pending":
            reasons.append("on-chain reveal pending: compared with the recorded result hash")
        else:
            reasons.append("on-chain reveal memo could not be fetched: compared with the recorded result hash")

    report["commit_match"] = commit_ok
    report["ok"] = failures == 0


def _check_run_id(run_id: str) -> None:
    if not RUN_ID_RE.match(run_id):
        raise ValueError(f"run id {run_id!r} must match {RUN_ID_RE.pattern}")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
