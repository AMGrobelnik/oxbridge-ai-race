"""Model registry and one provider-agnostic JSON call for the multi-model house agents.

Every house agent is a persona (world model) run on one registry model. The registry pins each
model by a concrete id and records its documented knowledge cutoff, so the sealed holdout can be
placed after every model's training data: `check_holdout` refuses to run otherwise.

Providers: Gemini direct (agent.ai.gemini), OpenRouter and OpenAI direct (OpenAI-compatible chat
completions over httpx). Keys come from the environment only and are never logged.
"""

import asyncio
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from . import prices

REGISTRY = Path(__file__).resolve().parent / "data" / "models.json"
SEP = "--"
"""Agent id = <persona id>--<model key>."""
ENDPOINTS = {"openrouter": "https://openrouter.ai/api/v1/chat/completions", "openai": "https://api.openai.com/v1/chat/completions"}
CONCURRENCY = {"gemini": int(os.environ.get("HARVEST_GEMINI_CONCURRENCY") or 4),
               "openrouter": int(os.environ.get("HARVEST_OPENROUTER_CONCURRENCY") or 8),
               "openai": int(os.environ.get("HARVEST_OPENAI_CONCURRENCY") or 8)}
ATTEMPTS = 5
BACKOFF_S = 2.0
MAX_TOKENS = 4000
_SEMS: dict[tuple[int, str], asyncio.Semaphore] = {}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class LeakageError(ValueError):
    """A model's knowledge cutoff is not safely before the sealed holdout."""


class ModelUnavailableError(httpx.HTTPError):
    """The provider failed after every retry. An httpx.HTTPError so existing fallbacks trigger."""


@lru_cache(maxsize=1)
def registry() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(REGISTRY.read_text())
    return data


def models() -> list[dict[str, Any]]:
    return list(registry()["models"])


def get(key: str) -> dict[str, Any]:
    for m in models():
        if m["key"] == key:
            return m
    raise KeyError(f"unknown model {key!r}")


def holdout_from() -> str:
    return str(registry()["holdout_from"])


def byo_model() -> str:
    return str(registry()["byo_model"])


def latest_cutoff(rows: list[dict[str, Any]] | None = None) -> str:
    return max(m["knowledge_cutoff"] for m in (rows if rows is not None else models()))


def month_label(month: str) -> str:
    y, m = month.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


def _month(date: str) -> str:
    """'2025-07-31' or '2025-07' -> '2025-07'."""
    return str(date)[:7]


def check_holdout(rows: list[dict[str, Any]] | None = None, start: str | None = None, *, personas: list[dict[str, Any]] | None = None) -> str:
    """Hard guard: every model's knowledge cutoff + 1 month (and every narrative persona's latest source + 1 month)
    must be at or before the holdout start, so every scored question resolves after anything the agents could know."""
    rows = models() if rows is None else rows
    start = holdout_from() if start is None else start
    bad = [f"{m['key']} (cutoff {m['knowledge_cutoff']})" for m in rows if prices.add_months(_month(m["knowledge_cutoff"]), 1) > start]
    bad += [f"persona {p['id']} (sources to {p['knowledge_cutoff']})" for p in personas or []
            if p.get("knowledge_cutoff") and prices.add_months(_month(p["knowledge_cutoff"]), 1) > start]
    if bad:
        raise LeakageError(f"holdout starts {start}, but these may have seen it in training: {', '.join(bad)}")
    return start


def earliest_holdout(rows: list[dict[str, Any]] | None = None, personas: list[dict[str, Any]] | None = None) -> str:
    """The first quarterly cutoff (Mar/Jun/Sep/Dec) at least one month after every cutoff in play."""
    rows = models() if rows is None else rows
    dates = [m["knowledge_cutoff"] for m in rows] + [p["knowledge_cutoff"] for p in personas or [] if p.get("knowledge_cutoff")]
    month = prices.add_months(max(_month(d) for d in dates), 1)
    while int(month[5:7]) % 3:
        month = prices.add_months(month, 1)
    return month


