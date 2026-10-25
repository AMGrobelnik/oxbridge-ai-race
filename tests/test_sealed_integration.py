"""Cross-workstream checks for Sealed: hashing, proposer retries, chain errors, the frozen demo."""

import asyncio
import json
from typing import Any

import pytest

from agent.sealed import chain, hashing, prereg, store
from agent.sealed.environment import load_environment
from agent.sealed.proposer import GeminiProposer, ProposerError


def test_whole_number_floats_hash_like_ints() -> None:
    # A bundle re-serialised by JavaScript turns 33.0 into 33; the hash must not change.
    assert hashing.canonical_json({"a": 33.0, "b": [1.0, 2.5]}) == hashing.canonical_json({"a": 33, "b": [1, 2.5]})
    assert hashing.sha256_json({"x": 0.1 + 0.2}) == hashing.sha256_json({"x": 0.3})


def _ctx() -> dict[str, Any]:
    return {
        "env": "toy", "goal": "g", "metric_label": "m", "handbook": "", "candidate_schema": {}, "champion": {},
        "champion_report": {"score": 0.0, "per_seed": [], "breakdown": {}, "seeds": [], "shift_id": "base"},
        "history": [], "iteration": 1, "iterations_total": 1,
    }


def test_gemini_proposer_retries_a_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.ai.gemini as gemini

    calls = {"n": 0}

    async def flaky(prompt: str, *, json_output: bool = False) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("connect failed")
        return json.dumps({"hypothesis": "h", "rationale": "r", "strategy": "continue", "candidate": {"x": 1}})

    monkeypatch.setattr(gemini, "ask_gemini", flaky)
    proposer = GeminiProposer()
    proposer.retry_delay_s = 0.0
    proposal = asyncio.run(proposer.propose(_ctx()))  # type: ignore[arg-type]
    assert proposal["candidate"] == {"x": 1} and calls["n"] == 2


def test_gemini_proposer_gives_up_after_its_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.ai.gemini as gemini

    async def down(prompt: str, *, json_output: bool = False) -> str:
        raise ConnectionError("connect failed")

    monkeypatch.setattr(gemini, "ask_gemini", down)
    proposer = GeminiProposer()
    proposer.retry_delay_s = 0.0
    with pytest.raises(ProposerError, match="ConnectionError"):
        asyncio.run(proposer.propose(_ctx()))  # type: ignore[arg-type]


def test_rpc_connect_timeout_reads_as_unreachable() -> None:
    from solana.exceptions import SolanaRpcException

    # The exact text solana-py raised in a live run on 25 Oct 2026.
    err = SolanaRpcException.__new__(SolanaRpcException)
    Exception.__init__(err, """<class 'httpx2.ConnectTimeout'> raised in "GetLatestBlockhash" endpoint request""")
    assert chain._human_error(err) == "pending on-chain: devnet RPC unreachable"


def test_frozen_epidemic_demo_verifies_against_the_installed_code() -> None:
    path = store.demo_path("epidemic")
    if not path.exists():
        pytest.skip("no frozen epidemic demo yet")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["mode"] == "cached" and record["status"] == "done"
    reveal = record["reveal"]
    assert reveal["verdict"] == "OVERFIT" and reveal["selected_iteration"] is not None
    report = prereg.verify_bundle(record, load_environment("epidemic"), commit_memo_onchain=None, reveal_memo_onchain=None)
    assert report["ok"], report["reasons"]
