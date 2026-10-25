"""Merchant records for Sealed Verify: the seeded demo merchants, onboarding validation and pin status.

A merchant is {id, name, lat, lng, kind, target, micropay, seed, physical_only, created_at, note}:
kind is "x402" (target = an https URL that answers 402), "solana_pay" (target = a solana: URL),
or "physical" (no target, listed only).
"""

import math
import re
import secrets
import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from agent.config import get_port
from agent.payments.config import is_address

from .probes import ParseError, parse_solana_pay_url, solana_pay_url

FRESH_S = 600  # green while the last PASS is younger than 10 minutes
KINGS_COLLEGE = (52.20431, 0.11653)
MAX_MERCHANTS = 80
NAME_MIN, NAME_MAX = 2, 48
PLACE_MAX = 60
URL_MAX = 300
_NAME_OK = re.compile(r"^[\w .,'&()!+#/@-]+$")

PAYER = "Hsw88CEieeGW7HxuXC5SDUYLnNZnfQ8Ew86PLVyHfuZX"
SELLER = "3VDpWHVvVpaQ6YWiFs7HhAyTXBZPWLB8KcLyp6ARSHG1"
FACILITATOR_FEE_PAYER = "CKPKJWNdJEqa81x7CkZ14BVPiY6y16Sxs7owznqtWYp5"
EMPTY_WALLET = "A14nBmi7XDwamqrZXnP7N8aHbo3LmTpv9d1z6mE97GQp"  # a fresh keypair: no account, no USDC


class InvalidMerchant(ValueError):
    """Onboarding input was rejected; the message is safe to show."""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def self_base_url() -> str:
    return f"http://127.0.0.1:{get_port()}"


def seed_merchants() -> list[dict[str, Any]]:
    """6-8 demo merchants around King's College, Cambridge. Two are deliberately broken."""
    base = self_base_url()
    rows = [
        ("kings-parade-data", "King's Parade Data Kiosk", 52.20395, 0.11795, "x402", f"{base}/premium-data", False, "our own x402 paid route /premium-data"),
        ("market-hill-cafe", "Market Hill Café", 52.20550, 0.11880, "solana_pay", solana_pay_url(FACILITATOR_FEE_PAYER, label="Market Hill Café"), False, "devnet wallet with a USDC account"),
        ("benet-st-coffee", "Bene't Street Coffee", 52.20300, 0.11865, "solana_pay", solana_pay_url(PAYER, label="Bene't Street Coffee"), False, "the race payer wallet (funded later)"),
        ("trinity-st-books", "Trinity Street Books", 52.20640, 0.11700, "solana_pay", solana_pay_url(SELLER, label="Trinity Street Books"), True, "the seller wallet; opted in to a 0.01 USDC test purchase"),
        ("dead-link-diner", "Dead Link Diner", 52.20470, 0.11350, "x402", "http://127.0.0.1:9/checkout", False, "broken on purpose: nothing listens there"),
        ("empty-wallet-deli", "Empty Wallet Deli", 52.20240, 0.11590, "solana_pay", solana_pay_url(EMPTY_WALLET, label="Empty Wallet Deli"), False, "broken on purpose: wallet has no USDC account"),
        ("silver-st-bakery", "Silver Street Bakery", 52.20180, 0.11420, "physical", None, False, "physical listing only: no crypto checkout to probe"),
    ]
    out = []
    for mid, name, lat, lng, kind, target, micro, note in rows:
        out.append({"id": mid, "name": name, "lat": lat, "lng": lng, "kind": kind, "target": target, "micropay": micro, "seed": True, "created_at": now_iso(), "note": f"demo merchant: {note}", "place": "Cambridge (demo merchant)"})
    return out


def clean_text(value: Any, *, field: str, min_len: int, max_len: int, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str):
        raise InvalidMerchant(f"{field} must be text")
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"\s+", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C").strip()
    if not min_len <= len(text) <= max_len:
        raise InvalidMerchant(f"{field} must be {min_len}-{max_len} characters")
    if pattern is not None and not pattern.fullmatch(text):
        raise InvalidMerchant(f"{field} may only use letters, digits, spaces and . , ' & ( ) ! + # / @ -")
    return text


