"""GET /premium-data costs PAID_API_PRICE_USD (default $0.01) in devnet USDC per call.

The handler only runs after the payment was verified. Replace it with what you want to sell.
"""

import os
import random
from datetime import datetime, timezone

from fastapi import FastAPI

from agent.payments.server import PaidRouteOptions, paid_route

FACTS = [
    "Solana devnet tokens have no monetary value: perfect for testing.",
    "HTTP status 402 'Payment Required' was reserved in 1997 and finally has a use.",
    "x402 lets a program pay for an API call without accounts, cards or API keys.",
]


def price() -> PaidRouteOptions:
    return PaidRouteOptions(price=f"${os.environ.get('PAID_API_PRICE_USD') or '0.01'}", description="One premium fact")


def register(app: FastAPI) -> None:
    @paid_route(app, "GET", "/premium-data", price)
    async def premium_data() -> dict[str, str]:
        return {"fact": random.choice(FACTS), "servedAt": datetime.now(timezone.utc).isoformat()}
