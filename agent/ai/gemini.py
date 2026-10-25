"""Asks Google Gemini a question with the official google-genai SDK.

GEMINI_API_KEY is read from the environment only. Never return it in a response or log it.
"""

import os

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-flash-latest"

_client: genai.Client | None = None


class MissingGeminiKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("GEMINI_API_KEY is not set. Add it to .env and restart the agent.")


def _get_client() -> genai.Client:
    global _client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise MissingGeminiKeyError()
    if _client is None:
        _client = genai.Client(api_key=api_key)
    return _client


async def ask_gemini(prompt: str, *, json_output: bool = False) -> str:
    """Returns the model's text. With json_output=True the model is constrained to answer with JSON."""
    config = types.GenerateContentConfig(response_mime_type="application/json") if json_output else None
    response = await _get_client().aio.models.generate_content(
        model=os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL,
        contents=prompt,
        config=config,
    )
    return response.text or ""
