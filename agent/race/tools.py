"""Calling the race's tools and knowledge source.

The tool service records every call: that record, not what you put in toolResults, is how the verifier
knows a tool was really used.
"""

from typing import Any

import httpx

from .protocol import Task


class ToolCallError(Exception):
    def __init__(self, tool: str, http_status: int | None, message: str) -> None:
        super().__init__(message)
        self.tool = tool
        # HTTP status from the tool, or None if it didn't answer (timeout, network).
        self.http_status = http_status


async def call_tool(tool: dict[str, Any], task: Task, tool_input: dict[str, Any], timeout_ms: int | None = None) -> Any:
    """POSTs tool_input to a tool and returns its JSON.

    One try only: deciding when to retry, how long to wait and when to give up is part of building a
    reliable agent, so that's yours to write.
    """
    name = str(tool.get("name", "tool"))
    token = task.get("challengeToken")
    if not token:
        raise ToolCallError(name, None, "The task has no challengeToken.")
    timeout_ms = timeout_ms or int((task.get("limits") or {}).get("timeoutMs") or 10_000)
    try:
        async with httpx.AsyncClient(timeout=timeout_ms / 1000, follow_redirects=False) as client:
            res = await client.post(str(tool["url"]), json=tool_input, headers={"authorization": f"Bearer {token}"})
    except httpx.TimeoutException as e:
        raise ToolCallError(name, None, f"{name} did not answer within {timeout_ms} ms") from e
    except httpx.HTTPError as e:
        raise ToolCallError(name, None, f"{name}: {e}") from e
    if res.status_code >= 400:
        raise ToolCallError(name, res.status_code, f"{name} returned HTTP {res.status_code}: {res.text[:200]}")
    try:
        return res.json()
    except ValueError as e:
        raise ToolCallError(name, res.status_code, f"{name} returned something that isn't JSON") from e


async def search_source(task: Task, query: str, timeout_ms: int | None = None) -> Any:
    """Searches the task's knowledge source. Returns its JSON (documents with ids you can cite)."""
    source = task.get("source")
    if not source:
        raise ToolCallError("source", None, "The task has no source.")
    return await call_tool({"name": "source", "url": source["url"]}, task, {"query": query}, timeout_ms)
