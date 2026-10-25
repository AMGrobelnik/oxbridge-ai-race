"""Time-machine replay over quarterly cutoffs with a sealed holdout (SPEC.md section 8, addendum R2). Owner: agents-replay."""

from pathlib import Path
from typing import Any

from .types import Persona, PriceFixture, QuestionSpec, ReplayResult, Split

DEMO = Path(__file__).resolve().parent / "data" / "demo_replay.json"
FIRST_CUTOFF = "2022-03"
HOLDOUT_FROM = "2025-09"


def cutoffs(fixture: PriceFixture) -> list[str]:
    raise NotImplementedError


def split(cutoff: str, fixture: PriceFixture, specs: list[QuestionSpec]) -> Split:
    raise NotImplementedError


async def run(*, fixture: PriceFixture, specs: list[QuestionSpec], personas: list[Persona], offline: bool = False, anchor: bool = False) -> ReplayResult:
    raise NotImplementedError


def freeze(result: ReplayResult, path: Path = DEMO) -> Path:
    raise NotImplementedError


def load_demo(path: Path = DEMO) -> ReplayResult:
    raise NotImplementedError


async def verify_commitments(result: ReplayResult, fixture: PriceFixture, *, tamper: bool = False) -> dict[str, Any]:
    """{"commitments_ok", "windows_ok", "details": [...]}."""
    raise NotImplementedError
