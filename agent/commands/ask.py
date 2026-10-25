"""python -m agent ask "What is x402?"  ->  prints Gemini's answer."""

import argparse
import sys

from agent.ai.gemini import MissingGeminiKeyError, ask_gemini
from agent.commands import Commands


def register(commands: Commands) -> None:
    cmd = commands.add_parser("ask", help="Ask Google Gemini a question")
    cmd.add_argument("prompt", nargs="+", help="The question")
    cmd.set_defaults(func=run)


async def run(args: argparse.Namespace) -> int:
    try:
        print(await ask_gemini(" ".join(args.prompt)))
    except MissingGeminiKeyError as err:
        print(err, file=sys.stderr)
        return 1
    return 0
