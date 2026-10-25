"""python -m agent keygen            -> buyer key: sets AGENT_PRIVATE_KEY (the wallet that pays)
python -m agent keygen --pay-to   -> seller address: sets PAY_TO_ADDRESS (where payments arrive)

Creates a throwaway Solana keypair for DEVNET testing and saves it to .env.
Existing values in .env are never overwritten.
"""

import argparse
import re

from solders.keypair import Keypair

from agent.commands import Commands
from agent.config import ENV_FILE


def register(commands: Commands) -> None:
    cmd = commands.add_parser("keygen", help="Create a throwaway devnet wallet and save it to .env")
    cmd.add_argument("--pay-to", action="store_true", help="Create the seller address (PAY_TO_ADDRESS) instead")
    cmd.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    key = "PAY_TO_ADDRESS" if args.pay_to else "AGENT_PRIVATE_KEY"
    current = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    if re.search(rf"^{key}=\S+", current, re.MULTILINE):
        print(f"{key} is already set in .env; leaving it alone.")
        return 0

    keypair = Keypair()
    address = str(keypair.pubkey())
    secret = str(keypair)  # base58 of the 64-byte secret key
    line = f"{key}={address if args.pay_to else secret}"
    empty_line = re.compile(rf"^{key}=[ \t]*$", re.MULTILINE)
    if empty_line.search(current):
        updated = empty_line.sub(line, current, count=1)  # fill the empty line copied from .env.example
    else:
        updated = current + ("\n" if current and not current.endswith("\n") else "") + line + "\n"
    ENV_FILE.write_text(updated)

    print(f"Saved {key} to .env (git-ignored). DEVNET USE ONLY.")
    print(f"Wallet address: {address}")
    print("Next: send it free devnet USDC at https://faucet.circle.com (choose Solana Devnet), then run `python -m agent wallets`.")
    if args.pay_to:
        print("Secret key (save it only if you want to move these test tokens later):")
        print(f"  {secret}")
    return 0
