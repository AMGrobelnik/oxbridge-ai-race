"""Pre-registered replication of "Phi-4 beats the market" on new monthly holdout cutoffs.

Runs the unchanged replay pipeline (same personas x models, masking, question bank and market book) on
cutoffs that the frozen demo never used, and writes the result next to this file. See
ai-inventor/scratchpad/oxbridge-hackathon/phi4-check.md for the pre-registration.
"""

import asyncio
import json
import os
from pathlib import Path

ENV = Path("/home/adrian/projects/oxbridge-ai-race/.env")
for line in ENV.read_text().splitlines():
    k, sep, v = line.partition("=")
    if sep and k.strip().isidentifier() and k.strip().isupper():
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from agent.harvest import models, personas, prices, questions, replay  # noqa: E402

NEW_CUTOFFS = ["2025-10", "2025-11", "2026-01", "2026-02"]
OUT = Path(__file__).resolve().parent / "phi4_replication.json"


def main() -> None:
    fixture = prices.load_fixture()
    specs = questions.load_bank()
    print("price data ends", prices.last_month(fixture))
    print("splits", {c: replay.split(c, fixture, specs) for c in NEW_CUTOFFS}, flush=True)
    replay.cutoffs = lambda _fixture: list(NEW_CUTOFFS)  # type: ignore[assignment]
    keys = [m["key"] for m in models.models()]
    result = asyncio.run(replay.run(fixture=fixture, specs=specs, personas=personas.load_personas(), model_keys=keys,
                                    llm_splits=("holdout",)))
    OUT.write_text(json.dumps(result, indent=1))
    src: dict[str, int] = {}
    for c in result["cutoffs"]:
        for f in c["forecasts"]:
            src[f["source"]] = src.get(f["source"], 0) + 1
    print("forecast sources", src)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
