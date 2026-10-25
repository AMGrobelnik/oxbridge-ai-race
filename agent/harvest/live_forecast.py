"""Live re-forecasting: house world models update their beliefs on open live markets with Gemini.

Every LIVE_REFORECAST_S seconds (default 240) `maybe_reforecast`, called from `LiveBook.house_step`
(which already runs off the event loop), starts one daemon thread that asks the next persona
(round-robin) to re-forecast every open live market (hard and soft) in one batched Gemini call.
The persona sees its world model, the latest price windows, the NOAA climate context, the
current market prices and the latest relevant news headlines per market (agent/harvest/news.py).
The answers replace that persona's forecasts in `book.agent_forecasts`, so `house_step` trades
toward the new beliefs. Any failure keeps the old forecasts.

HARVEST_OFFLINE=1, LIVE_REFORECAST_S=0 or a spent call budget (LIVE_REFORECAST_MAX_CALLS,
default 120 per process) disables the LLM. Climate context and news are used here only; the
sealed replay stays prices-only.
"""

import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import climate, forecaster, news, prices
from .types import Persona, QuestionInstance

if TYPE_CHECKING:
    from .market import LiveBook

TIMEOUT_S = 60.0
FIRST_DELAY_S = 30.0
WINDOW_MONTHS = 13
NEWS_PER_MARKET = 6

logger = logging.getLogger("agent")

_lock = threading.Lock()
_state: dict[str, Any] = {"next": 0, "due": None, "running_since": None, "calls": 0}
_cache: dict[str, Any] = {}
_client: Any = None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def interval_s() -> float:
    return _env_float("LIVE_REFORECAST_S", 240.0)


def max_calls() -> int:
    return int(_env_float("LIVE_REFORECAST_MAX_CALLS", 120))


def enabled() -> bool:
    return not forecaster.offline_env() and interval_s() > 0 and _state["calls"] < max_calls()


def reset() -> None:
    with _lock:
        _state.update({"next": 0, "due": None, "running_since": None, "calls": 0})


def _fixture() -> Any:
    if "prices" not in _cache:
        try:
            _cache["prices"] = prices.load_fixture()
        except (OSError, ValueError, KeyError, TypeError):
            _cache["prices"] = None
    return _cache["prices"]


def _climate_lines() -> list[str]:
    if "climate" not in _cache:
        _cache["climate"] = climate.context_lines(climate.load_climate())
    return list(_cache["climate"])


def open_questions(book: "LiveBook") -> list[QuestionInstance]:
    with book.lock:
        return [book.meta[q] for q, m in book.markets.items() if m["status"] == "open" and q in book.meta]


def build(book: "LiveBook", persona: Persona) -> tuple[str, list[QuestionInstance]]:
    """The live prompt for `persona` over all open live markets, and the questions in Q1..Qn order."""
    questions = open_questions(book)
    fixture = _fixture()
    windows: dict[str, list[tuple[str, float]]] = {}
    for q in questions:
        sid = q.get("series")
        if sid and fixture and sid in fixture["series"] and sid not in windows:
            pts = fixture["series"][sid]["points"]
            windows[sid] = prices.window(fixture, sid, pts[-1][0], WINDOW_MONTHS) if pts else []
    with book.lock:
        prices_now = {q["qid"]: book.price(q["qid"]) for q in questions}
    by_spec = news.headlines_for([str(q.get("spec_id") or q["qid"]) for q in questions], per_spec=NEWS_PER_MARKET)
    headlines = {q["qid"]: by_spec[s] for q in questions if (s := str(q.get("spec_id") or q["qid"])) in by_spec}
    prompt = forecaster.build_live_prompt(persona, questions, windows=windows, prices_now=prices_now, climate_lines=_climate_lines(),
                                          today=datetime.now(UTC).strftime("%Y-%m-%d"), news=headlines)
    return prompt, questions


def gemini_ask(prompt: str) -> str:
    """One synchronous Gemini call with a hard HTTP timeout, on a client private to this thread pool."""
    global _client
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    if _client is None:
        _client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(TIMEOUT_S * 1000)))
    model = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"
    response = _client.models.generate_content(
        model=model, contents=prompt, config=types.GenerateContentConfig(response_mime_type="application/json"))
    return response.text or ""


def reforecast_once(book: "LiveBook", persona: Persona, ask: Callable[[str], str] = gemini_ask) -> int:
    """Asks once and stores the parsed forecasts as `persona`'s live forecasts. Returns how many were updated.
    Raises on any failure (the caller falls back to the old forecasts)."""
    prompt, questions = build(book, persona)
    if not questions:
        return 0
    started = time.monotonic()
    text = ask(prompt)
    if time.monotonic() - started > TIMEOUT_S:
        raise TimeoutError("live re-forecast came back after the deadline")
    parsed = forecaster._parse(text, len(questions))
    rows = [(q["qid"], parsed[f"Q{i}"][0], parsed[f"Q{i}"][1]) for i, q in enumerate(questions, start=1) if f"Q{i}" in parsed]
    if not rows:
        raise ValueError("no forecasts parsed")
    return book.set_live_forecasts(persona["id"], rows)


def _run(book: "LiveBook", persona: Persona, ask: Callable[[str], str]) -> None:
    try:
        n = reforecast_once(book, persona, ask)
        logger.info("harvest live re-forecast: %s updated %d markets", persona["id"], n)
    except Exception as err:  # noqa: BLE001 - any failure keeps the old forecasts
        logger.warning("harvest live re-forecast for %s failed, keeping old forecasts: %s", persona["id"], type(err).__name__)
    finally:
        with _lock:
            _state["running_since"] = None


def maybe_reforecast(book: "LiveBook", *, now: float | None = None, ask: Callable[[str], str] = gemini_ask, wait: bool = False) -> bool:
    """Starts one background re-forecast if one is due. Never blocks the caller (unless `wait`, for tests)."""
    if not enabled() or not book.personas:
        return False
    now = time.monotonic() if now is None else now
    with _lock:
        if _state["due"] is None:
            _state["due"] = now + min(FIRST_DELAY_S, interval_s())
        running = _state["running_since"]
        if (running is not None and now - running < 3 * TIMEOUT_S) or now < _state["due"]:
            return False
        persona = book.personas[_state["next"] % len(book.personas)]
        _state["next"] += 1
        _state["calls"] += 1
        _state["due"] = now + interval_s()
        _state["running_since"] = now
    t = threading.Thread(target=_run, args=(book, persona, ask), name="harvest-reforecast", daemon=True)
    t.start()
    if wait:
        t.join(TIMEOUT_S + 5)
    return True
