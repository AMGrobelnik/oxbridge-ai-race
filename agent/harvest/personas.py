"""World-model personas (SPEC.md section 4). Owner: agents-replay."""

import json
from pathlib import Path

from .types import Persona

PERSONAS = Path(__file__).resolve().parent / "data" / "personas.json"


def load_personas(path: Path = PERSONAS) -> list[Persona]:
    personas: list[Persona] = json.loads(path.read_text())
    return personas
