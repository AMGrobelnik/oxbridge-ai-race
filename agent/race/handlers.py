"""Your race logic. The verifier sends one task at a time; each function answers one task type.

Return the output (text, data, toolResults) or raise a TaskError. adapter.py does the rest: checking
the verifier token, echoing the nonce and wrapping your answer in the ASRP/1 envelope.

The handlers are generic: none of them knows the verifier's questions. Gemini answers, extracts and
plans; toolrun.ToolRun makes the tool calls reliable and keeps the honest record of their results;
run_workflow pays on Solana devnet when the task asks for a payment.
"""

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from google.genai import errors as genai_errors

from agent.ai.gemini import MissingGeminiKeyError, ask_gemini

from .memory import memory
from .protocol import Task, TaskError, TaskOutput, TaskType
from .schema import conform, validate
from .toolrun import CALCULATOR, ToolRun

logger = logging.getLogger("agent")

Handler = Callable[[Task, dict[str, Any]], Awaitable[TaskOutput]]
_NOT_BUILT: set[Handler] = set()

MAX_PLAN_STEPS = 8
MAX_SEARCHES = 3


def not_yet(task_type: TaskType) -> Handler:
    """Marks a task type you haven't built yet. The verifier gets a clear error instead of a wrong answer."""

    async def handler(_task: Task, _request: dict[str, Any]) -> TaskOutput:
        raise TaskError("internal", f'This agent doesn\'t handle "{task_type}" tasks yet (HANDLERS["{task_type}"] in agent/race/handlers.py).')

    _NOT_BUILT.add(handler)
    return handler


def is_built(task_type: TaskType) -> bool:
    return HANDLERS[task_type] not in _NOT_BUILT


# --- shared helpers ---------------------------------------------------------------------------


def task_text(task: Task) -> str:
    """task["input"] as text: strings as they are, objects as JSON. Empty when there is no input."""
    value = task.get("input")
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) and value:
        return json.dumps(value, ensure_ascii=False)
    return ""


def require_text(task: Task) -> str:
    text = task_text(task)
    if not text:
        raise TaskError("invalid_input", "task.input is missing or empty.")
    return text


async def ask_model(prompt: str, *, json_output: bool = False) -> str:
    """ask_gemini with one retry, its failures mapped to protocol errors."""
    last: Exception | None = None
    for attempt in range(2):
        try:
            return (await ask_gemini(prompt, json_output=json_output)).strip()
        except MissingGeminiKeyError as e:
            raise TaskError("internal", str(e)) from e
        except (genai_errors.APIError, httpx.HTTPError) as e:
            last = e
            logger.warning("[race] model call failed (try %d): %s", attempt + 1, type(e).__name__)
            await asyncio.sleep(0.5)
    raise TaskError("upstream_unavailable", f"The AI model is unavailable ({type(last).__name__}).")


