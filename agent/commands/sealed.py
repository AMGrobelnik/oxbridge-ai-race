"""python -m agent sealed run|anchor|verify  (SPEC.md section 12). Owner: W1."""

import argparse
import json
import sys

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


def _receipt_line(label: str, receipt: dict | None) -> str:
    if not receipt:
        return f"{label}: none"
    if receipt.get("status") == "confirmed" and receipt.get("explorer_url"):
        return f"{label}: on-chain {receipt['explorer_url']}"
    reason = str(receipt.get("error") or "not anchored").removeprefix("pending on-chain: ")
    return f"{label}: pending on-chain ({reason})"


def _iteration_line(it: dict) -> str:
    dev = it.get("dev")
    score = f"{dev['score']:.3f}" if dev else "n/a"
    line = f"  [{it['index']:>2}] {it['decision']:<8} dev={score:<10} champion={it['champion_score']:.3f}  {it['hypothesis']}"
    if it.get("errors"):
        line += f"  ({'; '.join(it['errors'])[:160]})"
    return line


async def _cmd_run(args: argparse.Namespace) -> int:
    from agent.sealed.harness import run_sealed
    from agent.sealed.store import freeze_demo

    printed = {"n": 0, "sealed": False, "started": False}

    async def on_update(record: dict) -> None:
        if not printed["started"]:
            printed["started"] = True
            print(f"run_id {record['run_id']}  env {record['env']}  proposer {record['proposer']}", flush=True)
        reg = record.get("preregistration")
        if reg and not printed["sealed"]:
            printed["sealed"] = True
            print(f"sealed commitment {reg['commitment']}")
            print(_receipt_line("  commit memo", reg.get("chain")))
        for it in record["iterations"][printed["n"] :]:
            print(_iteration_line(it), flush=True)
        printed["n"] = len(record["iterations"])

    record = await run_sealed(args.env, iterations=args.iterations, proposer=args.proposer, anchor=not args.no_anchor, on_update=on_update)
    print(f"run {record['run_id']} finished with status {record['status']}")
    if record["status"] != "done" or not record.get("reveal"):
        print(f"run failed: {record.get('error')}", file=sys.stderr)
        return 1
    reveal = record["reveal"]
    for res in reveal["results"]:
        print(f"  finalist {res['iteration']:>2}: dev={res['dev_score']:.3f} holdout_mean={res['holdout_mean']:.3f} -> {res['verdict']}" + (f" ({'; '.join(res['reasons'])})" if res["reasons"] else ""))
    print(f"dev champion: iteration {reveal['dev_champion_iteration']} -> {reveal['verdict']}")
    print(f"selected: {'iteration ' + str(reveal['selected_iteration']) if reveal['selected_iteration'] is not None else 'none (no finalist generalises)'}")
    print(_receipt_line("commit memo", record["preregistration"]["chain"] if record.get("preregistration") else None))
    print(_receipt_line("reveal memo", reveal.get("chain")))
    if args.freeze:
        print(f"frozen demo written to {freeze_demo(record)}")
    return 0


async def _cmd_anchor(args: argparse.Namespace) -> int:
    from agent.sealed import chain
    from agent.sealed.store import RunNotFoundError, RunStore, freeze_demo

    store = RunStore()
    try:
        record = store.load(args.run_id)
    except (RunNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    record = await chain.anchor_pending(record)
    store.save(record)
    print(_receipt_line("commit memo", record["preregistration"]["chain"] if record.get("preregistration") else None))
    print(_receipt_line("reveal memo", record["reveal"]["chain"] if record.get("reveal") else None))
    if args.freeze:
        print(f"frozen demo written to {freeze_demo(record)}")
    return 0


async def _cmd_verify(args: argparse.Namespace) -> int:
    from agent.sealed import audit, chain, prereg
    from agent.sealed.environment import load_environment
    from agent.sealed.store import RunNotFoundError, RunStore

    try:
        record = RunStore().load(args.run_id)
    except (RunNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    commit_onchain = reveal_onchain = None
    reg_chain = (record.get("preregistration") or {}).get("chain") or {}
    rev_chain = (record.get("reveal") or {}).get("chain") or {}
    if reg_chain.get("status") == "confirmed" and reg_chain.get("signature"):
        commit_onchain = await chain.fetch_memo(reg_chain["signature"])
    if rev_chain.get("status") == "confirmed" and rev_chain.get("signature"):
        reveal_onchain = await chain.fetch_memo(rev_chain["signature"])
    round_memos = {i: m for i, m in (await audit.fetch_round_memos(record)).items() if m is not None}
    report = prereg.verify_bundle(
        record, load_environment(record["env"]), commit_memo_onchain=commit_onchain, reveal_memo_onchain=reveal_onchain, round_memos_onchain=round_memos
    )
    print(json.dumps(report, indent=2))
    audit_report = report.get("audit")
    if audit_report is None:
        print("audit chain: none in this bundle")
    elif audit_report["ok"]:
        print(f"audit chain: intact, {audit_report['length']} rounds, head {audit_report['head_recomputed']} ({audit_report['onchain_status']})")
    else:
        print(f"audit chain: BROKEN at round {audit_report['broken_at']}")
    print("VERIFIED: bundle matches the sealed record" if report["ok"] else "MISMATCH: bundle does not match the sealed record")
    return 0 if report["ok"] else 1


async def run(args: argparse.Namespace) -> int:
    handlers = {"run": _cmd_run, "anchor": _cmd_anchor, "verify": _cmd_verify}
    handler = handlers.get(getattr(args, "sealed_command", None) or "")
    if handler is None:
        print("usage: python -m agent sealed <run|anchor|verify> ...", file=sys.stderr)
        return 2
    return await handler(args)
