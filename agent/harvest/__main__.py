"""CLI: python -m agent.harvest fetch | replay [--offline] [--anchor] [--freeze]. Owner: agents-replay."""

import argparse
import asyncio
from typing import Any


def _fmt(x: Any) -> str:
    return "  -  " if x is None else f"{float(x):.3f}"


def _replay(args: argparse.Namespace) -> int:
    from agent.config import load_env

    from . import personas, prices, questions, replay

    load_env()
    fixture = prices.load_fixture()
    result = asyncio.run(replay.run(fixture=fixture, specs=questions.load_bank(), personas=personas.load_personas(), offline=args.offline, anchor=args.anchor))
    splits: dict[str, list[str]] = {}
    for c in result["cutoffs"]:
        splits.setdefault(c["split"], []).append(c["cutoff"])
    sources: dict[str, int] = {}
    for c in result["cutoffs"]:
        for f in c["forecasts"]:
            sources[f["source"]] = sources.get(f["source"], 0) + 1
    print(f"model {result['model']}  forecasts {sources}")
    for name in ("dev", "holdout", "live"):
        cs = splits.get(name, [])
        print(f"{name:8s} {len(cs):2d} cutoffs  {cs[0] + '..' + cs[-1] if cs else ''}")
    lb = result["scores"]
    print(f"\n{'#':>2} {'world model':26s} {'Brier hold':>10} {'Brier dev':>10} {'PnL hold':>9} {'PnL dev':>9}")
    for m in lb["world_models"]:
        print(f"{m['rank']:>2} {m['name']:26s} {_fmt(m['brier_holdout']):>10} {_fmt(m['brier_dev']):>10} {m['pnl_holdout']:>9.1f} {m['pnl_dev']:>9.1f}")
    print("baselines " + "  ".join(f"{k}={_fmt(v)}" for k, v in lb["baselines"].items()))
    try:
        from . import scoring

        print("\n" + scoring.insight(lb))
    except NotImplementedError:
        pass
    for cm in result["commitments"]:
        r = cm["receipt"]
        print(f"commit {cm['kind']:8s} {cm['memo'][:60]}...  {r['status']}{(' ' + r['error']) if r.get('error') else ''}")
    if args.freeze:
        print(f"froze {replay.freeze(result)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.harvest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    rp = sub.add_parser("replay")
    rp.add_argument("--offline", action="store_true")
    rp.add_argument("--anchor", action="store_true")
    rp.add_argument("--freeze", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "fetch":
        from . import prices

        fx = asyncio.run(prices.fetch_fixture())
        print(f"fetched {len(fx['series'])} series up to {prices.last_month(fx)}")
        return 0
    return _replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
