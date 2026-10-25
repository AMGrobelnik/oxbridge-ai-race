"""Per-round audit chain (agent/sealed/audit.py, SPEC.md section 13). No network."""

import asyncio
import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.payments import transfer
from agent.routes import sealed as routes
from agent.sealed import audit, chain, prereg
from agent.sealed.environment import Environment
from agent.server import app
from tests.test_sealed_harness import _run, toy_env  # noqa: F401 - fixture reuse
from tests.test_sealed_prereg import env, fake_wallet, make_record  # noqa: F401 - fixture reuse


def chained(environment: Environment, *, confirmed: bool = False) -> dict[str, Any]:
    record = make_record(environment, confirmed=confirmed)
    for it in record["iterations"]:
        audit.append_round(record, it)
    return record


def check(record: dict[str, Any], environment: Environment, **kw: Any) -> dict[str, Any]:
    return dict(prereg.verify_bundle(record, environment, commit_memo_onchain=None, reveal_memo_onchain=None, **kw))  # type: ignore[arg-type]


def test_chain_links_rounds_from_the_commitment(env: Environment) -> None:
    record = chained(env)
    log = record["audit"]
    rounds = log["rounds"]
    assert log["genesis"] == record["preregistration"]["commitment"]
    assert [r["index"] for r in rounds] == [0, 1, 2]
    assert rounds[0]["prev_sha256"] == log["genesis"]
    assert rounds[1]["prev_sha256"] == rounds[0]["sha256"] and rounds[2]["prev_sha256"] == rounds[1]["sha256"]
    assert log["head"] == rounds[-1]["sha256"]
    assert len({r["sha256"] for r in rounds}) == 3
    for r in rounds:
        assert r["memo"] == f"sealed:v1:round:{record['run_id']}:{r['index']}:{r['sha256']}"
        assert len(r["memo"].encode()) < 300 and audit.ROUND_MEMO_RE.match(r["memo"])
        assert r["chain"]["status"] == "pending"
    # The payload covers the proposal text, params, dev score and decision.
    payload = audit.round_payload(record["run_id"], log["genesis"], record["iterations"][1])
    assert {"hypothesis", "rationale", "strategy", "candidate", "dev_score", "decision", "prev", "index", "run_id", "v"} == set(payload)
    assert payload["dev_score"] == 2.5


def test_intact_chain_verifies(env: Environment) -> None:
    report = check(chained(env), env)
    assert report["ok"] and report["audit_match"] is True
    assert report["audit"]["length"] == 3 and report["audit"]["broken_at"] is None
    assert report["audit"]["onchain_status"] == "pending"
    assert all(r["match"] for r in report["audit"]["rounds"])


@pytest.mark.parametrize(
    ("index", "mutate"),
    [
        (0, lambda it: it.update(hypothesis="a different hypothesis")),
        (1, lambda it: it["candidate"].update(x=2.6)),
        (1, lambda it: it["dev"].update(score=9.9)),
        (2, lambda it: it.update(decision="KEEP")),
        (1, lambda it: it.update(rationale="edited")),
    ],
)
def test_tampering_with_any_round_breaks_the_chain(env: Environment, index: int, mutate: Any) -> None:
    record = chained(env)
    mutate(record["iterations"][index])
    report = check(record, env)
    assert report["ok"] is False and report["audit_match"] is False
    assert report["audit"]["broken_at"] == index
    assert any(f"breaks at round {index}" in r for r in report["reasons"])


def test_rewriting_a_stored_hash_or_dropping_a_round_breaks_the_chain(env: Environment) -> None:
    record = chained(env)
    record["audit"]["rounds"][1]["sha256"] = "f" * 64
    assert check(record, env)["audit"]["broken_at"] == 1

    record = chained(env)
    record["audit"]["rounds"].pop()
    report = check(record, env)
    assert report["ok"] is False and report["audit"]["broken_at"] == 2

    record = chained(env)
    record["audit"]["genesis"] = "e" * 64
    assert check(record, env)["audit"]["genesis_match"] is False


def test_rehashing_a_tampered_round_is_caught_by_its_successor_or_the_chain(env: Environment) -> None:
    """A forger who edits round 1 and recomputes only round 1's hash still breaks round 2's link."""
    record = chained(env)
    record["iterations"][1]["candidate"]["x"] = 7.0
    log = record["audit"]
    sha = audit.round_digest(record["run_id"], log["rounds"][0]["sha256"], record["iterations"][1])
    log["rounds"][1].update(sha256=sha, memo=audit.round_memo(record["run_id"], 1, sha))
    report = check(record, env)
    assert report["ok"] is False and report["audit"]["broken_at"] == 2


def test_bundle_without_audit_is_not_failed(env: Environment) -> None:
    report = check(make_record(env), env)
    assert report["ok"] is True and report["audit_match"] is None and report["audit"] is None


def test_onchain_round_memos_are_compared(env: Environment) -> None:
    record = chained(env)
    memos = {r["index"]: r["memo"] for r in record["audit"]["rounds"]}
    report = check(record, env, round_memos_onchain=memos)
    assert report["ok"] and report["audit"]["onchain_status"] == "confirmed"
    assert report["audit"]["rounds"][1]["sha256_onchain"] == record["audit"]["rounds"][1]["sha256"]

    forged = dict(memos)
    forged[1] = audit.round_memo(record["run_id"], 1, "a" * 64)
    report = check(record, env, round_memos_onchain=forged)
    assert report["ok"] is False and report["audit"]["broken_at"] == 1


