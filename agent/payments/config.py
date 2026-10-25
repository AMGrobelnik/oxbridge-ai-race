"""Devnet-only payment settings, read from the environment (.env)."""

import os

from solders.pubkey import Pubkey

# CAIP-2 network id for Solana devnet, as used by x402 V2.
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"

# Devnet USDC mint (Circle). This is the token x402 charges on devnet.
USDC_DEVNET_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
USDC_DECIMALS = 6

DEFAULT_FACILITATOR_URL = "https://x402.org/facilitator"
DEFAULT_SOLANA_RPC_URL = "https://api.devnet.solana.com"

# This starter only ever talks to Solana devnet, where tokens are free test tokens.
# Moving to mainnet (real money) is deliberately a code change, not an env change.
ALLOWED_NETWORKS = frozenset({SOLANA_DEVNET})


class PaymentsConfigError(Exception):
    """A payment setting is missing or not allowed. The message says how to fix it."""


def get_network() -> str:
    network = os.environ.get("X402_NETWORK") or SOLANA_DEVNET
    if network not in ALLOWED_NETWORKS:
        raise PaymentsConfigError(
            f"X402_NETWORK={network} is not allowed. This template only supports Solana devnet ({SOLANA_DEVNET})."
        )
    return SOLANA_DEVNET


def get_facilitator_url() -> str:
    return os.environ.get("X402_FACILITATOR_URL") or DEFAULT_FACILITATOR_URL


def get_rpc_url() -> str:
    return os.environ.get("SOLANA_RPC_URL") or DEFAULT_SOLANA_RPC_URL


def get_pay_to_address() -> str:
    pay_to = (os.environ.get("PAY_TO_ADDRESS") or "").strip()
    if not pay_to:
        raise PaymentsConfigError("PAY_TO_ADDRESS is not set. Add your Solana devnet wallet address to .env.")
    if not is_address(pay_to):
        raise PaymentsConfigError(f"PAY_TO_ADDRESS ({pay_to}) is not a valid Solana address.")
    return pay_to


def is_address(value: str) -> bool:
    try:
        Pubkey.from_string(value)
    except ValueError:
        return False
    return True
