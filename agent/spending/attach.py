"""Connects a SpendingTracker to an x402 client:

1. x402's built-in spend controls cap every single payment (defence in depth).
2. Before signing, the tracker must approve the amount, or the payment is aborted.
3. If the payment fails or is not settled, the amount goes back into the budget.
"""

from typing import Any

from x402 import (
    AbortResult,
    PaymentCreatedContext,
    PaymentCreationContext,
    PaymentCreationFailureContext,
    x402Client,
)
from x402.schemas import PaymentResponseContext

from agent.payments.config import SOLANA_DEVNET, USDC_DECIMALS, USDC_DEVNET_MINT

from .policy import micros_to_usd
from .tracker import SpendingTracker


def attach_spending_limits(client: x402Client, tracker: SpendingTracker) -> x402Client:
    if USDC_DECIMALS != 6:
        raise RuntimeError("Spending limits assume a 6-decimal USD stablecoin")

    # Reservation ids, keyed by the objects x402 hands to each hook. The objects are kept
    # alongside so their id() can't be reused while the payment is in flight.
    by_requirements: dict[int, tuple[Any, int]] = {}
    by_payload: dict[int, tuple[Any, int]] = {}

    def before(ctx: PaymentCreationContext) -> AbortResult | None:
        req = ctx.selected_requirements
        # Only devnet USDC is priced in dollars here; refuse anything else rather than guess its value.
        if req.network != SOLANA_DEVNET or req.asset != USDC_DEVNET_MINT:
            return AbortResult(reason=f"Refusing to pay in unknown asset {req.asset} on {req.network}")
        decision = tracker.try_reserve(int(req.get_amount()))
        if not decision.ok:
            return AbortResult(reason=decision.reason)
        by_requirements[id(req)] = (req, decision.reservation_id)
        return None

    def after(ctx: PaymentCreatedContext) -> None:
        entry = by_requirements.pop(id(ctx.selected_requirements), None)
        if entry is not None:
            inner = ctx.payment_payload.payload
            by_payload[id(inner)] = (inner, entry[1])

    def failure(ctx: PaymentCreationFailureContext) -> None:
        entry = by_requirements.pop(id(ctx.selected_requirements), None)
        if entry is not None:
            tracker.release(entry[1])

    def response(ctx: PaymentResponseContext) -> None:
        entry = by_payload.pop(id(ctx.payment_payload.payload), None)
        if entry is None:
            return  # Unknown: keep it counted (safe default).
        if ctx.settle_response is not None and ctx.settle_response.success:
            tracker.commit(entry[1])
        elif ctx.settle_response is not None or ctx.payment_required is not None:
            # Rejected at verify or settle: no money moved, so give it back.
            tracker.release(entry[1])
        # Network error: we can't tell whether it settled, so it stays counted.

    client.set_spend_controls({"max_amount_per_payment": f"${micros_to_usd(tracker.policy.max_payment_micros)}"})
    client.on_before_payment_creation(before)
    client.on_after_payment_creation(after)
    client.on_payment_creation_failure(failure)
    client.on_payment_response(response)
    return client
