"""Your race logic. The verifier sends one task at a time; each function answers one task type.

Return the output (text, data, toolResults) or raise a TaskError. adapter.py does the rest: checking
the verifier token, echoing the nonce and wrapping your answer in the ASRP/1 envelope.

Each race layer tests a capability, and building it is the challenge. The race page shows each
layer's instructions and what must work. Use any AI model you like.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import Task, TaskError, TaskOutput, TaskType

# from .tools import ToolCallError, call_tool, search_source
# If you added the Gemini module: from agent.ai.gemini import ask_gemini

Handler = Callable[[Task, dict[str, Any]], Awaitable[TaskOutput]]
_NOT_BUILT: set[Handler] = set()


def not_yet(task_type: TaskType) -> Handler:
    """Marks a task type you haven't built yet. The verifier gets a clear error instead of a wrong answer."""

    async def handler(_task: Task, _request: dict[str, Any]) -> TaskOutput:
        raise TaskError("internal", f'This agent doesn\'t handle "{task_type}" tasks yet (HANDLERS["{task_type}"] in agent/race/handlers.py).')

    _NOT_BUILT.add(handler)
    return handler


def is_built(task_type: TaskType) -> bool:
    return HANDLERS[task_type] not in _NOT_BUILT


HANDLERS: dict[TaskType, Handler] = {
    # First Intelligence: answer task["input"] (a question) with your AI model, in "text".
    #   async def respond(task, _request): return {"text": await ask_gemini(str(task["input"]))}
    "respond": not_yet("respond"),
    # Structured Outputs: turn task["input"] into JSON matching task["schema"] exactly, in "data".
    # Missing input or schema? raise TaskError("invalid_input", "...").
    "extract": not_yet("extract"),
    # Tool Calling and Reliability: decide which of task["tools"] to call (call_tool), use their results
    # in your answer and report them in "toolResults". Map tool failures to upstream_unavailable /
    # upstream_timeout.
    "use_tools": not_yet("use_tools"),
    # External Knowledge: answer from task["source"] (search_source) and cite the document ids you used.
    "answer_from_source": not_yet("answer_from_source"),
    # Memory and Context: remember earlier turns for task["sessionId"], and only for that session.
    # Use `memory` from .memory (history(session_id) and append(session_id, *turns)): with Upstash
    # Redis configured it survives serverless hosting, where a plain variable forgets between turns.
    "converse": not_yet("converse"),
    # Autonomous Workflow: plan and run several tool calls toward a goal, within task["limits"].
    # The on-chain payment stage also sends a run_workflow task ("Pay 0.01 devnet USDC to <address>
    # with the memo …"). With the payments module: await pay_usdc(load_agent_keypair(), to=..., amount_usdc=..., memo=...)
    # from agent.payments.transfer, then return {"data": {"signature": signature}}.
    "run_workflow": not_yet("run_workflow"),
}
