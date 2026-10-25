"""python -m agent race respond "What is 6 x 7?"  ->  sends one task to your running agent, like the verifier."""

import argparse
import json
import os
import secrets
import sys
import time

import httpx

from agent.commands import Commands


def register(commands: Commands) -> None:
    cmd = commands.add_parser("race", help="Send one race task to your running agent (python -m agent serve)")
    cmd.add_argument("type", nargs="?", default="respond", help="Task type, e.g. respond")
    cmd.add_argument("input", nargs="*", help="The task input")
    cmd.add_argument("--url", default=os.environ.get("RACE_APP_URL", "http://localhost:3000"), help="Your agent's URL")
    cmd.set_defaults(func=run)


async def run(args: argparse.Namespace) -> int:
    token = os.environ.get("AGENT_STUDIO_VERIFIER_TOKEN")
    if not token:
        print("AGENT_STUDIO_VERIFIER_TOKEN is not set. Copy it from your team page into .env.", file=sys.stderr)
        return 1
    base = args.url.rstrip("/")
    headers = {"authorization": f"Bearer {token}"}
    nonce = secrets.token_hex(16)
    body = {"protocol": "asrp/1", "nonce": nonce, "attempt": {"id": "local-try", "layer": "local", "number": 1}, "task": {"type": args.type, "input": " ".join(args.input) or "What is the capital of France?"}}
    async with httpx.AsyncClient(timeout=60) as client:
        health = await client.get(f"{base}/agent-studio/v1/health", headers=headers)
        print(f"GET /agent-studio/v1/health -> {health.status_code}", health.text)
        started = time.monotonic()
        res = await client.post(f"{base}/agent-studio/v1/tasks", json=body, headers=headers)
    print(f"POST /agent-studio/v1/tasks ({args.type}) -> {res.status_code} in {int((time.monotonic() - started) * 1000)} ms")
    try:
        out = res.json()
    except ValueError:
        print(res.text)
        return 1
    print(json.dumps(out, indent=2))
    if out.get("nonce") != nonce:
        print("Note: the nonce was not echoed. The verifier would reject this answer.")
    return 0