def parse_json(text: str) -> Any:
    """JSON from a model reply, tolerating code fences and stray prose around one object."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


async def ask_json(prompt: str) -> Any:
    """The model's answer as JSON, asking once more if the first reply isn't valid JSON."""
    for _ in range(2):
        try:
            return parse_json(await ask_model(prompt, json_output=True))
        except json.JSONDecodeError:
            prompt += "\n\nYour previous reply was not valid JSON. Reply with JSON only."
    raise TaskError("internal", "The model did not return valid JSON.")


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --- respond ----------------------------------------------------------------------------------


async def respond(task: Task, _request: dict[str, Any]) -> TaskOutput:
    question = require_text(task)
    prompt = (
        "Answer the request below. Follow any instruction about the form of the answer exactly "
        "(for example, a number only, or a word in capitals). Be correct and concise, with no preamble.\n\n"
        f"Request:\n{question}"
    )
    return {"text": await ask_model(prompt)}


# --- extract ----------------------------------------------------------------------------------


async def extract(task: Task, _request: dict[str, Any]) -> TaskOutput:
    text = task_text(task)
    schema = task.get("schema")
    if not text or not isinstance(schema, dict) or not schema:
        raise TaskError("invalid_input", "An extract task needs task.input (the text) and task.schema (a JSON Schema).")
    prompt = (
        "Extract structured data from the text below as one JSON value that matches the JSON Schema exactly: "
        "every required property, the right types (integers as JSON numbers), no properties the schema does not list. "
        "Copy names and phrases as written in the text. Write dates as YYYY-MM-DD.\n\n"
        f"JSON Schema:\n{dump(schema)}\n\nText:\n{text}"
    )
    problems: list[str] = []
    for _ in range(2):
        data = conform(await ask_json(prompt), schema)
        problems = validate(data, schema)
        if not problems:
            return {"data": data}
        prompt += f"\n\nYour previous answer {dump(data)} did not match the schema: {'; '.join(problems[:5])}. Fix it."
    raise TaskError("internal", f"Could not produce JSON matching the schema: {'; '.join(problems[:3])}")


# --- use_tools and run_workflow (planner + reliable tool calls) -------------------------------


def plan_prompt(task: Task, run: ToolRun, transcript: list[dict[str, Any]], note: str) -> str:
    limits = task.get("limits") or {}
    lines = [
        "You are an agent that completes a task by calling tools. Plan from the tool results only: never invent tool results.",
        "Reply with JSON only, in one of two forms:",
        '  {"calls": [{"tool": "<name>", "input": {...}}, ...]}   to call tools (they run in this order; later calls may need earlier results, so only batch independent calls)',
        '  {"final": {"text": "<short answer>", "data": <the result>}}   when done',
        f'Use the "{CALCULATOR}" tool for every arithmetic step; do not compute in your head.',
        "If a tool returns an error, adapt (correct the input, or report the step as not done). Do not repeat a call that already succeeded.",
    ]
    if run.max_spend is not None:
        lines.append(f"Spending limit: never act on anything (an order, a purchase) whose total is over {run.max_spend} USD; check the price or quote first and report such an item as refused.")
    lines.append(f"Tool-call budget: {run.max_calls - run.calls_made} calls left.")
    if limits:
        lines.append(f"Limits: {dump(limits)}")
    if isinstance(task.get("schema"), dict):
        lines.append(f'"data" must match this JSON Schema: {dump(task["schema"])}')
    lines += ["", f"Tools: {dump(run.specs())}", "", f"Task:\n{task_text(task)}", "", f"Calls so far: {dump(transcript) if transcript else 'none'}"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


async def run_tool_agent(task: Task) -> TaskOutput:
    """Plans and runs tool calls until the model gives a final answer that fits task.schema."""
    run = ToolRun(task)
    transcript: list[dict[str, Any]] = []
    schema = task.get("schema") if isinstance(task.get("schema"), dict) else None
    note = ""
    for _ in range(MAX_PLAN_STEPS):
        plan = await ask_json(plan_prompt(task, run, transcript, note))
        note = ""
        final = plan.get("final") if isinstance(plan, dict) else None
        calls = plan.get("calls") if isinstance(plan, dict) else None
        if isinstance(final, dict):
            data = final.get("data")
            if schema is not None:
                data = conform(data, schema)
                problems = validate(data, schema)
                if problems:
                    note = f"Your final data {dump(data)} did not match the schema: {'; '.join(problems[:5])}. Fix it."
                    continue
            output: TaskOutput = {"text": str(final.get("text") or ""), "toolResults": run.results}
            if data is not None:
                output["data"] = data
            return output
        if not isinstance(calls, list) or not calls:
            note = 'Reply with {"calls": [...]} or {"final": {...}}.'
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
            transcript.append(await run.invoke(str(call.get("tool")), tool_input))
    raise TaskError("limit_exceeded", f"No final answer after {MAX_PLAN_STEPS} planning steps.")


def require_tools(task: Task) -> None:
    tools = task.get("tools")
    if not isinstance(tools, list) or not tools or not task_text(task):
        raise TaskError("invalid_input", "This task needs task.input and at least one tool in task.tools.")
    if not task.get("challengeToken"):
        raise TaskError("invalid_input", "task.challengeToken is missing: the tools can't be called without it.")


async def use_tools(task: Task, _request: dict[str, Any]) -> TaskOutput:
    require_tools(task)
    return await run_tool_agent(task)


# --- answer_from_source -----------------------------------------------------------------------


async def answer_from_source(task: Task, _request: dict[str, Any]) -> TaskOutput:
    question = require_text(task)
    source = task.get("source")
    if not isinstance(source, dict) or not source.get("url"):
        raise TaskError("invalid_input", "An answer_from_source task needs task.source with a url.")
    run = ToolRun(task)
    searches = [await run.search(question)]
    seen_ids: set[str] = set()
    for _ in range(MAX_SEARCHES):
        for entry in searches:
            for doc in (entry.get("result") or {}).get("results") or []:
                if isinstance(doc, dict) and doc.get("id"):
                    seen_ids.add(str(doc["id"]))
        prompt = (
            "Answer the question using only the documents found in the knowledge source below. "
            f"Source: {dump(source.get('description', ''))}.\n"
            "Reply with JSON only, one of:\n"
            '  {"answer": "<the answer, quoting the exact value from the document>", "sourceIds": ["<id of each document you used>"]}\n'
            '  {"answer": null, "sourceIds": []}   if the documents do not contain the answer (never guess, and never answer from a document about a different subject)\n'
            '  {"search": "<a better query>"}   if a different search could find it\n\n'
            f"Question: {question}\n\nSearches so far: {dump(searches)}"
        )
        reply = await ask_json(prompt)
        query = reply.get("search") if isinstance(reply, dict) else None
        if isinstance(query, str) and query.strip() and len(searches) < MAX_SEARCHES:
            searches.append(await run.search(query.strip()))
            continue
        answer = reply.get("answer") if isinstance(reply, dict) else None
        if answer is None or not str(answer).strip():
            return {"text": "The source has no answer to this question.", "data": {"answer": None, "sourceIds": []}, "toolResults": run.results}
        ids = [str(i) for i in reply.get("sourceIds") or [] if str(i) in seen_ids]
        return {"text": str(answer), "data": {"answer": str(answer), "sourceIds": ids}, "toolResults": run.results}
    return {"text": "The source has no answer to this question.", "data": {"answer": None, "sourceIds": []}, "toolResults": run.results}


# --- converse ---------------------------------------------------------------------------------


async def converse(task: Task, _request: dict[str, Any]) -> TaskOutput:
    message = require_text(task)
    session_id = task.get("sessionId") if isinstance(task.get("sessionId"), str) else None
    history = await memory.history(session_id)
    transcript = "\n".join(f"{t['role'].capitalize()}: {t['text']}" for t in history) or "(no earlier turns)"
    prompt = (
        "You are a helpful assistant in an ongoing conversation. Answer general questions from your own knowledge. "
        "For facts about the user or their work (names, codes, places they told you), use only the earlier turns below and quote them exactly "
        "as given; if they never told you, say you don't know. Answer concisely.\n\n"
        f"Earlier turns:\n{transcript}\n\nUser: {message}\nAssistant:"
    )
    answer = await ask_model(prompt)
    await memory.append(session_id, {"role": "user", "text": message}, {"role": "assistant", "text": answer})
    return {"text": answer}


# --- run_workflow -----------------------------------------------------------------------------

_ADDRESS = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
_PAY = re.compile(rf"\bpay\s+(?P<amount>\d+(?:\.\d+)?)\s*(?:devnet\s+)?USDC\b.*?\bto\s+(?P<to>{_ADDRESS})\b", re.IGNORECASE | re.DOTALL)
_MEMO = re.compile(r"\bmemo\b[^\"“'`]*[\"“'`](?P<memo>[^\"”'`]+)[\"”'`]", re.IGNORECASE)


def parse_payment(task: Task) -> dict[str, Any] | None:
    """{to, amount, memo} when the task asks for a USDC payment, from text ("Pay 0.01 devnet USDC to <address>
    with the memo "...".") or from an input object with to/recipient, amount and memo."""
    value = task.get("input")
    if isinstance(value, dict):
        to = value.get("to") or value.get("recipient") or value.get("address")
        amount = value.get("amount") or value.get("amountUsdc")
        if isinstance(to, str) and re.fullmatch(_ADDRESS, to) and amount is not None:
            return {"to": to, "amount": float(amount), "memo": str(value["memo"]) if value.get("memo") else None}
        return None
    if not isinstance(value, str):
        return None
    pay = _PAY.search(value)
    if not pay:
        return None
    memo = _MEMO.search(value)
    return {"to": pay["to"], "amount": float(pay["amount"]), "memo": memo["memo"] if memo else None}


async def pay(task: Task, payment: dict[str, Any]) -> TaskOutput:
    from agent.paidapi.buyer import get_spending_tracker
    from agent.payments import cosign
    from agent.payments.config import PaymentsConfigError
    from agent.payments.transfer import TransferError, load_agent_keypair, pay_usdc

    amount, to, memo = payment["amount"], payment["to"], payment["memo"]
    max_spend = (task.get("limits") or {}).get("maxSpendUsd")
    if amount <= 0:
        raise TaskError("invalid_input", "The payment amount must be positive.")
    if max_spend is not None and amount > float(max_spend) + 1e-9:
        raise TaskError("limit_exceeded", f"Paying {amount} USDC is over the task's spending limit of {max_spend}.")
    if cosign.needs_cosign(amount):
        approval = cosign.queue(to, amount, memo, reason="AI Race run_workflow payment")
        logger.info("[race] %s devnet USDC to %s awaits human co-sign (%s)", amount, to, approval["id"])
        text = f"Awaiting human co-sign: paying {amount} devnet USDC to {to} is over the {cosign.cap_usd():.2f} USDC autonomy cap, so nothing was sent. Approval {approval['id']} is queued."
        return {"text": text, "data": {"status": "awaiting_cosign", "approvalId": approval["id"], "amount": amount, "to": to, "memo": memo, "capUsd": cosign.cap_usd()}}
    try:
        keypair = load_agent_keypair()
        signature = await pay_usdc(keypair, to=to, amount_usdc=amount, memo=memo, guard=get_spending_tracker())
    except PaymentsConfigError as e:
        raise TaskError("internal", str(e)) from e
    except TransferError as e:
        code = "limit_exceeded" if "spending limits" in str(e) else "upstream_unavailable"
        raise TaskError(code, str(e)) from e
    logger.info("[race] paid %s devnet USDC to %s: %s", amount, to, signature)
    return {"text": f"Paid {amount} devnet USDC to {to}.", "data": {"signature": signature}}


async def run_workflow(task: Task, _request: dict[str, Any]) -> TaskOutput:
    payment = parse_payment(task)
    if payment:
        return await pay(task, payment)
    require_tools(task)
    return await run_tool_agent(task)


HANDLERS: dict[TaskType, Handler] = {
    # First Intelligence: answer task["input"] in "text".
    "respond": respond,
    # Structured Outputs: task["input"] as JSON matching task["schema"], in "data"; invalid_input without them.
    "extract": extract,
    # Tool Calling and Reliability: plan calls to task["tools"], retry 503s, time out on slow tools,
    # report tool outages, and return the answer with the real "toolResults".
    "use_tools": use_tools,
    # External Knowledge: search task["source"], answer with the document ids used; answer null when it isn't there.
    "answer_from_source": answer_from_source,
    # Memory and Context: history per task["sessionId"] (memory.py), never shared across sessions.
    "converse": converse,
    # Autonomous Workflow: a devnet USDC payment when the task asks for one (returns {"signature"}),
    # otherwise a planned multi-step tool run within task["limits"].
    "run_workflow": run_workflow,
}
