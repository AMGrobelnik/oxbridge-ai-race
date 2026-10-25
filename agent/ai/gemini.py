"""Asks Google Gemini a question with the official google-genai SDK.

GEMINI_API_KEY is read from the environment only. Never return it in a response or log it.
"""

import asyncio
import os

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

DEFAULT_MODEL = "gemini-flash-latest"
DEFAULT_TIMEOUT_S = 30.0
RETRY_DELAY_S = 0.5

_client: genai.Client | None = None


class MissingGeminiKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("GEMINI_API_KEY is not set. Add it to .env and restart the agent.")


class GeminiUnavailableError(httpx.HTTPError):
    """Gemini timed out or was unreachable after the retry. An httpx.HTTPError so existing fallbacks trigger."""


def _timeout_s(timeout_s: float | None) -> float:
    if timeout_s is not None:
        return timeout_s
    return float(os.environ.get("GEMINI_TIMEOUT_S") or DEFAULT_TIMEOUT_S)


def _get_client() -> genai.Client:
    global _client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise MissingGeminiKeyError()
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


async def ask_gemini(prompt: str, *, json_output: bool = False, timeout_s: float | None = None, model: str | None = None) -> str:
    """Returns the model's text. With json_output=True the model is constrained to answer with JSON.

    Each attempt is cut off after timeout_s (default GEMINI_TIMEOUT_S, else 30). A timeout or transient
    connection/5xx error is retried once, then GeminiUnavailableError is raised. `model` pins a concrete model id
    (else GEMINI_MODEL, else the default).
    """
    limit = _timeout_s(timeout_s)
    config = types.GenerateContentConfig(response_mime_type="application/json") if json_output else None
    client = _get_client()
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    last: Exception | None = None
    for attempt in range(2):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(model=model, contents=prompt, config=config), limit
            )
            return response.text or ""
        except (TimeoutError, httpx.TransportError, genai_errors.ServerError) as exc:
            last = exc
            if attempt == 0:
                await asyncio.sleep(RETRY_DELAY_S)
    raise GeminiUnavailableError(f"Gemini call failed after retry ({type(last).__name__}, timeout {limit}s)") from last
