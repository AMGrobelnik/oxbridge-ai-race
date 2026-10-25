"""The protocol plumbing: authentication, the nonce, the envelope and routing by task type.

You shouldn't need to change this file. Your logic goes in handlers.py.
"""

import hmac
import logging
import os
from collections.abc import Mapping
from typing import Any

from .handlers import HANDLERS, is_built
from .protocol import PROTOCOL, TASK_TYPES, TaskError, TaskType

logger = logging.getLogger("agent")

Response = tuple[int, dict[str, Any]]


def app_name() -> str:
    return os.environ.get("AGENT_STUDIO_APP_NAME") or "agent-studio-agent"


def is_verifier(authorization: str | None, env: Mapping[str, str] | None = None) -> bool:
    """True when the request carries this agent's verifier token (from your team page)."""
    token = (env if env is not None else os.environ).get("AGENT_STUDIO_VERIFIER_TOKEN")
    if not token:
        return False
    return hmac.compare_digest((authorization or "").encode(), f"Bearer {token}".encode())


def implemented_task_types() -> list[TaskType]:
    """Task types whose handler you have written (reported in the health response)."""
    return [t for t in TASK_TYPES if is_built(t)]


def handle_health(authorization: str | None, env: Mapping[str, str] | None = None) -> Response:
    env = env if env is not None else os.environ
    if not env.get("AGENT_STUDIO_VERIFIER_TOKEN"):
        return 503, {"protocol": PROTOCOL, "status": "starting", "error": "AGENT_STUDIO_VERIFIER_TOKEN is not set. Copy it from your team page into .env."}
    if not is_verifier(authorization, env):
        return 401, {"protocol": PROTOCOL, "status": "error", "error": {"code": "unauthorized", "message": "unknown caller"}}
    return 200, {"protocol": PROTOCOL, "status": "ready", "name": app_name(), "capabilities": implemented_task_types()}


async def handle_task(authorization: str | None, body: Any, env: Mapping[str, str] | None = None) -> Response:
    request = body if isinstance(body, dict) else {}
    nonce = request.get("nonce") if isinstance(request.get("nonce"), str) else None

    def error(e: TaskError) -> Response:
        return e.status, {"protocol": PROTOCOL, "nonce": nonce, "status": "error", "error": {"code": e.code, "message": e.message}}

    if not is_verifier(authorization, env):
        return error(TaskError("unauthorized", "unknown caller"))
    task = request.get("task")
    if not nonce or not isinstance(task, dict) or task.get("type") not in TASK_TYPES:
        return error(TaskError("invalid_input", f'Expected {{ protocol: "{PROTOCOL}", nonce, task: {{ type, input }} }} with type one of {", ".join(TASK_TYPES)}.'))
    try:
        output = await HANDLERS[task["type"]](task, request)
        return 200, {"protocol": PROTOCOL, "nonce": nonce, "status": "ok", "output": output or {}}
    except TaskError as e:
        return error(e)
    except Exception as e:  # noqa: BLE001 - anything else is reported, not crashed on
        logger.exception("[race] %s handler failed", task["type"])
        return error(TaskError("internal", str(e) or "handler failed"))