def clean_coord(value: Any, *, field: str, limit: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidMerchant(f"{field} must be a number")
    try:
        number = float(value)
    except ValueError:
        raise InvalidMerchant(f"{field} must be a number") from None
    if not math.isfinite(number) or abs(number) > limit:
        raise InvalidMerchant(f"{field} must be between -{limit:g} and {limit:g}")
    return round(number, 6)


def classify_target(raw: Any) -> tuple[str, str]:
    """(kind, normalised target) for a pasted wallet address, Solana Pay URL or x402 URL."""
    if not isinstance(raw, str):
        raise InvalidMerchant("target must be text")
    target = raw.strip()
    if not target or len(target) > URL_MAX or any(unicodedata.category(ch)[0] == "C" or ch.isspace() for ch in target):
        raise InvalidMerchant(f"paste a wallet address, a solana: URL or an https x402 URL (max {URL_MAX} characters, no spaces)")
    if is_address(target):
        return "solana_pay", target
    if target.lower().startswith("solana:"):
        try:
            parse_solana_pay_url(target)
        except ParseError as err:
            raise InvalidMerchant(f"Solana Pay URL rejected: {err}") from None
        return "solana_pay", target
    parts = urlsplit(target)
    if parts.scheme in ("http", "https") and parts.hostname:
        if parts.username or parts.password:
            raise InvalidMerchant("x402 URL must not contain credentials")
        return "x402", target
    raise InvalidMerchant("target must be a Solana wallet address, a solana: URL or an http(s) x402 URL")


def target_identity(target: str | None) -> str | None:
    """What makes two targets the same checkout: the recipient wallet, or the x402 URL."""
    if not target:
        return None
    if target.lower().startswith("solana:"):
        try:
            return parse_solana_pay_url(target)["recipient"]
        except ParseError:
            return target
    return target.rstrip("/").lower()


def build_onboarded(payload: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    """A validated merchant from the onboarding form. Raises InvalidMerchant."""
    if not isinstance(payload, dict):
        raise InvalidMerchant("send a JSON object")
    if len(existing) >= MAX_MERCHANTS:
        raise InvalidMerchant("the map is full for this demo")
    name = clean_text(payload.get("name"), field="name", min_len=NAME_MIN, max_len=NAME_MAX, pattern=_NAME_OK)
    place = clean_text(payload.get("place") or "Cambridge", field="location", min_len=2, max_len=PLACE_MAX, pattern=_NAME_OK)
    lat = clean_coord(payload.get("lat"), field="lat", limit=90)
    lng = clean_coord(payload.get("lng"), field="lng", limit=180)
    kind, target = classify_target(payload.get("target"))
    if kind == "solana_pay" and not target.lower().startswith("solana:"):
        target = solana_pay_url(target, label=name)
    if any(target_identity(m.get("target")) == target_identity(target) for m in existing):
        raise InvalidMerchant("that wallet or URL is already on the map")
    if any(m["name"].lower() == name.lower() for m in existing):
        raise InvalidMerchant("a merchant with that name is already on the map")
    return {
        "id": "m-" + secrets.token_hex(4), "name": name, "lat": lat, "lng": lng, "kind": kind, "target": target,
        "micropay": False, "seed": False, "created_at": now_iso(), "note": "onboarded on the map", "place": place,
    }


def payment_link(merchant: dict[str, Any]) -> str | None:
    """The link a customer would pay with: the Solana Pay URL, or the x402 endpoint."""
    return merchant.get("target")


def _age_s(at: str | None, now: datetime) -> float | None:
    if not at:
        return None
    return (now - datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)).total_seconds()


def pin_status(merchant: dict[str, Any], last: dict[str, Any] | None, now: datetime | None = None) -> str:
    """green verified < 10 min, amber stale or pending, red failing, grey unprobed or physical-only."""
    if merchant.get("kind") == "physical" or last is None:
        return "grey"
    status = last.get("status")
    if status in ("FAIL", "UNREACHABLE"):
        return "red"
    if status == "PENDING":
        return "amber"
    age = _age_s(last.get("at"), now or datetime.now(UTC))
    return "green" if age is not None and age < FRESH_S else "amber"
