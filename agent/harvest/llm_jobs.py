"""Shared plumbing for the background agent jobs (debate, resolution evidence, news-drafted markets).

A `Job` is one daemon thread per process that runs a step every `<NAME>_S` seconds, with a per-process
LLM call cap `<NAME>_MAX_CALLS`. HARVEST_OFFLINE=1 or an interval of 0 disables it. `ask_json` sends one
prompt through the existing Gemini helper (live_forecast.gemini_ask, hard HTTP timeout) and parses the
JSON object in the answer; callers fall back on any exception.
"""

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from . import forecaster

logger = logging.getLogger("agent")
Ask = Callable[[str], str]
DEADLINE_S = 75.0


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def parse_json(text: str) -> dict[str, Any]:
    """The first {...} object in a model answer (fences and prose around it ignored). Raises ValueError."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the answer")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("answer is not a JSON object")
    return data


def default_ask(prompt: str) -> str:
    from . import live_forecast

    return live_forecast.gemini_ask(prompt)


def ask_json(prompt: str, ask: Ask | None = None) -> dict[str, Any]:
    """One LLM call, parsed. Raises on failure, on a late answer, or when HARVEST_OFFLINE=1."""
    if forecaster.offline_env():
        raise RuntimeError("HARVEST_OFFLINE=1: LLM disabled")
    started = time.monotonic()
    text = (ask or default_ask)(prompt)
    if time.monotonic() - started > DEADLINE_S:
        raise TimeoutError("LLM answered after the deadline")
    return parse_json(text or "")


class Job:
    """A capped background loop. `step()` returns nothing; it calls `spend()` before each LLM call."""

    def __init__(self, name: str, *, interval_s: float, max_calls: int, first_delay_s: float = 20.0) -> None:
        self.name = name
        self.env = name.upper()
        self.default_interval = interval_s
        self.default_max = max_calls
        self.first_delay_s = first_delay_s
        self.calls = 0
        self.runs = 0
        self.last_run: float | None = None
        self.last_error: str | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def interval_s(self) -> float:
        return _env_float(f"{self.env}_S", self.default_interval)

    def max_calls(self) -> int:
        return int(_env_float(f"{self.env}_MAX_CALLS", self.default_max))

    def enabled(self) -> bool:
        return not forecaster.offline_env() and self.interval_s() > 0

    def spend(self) -> bool:
        """Takes one LLM call from the budget; False when the cap is reached or the job is offline."""
        with self._lock:
            if forecaster.offline_env() or self.calls >= self.max_calls():
                return False
            self.calls += 1
            return True

    def reset(self) -> None:
        with self._lock:
            self.calls, self.runs, self.last_run, self.last_error = 0, 0, None, None

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled(), "running": bool(self._thread and self._thread.is_alive()), "interval_s": self.interval_s(),
                "llm_calls": self.calls, "max_calls": self.max_calls(), "runs": self.runs, "last_error": self.last_error}

    def run_once(self, step: Callable[[], Any]) -> Any:
        self.runs += 1
        self.last_run = time.time()
        try:
            out = step()
            self.last_error = None
            return out
        except Exception as err:  # noqa: BLE001 - one bad round never stops the loop
            self.last_error = type(err).__name__
            logger.warning("harvest %s step failed: %s: %s", self.name, type(err).__name__, str(err)[:200])
            return None

    def _loop(self, step: Callable[[], Any]) -> None:
        time.sleep(min(self.first_delay_s, max(self.interval_s(), 1.0)))
        while True:
            if self.enabled():
                self.run_once(step)
            time.sleep(max(self.interval_s(), 30.0))

    def ensure(self, step: Callable[[], Any]) -> bool:
        """Starts the loop thread once per process (no-op offline). True when it is running."""
        if not self.enabled():
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._thread = threading.Thread(target=self._loop, args=(step,), name=f"harvest-{self.name}", daemon=True)
            self._thread.start()
        return True