def test_harness_records_and_verifies_a_chain(toy_env: Environment, tmp_path: Any) -> None:  # noqa: F811
    from agent.sealed.store import RunStore

    record = _run(RunStore(tmp_path / "runs"), iterations=6, proposer="scripted")
    assert record["status"] == "done", record["error"]
    rounds = record["audit"]["rounds"]
    assert [r["index"] for r in rounds] == [it["index"] for it in record["iterations"]]
    assert record["audit"]["genesis"] == record["preregistration"]["commitment"]
    # The commit is pending, so every round stays pending with the commit's reason.
    assert all(r["chain"]["status"] == "pending" for r in rounds)
    assert audit.verify_chain(record)["ok"]  # type: ignore[index]
    tampered = copy.deepcopy(record)
    tampered["iterations"][4]["candidate"]["y"] = 2
    assert audit.verify_chain(tampered)["broken_at"] == 4  # type: ignore[index]


def test_anchor_round_waits_for_the_commit(env: Environment) -> None:
    record = chained(env)
    entry = record["audit"]["rounds"][0]
    called: list[str] = []

    async def fake_anchor(memo: str, **_: Any) -> dict[str, Any]:
        called.append(memo)
        return chain.confirmed_receipt(memo, "SIGX")

    original = chain.anchor
    chain.anchor = fake_anchor  # type: ignore[assignment]
    try:
        assert asyncio.run(audit.anchor_round(record, entry))["status"] == "pending" and not called
        assert asyncio.run(audit.anchor_round(record, entry, enabled=False))["error"] == "anchoring disabled"
        record["preregistration"]["chain"] = chain.confirmed_receipt(record["preregistration"]["memo"], "SIGC")
        assert asyncio.run(audit.anchor_round(record, entry))["signature"] == "SIGX" and called == [entry["memo"]]
    finally:
        chain.anchor = original  # type: ignore[assignment]


def test_anchor_pending_anchors_rounds_between_commit_and_reveal(monkeypatch: pytest.MonkeyPatch, env: Environment, fake_wallet: dict[str, Any]) -> None:  # noqa: F811
    memos: list[str] = []

    async def fake_pay(payer: Any, *, to: str, amount_usdc: float, memo: str, guard: Any, timeout_s: float) -> str:
        memos.append(memo)
        return f"SIG{len(memos)}"

    monkeypatch.setattr(transfer, "pay_usdc", fake_pay)
    record = chained(env)
    updated = asyncio.run(chain.anchor_pending(record))  # type: ignore[arg-type]
    expected = [record["preregistration"]["memo"], *[r["memo"] for r in record["audit"]["rounds"]], record["reveal"]["memo"]]
    assert memos == expected
    assert [r["chain"]["signature"] for r in updated["audit"]["rounds"]] == ["SIG2", "SIG3", "SIG4"]
    asyncio.run(chain.anchor_pending(updated))
    assert len(memos) == len(expected)  # confirmed rounds are left alone


def test_anchor_pending_holds_rounds_until_commit_lands(monkeypatch: pytest.MonkeyPatch, env: Environment, fake_wallet: dict[str, Any]) -> None:  # noqa: F811
    async def fail(*a: Any, **k: Any) -> str:
        raise transfer.TransferError("insufficient funds")

    monkeypatch.setattr(transfer, "pay_usdc", fail)
    updated = asyncio.run(chain.anchor_pending(chained(env)))  # type: ignore[arg-type]
    for r in updated["audit"]["rounds"]:
        assert r["chain"]["status"] == "pending"
        assert r["chain"]["error"] == "pending on-chain: agent wallet not funded (devnet SOL/USDC)"


def test_verify_route_fetches_confirmed_round_memos(monkeypatch: pytest.MonkeyPatch, env: Environment, tmp_path: Any) -> None:  # noqa: F811
    record = chained(env)
    for r in record["audit"]["rounds"]:
        r["chain"] = chain.confirmed_receipt(r["memo"], f"SIGR{r['index']}")
    by_sig = {f"SIGR{r['index']}": r["memo"] for r in record["audit"]["rounds"]}

    async def fake_fetch(signature: str) -> str | None:
        return by_sig.get(signature)

    monkeypatch.setattr(routes.chain, "fetch_memo", fake_fetch)
    monkeypatch.setattr(routes, "load_environment", lambda name: env)
    monkeypatch.setattr(routes, "list_environments", lambda: ["toy"])
    monkeypatch.setenv("SEALED_RUNS_DIR", str(tmp_path))
    client = TestClient(app)
    body = client.post("/sealed/api/verify", json={"record": record}).json()
    assert body["ok"] is True and body["audit_match"] is True
    assert body["audit"]["onchain_status"] == "confirmed"

    record["iterations"][1]["candidate"]["x"] = 3.0
    body = client.post("/sealed/api/verify", json={"record": record}).json()
    assert body["ok"] is False and body["audit"]["broken_at"] == 1
