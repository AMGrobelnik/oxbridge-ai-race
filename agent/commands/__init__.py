"""Command-line commands: `python -m agent <command>`.

Every file in this folder adds its own commands with a `register(commands)` function:

    import argparse
    from agent.commands import Commands

    def register(commands: Commands) -> None:
        cmd = commands.add_parser("hello", help="Say hello")
        cmd.set_defaults(func=run)

    def run(args: argparse.Namespace) -> int:   # may also be `async def`
        print("hello")
        return 0
"""

import argparse
from typing import TypeAlias

Commands: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"
