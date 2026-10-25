"""Mystery-shopper probes for Sealed Verify. None of them spends money except the opt-in micro-payment.

Every probe returns a ProbeResult with a status (PASS, FAIL, UNREACHABLE, PENDING) and a reason:

- x402: GET the endpoint, expect HTTP 402 with a payment offer (amount, asset, network, payTo).
- solana_pay: parse a `solana:<recipient>?amount=…&spl-token=…` URL and check on devnet that the
  recipient exists and holds a token account for the requested mint (USDC by default).
- micropay: pay the merchant 0.01 devnet USDC with pay_usdc; an unfunded wallet gives PENDING.
"""

import asyncio
import base64
import ipaddress
import json
import socket
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from agent.payments.config import SOLANA_DEVNET, USDC_DECIMALS, USDC_DEVNET_MINT, get_rpc_url, is_address

Status = Literal["PASS", "FAIL", "UNREACHABLE", "PENDING"]
ProbeType = Literal["x402", "solana_pay", "micropay"]
PROBE_TYPES: tuple[str, ...] = ("x402", "solana_pay", "micropay")

X402_TIMEOUT_S = 6.0
RPC_TIMEOUT_S = 8.0
MICROPAY_TIMEOUT_S = 40.0
MICROPAY_USDC = 0.01
KNOWN_ASSETS = {USDC_DEVNET_MINT: ("USDC", USDC_DECIMALS)}
NETWORK_LABELS = {SOLANA_DEVNET: "Solana devnet", "solana-devnet": "Solana devnet", "solana": "Solana mainnet"}


@dataclass
class ProbeResult:
    probe: str
    status: Status
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)
    ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ParseError(ValueError):
    """The input is not a valid offer or payment URL; the message says why."""


# ---------- x402 offer ----------


def _decode_header(value: str) -> Any:
    raw = value.strip()
    try:
        return json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), validate=False))
    except (ValueError, UnicodeDecodeError):
        try:
            return json.loads(raw)
        except ValueError:
            raise ParseError("PAYMENT-REQUIRED header is neither base64 JSON nor JSON") from None


def _format_amount(amount: str, asset: str) -> str:
    symbol, decimals = KNOWN_ASSETS.get(asset, (None, None))
    if symbol is None or decimals is None:
        return f"{amount} base units of {asset[:12]}…" if len(asset) > 12 else f"{amount} {asset}"
    return f"{(Decimal(amount) / Decimal(10) ** decimals).normalize():f} {symbol}"


def parse_x402_offer(status_code: int, headers: dict[str, str], body: bytes | str) -> dict[str, Any]:
    """The first payment option of a 402 answer (x402 V2 PAYMENT-REQUIRED header, or a V1 JSON body).

    Returns {version, scheme, network, network_label, asset, amount, amount_label, pay_to, resource}.
    Raises ParseError when the answer is not a usable 402 offer."""
    if status_code != 402:
        raise ParseError(f"expected HTTP 402 Payment Required, got HTTP {status_code}")
    lowered = {k.lower(): v for k, v in headers.items()}
    payload: Any = None
    if lowered.get("payment-required"):
        payload = _decode_header(lowered["payment-required"])
    else:
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise ParseError("402 without a PAYMENT-REQUIRED header or a JSON body") from None
    if not isinstance(payload, dict):
        raise ParseError("payment offer is not a JSON object")
    accepts = payload.get("accepts")
    if not isinstance(accepts, list) or not accepts or not isinstance(accepts[0], dict):
        raise ParseError("402 offer lists no payment options (accepts is empty)")
    option = accepts[0]
    amount = option.get("amount", option.get("maxAmountRequired"))
    fields = {"scheme": option.get("scheme"), "network": option.get("network"), "asset": option.get("asset"), "payTo": option.get("payTo")}
    missing = [k for k, v in {**fields, "amount": amount}.items() if not isinstance(v, (str, int)) or str(v) == ""]
    if missing:
        raise ParseError(f"402 offer is missing {', '.join(missing)}")
    amount_s = str(amount)
    if not amount_s.isdigit() or int(amount_s) <= 0:
        raise ParseError(f"402 offer amount {amount_s!r} is not a positive integer")
    network = str(fields["network"])
    resource = payload.get("resource")
    return {
        "version": payload.get("x402Version", 1),
        "scheme": str(fields["scheme"]),
        "network": network,
        "network_label": NETWORK_LABELS.get(network, network),
        "asset": str(fields["asset"]),
        "amount": amount_s,
        "amount_label": _format_amount(amount_s, str(fields["asset"])),
        "pay_to": str(fields["payTo"]),
        "resource": resource.get("url") if isinstance(resource, dict) else resource,
    }


# ---------- Solana Pay ----------


