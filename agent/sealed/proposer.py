"""Candidate proposers for the loop (SPEC.md section 5). Owner: W1.

A proposer only ever receives a ProposerContext, which has no holdout field.
"""

from typing import Protocol

from .environment import Environment
from .records import Proposal, ProposerContext


class ProposerError(Exception):
    """The proposer could not produce a usable proposal; the harness records INVALID."""


class Proposer(Protocol):
    name: str

    async def propose(self, ctx: ProposerContext) -> Proposal: ...


class GeminiProposer:
    """One ask_gemini(prompt, json_output=True) call per iteration."""

    name = "gemini"

    def build_prompt(self, ctx: ProposerContext) -> str:
        raise NotImplementedError("W1")

    async def propose(self, ctx: ProposerContext) -> Proposal:
        raise NotImplementedError("W1")


class ScriptedProposer:
    """Replays env.demo_trajectory() in order: deterministic, no network."""

    name = "scripted"

    def __init__(self, env: Environment) -> None:
        self.trajectory = env.demo_trajectory()

    async def propose(self, ctx: ProposerContext) -> Proposal:
        raise NotImplementedError("W1")


def make_proposer(kind: str, env: Environment) -> Proposer:
    """"gemini" or "scripted"; ValueError otherwise."""
    raise NotImplementedError("W1")
