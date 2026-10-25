"""Resilience Fund: fees to a devnet treasury, allocated by market-implied risk (SPEC.md section 10). Owner: market-engine."""

from typing import Any

from .types import FundState

MEMO_PREFIX = "harvest:v1:fund"


def allocate(open_markets: list[dict[str, Any]], treasury: float) -> FundState:
    """open_markets items carry qid, region, crop, adverse_if, p_yes."""
    raise NotImplementedError
