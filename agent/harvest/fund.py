"""Resilience Fund: fees to a devnet treasury, allocated by market-implied risk (SPEC.md section 10). Owner: market-engine."""

from typing import Any

from agent.sealed.hashing import sha256_json

from .types import Allocation, FundState

MEMO_PREFIX = "harvest:v1:fund"


def fund_memo(treasury: float, allocations: list[Allocation]) -> str:
    return f"{MEMO_PREFIX}:{treasury:.2f}:{sha256_json(allocations)[:32]}"


def allocate(open_markets: list[dict[str, Any]], treasury: float, *, fee_rate: float | None = None) -> FundState:
    """open_markets items carry qid, region, crop, adverse_if, p_yes.

    Futarchy-lite: risk_q is the market's adverse probability; risk per (region, crop) is the
    mean; each group gets share = risk / sum(risk) of the treasury."""
    from .market import FEE_RATE

    groups: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for m in open_markets:
        p = float(m["p_yes"])
        risk_q = p if m.get("adverse_if", "yes") == "yes" else 1.0 - p
        groups.setdefault((m.get("region") or "Europe", m.get("crop") or "mixed"), []).append((m["qid"], risk_q))
    risks = {k: sum(r for _, r in v) / len(v) for k, v in groups.items()}
    total = sum(risks.values())
    allocations: list[Allocation] = []
    for (region, crop), risk in sorted(risks.items(), key=lambda kv: -kv[1]):
        share = risk / total if total > 0 else 0.0
        allocations.append({"region": region, "crop": crop, "risk": risk, "share": share, "amount": share * treasury,
                            "questions": [q for q, _ in groups[(region, crop)]]})
    return {"treasury": treasury, "fee_rate": FEE_RATE if fee_rate is None else fee_rate, "allocations": allocations,
            "memo": fund_memo(treasury, allocations), "receipt": None}
