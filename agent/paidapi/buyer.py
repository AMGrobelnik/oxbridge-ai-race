"""The agent that buys from paid APIs. One per process, so the budget is shared by every
purchase until the process restarts."""

import json
from dataclasses import dataclass
from typing import Any

from x402 import x402Client

from agent.payments.client import (
    create_payment_client,
    create_paying_http_client,
    load_agent_signer,
    read_payment_error,
    read_payment_receipt,
)
from agent.spending.attach import attach_spending_limits
from agent.spending.policy import read_spending_policy
from agent.spending.tracker import SpendingTracker

_tracker: SpendingTracker | None = None
_client: x402Client | None = None


def get_spending_tracker() -> SpendingTracker:
    global _tracker
    if _tracker is None:
        _tracker = SpendingTracker(read_spending_policy())
    return _tracker


def _get_payment_client() -> x402Client:
    # Not cached until it succeeds, so the next call retries after .env is fixed.
    global _client
    if _client is None:
        _client = attach_spending_limits(create_payment_client(load_agent_signer()), get_spending_tracker())
    return _client


@dataclass
class PurchaseResult:
    status: int
    payment_error: str | None
    """Set when the seller rejected our payment, e.g. "insufficient_funds"."""
    data: Any
    receipt: dict[str, object] | None
    spending: dict[str, object]

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "paymentError": self.payment_error,
            "data": self.data,
            "receipt": self.receipt,
            "spending": self.spending,
        }


async def buy(url: str) -> PurchaseResult:
    """GETs `url`, paying for it if it answers 402, within the spending limits.

    Raises if the payment is refused by the limits or the agent is not configured.
    """
    async with create_paying_http_client(_get_payment_client()) as http:
        response = await http.get(url)
    try:
        data: Any = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = response.text
    return PurchaseResult(
        status=response.status_code,
        payment_error=read_payment_error(response),
        data=data,
        receipt=read_payment_receipt(response),
        spending=get_spending_tracker().snapshot(),
    )
