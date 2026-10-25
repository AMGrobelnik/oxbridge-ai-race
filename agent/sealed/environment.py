"""Loads environment plug-ins from agent/sealed/envs/<name>/ (SPEC.md section 4).

A plug-in package provides sim.py, eval.py and holdout.json (sealed), plus handbook.md and
demo_trajectory.json (not sealed). This module is the only place that knows the file names.
"""

import copy
import importlib
import json
import pkgutil
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from . import envs
from .records import BASE_SHIFT, Candidate, EnvPublicView, EvalSpec, Holdout, Proposal, ScoreReport, Shift

ENVS_DIR = Path(envs.__file__).parent
SEALED_FILES = ("sim.py", "eval.py")


class UnknownEnvironmentError(LookupError):
    pass


@dataclass(frozen=True)
class Environment:
    name: str
    directory: Path
    sim: ModuleType
    evaluator: ModuleType

    @property
    def title(self) -> str:
        return str(self.sim.TITLE)

    @property
    def goal(self) -> str:
        return str(self.sim.GOAL)

    @cached_property
    def spec(self) -> EvalSpec:
        return cast(EvalSpec, self.evaluator.SPEC)

    @property
    def candidate_schema(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.sim.CANDIDATE_SCHEMA)

    def baseline(self) -> Candidate:
        """The naive starting candidate (a fresh copy every call)."""
        return copy.deepcopy(self.sim.baseline())

    def validate(self, candidate: Candidate) -> list[str]:
        """Schema and range errors; an empty list means the candidate is safe to simulate."""
        return list(self.sim.validate(candidate))

    def evaluate(self, candidate: Candidate, *, seeds: list[int], shift: Shift) -> ScoreReport:
        return cast(ScoreReport, self.evaluator.evaluate(candidate, seeds=seeds, shift=shift))

    def dev_report(self, candidate: Candidate) -> ScoreReport:
        """Scores on the dev seeds under the base scenario: the only score the loop sees."""
        return self.evaluate(candidate, seeds=list(self.spec["dev_seeds"]), shift=BASE_SHIFT)

    def handbook(self) -> str:
        path = self.directory / "handbook.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def demo_trajectory(self) -> list[Proposal]:
        path = self.directory / "demo_trajectory.json"
        return cast(list[Proposal], json.loads(path.read_text(encoding="utf-8"))) if path.exists() else []

    def load_holdout(self) -> Holdout:
        """Sealed: only prereg.py and the harness reveal step may call this."""
        return cast(Holdout, json.loads((self.directory / "holdout.json").read_text(encoding="utf-8")))

    def code_files(self) -> list[Path]:
        """The files whose bytes make up code_sha256, in hashing order."""
        return [self.directory / name for name in SEALED_FILES]

    def public_view(self) -> EnvPublicView:
        return {
            "name": self.name,
            "title": self.title,
            "goal": self.goal,
            "metric_label": self.spec["metric_label"],
            "candidate_schema": self.candidate_schema,
            "dev_seeds": list(self.spec["dev_seeds"]),
        }


def list_environments() -> list[str]:
    return sorted(m.name for m in pkgutil.iter_modules(envs.__path__) if m.ispkg and not m.name.startswith("_"))


def load_environment(name: str) -> Environment:
    if name not in list_environments():
        raise UnknownEnvironmentError(f"Unknown environment {name!r}. Available: {', '.join(list_environments()) or 'none'}.")
    sim = importlib.import_module(f"{envs.__name__}.{name}.sim")
    evaluator = importlib.import_module(f"{envs.__name__}.{name}.eval")
    return Environment(name=name, directory=ENVS_DIR / name, sim=sim, evaluator=evaluator)
