"""Reliable tool use for one task: retries, timeouts, call and spending limits, and the record of
what the tools really returned (reported as toolResults).

    run = ToolRun(task)
    entry = await run.invoke("lookup_record", {"id": "rec-1"})   # {"tool", "input", "result" | "error"}
    run.results                                                    # successful calls, for toolResults

Failures become protocol errors the verifier expects: a tool that keeps answering 503 is
upstream_unavailable, a tool slower than task.limits.timeoutMs is upstream_timeout. Other tool errors
(404, 409, ...) are returned as entries so the planner can react to them.
"""

import ast
import asyncio
import operator
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .protocol import Task, TaskError
from .tools import ToolCallError, call_tool, search_source

#: A local tool every planner gets, so arithmetic is exact instead of guessed by the model.
CALCULATOR = "calculate"
CALCULATOR_SPEC = {
    "name": CALCULATOR,
    "description": "Evaluate an arithmetic expression exactly (+ - * / ** parentheses, round(x, n), sum, min, max, abs). Local: not reported as a tool result.",
    "inputSchema": {"type": "object", "required": ["expression"], "properties": {"expression": {"type": "string"}}},
}

TRIES = 4
RETRY_STATUSES = {429, 500, 502, 503}
DEFAULT_MAX_CALLS = 12
DEFAULT_TIMEOUT_MS = 10_000


class ToolRun:
    def __init__(self, task: Task) -> None:
        self.task = task
        self.tools: dict[str, dict[str, Any]] = {str(t.get("name")): t for t in task.get("tools") or [] if isinstance(t, dict) and t.get("url")}
        limits = task.get("limits") or {}
        self.max_calls = int(limits.get("maxToolCalls") or DEFAULT_MAX_CALLS)
        self.max_spend: float | None = float(limits["maxSpendUsd"]) if limits.get("maxSpendUsd") is not None else None
        self.timeout_ms = int(limits.get("timeoutMs") or DEFAULT_TIMEOUT_MS)
        self.calls_made = 0
        self.results: list[dict[str, Any]] = []

    def specs(self) -> list[dict[str, Any]]:
        """The tools the planner may use, as the task describes them, plus the local calculator."""
        remote = [{k: t.get(k) for k in ("name", "description", "inputSchema")} for t in self.tools.values()]
        return [*remote, CALCULATOR_SPEC]

    async def invoke(self, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        entry: dict[str, Any] = {"tool": name, "input": tool_input}
        if name == CALCULATOR:
            try:
                entry["result"] = calculate(str(tool_input.get("expression", "")))
            except (ValueError, ZeroDivisionError, SyntaxError, TypeError) as e:
                entry["error"] = f"cannot evaluate: {e}"
            return entry
        tool = self.tools.get(name)
        if tool is None:
            entry["error"] = f"no tool named {name}; available: {', '.join(self.tools)}"
            return entry
        refusal = self._over_budget(tool_input)
        if refusal:
            entry["error"] = refusal
            return entry
        outcome = await self._with_retries(name, lambda: call_tool(tool, self.task, tool_input, self.timeout_ms))
        if isinstance(outcome, ToolCallError):
            entry["error"] = str(outcome)
        else:
            entry["result"] = outcome
        return entry

    async def search(self, query: str) -> dict[str, Any]:
        """search_source on the task's knowledge source, with the same retries and record keeping."""
        entry: dict[str, Any] = {"tool": "search_source", "input": {"query": query}}
        outcome = await self._with_retries("search_source", lambda: search_source(self.task, query, self.timeout_ms))
        if isinstance(outcome, ToolCallError):
            entry["error"] = str(outcome)
        else:
            entry["result"] = outcome
        return entry

    async def _with_retries(self, name: str, send: Callable[[], Awaitable[Any]]) -> Any:
        """Result, or a ToolCallError the planner can react to. Raises TaskError for outages and timeouts."""
        for attempt in range(1, TRIES + 1):
            if self.calls_made >= self.max_calls:
                return ToolCallError(name, None, f"tool-call limit of {self.max_calls} reached: stop calling tools and give the final answer")
            self.calls_made += 1
            try:
                result = await send()
            except ToolCallError as e:
                if isinstance(e.__cause__, httpx.TimeoutException):
                    raise TaskError("upstream_timeout", f"{name} did not answer within {self.timeout_ms} ms") from e
                if e.http_status is not None and e.http_status not in RETRY_STATUSES:
                    return e
                if attempt == TRIES:
                    raise TaskError("upstream_unavailable", f"{name} is unavailable ({e}) after {TRIES} tries") from e
                await asyncio.sleep(0.25 * 2 ** (attempt - 1))
                continue
            self.results.append({"tool": name, "result": result})
            return result
        raise TaskError("upstream_unavailable", f"{name} is unavailable")

    def _over_budget(self, tool_input: dict[str, Any]) -> str | None:
        """Refuses a call that acts on an earlier result (a quote, say) whose total is over task.limits.maxSpendUsd."""
        if self.max_spend is None:
            return None
        refs = {v for v in tool_input.values() if isinstance(v, str)}
        for prior in self.results:
            result = prior["result"]
            if not isinstance(result, dict):
                continue
            ids = {v for k, v in result.items() if isinstance(v, str) and k.lower().endswith("id")}
            totals = [v for k, v in result.items() if k.lower().startswith("total") and isinstance(v, (int, float))]
            if refs & ids and totals and max(totals) > self.max_spend:
                return f"refused before calling: {', '.join(sorted(refs & ids))} totals {max(totals)}, over the spending limit of {self.max_spend}"
        return None


_OPS: dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_FUNCS: dict[str, Callable[..., Any]] = {"round": round, "sum": lambda *xs: sum(xs[0] if len(xs) == 1 and isinstance(xs[0], list) else xs), "min": min, "max": max, "abs": abs}


def calculate(expression: str) -> float | int:
    """Safely evaluates arithmetic (no names, attributes or calls other than _FUNCS)."""

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        if isinstance(node, (ast.List, ast.Tuple)):
            return [ev(e) for e in node.elts]
        raise ValueError(f"unsupported expression element {type(node).__name__}")

    if len(expression) > 500:
        raise ValueError("expression too long")
    return ev(ast.parse(expression, mode="eval"))
