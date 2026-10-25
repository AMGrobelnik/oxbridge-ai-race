"""ASRP/1, the Agent Studio Race Protocol: how the race verifier talks to your agent.

Full reference: https://github.com/Db0706/superteam-agent-studio/blob/main/docs/VERIFICATION_SDK.md
"""

from typing import Any, Literal

PROTOCOL = "asrp/1"

TaskType = Literal["respond", "extract", "use_tools", "answer_from_source", "converse", "run_workflow"]
TASK_TYPES: tuple[TaskType, ...] = ("respond", "extract", "use_tools", "answer_from_source", "converse", "run_workflow")

ErrorCode = Literal["invalid_input", "upstream_unavailable", "upstream_timeout", "limit_exceeded", "unauthorized", "internal"]

HTTP_STATUS: dict[str, int] = {
    "invalid_input": 400,
    "unauthorized": 401,
    "limit_exceeded": 422,
    "internal": 500,
    "upstream_unavailable": 503,
    "upstream_timeout": 504,
}

# A task is a dict with: type, input (str or dict), and optionally schema, sessionId, tools
# ([{name, description, url, inputSchema}]), source ({url, description}), challengeToken, limits.
Task = dict[str, Any]
# Your answer: any of text (str), data (structured, matching task["schema"]), toolResults ([{tool, result}]).
TaskOutput = dict[str, Any]


class TaskError(Exception):
    """Raise from a handler to answer with a protocol error. The right HTTP status is chosen for you."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]
