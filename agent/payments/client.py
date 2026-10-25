"""Buyer side: an HTTP client that automatically pays when a server answers 402 Payment Required.

It signs with AGENT_PRIVATE_KEY, so it must only ever run in this agent, never in code you give to users.
"""

import json
import os

import httpx
from solders.keypair import Keypair
from x402 import x402Client
from x402.http import PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER
from x402.http.clients import x402HttpxClient
from x402.http.utils import decode_payment_required_header, decode_payment_response_header
from x402.mechanisms.svm import KeypairSigner
from x402.mechanisms.svm.exact import ExactSvmClientScheme

from .config import PaymentsConfigError, get_network, get_rpc_url


def parse_secret_key(raw: str) -> Keypair:
    """Accepts the JSON byte array that `solana-keygen` writes or the base58 string that wallets export."""
    try:
        if raw.startswith("["):
            secret = bytes(json.loads(raw))
        else:
            secret = bytes(Keypair.from_base58_string(raw))
    except ValueError:
        raise PaymentsConfigError("AGENT_PRIVATE_KEY is not valid base58 or a JSON byte array.") from None
    if len(secret) != 64:
        raise PaymentsConfigError(f"AGENT_PRIVATE_KEY must be a 64-byte secret key (got {len(secret)} bytes).")
    try:
        return Keypair.from_bytes(secret)
    except ValueError:
        raise PaymentsConfigError("AGENT_PRIVATE_KEY is not a valid Solana secret key.") from None


def load_agent_signer() -> KeypairSigner:
    raw = (os.environ.get("AGENT_PRIVATE_KEY") or "").strip()
    if not raw:
        raise PaymentsConfigError(
            "AGENT_PRIVATE_KEY is not set. Create a throwaway devnet keypair (python -m agent keygen) and add it to .env."
        )
    return KeypairSigner(parse_secret_key(raw))


def create_payment_client(signer: KeypairSigner) -> x402Client:
    """An x402 client that can pay on Solana devnet. Add spending limits before using it."""
    client = x402Client()
    client.register(get_network(), ExactSvmClientScheme(signer, rpc_url=get_rpc_url()))
    return client


def create_paying_http_client(client: x402Client, timeout: float = 60) -> httpx.AsyncClient:
    """An httpx.AsyncClient that pays for 402 responses with `client`. Use it with `async with`."""
    return x402HttpxClient(client, timeout=timeout)


def read_payment_receipt(response: httpx.Response) -> dict[str, object] | None:
    """Reads the PAYMENT-RESPONSE header (settlement receipt) from a paid response, if any."""
    header = response.headers.get(PAYMENT_RESPONSE_HEADER)
    if not header:
        return None
    return decode_payment_response_header(header).model_dump(by_alias=True, exclude_none=True)


def read_payment_error(response: httpx.Response) -> str | None:
    """If the server still answered 402 after we paid, this says why (e.g. not enough devnet USDC)."""
    if response.status_code != 402:
        return None
    header = response.headers.get(PAYMENT_REQUIRED_HEADER)
    if not header:
        return "Payment required"
    try:
        reason = decode_payment_required_header(header).error or "Payment required"
    except Exception:
        return "Payment required (could not decode PAYMENT-REQUIRED header)"
    if "insufficient" in reason or "simulation_failed" in reason:
        return f"{reason} (usually a wallet is not funded yet: run `python -m agent wallets`)"
    return reason