def cutoff_line(rows: list[dict[str, Any]] | None = None) -> str:
    return f"Every scored question resolves after every model's training cutoff (latest: {month_label(latest_cutoff(rows))})."


def public_rows(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    keep = ("key", "label", "family", "provider", "model_id", "knowledge_cutoff", "source")
    return [{k: m.get(k) for k in keep} for m in (models() if rows is None else rows)]


# ---------------------------------------------------------------- agents = persona x model
def agent_id(persona_id: str, model_key: str) -> str:
    return f"{persona_id}{SEP}{model_key}"


def expand(personas: list[dict[str, Any]], keys: list[str] | None = None) -> list[dict[str, Any]]:
    """One agent per (persona, model): same world model, beliefs and heuristic; id, name and model differ."""
    rows = [get(k) for k in keys] if keys else models()
    out = []
    for m in rows:
        for p in personas:
            out.append({**p, "id": agent_id(p["id"], m["key"]), "name": f"{p['name']} · {m['label']}",
                        "persona": p["id"], "persona_name": p["name"], "model": m["key"], "model_label": m["label"]})
    return out


# ---------------------------------------------------------------- one JSON call
def _sem(provider: str) -> asyncio.Semaphore:
    key = (id(asyncio.get_running_loop()), provider)
    if key not in _SEMS:
        _SEMS[key] = asyncio.Semaphore(CONCURRENCY.get(provider, 4))
    return _SEMS[key]


async def _chat(m: dict[str, Any], prompt: str, timeout_s: float) -> str:
    key = os.environ.get(m["env"])
    if not key:
        raise ModelUnavailableError(f"{m['env']} is not set")
    body: dict[str, Any] = {"model": m["model_id"], "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": MAX_TOKENS,
                            **m.get("extra", {})}
    body["response_format"] = {"type": "json_object"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(ENDPOINTS[m["provider"]], json=body, headers={"Authorization": f"Bearer {key}"})
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise ModelUnavailableError(f"{m['key']}: provider error {str(data['error'])[:120]}")
    return str(data["choices"][0]["message"].get("content") or "")


async def _once(m: dict[str, Any], prompt: str, timeout_s: float) -> str:
    if m["provider"] == "gemini":
        from agent.ai.gemini import ask_gemini

        return await ask_gemini(prompt, json_output=True, timeout_s=timeout_s, model=m["model_id"])
    return await _chat(m, prompt, timeout_s)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (408, 409, 425, 429) or exc.response.status_code >= 500
    code = getattr(exc, "code", None)
    if type(exc).__module__.startswith("google") and isinstance(code, int):
        return code in (408, 429) or code >= 500
    return isinstance(exc, (httpx.HTTPError, TimeoutError, KeyError, IndexError, ValueError))


def extract_json(text: str) -> str:
    """The JSON object in a reply, without markdown fences or chatter around it."""
    text = text.strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


async def ask(model_key: str, prompt: str, *, timeout_s: float = 60.0) -> str:
    """The model's text answer (JSON requested). Retries 429/5xx/timeouts with backoff under a per-provider cap."""
    m = get(model_key)
    last: Exception | None = None
    async with _sem(m["provider"]):
        for attempt in range(ATTEMPTS):
            try:
                text = extract_json(await asyncio.wait_for(_once(m, prompt, timeout_s), timeout_s + 5))
                json.loads(text)  # a malformed reply is retried like a transient error
                return text
            except Exception as exc:  # noqa: BLE001 - classified below; anything unexpected is re-raised
                if not _retryable(exc):
                    raise
                last = exc
                if attempt < ATTEMPTS - 1:
                    await asyncio.sleep(BACKOFF_S * (2 ** attempt))
    raise ModelUnavailableError(f"{model_key} failed after {ATTEMPTS} attempts ({type(last).__name__})") from last
