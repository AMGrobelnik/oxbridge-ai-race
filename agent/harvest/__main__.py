"""CLI: python -m agent.harvest fetch | replay [--offline] [--anchor] [--freeze]. Owner: agents-replay."""

import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.harvest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch")
    rp = sub.add_parser("replay")
    rp.add_argument("--offline", action="store_true")
    rp.add_argument("--anchor", action="store_true")
    rp.add_argument("--freeze", action="store_true")
    parser.parse_args(argv)
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
