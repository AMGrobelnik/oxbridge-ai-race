"""Candidate proposers for the loop (SPEC.md section 5). Owner: W1.

A proposer only ever receives a ProposerContext, which has no holdout field.
"""

import copy
import json
import re
from typing import Any, Protocol

from .environment import Environment
from .records import Proposal, ProposerContext

STRATEGIES = ("continue", "pivot", "widen")
HISTORY_WINDOW = 6

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class ProposerError(Exception):
    """The proposer could not produce a usable proposal; the harness records INVALID."""


class Proposer(Protocol):
    name: str

    async def propose(self, ctx: ProposerContext) -> Proposal: ...


def _fmt_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def parse_proposal(text: str) -> Proposal:
    """Parses a model reply into a Proposal; raises ProposerError on anything unusable."""
    raw = (text or "").strip()
    fenced = _FENCE_RE.match(raw)
    if fenced:
        raw = fenced.group(1)
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise ProposerError("proposer reply contained no JSON object")
        raw = raw[start : end + 1]
    try:
        data: Any = json.loads(raw)
    except ValueError as exc:
        raise ProposerError(f"proposer reply was not valid JSON: {exc}") from exc
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ProposerError("proposer reply was not a JSON object")
    candidate = data.get("candidate")
    if not isinstance(candidate, dict):
        raise ProposerError("proposer reply has no 'candidate' object")
    hypothesis = str(data.get("hypothesis") or "").strip()
    if not hypothesis:
        raise ProposerError("proposer reply has no 'hypothesis'")
    strategy = str(data.get("strategy") or "continue").strip().lower()
    if strategy not in STRATEGIES:
        strategy = "continue"
    return {
        "hypothesis": hypothesis.splitlines()[0][:400],
        "rationale": str(data.get("rationale") or "").strip()[:2000],
        "strategy": strategy,
        "candidate": candidate,
    }


class GeminiProposer:
    """One ask_gemini(prompt, json_output=True) call per iteration."""

    name = "gemini"

    def build_prompt(self, ctx: ProposerContext) -> str:
        report = ctx["champion_report"]
        history = ctx["history"][-HISTORY_WINDOW:]
        history_lines = [
            f"- iter {h['index']}: {h['decision']} (dev score {_fmt_score(h['dev_score'])}): {h['hypothesis']}" for h in history
        ] or ["- (no experiments yet: only the baseline has been scored)"]
        breakdown = json.dumps(report.get("breakdown", {}), indent=2, sort_keys=True)
        return "\n".join(
            [
                "You are the proposer in an automated research loop. Each turn you run ONE experiment:",
                "state a single falsifiable hypothesis about what will raise the score, then submit the",
                "full candidate that tests it. The harness scores it on a fixed development set and keeps",
                "it only if it beats the current champion; otherwise it is reverted.",
                "",
                "Ground rules:",
                "- Change one idea at a time so the result tells you something; small, targeted edits beat rewrites.",
                "- Learn from the history: do not repeat a reverted idea unchanged; build on what was kept.",
                "- If several attempts in a row were reverted, pivot to a different lever; if gains are",
                "  flattening, widen the search to an unexplored region.",
                "- Prefer candidates that should hold up under conditions you have not seen, not just the",
                "  development set: a later, unseen test will check robustness.",
                "- The candidate must satisfy the JSON schema exactly (every required field, valid ranges).",
                "",
                f"GOAL: {ctx['goal']}",
                f"METRIC (higher is better): {ctx['metric_label']}",
                f"ITERATION: {ctx['iteration']} of {ctx['iterations_total']}",
                "",
                "DOMAIN HANDBOOK:",
                ctx["handbook"].strip() or "(none)",
                "",
                "CANDIDATE JSON SCHEMA:",
                json.dumps(ctx["candidate_schema"], indent=2, sort_keys=True),
                "",
                f"CURRENT CHAMPION (dev score {_fmt_score(report.get('score'))}):",
                json.dumps(ctx["champion"], indent=2, sort_keys=True),
                "",
                "CHAMPION SCORE BREAKDOWN (mean over dev seeds):",
                breakdown,
                "",
                f"RECENT HISTORY (last {HISTORY_WINDOW}):",
                *history_lines,
                "",
                "Reply with ONE JSON object and nothing else:",
                '{"hypothesis": "<one line, falsifiable: changing X will raise the score because Y>",',
                ' "rationale": "<2-4 sentences grounded in the breakdown and the history>",',
                ' "strategy": "continue" | "pivot" | "widen",',
                ' "candidate": { <the FULL candidate, every field, not a diff> }}',
            ]
        )

    async def propose(self, ctx: ProposerContext) -> Proposal:
        from agent.ai.gemini import MissingGeminiKeyError, ask_gemini

        try:
            text = await ask_gemini(self.build_prompt(ctx), json_output=True)
        except MissingGeminiKeyError as exc:
            raise ProposerError("GEMINI_API_KEY is not set") from exc
        except Exception as exc:  # noqa: BLE001 - any SDK/network failure is one INVALID iteration
            raise ProposerError(f"Gemini call failed: {type(exc).__name__}") from exc
        return parse_proposal(text)


class ScriptedProposer:
    """Replays env.demo_trajectory() in order: deterministic, no network."""

    name = "scripted"

    def __init__(self, env: Environment) -> None:
        self.trajectory = env.demo_trajectory()

    async def propose(self, ctx: ProposerContext) -> Proposal:
        i = ctx["iteration"] - 1
        if i < 0 or i >= len(self.trajectory):
            raise ProposerError(f"scripted trajectory has {len(self.trajectory)} steps; iteration {ctx['iteration']} is out of range")
        step = self.trajectory[i]
        try:
            return {
                "hypothesis": str(step["hypothesis"]),
                "rationale": str(step.get("rationale", "")),
                "strategy": str(step.get("strategy", "continue")),
                "candidate": copy.deepcopy(step["candidate"]),
            }
        except (KeyError, TypeError) as exc:
            raise ProposerError(f"scripted step {ctx['iteration']} is malformed: {exc}") from exc


def make_proposer(kind: str, env: Environment) -> Proposer:
    """"gemini" or "scripted"; ValueError otherwise."""
    if kind == "gemini":
        return GeminiProposer()
    if kind == "scripted":
        return ScriptedProposer(env)
    raise ValueError(f"Unknown proposer {kind!r}: expected 'gemini' or 'scripted'")
