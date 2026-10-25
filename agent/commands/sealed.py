"""python -m agent sealed run|anchor|verify  (SPEC.md section 12). Owner: W1."""

import argparse

from agent.commands import Commands


def register(commands: Commands) -> None:
    cmd = commands.add_parser("sealed", help="Sealed: pre-registered autoresearch (run, anchor, verify)")
    sub = cmd.add_subparsers(dest="sealed_command", metavar="<run|anchor|verify>")
    run_cmd = sub.add_parser("run", help="Seal, iterate, reveal and anchor one run")
    run_cmd.add_argument("--env", default="epidemic")
    run_cmd.add_argument("--iterations", type=int, default=8)
    run_cmd.add_argument("--proposer", choices=["gemini", "scripted"], default="gemini")
    run_cmd.add_argument("--no-anchor", action="store_true", help="Skip devnet memos (receipts stay pending)")
    run_cmd.add_argument("--freeze", action="store_true", help="Also write agent/sealed/demo/<env>_demo.json")
    anchor_cmd = sub.add_parser("anchor", help="Retry pending devnet memos for a run")
    anchor_cmd.add_argument("run_id")
    anchor_cmd.add_argument("--freeze", action="store_true")
    verify_cmd = sub.add_parser("verify", help="Re-hash a run against its on-chain memos")
    verify_cmd.add_argument("run_id")
    cmd.set_defaults(func=run)


async def run(args: argparse.Namespace) -> int:
    raise NotImplementedError("W1")
