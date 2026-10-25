"""Human co-sign guardrail: an agent payment over the co-sign cap is queued for a human instead of sent.

Limited autonomy, human sign-off on big actions, and an audit trail of every decision:

    if needs_cosign(amount_usdc):
        approval = queue(to, amount_usdc, memo, reason="AI Race run_workflow task")  # nothing is sent

The cap is COSIGN_CAP_USD in .env (default 0.50 devnet USDC). Approvals and the audit log live in
COSIGN_DIR (default .cosign/ at the repo root, git-ignored): approvals.json holds every request and its
state, audit.jsonl gets one line per decision (approve, reject) and per send attempt.

An approved payment that cannot land yet (unfunded wallet, devnet down) is kept as
"approved_pending_onchain" with a "pending on-chain: ..." reason; approving it again retries the send.
"""

import json
import os
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.config import PROJECT_ROOT
from agent.spending.policy import usd_to_micros

from . import transfer
from .config import PaymentsConfigError

DEFAULT_COSIGN_CAP_USD = "0.50"
EXPLORER = "https://explorer.solana.com/tx/{}?cluster=devnet"
OPEN_STATES = ("pending", "approved_pending_onchain")

_lock = threading.Lock()


class CosignError(Exception):
    """A decision that cannot be applied (unknown id, already decided)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def cap_micros() -> int:
    return usd_to_micros(os.environ.get("COSIGN_CAP_USD") or DEFAULT_COSIGN_CAP_USD)


def cap_usd() -> float:
    return cap_micros() / 1_000_000


def needs_cosign(amount_usdc: float) -> bool:
    return round(amount_usdc * 1_000_000) > cap_micros()


def _dir() -> Path:
    return Path(os.environ.get("COSIGN_DIR") or PROJECT_ROOT / ".cosign")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load() -> dict[str, dict[str, Any]]:
    path = _dir() / "approvals.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save(approvals: dict[str, dict[str, Any]]) -> None:
    path = _dir() / "approvals.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(approvals, indent=2), encoding="utf-8")
    tmp.replace(path)


def _audit(event: str, approval: dict[str, Any], **extra: Any) -> dict[str, Any]:
    record = {"at": _now(), "event": event, "approval_id": approval["id"], "amount_usdc": approval["amount_usdc"], "to": approval["to"], "memo": approval["memo"], **extra}
    path = _dir() / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return record


def queue(to: str, amount_usdc: float, memo: str | None, reason: str) -> dict[str, Any]:
    """Records a payment request for a human to approve or reject, and returns it."""
    approval = {
        "id": f"cs_{secrets.token_hex(6)}",
        "status": "pending",
        "amount_usdc": amount_usdc,
        "to": to,
        "memo": memo,
        "reason": f"{reason}: {amount_usdc} USDC is over the co-sign cap of {cap_usd():.2f} USDC",
        "cap_usd": cap_usd(),
        "created_at": _now(),
        "decided_at": None,
        "decided_by": None,
        "note": None,
        "signature": None,
        "explorer_url": None,
        "onchain_error": None,
    }
    with _lock:
        approvals = _load()
        approvals[approval["id"]] = approval
        _save(approvals)
        _audit("queued", approval, reason=approval["reason"])
    return approval


def list_approvals(status: str | None = None) -> list[dict[str, Any]]:
    """Newest first; status "open" means pending or approved but not yet on chain."""
    items = sorted(_load().values(), key=lambda a: a["created_at"], reverse=True)
    if status == "open":
        return [a for a in items if a["status"] in OPEN_STATES]
    return [a for a in items if status is None or a["status"] == status]


def read_audit(limit: int = 200) -> list[dict[str, Any]]:
    path = _dir() / "audit.jsonl"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    return [json.loads(line) for line in reversed(lines) if line.strip()]


def _get_open(approval_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    approvals = _load()
    approval = approvals.get(approval_id)
    if approval is None:
        raise CosignError(f"Unknown approval {approval_id!r}", 404)
    if approval["status"] not in OPEN_STATES:
        raise CosignError(f"Approval {approval_id!r} is already {approval['status']}", 409)
    return approvals, approval


def reject(approval_id: str, by: str = "human", note: str | None = None) -> dict[str, Any]:
    with _lock:
        approvals, approval = _get_open(approval_id)
        if approval["status"] != "pending":
            raise CosignError(f"Approval {approval_id!r} was already approved; it can only be retried", 409)
        approval.update(status="rejected", decided_at=_now(), decided_by=by, note=note)
        _save(approvals)
        _audit("rejected", approval, by=by, note=note)
    return approval


async def approve(approval_id: str, by: str = "human", note: str | None = None) -> dict[str, Any]:
    """Human co-sign: sends the queued payment with pay_usdc. The per-payment autonomy cap does not
    apply to a co-signed payment, but it still counts against the agent's total budget."""
    from agent.paidapi.buyer import get_spending_tracker

    with _lock:
        approvals, approval = _get_open(approval_id)
        approval.update(status="approved_pending_onchain", decided_at=approval["decided_at"] or _now(), decided_by=by, note=note or approval["note"])
        _save(approvals)
        _audit("approved", approval, by=by, note=note)
    try:
        signature = await transfer.pay_usdc(transfer.load_agent_keypair(), to=approval["to"], amount_usdc=approval["amount_usdc"], memo=approval["memo"], guard=get_spending_tracker().cosigned_guard())
    except (transfer.TransferError, PaymentsConfigError, OSError) as err:
        first = (str(err).splitlines() or [type(err).__name__])[0].strip() or type(err).__name__
        error = f"pending on-chain: {first[:160]}"
        if any(m in str(err).lower() for m in ("no record of a prior credit", "insufficient funds", "invalidaccountdata", "accountnotfound")):
            error = "pending on-chain: agent wallet not funded (devnet SOL/USDC)"
        if isinstance(err, PaymentsConfigError):
            error = "pending on-chain: agent wallet not configured (AGENT_PRIVATE_KEY in .env)"
        updates: dict[str, Any] = {"onchain_error": error}
        event, extra = "send_pending", {"onchain_error": error}
    else:
        updates = {"status": "sent", "signature": signature, "explorer_url": EXPLORER.format(signature), "onchain_error": None}
        event, extra = "sent", {"signature": signature}
    with _lock:
        approvals = _load()
        approval = approvals[approval_id]
        approval.update(updates)
        _save(approvals)
        _audit(event, approval, by=by, **extra)
    return approval

