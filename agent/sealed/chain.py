"""Anchors Sealed memos on Solana devnet through the starter's pay_usdc memo path (SPEC.md section 7). Owner: W2.

anchor() never raises: an unfunded wallet, a missing key or an unreachable RPC gives a
"pending" receipt that anchor_pending() can upgrade once the wallet is funded.
"""

import asyncio
import copy
import json
import os
import re
from datetime import UTC, datetime
from typing import Any

from .records import ChainReceipt, RunRecord

CLUSTER = "devnet"
DEFAULT_ANCHOR_USDC = 0.01
MAX_MEMO_BYTES = 566
FETCH_TIMEOUT_S = 20

_MEMO_LOG_RE = re.compile(r'^Program log: Memo \(len \d+\): "(?P<memo>.*)"$')
_NOT_FUNDED_MARKERS = ("no record of a prior credit", "insufficient funds", "custom program error: 0x1", "invalidaccountdata", "invalid account data", "accountnotfound", "no devnet")


def explorer_url(signature: str) -> str:
    """https://explorer.solana.com/tx/<signature>?cluster=devnet"""
    return f"https://explorer.solana.com/tx/{signature}?cluster={CLUSTER}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def pending_receipt(memo: str, error: str | None = None) -> ChainReceipt:
    """A receipt for a memo not (yet) on chain."""
    return {"status": "pending", "memo": memo, "signature": None, "explorer_url": None, "cluster": CLUSTER, "at": _now(), "error": error}


def confirmed_receipt(memo: str, signature: str) -> ChainReceipt:
    return {"status": "confirmed", "memo": memo, "signature": signature, "explorer_url": explorer_url(signature), "cluster": CLUSTER, "at": _now(), "error": None}


def _human_error(err: BaseException) -> str:
    """A short 'pending on-chain: ...' line. Never echoes key material (key errors get a fixed text)."""
    from agent.payments.config import PaymentsConfigError

    if isinstance(err, (asyncio.TimeoutError, TimeoutError)):
        return "pending on-chain: devnet did not confirm in time"
    if isinstance(err, PaymentsConfigError):
        return "pending on-chain: agent wallet not configured (AGENT_PRIVATE_KEY in .env)"
    detail = str(err)
    low = detail.lower()
    if any(marker in low for marker in _NOT_FUNDED_MARKERS):
        return "pending on-chain: agent wallet not funded (devnet SOL/USDC)"
    if "spending limits" in low:
        return "pending on-chain: blocked by the agent's spending limits"
    if "not confirmed in time" in low:
        return "pending on-chain: devnet did not confirm in time"
    transport = ("connecttimeout", "connecterror", "readtimeout", "networkerror", "solanarpcexception")
    if isinstance(err, OSError) or type(err).__module__.startswith(("httpx", "httpcore")) or any(m in low or m == type(err).__name__.lower() for m in transport):
        return "pending on-chain: devnet RPC unreachable"
    first = (detail.splitlines() or [type(err).__name__])[0].strip() or type(err).__name__
    return f"pending on-chain: {first[:160]}"


async def _send(memo: str, to: str | None, amount_usdc: float | None, timeout_s: float) -> str:
    from agent.paidapi import buyer
    from agent.payments import transfer
    from agent.payments.config import PaymentsConfigError

    try:
        keypair = transfer.load_agent_keypair()
    except PaymentsConfigError:
        raise
    except Exception:
        raise PaymentsConfigError("agent key unreadable") from None
    recipient = to or os.environ.get("SEALED_ANCHOR_TO", "").strip() or str(keypair.pubkey())
    amount = amount_usdc or float(os.environ.get("SEALED_ANCHOR_USDC", "").strip() or DEFAULT_ANCHOR_USDC)
    return await transfer.pay_usdc(keypair, to=recipient, amount_usdc=amount, memo=memo, guard=buyer.get_spending_tracker(), timeout_s=timeout_s)


async def anchor(memo: str, *, to: str | None = None, amount_usdc: float | None = None, timeout_s: float = 45) -> ChainReceipt:
    """pay_usdc(load_agent_keypair(), to or SEALED_ANCHOR_TO or own address, amount_usdc or
    SEALED_ANCHOR_USDC or 0.01, memo=memo, guard=get_spending_tracker()). Confirmed receipt on
    success, pending receipt with a short human error otherwise. Never raises, never logs keys."""
    assert len(memo.encode("utf-8")) < MAX_MEMO_BYTES, "memo too long for a Solana memo instruction"
    try:
        signature = await asyncio.wait_for(_send(memo, to, amount_usdc, timeout_s), timeout_s + 10)
    except asyncio.CancelledError:
        raise
    except Exception as err:  # every failure becomes a pending receipt the UI can show and retry
        return pending_receipt(memo, _human_error(err))
    return confirmed_receipt(memo, str(signature))


def memo_from_transaction(tx: Any) -> str | None:
    """The memo in a jsonParsed getTransaction result: the spl-memo instruction, else the memo log line."""
    found: list[str] = []
    logs: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("program") == "spl-memo" and isinstance(node.get("parsed"), str):
                found.append(node["parsed"])
            if isinstance(node.get("logMessages"), list):
                logs.extend(str(line) for line in node["logMessages"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(tx)
    if found:
        return found[0]
    for line in logs:
        m = _MEMO_LOG_RE.match(line)
        if m:
            return m["memo"]
    return None


async def _fetch_tx_json(signature: str) -> Any:
    from solana.rpc.async_api import AsyncClient
    from solders.signature import Signature

    from agent.payments.config import get_rpc_url

    async with AsyncClient(get_rpc_url()) as client:
        resp = await client.get_transaction(Signature.from_string(signature), encoding="jsonParsed", max_supported_transaction_version=0)
    return json.loads(resp.to_json())


async def fetch_memo(signature: str) -> str | None:
    """The memo text of a devnet transaction, or None if it is missing or unreadable."""
    try:
        tx = await asyncio.wait_for(_fetch_tx_json(signature), FETCH_TIMEOUT_S)
        return memo_from_transaction(tx)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def anchor_pending(record: RunRecord) -> RunRecord:
    """Re-anchors every pending receipt (commit first, then audit rounds, then reveal) and returns the updated record.
    Confirmed receipts are left untouched."""
    updated = copy.deepcopy(record)
    prereg = updated.get("preregistration")
    reveal = updated.get("reveal")
    changed = False
    if prereg is not None and prereg["chain"]["status"] == "pending":
        prereg["chain"] = await anchor(prereg["memo"])
        changed = True
    rounds = (updated.get("audit") or {}).get("rounds") or []
    for entry in rounds:
        if entry["chain"]["status"] != "pending":
            continue
        if prereg is not None and prereg["chain"]["status"] != "confirmed":
            # A round chains from the commitment, so it never lands before the commit.
            entry["chain"] = pending_receipt(entry["memo"], prereg["chain"]["error"] or "pending on-chain: commit not anchored yet")
        else:
            entry["chain"] = await anchor(entry["memo"])
        changed = True
    if reveal is not None and reveal["chain"]["status"] == "pending":
        if prereg is not None and prereg["chain"]["status"] != "confirmed":
            # The reveal must never land before the commit it refers to.
            reveal["chain"] = pending_receipt(reveal["memo"], prereg["chain"]["error"] or "pending on-chain: commit not anchored yet")
        else:
            reveal["chain"] = await anchor(reveal["memo"])
        changed = True
    if changed:
        updated["updated_at"] = _now()
    return updated
