"""Question bank, instantiation at a cutoff and resolution (SPEC.md section 3). Owner: data-questions."""

from pathlib import Path

from .types import PriceFixture, QuestionInstance, QuestionSpec, Resolution

BANK = Path(__file__).resolve().parent / "data" / "questions.json"


def load_bank(path: Path = BANK) -> list[QuestionSpec]:
    raise NotImplementedError


def hard_specs(bank: list[QuestionSpec]) -> list[QuestionSpec]:
    return [s for s in bank if s["kind"] == "hard"]


def soft_specs(bank: list[QuestionSpec]) -> list[QuestionSpec]:
    return [s for s in bank if s["kind"] == "soft"]


def instantiate(spec: QuestionSpec, cutoff: str | None, fixture: PriceFixture) -> QuestionInstance:
    """Hard: qid f"{id}@{cutoff}", base = value at cutoff, target per rule. Soft: qid = id, cutoff None."""
    raise NotImplementedError


def resolve(instance: QuestionInstance, fixture: PriceFixture) -> Resolution:
    """Pure. outcome None when resolve_month is past the fixture or the question is soft."""
    raise NotImplementedError
