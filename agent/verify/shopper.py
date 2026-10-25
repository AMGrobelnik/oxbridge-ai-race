"""The mystery-shopper agent: Gemini picks each merchant's probe and writes a one-line verdict.

Both calls are batched (one per round), capped by a short timeout, and fall back to rules when
Gemini is missing, slow or answers with something unusable, so a round never waits on the LLM.
"""

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger("agent")

GEMINI_TIMEOUT_S = 12.0
MICROPAY_EVERY_S = 1800
VERDICT_MAX = 140


def allowed_probes(merchant: dict[str, Any], micropay_ok: bool) -> list[str]:
    kind = merchant.get("kind")
    if kind == "x402":
        return ["x402"]
    if kind == "solana_pay":
        return ["micropay", "solana_pay"] if merchant.get("micropay") and micropay_ok else ["solana_pay"]
    return []


def rule_plan(merchant: dict[str, Any], micropay_ok: bool) -> str | None:
    options = allowed_probes(merchant, micropay_ok)
    return options[0] if options else None


def rule_verdict(name: str, result: dict[str, Any]) -> str:
    status, probe = result["status"], result["probe"]
    reason = result.get("reason", "")
    if status == "PASS":
        if probe == "x402":
            offer = (result.get("detail") or {}).get("offer") or {}
            return f"{name}'s paywall quoted {offer.get('amount_label', 'a price')} on {offer.get('network_label', 'Solana')}: checkout works."
        if probe == "micropay":
            return f"Bought a 0.01 USDC test item from {name}; the payment confirmed on devnet."
        return f"{name}'s Solana Pay link points at a wallet that can receive USDC: payable."
    if status == "PENDING":
        return f"Tried a 0.01 USDC test purchase at {name}; it is {reason.removeprefix('pending on-chain: ') or 'pending'}, retrying next round."
    if status == "UNREACHABLE":
        return f"Couldn't reach {name}'s checkout ({reason}); customers would hit the same wall."
    return f"{name}'s checkout failed: {reason}."


def _clip(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    line = " ".join(text.split())
    if not line:
        return None
    return line if len(line) <= VERDICT_MAX else line[: VERDICT_MAX - 1].rstrip() + "…"


async def _ask(prompt: str) -> Any:
    if not os.environ.get("GEMINI_API_KEY") or os.environ.get("VERIFY_GEMINI", "1") == "0":
        raise RuntimeError("Gemini disabled")
    from agent.ai.gemini import ask_gemini

    text = await asyncio.wait_for(ask_gemini(prompt, json_output=True), GEMINI_TIMEOUT_S)
    return json.loads(text)


async def plan(merchants: list[dict[str, Any]], micropay_ok: dict[str, bool]) -> tuple[dict[str, str], str]:
    """{merchant_id: probe_type} for every probeable merchant, and who decided ("gemini" or "rules")."""
    rules = {m["id"]: p for m in merchants if (p := rule_plan(m, micropay_ok.get(m["id"], False)))}
    choices = {m["id"]: allowed_probes(m, micropay_ok.get(m["id"], False)) for m in merchants if m["id"] in rules}
    if not choices:
        return rules, "rules"
    listing = [{"id": m["id"], "kind": m["kind"], "target_scheme": (m.get("target") or "").split(":")[0], "opted_in_test_purchase": bool(m.get("micropay")), "allowed": choices[m["id"]]} for m in merchants if m["id"] in choices]
    prompt = (
        "You are a mystery-shopper agent auditing crypto merchants on a map. For each merchant pick exactly one probe "
        "from its 'allowed' list: x402 = request the paid endpoint and check it answers HTTP 402 with a payment offer; "
        "solana_pay = check on Solana devnet that the recipient wallet can receive USDC; micropay = make a real 0.01 "
        "USDC test purchase (only for merchants that opted in; prefer it when allowed, it is the strongest evidence). "
        'Answer JSON only: {"<id>": "<probe>", ...}.\nMerchants: ' + json.dumps(listing)
    )
    try:
        answer = await _ask(prompt)
    except Exception as err:  # Gemini is optional: any failure falls back to the rules
        logger.info("sealed-verify plan: rules fallback (%s)", type(err).__name__)
        return rules, "rules"
    if not isinstance(answer, dict):
        return rules, "rules"
    picked = {mid: (answer[mid] if answer.get(mid) in options else rules[mid]) for mid, options in choices.items()}
    return picked, "gemini"


async def verdicts(results: list[tuple[str, dict[str, Any]]]) -> tuple[list[str], str]:
    """One human-readable line per (merchant name, probe result), and who wrote them."""
    fallback = [rule_verdict(name, r) for name, r in results]
    if not results:
        return fallback, "rules"
    listing = [{"i": i, "merchant": name, "probe": r["probe"], "status": r["status"], "reason": r["reason"]} for i, (name, r) in enumerate(results)]
    prompt = (
        "You are a mystery shopper who just tested these crypto merchants' checkouts. For each result write ONE plain "
        "sentence (max 120 characters) a customer looking at a map would understand: say whether paying works and why. "
        "Do not invent facts beyond the reason given. Statuses: PASS works, FAIL broken, UNREACHABLE no answer, PENDING "
        'payment not settled yet. Answer JSON only: {"verdicts": ["...", ...]} in the same order.\nResults: ' + json.dumps(listing)
    )
    try:
        answer = await _ask(prompt)
        lines = answer.get("verdicts") if isinstance(answer, dict) else None
    except Exception as err:  # Gemini is optional: any failure falls back to the rules
        logger.info("sealed-verify verdicts: rules fallback (%s)", type(err).__name__)
        return fallback, "rules"
    if not isinstance(lines, list) or len(lines) != len(results):
        return fallback, "rules"
    cleaned = [_clip(line) or fallback[i] for i, line in enumerate(lines)]
    return cleaned, "gemini"
