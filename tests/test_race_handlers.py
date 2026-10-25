"""Offline tests for the AI Race handlers: Gemini, the tool service and payments are faked.

Run: python -m pytest tests -q
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agent.payments import transfer
from agent.race import adapter, handlers, toolrun
from agent.race.memory import Memory
from agent.race.tools import ToolCallError

TOKEN = "test-verifier-token"
ENV = {"AGENT_STUDIO_VERIFIER_TOKEN": TOKEN}
NONCE = "0123456789abcdef0123"


def run_task(task: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    body = {"protocol": "asrp/1", "nonce": NONCE, "attempt": {"id": "a", "layer": "l", "number": 1}, "task": task}
    return asyncio.run(adapter.handle_task(f"Bearer {TOKEN}", body, ENV))


class FakeModel:
    """Replays scripted replies (str, or anything JSON-serialisable) and records the prompts."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str, *, json_output: bool = False) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply)


class FakeTools:
    """A tool service: answers by tool name, records every call, can fail or time out on demand."""

    def __init__(self, answers: dict[str, Callable[[dict[str, Any]], Any]], fail_first: dict[str, int] | None = None, down: set[str] | None = None, slow: set[str] | None = None) -> None:
        self.answers, self.fail_first, self.down, self.slow = answers, dict(fail_first or {}), down or set(), slow or set()
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    async def __call__(self, tool: dict[str, Any], task: dict[str, Any], tool_input: dict[str, Any], timeout_ms: int | None = None) -> Any:
        name = tool["name"]
        if name in self.slow:
            self.calls.append((name, tool_input, 0))
            raise ToolCallError(name, None, "timed out") from httpx.ReadTimeout("slow")
        if name in self.down or self.fail_first.get(name, 0) > 0:
            self.fail_first[name] = self.fail_first.get(name, 0) - 1
            self.calls.append((name, tool_input, 503))
            raise ToolCallError(name, 503, f"{name} returned HTTP 503")
        result = self.answers[name](tool_input)
        if isinstance(result, int):
            self.calls.append((name, tool_input, result))
            raise ToolCallError(name, result, f"{name} returned HTTP {result}")
        self.calls.append((name, tool_input, 200))
        return result


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(toolrun.asyncio, "sleep", instant)


def use(monkeypatch: pytest.MonkeyPatch, model: FakeModel | None = None, tools: FakeTools | None = None) -> None:
    if model is not None:
        monkeypatch.setattr(handlers, "ask_gemini", model)
    if tools is not None:
        monkeypatch.setattr(toolrun, "call_tool", tools)

        async def search(task: dict[str, Any], query: str, timeout_ms: int | None = None) -> Any:
            return await tools({"name": "search_source", "url": task["source"]["url"]}, task, {"query": query}, timeout_ms)

        monkeypatch.setattr(toolrun, "search_source", search)


def tool_specs(*names: str) -> list[dict[str, Any]]:
    return [{"name": n, "description": n, "url": f"https://tools.test/{n}", "inputSchema": {"type": "object"}} for n in names]


# --- envelope and respond ---------------------------------------------------------------------


def test_respond_answers_in_the_envelope_with_the_nonce(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel(" 112 ")
    use(monkeypatch, model)
    status, body = run_task({"type": "respond", "input": "What is 45 plus 67? Answer with the number."})
    assert status == 200
    assert body == {"protocol": "asrp/1", "nonce": NONCE, "status": "ok", "output": {"text": "112"}}
    assert "45 plus 67" in model.prompts[0]


def test_wrong_token_is_401() -> None:
    status, body = asyncio.run(adapter.handle_task("Bearer nope", {"nonce": NONCE, "task": {"type": "respond", "input": "x"}}, ENV))
    assert status == 401 and body["error"]["code"] == "unauthorized"


def test_every_task_type_is_reported_as_built() -> None:
    assert adapter.implemented_task_types() == list(handlers.HANDLERS)


# --- extract ----------------------------------------------------------------------------------

PERSON = {
    "type": "object",
    "required": ["name", "age", "startDate"],
    "additionalProperties": False,
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}, "startDate": {"type": "string", "format": "date"}},
}


def test_extract_conforms_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeModel({"name": "Mei Chen", "age": "34", "startDate": "2021-03-04", "note": "dropped"}))
    status, body = run_task({"type": "extract", "input": "Mei Chen, 34, started 4 March 2021.", "schema": PERSON})
    assert status == 200
    assert body["output"]["data"] == {"name": "Mei Chen", "age": 34, "startDate": "2021-03-04"}


