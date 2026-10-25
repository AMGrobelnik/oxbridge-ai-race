"""Anchors Sealed memos on Solana devnet through the starter's pay_usdc memo path (SPEC.md section 7). Owner: W2.

anchor() never raises: an unfunded wallet, a missing key or an unreachable RPC gives a
"pending" receipt that anchor_pending() can upgrade once the wallet is funded.
"""

from .records import ChainReceipt, RunRecord

CLUSTER = "devnet"
DEFAULT_ANCHOR_USDC = 0.01


def explorer_url(signature: str) -> str:
    """https://explorer.solana.com/tx/<signature>?cluster=devnet"""
    return f"https://explorer.solana.com/tx/{signature}?cluster={CLUSTER}"


def pending_receipt(memo: str, error: str | None = None) -> ChainReceipt:
    """A receipt for a memo not (yet) on chain."""
    raise NotImplementedError("W2")


async def anchor(memo: str, *, to: str | None = None, amount_usdc: float | None = None, timeout_s: float = 45) -> ChainReceipt:
    """pay_usdc(load_agent_keypair(), to or SEALED_ANCHOR_TO or own address, amount_usdc or
    SEALED_ANCHOR_USDC or 0.01, memo=memo, guard=get_spending_tracker()). Confirmed receipt on
    success, pending receipt with a short human error otherwise. Never raises, never logs keys."""
    raise NotImplementedError("W2")


async def fetch_memo(signature: str) -> str | None:
    """The memo text of a devnet transaction, or None if it is missing or unreadable."""
    raise NotImplementedError("W2")


async def anchor_pending(record: RunRecord) -> RunRecord:
    """Re-anchors every pending receipt (commit first, then reveal) and returns the updated record.
    Confirmed receipts are left untouched."""
    raise NotImplementedError("W2")
