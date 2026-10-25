"""Offline tests for agent/sealed/prereg.py and chain.py (no network: pay_usdc is monkeypatched)."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.paidapi import buyer
from agent.payments import transfer
from agent.payments.config import PaymentsConfigError
from agent.sealed import chain, prereg
from agent.sealed.environment import Environment
from agent.sealed.hashing import canonical_json, sha256_hex, sha256_json

HOLDOUT = {"seeds": [901, 902], "shifts": [{"id": "hot", "label": "Hot", "params": {"beta": 1.4}}]}
SALT = "ab" * 32
RUN_ID = "test-run-01"


@pytest.fixture
def env(tmp_path: Path) -> Environment:
    (tmp_path / "sim.py").write_text("def simulate(c, *, seed, shift):\n    return {'x': c['x']}\n")
    (tmp_path / "eval.py").write_text("SPEC = {'dev_seeds': [1, 2, 3]}\n")
    (tmp_path / "holdout.json").write_text(json.dumps(HOLDOUT))
    sim = SimpleNamespace(TITLE="Toy", GOAL="Maximise x.", CANDIDATE_SCHEMA={})
    evaluator = SimpleNamespace(SPEC={"dev_seeds": [1, 2, 3], "metric_label": "x"})
    return Environment(name="toy", directory=tmp_path, sim=sim, evaluator=evaluator)  # type: ignore[arg-type]


def _iteration(index: int, x: float, decision: str) -> dict[str, Any]:
    return {
        "index": index, "proposer": "scripted", "hypothesis": "h", "rationale": "r", "strategy": "continue",
        "candidate": {"x": x}, "diff": [], "dev": {"score": x, "per_seed": [x], "breakdown": {}, "seeds": [1], "shift_id": "base"},
        "decision": decision, "errors": [], "champion_score": x, "duration_s": 0.1, "at": "2026-10-25T13:00:00Z",
    }


def _result(iteration: int, dev: float, base: float, hot: float, verdict: str) -> dict[str, Any]:
    return {
        "iteration": iteration, "dev_score": dev, "per_shift": {"base": base, "hot": hot}, "shift_labels": {"base": "Base", "hot": "Hot"},
        "holdout_mean": (base + hot) / 2, "worst_shift_id": "hot", "worst_score": hot, "relative_gap": 0.1, "verdict": verdict, "reasons": [],
    }


def make_record(env: Environment, *, confirmed: bool = False) -> dict[str, Any]:
    pre = prereg.build_preregistration(env, RUN_ID, SALT, sealed_at="2026-10-25T13:00:00Z")
    record: dict[str, Any] = {
        "schema": "sealed.run/v1", "run_id": RUN_ID, "env": "toy", "title": "Toy", "goal": "g", "metric_label": "x",
        "mode": "live", "proposer": "scripted", "iterations_planned": 2, "status": "done",
        "created_at": "2026-10-25T13:00:00Z", "updated_at": "2026-10-25T13:00:00Z",
        "preregistration": pre,
        "iterations": [_iteration(0, 1.0, "BASELINE"), _iteration(1, 2.5, "KEEP"), {**_iteration(2, 0.0, "INVALID"), "dev": None}],
        "reveal": None, "error": None,
    }
    record["reveal"] = {
        "salt": SALT, "holdout": copy.deepcopy(HOLDOUT), "revealed_at": "2026-10-25T13:05:00Z",
        "results": [_result(0, 1.0, 0.9, 0.8, "GENERALISES"), _result(1, 2.5, 1.0, 0.1, "OVERFIT")],
        "dev_champion_iteration": 1, "selected_iteration": 0, "verdict": "OVERFIT",
        "result_sha256": "", "memo": "", "chain": chain.pending_receipt("", "pending on-chain: not anchored yet"),
    }
    result = prereg.result_digest(record)  # type: ignore[arg-type]
    memo = prereg.reveal_memo(RUN_ID, SALT, result, "OVERFIT", 0)
    record["reveal"].update(result_sha256=result, memo=memo, chain=chain.pending_receipt(memo))
    if confirmed:
        record["preregistration"]["chain"] = chain.confirmed_receipt(pre["memo"], "SIGC")
        record["reveal"]["chain"] = chain.confirmed_receipt(memo, "SIGR")
    return record


def verify(record: dict[str, Any], env: Environment, commit: str | None = None, reveal: str | None = None) -> dict[str, Any]:
    return dict(prereg.verify_bundle(record, env, commit_memo_onchain=commit, reveal_memo_onchain=reveal))  # type: ignore[arg-type]


# --- hashes and memos ---------------------------------------------------------------------


def test_salt_is_64_hex_and_random() -> None:
    a, b = prereg.new_salt(), prereg.new_salt()
    assert len(a) == 64 and int(a, 16) >= 0 and a != b


def test_hashes_match_spec_formulas(env: Environment) -> None:
    files = [env.directory / "sim.py", env.directory / "eval.py"]
    expected_code = sha256_hex(b"".join(f.name.encode() + b"\0" + f.read_bytes() + b"\0" for f in files))
    assert prereg.code_digest(env) == expected_code
    assert prereg.holdout_commitment(HOLDOUT, SALT) == sha256_hex(canonical_json({"salt": SALT, "holdout": HOLDOUT}))
    pre = prereg.build_preregistration(env, RUN_ID, SALT, sealed_at="t")
    assert pre == prereg.build_preregistration(env, RUN_ID, SALT, sealed_at="t") | {"chain": pre["chain"]}
    assert pre["commitment"] == sha256_json(
        {"v": 1, "run_id": RUN_ID, "env": "toy", "code_sha256": expected_code, "dev_seeds": [1, 2, 3], "holdout_commitment": pre["holdout_commitment"]}
    )
    assert pre["chain"]["status"] == "pending" and pre["chain"]["memo"] == pre["memo"] and pre["chain"]["signature"] is None


def test_result_digest_payload_and_float_rounding(env: Environment) -> None:
    record = make_record(env)
    payload = prereg.result_payload(record)  # type: ignore[arg-type]
    assert set(payload) == {"v", "run_id", "commitment", "iterations", "results", "selected_iteration"}
    assert payload["iterations"][2]["dev_score"] is None
    assert set(payload["results"][0]) == {"iteration", "per_shift", "verdict"}
    noisy = copy.deepcopy(record)
    noisy["iterations"][1]["dev"]["score"] = 2.5 + 1e-12
    assert prereg.result_digest(noisy) == prereg.result_digest(record)  # type: ignore[arg-type]


def test_memos_round_trip_and_stay_short() -> None:
    run_id = "r" * 64
    c = prereg.commit_memo(run_id, "c" * 64)
    m = prereg.COMMIT_MEMO_RE.match(c)
    assert m and m["run_id"] == run_id and m["commitment"] == "c" * 64 and len(c.encode()) < 300
    for selected, text in ((None, "none"), (7, "7")):
        r = prereg.reveal_memo(run_id, SALT, "d" * 64, "GENERALISES", selected)
        m = prereg.REVEAL_MEMO_RE.match(r)
        assert m and m["salt"] == SALT and m["result"] == "d" * 64 and m["selected"] == text and len(r.encode()) < 300
    with pytest.raises(ValueError):
        prereg.commit_memo("bad id!", "c" * 64)


# --- verify_bundle -------------------------------------------------------------------------


def test_untouched_bundle_verifies_with_pending_chain(env: Environment) -> None:
    rep = verify(make_record(env), env)
    assert rep["ok"] and rep["code_match"] and rep["commit_match"] and rep["result_match"]
    assert rep["onchain_status"] == "pending"
    assert prereg.PENDING_REASON in rep["reasons"]


def test_tampered_candidate_breaks_result(env: Environment) -> None:
    record = make_record(env)
    record["iterations"][1]["candidate"]["x"] = 2.6
    rep = verify(record, env)
    assert not rep["ok"] and rep["result_match"] is False and rep["commit_match"]
    assert any("result hash" in r for r in rep["reasons"])


@pytest.mark.parametrize("field", ["salt", "seed"])
def test_salt_or_holdout_change_breaks_commit(env: Environment, field: str) -> None:
    record = make_record(env)
    if field == "salt":
        record["reveal"]["salt"] = "cd" * 32
    else:
        record["reveal"]["holdout"]["seeds"][0] = 999
    rep = verify(record, env)
    assert not rep["ok"] and rep["commit_match"] is False


def test_edited_sim_breaks_code_match(env: Environment) -> None:
    record = make_record(env)
    (env.directory / "sim.py").write_text("def simulate(c, *, seed, shift):\n    return {'x': 99}\n")
    rep = verify(record, env)
    assert not rep["ok"] and rep["code_match"] is False


def test_onchain_memos_confirmed_match_and_mismatch(env: Environment) -> None:
    record = make_record(env, confirmed=True)
    commit, reveal = record["preregistration"]["memo"], record["reveal"]["memo"]
    rep = verify(record, env, commit, reveal)
    assert rep["ok"] and rep["onchain_status"] == "confirmed" and rep["commitment_onchain"] == rep["commitment_recomputed"]

    other_result = prereg.reveal_memo(RUN_ID, SALT, "e" * 64, "OVERFIT", 0)
    rep = verify(record, env, commit, other_result)
    assert not rep["ok"] and rep["result_match"] is False
    assert "result hash does not match the on-chain reveal memo" in rep["reasons"]

    rep = verify(record, env, prereg.commit_memo(RUN_ID, "f" * 64), reveal)
    assert not rep["ok"] and rep["commit_match"] is False

    rep = verify(record, env, commit, prereg.reveal_memo(RUN_ID, "cd" * 32, record["reveal"]["result_sha256"], "OVERFIT", 0))
    assert not rep["ok"] and rep["commit_match"] is False


def test_confirmed_but_unfetched_is_unreachable(env: Environment) -> None:
    rep = verify(make_record(env, confirmed=True), env)
    assert rep["ok"] and rep["onchain_status"] == "unreachable"


def test_malformed_bundle_never_raises(env: Environment) -> None:
    for bad in ({}, {"preregistration": {"run_id": RUN_ID}}, {**make_record(env), "iterations": [{"index": 0}]}):
        rep = verify(bad, env)
        assert rep["ok"] is False and rep["reasons"]


# --- chain ---------------------------------------------------------------------------------


class _Guard:
    pass


@pytest.fixture
def fake_wallet(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from solders.keypair import Keypair

    calls: dict[str, Any] = {}
    kp = Keypair()
    monkeypatch.setattr(transfer, "load_agent_keypair", lambda: kp)
    monkeypatch.setattr(buyer, "get_spending_tracker", lambda: _Guard())
    monkeypatch.delenv("SEALED_ANCHOR_TO", raising=False)
    monkeypatch.delenv("SEALED_ANCHOR_USDC", raising=False)
    calls["pubkey"] = str(kp.pubkey())
    return calls


def test_anchor_confirmed(monkeypatch: pytest.MonkeyPatch, fake_wallet: dict[str, Any]) -> None:
    async def fake_pay(payer: Any, *, to: str, amount_usdc: float, memo: str, guard: Any, timeout_s: float) -> str:
        fake_wallet.update(to=to, amount=amount_usdc, memo=memo, guard=guard)
        return "SIG"

    monkeypatch.setattr(transfer, "pay_usdc", fake_pay)
    r = asyncio.run(chain.anchor("sealed:v1:commit:x:" + "0" * 64))
    assert r["status"] == "confirmed" and r["signature"] == "SIG" and r["error"] is None
    assert r["explorer_url"] == "https://explorer.solana.com/tx/SIG?cluster=devnet"
    assert fake_wallet["to"] == fake_wallet["pubkey"] and fake_wallet["amount"] == chain.DEFAULT_ANCHOR_USDC
    assert isinstance(fake_wallet["guard"], _Guard)

    monkeypatch.setenv("SEALED_ANCHOR_TO", "Hsw88CEieeGW7HxuXC5SDUYLnNZnfQ8Ew86PLVyHfuZX")
    monkeypatch.setenv("SEALED_ANCHOR_USDC", "0.02")
    asyncio.run(chain.anchor("m"))
    assert fake_wallet["to"] == "Hsw88CEieeGW7HxuXC5SDUYLnNZnfQ8Ew86PLVyHfuZX" and fake_wallet["amount"] == 0.02


def test_anchor_transfer_error_is_pending(monkeypatch: pytest.MonkeyPatch, fake_wallet: dict[str, Any]) -> None:
    async def fail(*a: Any, **k: Any) -> str:
        raise transfer.TransferError("Attempt to debit an account but found no record of a prior credit.")

    monkeypatch.setattr(transfer, "pay_usdc", fail)
    r = asyncio.run(chain.anchor("m"))
    assert r["status"] == "pending" and r["signature"] is None
    assert r["error"] == "pending on-chain: agent wallet not funded (devnet SOL/USDC)"


def test_anchor_missing_key_is_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_key() -> Any:
        raise PaymentsConfigError("AGENT_PRIVATE_KEY is not set.")

    monkeypatch.setattr(transfer, "load_agent_keypair", no_key)
    r = asyncio.run(chain.anchor("m"))
    assert r["status"] == "pending" and r["error"] is not None and r["error"].startswith("pending on-chain:")


def test_anchor_timeout_and_rpc_errors_are_pending(monkeypatch: pytest.MonkeyPatch, fake_wallet: dict[str, Any]) -> None:
    async def hang(*a: Any, **k: Any) -> str:
        await asyncio.sleep(60)
        return "never"

    monkeypatch.setattr(transfer, "pay_usdc", hang)
    monkeypatch.setattr(chain.asyncio, "wait_for", _short_wait_for)
    r = asyncio.run(chain.anchor("m", timeout_s=0))
    assert r["status"] == "pending" and r["error"] == "pending on-chain: devnet did not confirm in time"

    async def refused(*a: Any, **k: Any) -> str:
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(transfer, "pay_usdc", refused)
    assert asyncio.run(chain.anchor("m"))["error"] == "pending on-chain: devnet RPC unreachable"


_real_wait_for = asyncio.wait_for


async def _short_wait_for(aw: Any, timeout: float) -> Any:
    return await _real_wait_for(aw, 0.05)


def test_anchor_pending_upgrades_only_pending(monkeypatch: pytest.MonkeyPatch, env: Environment, fake_wallet: dict[str, Any]) -> None:
    memos: list[str] = []

    async def fake_pay(payer: Any, *, to: str, amount_usdc: float, memo: str, guard: Any, timeout_s: float) -> str:
        memos.append(memo)
        return f"SIG{len(memos)}"

    monkeypatch.setattr(transfer, "pay_usdc", fake_pay)
    record = make_record(env)
    updated = asyncio.run(chain.anchor_pending(record))  # type: ignore[arg-type]
    assert record["preregistration"]["chain"]["status"] == "pending"  # input untouched (deep copy)
    assert updated["preregistration"]["chain"]["signature"] == "SIG1"
    assert updated["reveal"]["chain"]["signature"] == "SIG2"  # type: ignore[index]
    assert memos == [record["preregistration"]["memo"], record["reveal"]["memo"]]

    again = asyncio.run(chain.anchor_pending(updated))
    assert again["preregistration"]["chain"] == updated["preregistration"]["chain"] and len(memos) == 2


def test_anchor_pending_holds_reveal_until_commit_lands(monkeypatch: pytest.MonkeyPatch, env: Environment, fake_wallet: dict[str, Any]) -> None:
    async def fail(*a: Any, **k: Any) -> str:
        raise transfer.TransferError("insufficient funds")

    monkeypatch.setattr(transfer, "pay_usdc", fail)
    updated = asyncio.run(chain.anchor_pending(make_record(env)))  # type: ignore[arg-type]
    assert updated["preregistration"]["chain"]["status"] == "pending"
    assert updated["reveal"]["chain"]["status"] == "pending"  # type: ignore[index]
    assert updated["reveal"]["chain"]["error"] == "pending on-chain: agent wallet not funded (devnet SOL/USDC)"  # type: ignore[index]


def test_memo_from_transaction_parses_instruction_and_logs() -> None:
    tx = {"result": {"transaction": {"message": {"instructions": [{"program": "spl-memo", "programId": "Memo", "parsed": "sealed:v1:x"}]}}, "meta": {}}}
    assert chain.memo_from_transaction(tx) == "sealed:v1:x"
    tx = {"result": {"meta": {"logMessages": ["Program X invoke [1]", 'Program log: Memo (len 11): "sealed:v1:y"']}}}
    assert chain.memo_from_transaction(tx) == "sealed:v1:y"
    assert chain.memo_from_transaction({"result": None}) is None