def test_extract_retries_with_the_schema_problems(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel({"name": "Mei Chen", "age": 34, "startDate": "4 March 2021"}, {"name": "Mei Chen", "age": 34, "startDate": "2021-03-04"})
    use(monkeypatch, model)
    status, body = run_task({"type": "extract", "input": "text", "schema": PERSON})
    assert status == 200 and body["output"]["data"]["startDate"] == "2021-03-04"
    assert "must be a date" in model.prompts[1]


@pytest.mark.parametrize("task", [{"type": "extract", "input": ""}, {"type": "extract", "input": "text"}, {"type": "extract", "input": "", "schema": PERSON}])
def test_extract_rejects_malformed_tasks(task: dict[str, Any]) -> None:
    status, body = run_task(task)
    assert status == 400 and body["error"]["code"] == "invalid_input" and body["nonce"] == NONCE


# --- use_tools --------------------------------------------------------------------------------

RECORDS = {"rec-a": {"id": "rec-a", "amount": 500, "currency": "EUR"}, "rec-b": {"id": "rec-b", "amount": 250, "currency": "EUR"}}


def invoice_tools(**faults: Any) -> FakeTools:
    return FakeTools({"lookup_record": lambda i: RECORDS.get(i["id"], 404), "exchange_rate": lambda i: {"from": i["from"], "to": i["to"], "rate": 0.8123}}, **faults)


INVOICE_PLAN = [
    {"calls": [{"tool": "lookup_record", "input": {"id": "rec-a"}}, {"tool": "lookup_record", "input": {"id": "rec-b"}}]},
    {"calls": [{"tool": "exchange_rate", "input": {"from": "EUR", "to": "GBP"}}, {"tool": "calculate", "input": {"expression": "round((500 + 250) * 0.8123, 2)"}}]},
    {"final": {"text": "609.23 GBP", "data": {"totalGbp": 609.23}}},
]
INVOICE_TASK = {
    "type": "use_tools",
    "input": "Look up records rec-a and rec-b, add them and convert to GBP.",
    "tools": tool_specs("lookup_record", "exchange_rate"),
    "challengeToken": "challenge",
    "schema": {"type": "object", "required": ["totalGbp"], "properties": {"totalGbp": {"type": "number"}}},
}


def test_use_tools_calls_tools_and_reports_only_real_results(monkeypatch: pytest.MonkeyPatch) -> None:
    model, tools = FakeModel(*INVOICE_PLAN), invoice_tools()
    use(monkeypatch, model, tools)
    status, body = run_task(INVOICE_TASK)
    assert status == 200
    assert body["output"]["data"] == {"totalGbp": 609.23}
    assert [r["tool"] for r in body["output"]["toolResults"]] == ["lookup_record", "lookup_record", "exchange_rate"]
    assert [c[0] for c in tools.calls] == ["lookup_record", "lookup_record", "exchange_rate"]
    # The planner saw the calculator's exact result.
    assert "609.23" in model.prompts[2]


def test_use_tools_retries_a_tool_that_fails_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = invoice_tools(fail_first={"lookup_record": 2})
    use(monkeypatch, FakeModel(*INVOICE_PLAN), tools)
    status, body = run_task(INVOICE_TASK)
    assert status == 200 and body["output"]["data"]["totalGbp"] == 609.23
    assert [c[2] for c in tools.calls[:3]] == [503, 503, 200]


def test_use_tools_reports_a_tool_that_stays_down(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = invoice_tools(down={"exchange_rate"})
    use(monkeypatch, FakeModel({"calls": [{"tool": "exchange_rate", "input": {"from": "USD", "to": "GBP"}}]}), tools)
    status, body = run_task({**INVOICE_TASK, "input": "Get the USD to GBP rate."})
    assert status == 503 and body["error"]["code"] == "upstream_unavailable"
    assert len(tools.calls) == toolrun.TRIES


def test_use_tools_gives_up_on_a_slow_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = invoice_tools(slow={"lookup_record"})
    use(monkeypatch, FakeModel({"calls": [{"tool": "lookup_record", "input": {"id": "slow"}}]}), tools)
    status, body = run_task({**INVOICE_TASK, "limits": {"timeoutMs": 5000}})
    assert status == 504 and body["error"]["code"] == "upstream_timeout"
    assert len(tools.calls) == 1


def test_use_tools_rejects_a_task_without_tools() -> None:
    status, body = run_task({"type": "use_tools", "input": {"unexpected": True}})
    assert status == 400 and body["error"]["code"] == "invalid_input"


def test_use_tools_feeds_tool_errors_back_to_the_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel({"calls": [{"tool": "lookup_record", "input": {"id": "missing"}}]}, {"final": {"text": "no such record", "data": {"totalGbp": 0}}})
    use(monkeypatch, model, invoice_tools())
    status, body = run_task(INVOICE_TASK)
    assert status == 200 and body["output"]["toolResults"] == []
    assert "HTTP 404" in model.prompts[1]


# --- answer_from_source -----------------------------------------------------------------------

DOCS = {"results": [{"id": "doc-1", "title": "The seed bank project", "text": "the seed bank project's access code is heron-123."}]}
SOURCE_TASK = {"type": "answer_from_source", "input": "What is the access code for the seed bank project?", "source": {"url": "https://tools.test/source", "description": "Project notes"}, "tools": tool_specs("search_source"), "challengeToken": "challenge"}


def test_answer_from_source_answers_and_cites(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = FakeTools({"search_source": lambda _i: DOCS})
    use(monkeypatch, FakeModel({"answer": "heron-123", "sourceIds": ["doc-1", "doc-invented"]}), tools)
    status, body = run_task(SOURCE_TASK)
    assert status == 200
    assert body["output"]["data"] == {"answer": "heron-123", "sourceIds": ["doc-1"]}
    assert tools.calls[0][0] == "search_source"


def test_answer_from_source_abstains(monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, FakeModel({"answer": None, "sourceIds": []}), FakeTools({"search_source": lambda _i: DOCS}))
    status, body = run_task({**SOURCE_TASK, "input": "What is the access code for the observatory project?"})
    assert status == 200 and body["output"]["data"] == {"answer": None, "sourceIds": []}


def test_answer_from_source_can_search_again(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = FakeTools({"search_source": lambda i: DOCS if i["query"] == "seed bank" else {"results": []}})
    use(monkeypatch, FakeModel({"search": "seed bank"}, {"answer": "heron-123", "sourceIds": ["doc-1"]}), tools)
    status, body = run_task(SOURCE_TASK)
    assert status == 200 and body["output"]["data"]["sourceIds"] == ["doc-1"]
    assert [c[1]["query"] for c in tools.calls] == [SOURCE_TASK["input"], "seed bank"]


# --- converse ---------------------------------------------------------------------------------


def test_converse_keeps_history_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "memory", Memory(env={}))
    model = FakeModel("Noted.", "It is heron-42.", "I don't know.")
    use(monkeypatch, model)
    run_task({"type": "converse", "input": "Our codename is heron-42.", "sessionId": "sess-a"})
    run_task({"type": "converse", "input": "What is our codename?", "sessionId": "sess-a"})
    status, body = run_task({"type": "converse", "input": "What is our codename?", "sessionId": "sess-b"})
    assert status == 200 and body["output"]["text"] == "I don't know."
    assert "heron-42" in model.prompts[1]
    assert "heron-42" not in model.prompts[2]


# --- run_workflow -----------------------------------------------------------------------------

ADDRESS = "Hsw88CEieeGW7HxuXC5SDUYLnNZnfQ8Ew86PLVyHfuZX"
SIGNATURE = "5" * 88


@pytest.fixture
def payments(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def fake_pay(payer: Any, to: str, amount_usdc: float, memo: str | None = None, guard: Any = None, **_: Any) -> str:
        sent.append({"to": to, "amount": amount_usdc, "memo": memo, "guarded": guard is not None})
        return SIGNATURE

    monkeypatch.setattr(transfer, "pay_usdc", fake_pay)
    monkeypatch.setattr(transfer, "load_agent_keypair", lambda: object())
    return sent


def test_run_workflow_pays_with_the_memo(payments: list[dict[str, Any]]) -> None:
    task = {"type": "run_workflow", "input": f'Pay 0.01 devnet USDC to {ADDRESS} with the memo "agent-studio-3f2a1b9c0d". Return {{ "signature": "<transaction signature>" }} in output.data.', "limits": {"maxSpendUsd": 0.01}}
    status, body = run_task(task)
    assert status == 200 and body["output"]["data"] == {"signature": SIGNATURE}
    assert payments == [{"to": ADDRESS, "amount": 0.01, "memo": "agent-studio-3f2a1b9c0d", "guarded": True}]


def test_run_workflow_refuses_to_pay_over_the_limit(payments: list[dict[str, Any]]) -> None:
    status, body = run_task({"type": "run_workflow", "input": f'Pay 5 USDC to {ADDRESS} with the memo "m".', "limits": {"maxSpendUsd": 0.01}})
    assert status == 422 and body["error"]["code"] == "limit_exceeded" and payments == []


def test_run_workflow_reports_an_unfunded_wallet(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unfunded(*_a: Any, **_k: Any) -> str:
        raise transfer.TransferError("no record of a prior credit (the agent wallet has no devnet SOL for fees)")

    monkeypatch.setattr(transfer, "pay_usdc", unfunded)
    monkeypatch.setattr(transfer, "load_agent_keypair", lambda: object())
    status, body = run_task({"type": "run_workflow", "input": f'Pay 0.01 devnet USDC to {ADDRESS} with the memo "m".'})
    assert status == 503 and "devnet SOL" in body["error"]["message"]


def test_parse_payment_from_an_object() -> None:
    assert handlers.parse_payment({"input": {"to": ADDRESS, "amount": "0.02", "memo": "x"}}) == {"to": ADDRESS, "amount": 0.02, "memo": "x"}
    assert handlers.parse_payment({"input": "We need 2 × SENSOR-12."}) is None


def test_run_workflow_plans_orders_within_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    inventory = {"A-1": {"unitPriceUsd": 20}, "B-2": {"unitPriceUsd": 150}}
    tools = FakeTools(
        {
            "check_inventory": lambda i: {"sku": i["sku"], "available": 50, **inventory[i["sku"]]},
            "create_quote": lambda i: {"quoteId": f"q-{i['sku']}-{i['quantity']}", "totalUsd": inventory[i["sku"]]["unitPriceUsd"] * i["quantity"]},
            "place_order": lambda i: {"orderId": f"o-{i['quoteId'][2:]}", "status": "confirmed"},
        }
    )
    model = FakeModel(
        {"calls": [{"tool": "check_inventory", "input": {"sku": "A-1"}}, {"tool": "check_inventory", "input": {"sku": "B-2"}}]},
        {"calls": [{"tool": "create_quote", "input": {"sku": "A-1", "quantity": 2}}, {"tool": "create_quote", "input": {"sku": "B-2", "quantity": 3}}]},
        # The planner tries the over-budget quote too: the guard refuses it before it reaches the tool.
        {"calls": [{"tool": "place_order", "input": {"quoteId": "q-A-1-2"}}, {"tool": "place_order", "input": {"quoteId": "q-B-2-3"}}]},
        {"final": {"text": "done", "data": {"orders": [{"sku": "A-1", "quantity": 2, "status": "placed", "orderId": "o-A-1-2"}, {"sku": "B-2", "quantity": 3, "status": "refused"}]}}},
    )
    use(monkeypatch, model, tools)
    task = {"type": "run_workflow", "input": "We need 2 × A-1 and 3 × B-2, at most $50 per order.", "tools": tool_specs("check_inventory", "create_quote", "place_order"), "challengeToken": "c", "limits": {"maxToolCalls": 12, "maxSpendUsd": 50}, "schema": {"type": "object", "required": ["orders"], "properties": {"orders": {"type": "array"}}}}
    status, body = run_task(task)
    assert status == 200 and body["output"]["data"]["orders"][1]["status"] == "refused"
    assert ("place_order", {"quoteId": "q-B-2-3"}, 200) not in tools.calls
    assert [c[0] for c in tools.calls].count("place_order") == 1
    assert "over the spending limit" in model.prompts[3]


def test_tool_call_limit_stops_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    tools = invoice_tools()
    model = FakeModel({"calls": [{"tool": "lookup_record", "input": {"id": "rec-a"}}] * 3}, {"final": {"text": "stopped", "data": {"totalGbp": 0}}})
    use(monkeypatch, model, tools)
    status, _ = run_task({**INVOICE_TASK, "limits": {"maxToolCalls": 2}})
    assert status == 200 and len(tools.calls) == 2
    assert "tool-call limit of 2 reached" in model.prompts[1]


# --- the calculator ---------------------------------------------------------------------------


def test_calculator_is_exact_and_safe() -> None:
    assert toolrun.calculate("round((523 + 871) * 0.8123, 2)") == 1132.35
    assert toolrun.calculate("sum([1, 2, 3]) * 2") == 12
    with pytest.raises(ValueError):
        toolrun.calculate("__import__('os').system('true')")
