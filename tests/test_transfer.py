"""Offline tests for agent/payments/transfer.py (no network)."""

import asyncio
import unittest
from dataclasses import dataclass

from solders.keypair import Keypair
from spl.token.constants import TOKEN_PROGRAM_ID

from agent.payments.transfer import MEMO_PROGRAM_ID, TransferError, build_usdc_transfer_instructions, pay_usdc, usdc_to_micros


@dataclass
class Decision:
    ok: bool
    reservation_id: int = 0
    reason: str = ""


class BlockingGuard:
    def try_reserve(self, amount_micros: int) -> Decision:
        return Decision(False, reason="over budget")

    def release(self, reservation_id: int) -> None: ...
    def commit(self, reservation_id: int) -> None: ...


class TransferTest(unittest.TestCase):
    def test_builds_create_transfer_and_memo(self) -> None:
        payer, to = Keypair(), Keypair()
        ixs = build_usdc_transfer_instructions(payer, str(to.pubkey()), 0.01, "agent-studio-abc")
        self.assertEqual(len(ixs), 3)
        self.assertEqual(ixs[1].program_id, TOKEN_PROGRAM_ID)
        self.assertEqual(ixs[2].program_id, MEMO_PROGRAM_ID)
        self.assertEqual(bytes(ixs[2].data), b"agent-studio-abc")
        self.assertEqual(usdc_to_micros(0.01), 10_000)
        self.assertEqual(len(build_usdc_transfer_instructions(payer, str(to.pubkey()), 1)), 2)

    def test_rejects_bad_input(self) -> None:
        with self.assertRaises(TransferError):
            build_usdc_transfer_instructions(Keypair(), "not-an-address", 1)
        with self.assertRaises(TransferError):
            build_usdc_transfer_instructions(Keypair(), str(Keypair().pubkey()), 0)

    def test_spending_guard_can_block(self) -> None:
        with self.assertRaisesRegex(TransferError, "spending limits: over budget"):
            asyncio.run(pay_usdc(Keypair(), str(Keypair().pubkey()), 5, guard=BlockingGuard()))
