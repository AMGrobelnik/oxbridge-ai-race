"""Gemini forecaster with masked prompts, cache and deterministic fallback (SPEC.md section 5, addendum R2). Owner: agents-replay."""

from pathlib import Path
from typing import Any

from .types import Forecast, Mask, Persona, PriceFixture, QuestionInstance

CACHE = Path(__file__).resolve().parent / "data" / "llm_cache.json"
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"forecasts": {"type": "array", "items": {"type": "object", "properties": {"qid": {"type": "string"}, "p_yes": {"type": "number"}, "rationale": {"type": "string"}}, "required": ["qid", "p_yes", "rationale"]}}},
    "required": ["forecasts"],
}


def make_mask(series_ids: list[str], seed: int) -> Mask:
    raise NotImplementedError


def mask_window(points: list[tuple[str, float]], cutoff: str) -> list[tuple[str, float]]:
    """Relative months t-11..t0 and prices rebased to 100 at the first point."""
    raise NotImplementedError


def unmask(text: str, mask: Mask) -> str:
    raise NotImplementedError


def build_prompt(persona: Persona, questions: list[QuestionInstance], fixture: PriceFixture, cutoff: str, mask: Mask) -> str:
    raise NotImplementedError


def fallback(persona: Persona, question: QuestionInstance, fixture: PriceFixture, cutoff: str) -> Forecast:
    raise NotImplementedError


async def forecast(persona: Persona, questions: list[QuestionInstance], fixture: PriceFixture, cutoff: str, mask: Mask, *, offline: bool = False) -> list[Forecast]:
    raise NotImplementedError
