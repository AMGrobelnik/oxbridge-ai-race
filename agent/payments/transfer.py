"""A direct devnet USDC transfer with an optional memo, signed by the agent's wallet (AGENT_PRIVATE_KEY).

This is what the AI Race's on-chain payment stage asks for:

    signature = await pay_usdc(load_agent_keypair(), to="<address>", amount_usdc=0.01, memo="<memo from the task>")
    return {"data": {"signature": signature}}

x402 (client.py) is for paying APIs that answer 402; this is for paying an address directly.
Devnet only: the agent wallet needs devnet SOL for fees and devnet USDC (see the README;
"python -m agent wallets" checks).
"""

import asyncio
import os
import time
from typing import Protocol

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    TransferCheckedParams,
    create_idempotent_associated_token_account,
    get_associated_token_address,
    transfer_checked,
)

from .client import parse_secret_key
from .config import USDC_DECIMALS, USDC_DEVNET_MINT, PaymentsConfigError, get_rpc_url

MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")


class TransferError(Exception):
    pass


class SpendDecisionLike(Protocol):
    ok: bool
    reservation_id: int
    reason: str


class SpendGuard(Protocol):
    """Optional spending guard (the spending module's SpendingTracker fits this shape)."""

    def try_reserve(self, amount_micros: int) -> SpendDecisionLike: ...
    def release(self, reservation_id: int) -> None: ...
    def commit(self, reservation_id: int) -> None: ...


def load_agent_keypair() -> Keypair:
    raw = (os.environ.get("AGENT_PRIVATE_KEY") or "").strip()
    if not raw:
        raise PaymentsConfigError("AGENT_PRIVATE_KEY is not set. Create a throwaway devnet keypair (python -m agent keygen) and add it to .env.")
    return parse_secret_key(raw)


def usdc_to_micros(amount_usdc: float) -> int:
    if not isinstance(amount_usdc, (int, float)) or amount_usdc <= 0:
        raise TransferError("amount_usdc must be a positive number.")
    return round(amount_usdc * 10**USDC_DECIMALS)


def build_usdc_transfer_instructions(payer: Keypair, to: str, amount_usdc: float, memo: str | None = None) -> list[Instruction]:
    """Create the recipient's USDC account if needed, transfer, and add the memo."""
    try:
        owner = Pubkey.from_string(to)
    except ValueError:
        raise TransferError(f'"{to}" is not a Solana address.') from None
    mint = Pubkey.from_string(USDC_DEVNET_MINT)
    source = get_associated_token_address(payer.pubkey(), mint)
    dest = get_associated_token_address(owner, mint)
    instructions = [
        create_idempotent_associated_token_account(payer.pubkey(), owner, mint),
        transfer_checked(TransferCheckedParams(program_id=TOKEN_PROGRAM_ID, source=source, mint=mint, dest=dest, owner=payer.pubkey(), amount=usdc_to_micros(amount_usdc), decimals=USDC_DECIMALS, signers=[])),
    ]
    if memo:
        instructions.append(Instruction(MEMO_PROGRAM_ID, memo.encode("utf-8"), []))
    return instructions


async def pay_usdc(payer: Keypair, to: str, amount_usdc: float, memo: str | None = None, guard: SpendGuard | None = None, rpc_url: str | None = None, timeout_s: float = 45) -> str:
    """Sends the transfer, waits until devnet confirms it, and returns the signature.

    With a guard, the amount is reserved against your budget first and released if the transfer doesn't land.
    """
    instructions = build_usdc_transfer_instructions(payer, to, amount_usdc, memo)
    reservation: int | None = None
    if guard:
        decision = guard.try_reserve(usdc_to_micros(amount_usdc))
        if not decision.ok:
            raise TransferError(f"Payment blocked by your spending limits: {decision.reason}")
        reservation = decision.reservation_id
    try:
        async with AsyncClient(rpc_url or get_rpc_url()) as client:
            blockhash = (await client.get_latest_blockhash(Confirmed)).value.blockhash
            tx = VersionedTransaction(MessageV0.try_compile(payer.pubkey(), instructions, [], blockhash), [payer])
            signature = (await client.send_raw_transaction(bytes(tx), TxOpts(preflight_commitment=Confirmed))).value
            deadline = time.monotonic() + timeout_s
            while True:
                status = (await client.get_signature_statuses([signature])).value[0]
                if status and status.err:
                    raise TransferError(f"The transfer failed on chain: {status.err}")
                if status and status.confirmation_status and str(status.confirmation_status).lower().endswith(("confirmed", "finalized")):
                    break
                if time.monotonic() > deadline:
                    raise TransferError(f"Not confirmed in time (signature {signature}).")
                await asyncio.sleep(1)
        if guard and reservation is not None:
            guard.commit(reservation)
        return str(signature)
    except Exception as err:
        if guard and reservation is not None:
            guard.release(reservation)
        if isinstance(err, TransferError):
            raise
        raise TransferError(_explain(err)) from err


def _explain(err: Exception) -> str:
    detail = str(err)
    if "no record of a prior credit" in detail:
        return f'{detail} (the agent wallet has no devnet SOL for fees: get some at faucet.solana.com, then check with "python -m agent wallets")'
    if "insufficient funds" in detail or "custom program error: 0x1" in detail:
        return f'{detail} (the agent wallet doesn\'t have enough devnet USDC: get some at faucet.circle.com (Solana Devnet), then check with "python -m agent wallets")'
    if "InvalidAccountData" in detail or "invalid account data" in detail or "AccountNotFound" in detail:
        return f'{detail} (the agent wallet has no devnet USDC yet: get some at faucet.circle.com (Solana Devnet), then check with "python -m agent wallets")'
    return detail or "The transfer failed."
