"""python -m agent wallets -> shows whether the buyer and seller wallets are ready for devnet payments."""

import argparse
import os

import httpx

from agent.commands import Commands
from agent.payments.client import parse_secret_key
from agent.payments.config import USDC_DEVNET_MINT, PaymentsConfigError, get_rpc_url


def register(commands: Commands) -> None:
    cmd = commands.add_parser("wallets", help="Check the buyer and seller wallets hold devnet USDC")
    cmd.set_defaults(func=run)


def usdc_balance(owner: str) -> str | None:
    """The owner's devnet USDC balance, or None if it has no USDC token account yet."""
    response = httpx.post(
        get_rpc_url(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": USDC_DEVNET_MINT}, {"encoding": "jsonParsed"}],
        },
        timeout=30,
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        raise RuntimeError(f"Solana RPC error: {body['error'].get('message', body['error'])}")
    accounts = body["result"]["value"]
    if not accounts:
        return None
    return accounts[0]["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"]


def report(label: str, owner: str | None, missing_hint: str) -> bool:
    if not owner:
        print(f"✗ {label}: not set. {missing_hint}")
        return False
    balance = usdc_balance(owner)
    if balance is None:
        print(f"✗ {label} {owner}: no devnet USDC account yet. Send it devnet USDC from https://faucet.circle.com")
        return False
    print(f"✓ {label} {owner}: {balance} devnet USDC")
    return True


def run(args: argparse.Namespace) -> int:
    pay_to = (os.environ.get("PAY_TO_ADDRESS") or "").strip() or None
    raw = (os.environ.get("AGENT_PRIVATE_KEY") or "").strip()
    try:
        buyer = str(parse_secret_key(raw).pubkey()) if raw else None
    except PaymentsConfigError as err:
        print(f"✗ Buyer  (AGENT_PRIVATE_KEY): {err}")
        return 1
    seller_ok = report("Seller (PAY_TO_ADDRESS)", pay_to, "Run: python -m agent keygen --pay-to")
    buyer_ok = report("Buyer  (AGENT_PRIVATE_KEY)", buyer, "Run: python -m agent keygen")
    if seller_ok and buyer_ok:
        print("\nReady: both wallets can send/receive devnet USDC.")
        return 0
    print("\nNot ready yet. Both wallets need a devnet USDC account: the buyer to pay, the seller to receive.")
    return 1
