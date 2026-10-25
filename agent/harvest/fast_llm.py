"""One fast, short-deadline LLM call (Gemini Flash-Lite by default) for interactive features: scenarios and "why did this move?".

Synchronous, so callers run it in a thread. A private client carries a hard HTTP timeout. FAST_LLM_MODEL overrides the
model; FAST_LLM_MAX_CALLS (default 400 per process) caps spend. Raises on any failure; callers fall back.
"""

import os
import threading
from typing import Any

from . import forecaster

_lock = threading.Lock()
_state: dict[str, Any] = {"client": None, "calls": 0}


def model() -> str:
    return os.environ.get("FAST_LLM_MODEL") or "gemini-2.5-flash-lite"


def max_calls() -> int:
    try:
        return int(os.environ.get("FAST_LLM_MAX_CALLS") or 400)
    except ValueError:
        return 400


def available() -> bool:
    return not forecaster.offline_env() and bool(os.environ.get("GEMINI_API_KEY")) and _state["calls"] < max_calls()


def ask(prompt: str, timeout_s: float = 20.0, *, model_id: str | None = None, thinking_budget: int | None = None) -> str:
    """The model's JSON text for `prompt` (from `model_id`, else `model()`); `thinking_budget=0` turns thinking off."""
    from google import genai
    from google.genai import types

    if not available():
        raise RuntimeError("fast LLM unavailable (offline, no key or budget spent)")
    with _lock:
        _state["calls"] += 1
        if _state["client"] is None:
            _state["client"] = genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        client = _state["client"]
    response = client.models.generate_content(model=model_id or model(), contents=prompt,
                                              config=types.GenerateContentConfig(
                                                  response_mime_type="application/json", temperature=0.4,
                                                  thinking_config=None if thinking_budget is None else types.ThinkingConfig(thinking_budget=thinking_budget)))
    return response.text or ""