def parse_solana_pay_url(url: str) -> dict[str, Any]:
    """A Solana Pay transfer request: {recipient, amount, spl_token, label, message, memo, reference}.

    Raises ParseError for anything else (including interactive transaction-request URLs)."""
    if not isinstance(url, str) or not url.lower().startswith("solana:"):
        raise ParseError("a Solana Pay URL starts with solana:")
    rest = url[len("solana:"):]
    recipient, _, query = rest.partition("?")
    recipient = unquote(recipient)
    if recipient.lower().startswith(("http:", "https:")):
        raise ParseError("transaction-request URLs (solana:https://…) are not transfer requests")
    if not is_address(recipient):
        raise ParseError(f"recipient {recipient[:60]!r} is not a Solana address")
    params = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
    amount = params.get("amount")
    if amount is not None:
        try:
            value = Decimal(amount)
        except InvalidOperation:
            raise ParseError(f"amount {amount!r} is not a number") from None
        if not value.is_finite() or value < 0 or amount.lower().startswith(("+", "-")) or "e" in amount.lower():
            raise ParseError(f"amount {amount!r} must be a plain non-negative decimal")
    spl_token = params.get("spl-token")
    if spl_token is not None and not is_address(spl_token):
        raise ParseError(f"spl-token {spl_token[:60]!r} is not a mint address")
    return {
        "recipient": recipient,
        "amount": amount,
        "spl_token": spl_token,
        "label": params.get("label"),
        "message": params.get("message"),
        "memo": params.get("memo"),
        "reference": params.get("reference"),
    }


def solana_pay_url(recipient: str, *, amount: str = "0.01", label: str | None = None, spl_token: str = USDC_DEVNET_MINT) -> str:
    """solana:<recipient>?amount=<amount>&spl-token=<USDC devnet>&label=<label>"""
    from urllib.parse import quote

    url = f"solana:{recipient}?amount={amount}&spl-token={spl_token}"
    if label:
        url += f"&label={quote(label, safe='')}"
    return url


async def _rpc(client: httpx.AsyncClient, rpc_url: str, method: str, params: list[Any]) -> Any:
    res = await client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if res.status_code == 429:
        raise httpx.HTTPStatusError("devnet RPC rate-limited (429)", request=res.request, response=res)
    res.raise_for_status()
    body = res.json()
    if body.get("error"):
        raise RuntimeError(f"RPC {method} error: {str(body['error'].get('message', body['error']))[:120]}")
    return body["result"]


async def check_solana_pay(url: str, *, rpc_url: str | None = None, timeout_s: float = RPC_TIMEOUT_S) -> ProbeResult:
    started = time.monotonic()
    try:
        parsed = parse_solana_pay_url(url)
    except ParseError as err:
        return ProbeResult("solana_pay", "FAIL", f"bad Solana Pay URL: {err}", ms=_ms(started))
    mint = parsed["spl_token"] or None
    detail: dict[str, Any] = {"recipient": parsed["recipient"], "mint": mint, "amount": parsed["amount"]}
    rpc = rpc_url or get_rpc_url()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            info = await _rpc(client, rpc, "getAccountInfo", [parsed["recipient"], {"encoding": "base64"}])
            detail["account_exists"] = info.get("value") is not None
            detail["lamports"] = (info.get("value") or {}).get("lamports", 0)
            if mint:
                accounts = await _rpc(client, rpc, "getTokenAccountsByOwner", [parsed["recipient"], {"mint": mint}, {"encoding": "jsonParsed"}])
                tokens = [a for a in accounts.get("value", []) if (a.get("account", {}).get("data", {}).get("parsed", {}).get("info", {}).get("state")) == "initialized"]
                detail["token_accounts"] = len(tokens)
    except (httpx.HTTPError, RuntimeError, ValueError, KeyError, TimeoutError) as err:
        return ProbeResult("solana_pay", "UNREACHABLE", f"devnet RPC unreachable: {_short(err)}", detail, _ms(started))
    symbol = KNOWN_ASSETS.get(mint or "", ("token",))[0] if mint else "SOL"
    if mint:
        if detail["token_accounts"]:
            exists = "funded wallet" if detail["account_exists"] else "token account only"
            return ProbeResult("solana_pay", "PASS", f"payable: recipient has a {symbol} token account on devnet ({exists})", detail, _ms(started))
        if not detail["account_exists"]:
            return ProbeResult("solana_pay", "FAIL", f"recipient does not exist on devnet and has no {symbol} account", detail, _ms(started))
        return ProbeResult("solana_pay", "FAIL", f"recipient exists but has no {symbol} token account", detail, _ms(started))
    if detail["account_exists"]:
        return ProbeResult("solana_pay", "PASS", "payable: recipient account exists on devnet (SOL transfer)", detail, _ms(started))
    return ProbeResult("solana_pay", "FAIL", "recipient account does not exist on devnet", detail, _ms(started))


