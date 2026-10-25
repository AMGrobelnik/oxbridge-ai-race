"""python -m agent buy                                         -> buys http://localhost:3000/premium-data
python -m agent buy http://localhost:3000/premium-data -n 3  -> buys 3 times (watch the budget)

Pays with AGENT_PRIVATE_KEY from .env, within the spending limits (AGENT_BUDGET_USD, MAX_PAYMENT_USD).
"""

import argparse
import json
import os

from agent.commands import Commands
from agent.paidapi.buyer import buy


def register(commands: Commands) -> None:
    cmd = commands.add_parser("buy", help="Pay for a x402-protected URL, within the spending limits")
    cmd.add_argument("url", nargs="?", help="Default: this project's /premium-data on localhost")
    cmd.add_argument("-n", "--times", type=int, default=1, help="How many times to buy (default 1)")
    cmd.set_defaults(func=run)


async def run(args: argparse.Namespace) -> int:
    url = args.url or f"http://localhost:{os.environ.get('PORT') or 3000}/premium-data"
    for i in range(1, args.times + 1):
        try:
            result = await buy(url)
        except Exception as err:
            print(f"#{i} refused/failed: {err}")
            return 1
        print(f"#{i} status {result.status}")
        if result.payment_error:
            print(f"  payment rejected: {result.payment_error}")
        print(f"  data: {json.dumps(result.data)}")
        if result.receipt:
            print(f"  receipt: {json.dumps(result.receipt)}")
        print(f"  spending: {json.dumps(result.spending)}")
    return 0
