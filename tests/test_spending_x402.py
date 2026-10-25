"""Offline test: the spending limits wired into the real x402 client and its httpx transport,
against a fake paid server. No network, no wallet, no money.
Run: python -m unittest discover -s tests -v
"""

import secrets
import unittest
from typing import Any

import httpx
from x402 import x402Client
from x402.http import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, PAYMENT_SIGNATURE_HEADER
from x402.http.clients.httpx import x402AsyncTransport
from x402.http.utils import encode_payment_required_header, encode_payment_response_header
from x402.mechanisms.svm import find_default_asset
from x402.schemas import PaymentRequired, PaymentRequirements, ResourceInfo, SettleResponse

from agent.payments.config import SOLANA_DEVNET, USDC_DEVNET_MINT
from agent.spending.attach import attach_spending_limits
from agent.spending.policy import read_spending_policy
from agent.spending.tracker import SpendingTracker


class FakeScheme:
    """Signs nothing: just lets us see which payments the client agreed to make."""

    scheme = "exact"
    find_default_asset = staticmethod(find_default_asset)

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        return {"transaction": f"fake-{secrets.token_hex(8)}"}


def payment_required(amount: str, asset: str, error: str = "Payment required") -> str:
    return encode_payment_required_header(
        PaymentRequired(
            x402_version=2,
            error=error,
            resource=ResourceInfo(url="http://test/paid", description="test", mime_type="application/json"),
            accepts=[
                PaymentRequirements(
                    scheme="exact",
                    network=SOLANA_DEVNET,
                    asset=asset,
                    amount=amount,
                    pay_to="x",
                    max_timeout_seconds=60,
                    extra={},
                )
            ],
        )
    )


def fake_server(amount: str, settle: str, asset: str = USDC_DEVNET_MINT) -> httpx.MockTransport:
    """A fake seller: 402 without payment; with payment, settles (or not) according to `settle`."""

    def handle(request: httpx.Request) -> httpx.Response:
        if not request.headers.get(PAYMENT_SIGNATURE_HEADER):
            return httpx.Response(402, json={}, headers={PAYMENT_REQUIRED_HEADER: payment_required(amount, asset)})
        if settle == "reject":
            header = payment_required(amount, asset, error="insufficient_funds")
            return httpx.Response(402, json={}, headers={PAYMENT_REQUIRED_HEADER: header})
        receipt = encode_payment_response_header(SettleResponse(success=True, transaction="tx", network=SOLANA_DEVNET))
        return httpx.Response(200, json={"ok": True}, headers={PAYMENT_RESPONSE_HEADER: receipt})

    return httpx.MockTransport(handle)


def setup(budget: str, max_payment: str, server: httpx.MockTransport) -> tuple[SpendingTracker, httpx.AsyncClient]:
    tracker = SpendingTracker(read_spending_policy({"AGENT_BUDGET_USD": budget, "MAX_PAYMENT_USD": max_payment}))
    client = x402Client()
    client.register(SOLANA_DEVNET, FakeScheme())
    attach_spending_limits(client, tracker)
    return tracker, httpx.AsyncClient(transport=x402AsyncTransport(client, transport=server))


class SpendingX402Test(unittest.IsolatedAsyncioTestCase):
    async def test_settled_payments_count_until_the_budget_runs_out(self) -> None:
        tracker, http = setup("0.03", "0.01", fake_server("10000", "ok"))  # $0.01 each
        async with http:
            for _ in range(3):
                self.assertEqual((await http.get("http://test/paid")).status_code, 200)
            self.assertEqual(tracker.snapshot()["spentUsd"], "0.03")
            with self.assertRaisesRegex(Exception, "exceed the budget"):
                await http.get("http://test/paid")
        self.assertEqual(tracker.snapshot()["payments"], 3)

    async def test_payments_the_seller_rejects_are_given_back(self) -> None:
        tracker, http = setup("1", "0.10", fake_server("10000", "reject"))
        async with http:
            self.assertEqual((await http.get("http://test/paid")).status_code, 402)
        self.assertEqual(tracker.snapshot()["spentUsd"], "0.00")

    async def test_price_above_the_per_payment_cap_is_refused_before_signing(self) -> None:
        tracker, http = setup("1", "0.05", fake_server("60000", "ok"))  # $0.06
        async with http:
            with self.assertRaisesRegex(Exception, "max_amount_per_payment|per-payment cap"):
                await http.get("http://test/paid")
        self.assertEqual(tracker.snapshot()["spentUsd"], "0.00")

    async def test_unknown_tokens_are_refused(self) -> None:
        other_mint = "So11111111111111111111111111111111111111112"
        tracker, http = setup("1", "0.10", fake_server("1", "ok", other_mint))
        async with http:
            with self.assertRaises(Exception):
                await http.get("http://test/paid")
        self.assertEqual(tracker.snapshot()["payments"], 0)

    async def test_other_default_devnet_stablecoins_are_refused(self) -> None:
        # x402 itself allows every default devnet asset (USDC, USDG, PYUSD); this template only pays in USDC.
        usdg = "4F6PM96JJxngmHnZLBh9n58RH4aTVNWvDs2nuwrT5BP7"
        tracker, http = setup("1", "0.10", fake_server("10000", "ok", usdg))
        async with http:
            with self.assertRaisesRegex(Exception, "unknown asset"):
                await http.get("http://test/paid")
        self.assertEqual(tracker.snapshot()["payments"], 0)


if __name__ == "__main__":
    unittest.main()