# ---------- x402 endpoint ----------


class BlockedTarget(ValueError):
    """The URL points at a private, loopback or otherwise non-public address."""


def _blocked_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved or addr.is_unspecified


async def assert_public_host(url: str) -> None:
    """Raises BlockedTarget unless every address the host resolves to is public (SSRF guard)."""
    host = urlsplit(url).hostname
    if not host:
        raise BlockedTarget("URL has no host")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as err:
        raise BlockedTarget(f"host {host!r} does not resolve ({type(err).__name__})") from None
    for info in infos:
        if _blocked_ip(str(info[4][0])):
            raise BlockedTarget(f"host {host!r} resolves to a non-public address")


async def check_x402(url: str, *, allow_private: bool = False, timeout_s: float = X402_TIMEOUT_S) -> ProbeResult:
    started = time.monotonic()
    try:
        if not allow_private:
            await assert_public_host(url)
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            res = await client.get(url, headers={"accept": "application/json", "user-agent": "sealed-verify-mystery-shopper/1"})
            body = res.content[:65536]
    except BlockedTarget as err:
        return ProbeResult("x402", "FAIL", f"refused to probe: {err}", ms=_ms(started))
    except httpx.TimeoutException:
        return ProbeResult("x402", "UNREACHABLE", f"no answer within {timeout_s:g}s", ms=_ms(started))
    except httpx.HTTPError as err:
        return ProbeResult("x402", "UNREACHABLE", f"connection failed: {_short(err)}", ms=_ms(started))
    try:
        offer = parse_x402_offer(res.status_code, dict(res.headers), body)
    except ParseError as err:
        return ProbeResult("x402", "FAIL", f"no valid x402 offer: {err}", {"http_status": res.status_code}, _ms(started))
    reason = f"402 offer: {offer['amount_label']} on {offer['network_label']} to {offer['pay_to'][:4]}…{offer['pay_to'][-4:]}"
    return ProbeResult("x402", "PASS", reason, {"http_status": 402, "offer": offer}, _ms(started))


# ---------- opt-in micro-payment ----------


async def micropay(recipient: str, *, memo: str, amount_usdc: float = MICROPAY_USDC, timeout_s: float = MICROPAY_TIMEOUT_S) -> ProbeResult:
    """Pays the merchant 0.01 devnet USDC within the agent's spending limits. Never raises:
    an unfunded or unconfigured wallet, a spending block or a slow devnet gives PENDING."""
    from agent.paidapi import buyer
    from agent.payments import transfer
    from agent.payments.config import PaymentsConfigError
    from agent.sealed.chain import explorer_url

    started = time.monotonic()
    detail: dict[str, Any] = {"recipient": recipient, "amount_usdc": amount_usdc, "memo": memo}
    try:
        keypair = transfer.load_agent_keypair()
    except PaymentsConfigError:
        return ProbeResult("micropay", "PENDING", "pending on-chain: agent wallet not configured", detail, _ms(started))
    except Exception:  # an unreadable key: never echo it
        return ProbeResult("micropay", "PENDING", "pending on-chain: agent key unreadable", detail, _ms(started))
    try:
        signature = await asyncio.wait_for(
            transfer.pay_usdc(keypair, to=recipient, amount_usdc=amount_usdc, memo=memo, guard=buyer.get_spending_tracker(), timeout_s=timeout_s),
            timeout_s + 10,
        )
    except asyncio.CancelledError:
        raise
    except Exception as err:  # every failure is a pending payment the next round retries
        return ProbeResult("micropay", "PENDING", _pending_reason(err), detail, _ms(started))
    detail.update(signature=str(signature), explorer_url=explorer_url(str(signature)))
    return ProbeResult("micropay", "PASS", f"paid {amount_usdc:g} USDC on devnet and it confirmed", detail, _ms(started))


def _pending_reason(err: BaseException) -> str:
    low = str(err).lower()
    if isinstance(err, (asyncio.TimeoutError, TimeoutError)) or "not confirmed in time" in low:
        return "pending on-chain: devnet did not confirm in time"
    if any(m in low for m in ("no record of a prior credit", "insufficient funds", "0x1", "invalidaccountdata", "invalid account data", "accountnotfound", "no devnet")):
        return "pending on-chain: agent wallet not funded (devnet SOL/USDC)"
    if "spending limits" in low:
        return "pending on-chain: blocked by the agent's spending limits"
    return "pending on-chain: payment did not go through"


def _short(err: BaseException) -> str:
    text = str(err).splitlines()[0] if str(err) else type(err).__name__
    return text[:120]


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
