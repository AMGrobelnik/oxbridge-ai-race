"""Pay.sh (https://pay.sh): pay x402 APIs with the `pay` CLI from the Solana Foundation, and
draft a listing so other agents can find your API in the Pay.sh catalog.

python -m agent paysh                     -> pays this agent's GET /premium-data once, from your Pay.sh wallet
python -m agent paysh <url>               -> pays any x402 URL (add --sandbox for Pay.sh's test network)
python -m agent paysh --wallet            -> shows your Pay.sh devnet wallet and its USDC balance
python -m agent paysh --listing [team]    -> writes pay-listing/providers/<team>/<name>/PAY.md and checks it

Needs the `pay` CLI: `brew install pay` or `npm install -g @solana/pay`.
"""

import argparse
import os
import re
import subprocess
from pathlib import Path

from agent.commands import Commands
from agent.commands.wallets import usdc_balance

PAID_PATH = "/premium-data"
FAUCET = "https://faucet.circle.com"


def register(commands: Commands) -> None:
    cmd = commands.add_parser("paysh", help="Pay x402 APIs with the Pay.sh CLI, or draft a Pay.sh catalog listing")
    cmd.add_argument("url", nargs="?", help="Default: this project's /premium-data on localhost")
    cmd.add_argument("--sandbox", action="store_true", help="Pay on Pay.sh's hosted test network")
    cmd.add_argument("--wallet", action="store_true", help="Show your Pay.sh devnet wallet and balance")
    cmd.add_argument("--listing", nargs="?", const="your-team", metavar="TEAM", help="Write and check a PAY.md catalog listing")
    cmd.set_defaults(func=run)


def pay(args: list[str], cwd: str | None = None, capture: bool = False) -> subprocess.CompletedProcess[str] | None:
    """Runs `pay` with these arguments. Returns None (after explaining) when `pay` is not installed."""
    try:
        return subprocess.run(
            ["pay", *args],
            cwd=cwd,
            text=True,
            capture_output=capture,
            env={**os.environ, "NO_COLOR": "1"},
            check=False,
        )
    except FileNotFoundError:
        print("The `pay` CLI is not installed. Install it with `brew install pay` or `npm install -g @solana/pay`, then run this again.")
        return None


def devnet_wallet_from(output: str) -> str | None:
    """The devnet wallet address in `pay account list` output, if there is one."""
    match = re.search(r"\[devnet ([1-9A-HJ-NP-Za-km-z]{32,44})\]", output)
    return match.group(1) if match else None


def devnet_wallet() -> str | None:
    """Your Pay.sh devnet wallet address, if `pay` has made one (it does on your first devnet payment)."""
    result = pay(["account", "list"], capture=True)
    return None if result is None else devnet_wallet_from(result.stdout + result.stderr)


def wallet() -> int:
    owner = devnet_wallet()
    if owner is None:
        print("No Pay.sh devnet wallet yet. `python -m agent paysh` makes one on its first payment; then run `python -m agent paysh --wallet` again.")
        return 0
    balance = usdc_balance(owner)
    print(f"Pay.sh devnet wallet: {owner}")
    print(f"✗ No devnet USDC yet. Send it some from {FAUCET} (choose Solana Devnet)." if balance is None else f"✓ {balance} devnet USDC")
    return 0


def pay_once(url: str | None, sandbox: bool) -> int:
    target = url or f"http://localhost:{os.environ.get('PORT') or 3000}{PAID_PATH}"
    print(f"Paying {target} with Pay.sh…")
    result = pay([*(["--sandbox"] if sandbox else []), "curl", "-s", target], capture=True)
    if result is None:
        return 1
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip())
    if "402 again after payment" in result.stdout + result.stderr:
        print(f"\nThe payment was refused. Usually your Pay.sh wallet has no devnet USDC: run `python -m agent paysh --wallet`, then fund it at {FAUCET}.")
        return 1
    return result.returncode


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def listing_markdown(name: str, service_url: str, price: float) -> str:
    """A PAY.md for the Pay.sh catalog: one paid GET endpoint, priced per request.
    The catalog's rules are in https://github.com/solana-foundation/pay-skills/blob/main/CONTRIBUTING.md
    """
    title = " ".join(part.capitalize() for part in name.split("-"))
    return f"""---
name: {name}
title: "{title}"
description: "Pay-per-request premium data from {title}, a Python agent built with Superteam Agent Studio, returning one short JSON fact per paid call."
use_case: "Use when an agent needs a small paid JSON fact from {title}, or to test an x402 purchase in USDC on Solana end to end."
category: data
service_url: {service_url}
endpoints:
  - method: GET
    path: {PAID_PATH[1:]}
    description: "Return one premium fact as JSON after an x402 payment in USDC on Solana"
    pricing:
      dimensions:
        - direction: usage
          unit: requests
          scale: 1
          tiers:
            - price_usd: {price:g}
---

{title} sells one endpoint, `GET {PAID_PATH}`, for ${price:g} per request.
Edit this file to describe what your API really returns before you submit it.
"""


def listing(team: str) -> int:
    name = slug(Path.cwd().name) or "my-agent"
    operator = slug(team) or "your-team"
    service_url = os.environ.get("PAYSH_SERVICE_URL") or "https://your-api.example.com"
    price = float(os.environ.get("PAID_API_PRICE_USD") or "0.01")
    folder = Path("pay-listing", "providers", operator, name)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "PAY.md").write_text(listing_markdown(name, service_url, price), encoding="utf-8")
    print(f"Wrote {folder / 'PAY.md'}")
    if "example.com" in service_url:
        print("Set PAYSH_SERVICE_URL in .env to your deployed https:// address, then run this again.")
    print("The public catalog only lists APIs paid on Solana mainnet. Submit it once you deploy for real money: https://github.com/solana-foundation/pay-skills\n")
    result = pay(["catalog", "check", str(Path("providers", operator, name, "PAY.md")), "--no-probe"], cwd="pay-listing")
    return 1 if result is None else result.returncode


def run(args: argparse.Namespace) -> int:
    if args.wallet:
        return wallet()
    if args.listing is not None:
        return listing(args.listing)
    return pay_once(args.url, args.sandbox)
