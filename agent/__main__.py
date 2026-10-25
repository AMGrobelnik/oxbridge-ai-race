"""The agent's command line: `python -m agent <command>`.

Run it without a command to see every command and feature in this project.
"""

import argparse
import asyncio
import inspect
import sys

from . import commands
from .config import get_port, load_env
from .modules import modules
from .plugins import load_plugins


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent", description="Your Superteam agent.")
    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    serve = subcommands.add_parser("serve", help="Run the HTTP service on http://localhost:PORT (default 3000)")
    serve.add_argument("--reload", action="store_true", help="Restart when you save a file (for development)")
    serve.set_defaults(func=serve_http)

    # Every file in agent/commands adds its own commands.
    for plugin in load_plugins(commands):
        plugin.register(subcommands)
    return parser


def serve_http(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    # 127.0.0.1 = only this computer can connect. Set HOST=0.0.0.0 when you deploy.
    host = os.environ.get("HOST") or "127.0.0.1"
    uvicorn.run("agent.server:app", host=host, port=get_port(), reload=args.reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_env()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        print("\nFeatures in this project:")
        for module in modules or [{"id": "base", "name": "Base agent", "summary": "No modules yet."}]:
            print(f"  - {module['name']}: {module['summary']}")
        return 0
    result = args.func(args)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return int(result or 0)


if __name__ == "__main__":
    sys.exit(main())
