"""Asks Google Gemini a question with the official google-genai SDK.

GEMINI_API_KEY is read from the environment only. Never return it in a response or log it.
"""

import os

from google import genai

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


async def ask_gemini(prompt: str) -> str:
    response = await _get_client().aio.models.generate_content(
        model=os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL,
        contents=prompt,
    )
    return response.text or ""
