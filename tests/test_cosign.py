"""Human co-sign guardrail: over-cap agent payments are queued, approved or rejected, and audited. Offline."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.paidapi import buyer
from agent.payments import cosign, transfer
from agent.race import adapter
from agent.spending.policy import SpendingPolicy
from agent.spending.tracker import SpendingTracker

TOKEN = "test-verifier-token"
ADDRESS = "Hsw88CEieeGW7HxuXC5SDUYLnNZnfQ8Ew86PLVyHfuZX"
SIGNATURE = "5" * 88


@pytest.fixture(autouse=True)
def isolated(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSIGN_DIR", str(tmp_path / "cosign"))
    monkeypatch.delenv("COSIGN_CAP_USD", raising=False)
    monkeypatch.delenv("COSIGN_TOKEN", raising=False)
    monkeypatch.setattr(buyer, "_tracker", SpendingTracker(SpendingPolicy(budget_micros=1_000_000, max_payment_micros=100_000)))


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    async def fake_pay(payer: Any, to: str, amount_usdc: float, memo: str | None = None, guard: Any = None, **_: Any) -> str:
        if guard is not None:
            decision = guard.try_reserve(round(amount_usdc * 1_000_000))
            if not decision.ok:
                raise transfer.TransferError(f"Payment blocked by your spending limits: {decision.reason}")
            guard.commit(decision.reservation_id)
        out.append({"to": to, "amount": amount_usdc, "memo": memo})
        return SIGNATURE

    monkeypatch.setattr(transfer, "pay_usdc", fake_pay)
    monkeypatch.setattr(transfer, "load_agent_keypair", lambda: object())
    return out


def run_workflow(amount: float, max_spend: float | None = None) -> tuple[int, dict[str, Any]]:
    task: dict[str, Any] = {"type": "run_workflow", "input": f'Pay {amount} devnet USDC to {ADDRESS} with the memo "m-{amount}".'}
    if max_spend is not None:
        task["limits"] = {"maxSpendUsd": max_spend}
    body = {"protocol": "asrp/1", "nonce": "0123456789abcdef0123", "attempt": {"id": "a", "layer": "l", "number": 1}, "task": task}
    return asyncio.run(adapter.handle_task(f"Bearer {TOKEN}", body, {"AGENT_STUDIO_VERIFIER_TOKEN": TOKEN}))


@pytest.fixture
def client() -> TestClient:
    from agent.server import app

    return TestClient(app)


def test_default_cap_and_verifier_amounts_stay_under_it() -> None:
    assert cosign.cap_usd() == 0.5
    assert not cosign.needs_cosign(0.01) and not cosign.needs_cosign(0.5) and cosign.needs_cosign(0.51)


def test_under_cap_payment_goes_through_unchanged(sent: list[dict[str, Any]]) -> None:
    status, body = run_workflow(0.01, max_spend=0.01)
    assert status == 200 and body["output"]["data"] == {"signature": SIGNATURE}
    assert sent == [{"to": ADDRESS, "amount": 0.01, "memo": "m-0.01"}] and cosign.list_approvals() == []


def test_over_cap_payment_is_queued_not_sent(sent: list[dict[str, Any]]) -> None:
    status, body = run_workflow(0.75)
    assert status == 200 and sent == []
    data = body["output"]["data"]
    assert data["status"] == "awaiting_cosign" and "Awaiting human co-sign" in body["output"]["text"]
    [approval] = cosign.list_approvals("open")
    assert approval["id"] == data["approvalId"] and approval["amount_usdc"] == 0.75 and approval["memo"] == "m-0.75" and "co-sign cap" in approval["reason"]


def test_cap_comes_from_env(sent: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COSIGN_CAP_USD", "0.005")
    _, body = run_workflow(0.01)
    assert body["output"]["data"]["status"] == "awaiting_cosign" and sent == []


def test_approve_sends_past_the_per_payment_cap_and_audits(sent: list[dict[str, Any]], client: TestClient) -> None:
    run_workflow(0.75)
    pending = client.get("/api/cosign/pending").json()
    assert pending["capUsd"] == 0.5 and len(pending["pending"]) == 1
    approval_id = pending["pending"][0]["id"]
    res = client.post(f"/api/cosign/{approval_id}/approve", json={"by": "judge", "note": "ok"})
    assert res.status_code == 200 and res.json()["status"] == "sent" and res.json()["signature"] == SIGNATURE
    assert sent == [{"to": ADDRESS, "amount": 0.75, "memo": "m-0.75"}]
    assert client.get("/api/cosign/pending").json()["pending"] == []
    assert client.post(f"/api/cosign/{approval_id}/approve").status_code == 409
    events = [a["event"] for a in client.get("/api/cosign/audit").json()["audit"]]
    assert events == ["sent", "approved", "queued"]


def test_unfunded_wallet_gives_pending_onchain_and_retry_works(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    async def unfunded(*_a: Any, **_k: Any) -> str:
        raise transfer.TransferError("no record of a prior credit (the agent wallet has no devnet SOL for fees)")

    monkeypatch.setattr(transfer, "pay_usdc", unfunded)
    monkeypatch.setattr(transfer, "load_agent_keypair", lambda: object())
    approval = cosign.queue(ADDRESS, 0.75, "m", reason="test")
    body = client.post(f"/api/cosign/{approval['id']}/approve").json()
    assert body["status"] == "approved_pending_onchain" and body["onchain_error"].startswith("pending on-chain:")
    assert [a["id"] for a in client.get("/api/cosign/pending").json()["pending"]] == [approval["id"]]

    async def funded(*_a: Any, **_k: Any) -> str:
        return SIGNATURE

    monkeypatch.setattr(transfer, "pay_usdc", funded)
    assert client.post(f"/api/cosign/{approval['id']}/approve").json()["status"] == "sent"


def test_reject_and_token(sent: list[dict[str, Any]], client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    approval = cosign.queue(ADDRESS, 0.9, "m", reason="test")
    monkeypatch.setenv("COSIGN_TOKEN", "s3cret")
    assert client.post(f"/api/cosign/{approval['id']}/reject").status_code == 401
    res = client.post(f"/api/cosign/{approval['id']}/reject", json={"note": "too big"}, headers={"Authorization": "Bearer s3cret"})
    assert res.status_code == 200 and res.json()["status"] == "rejected" and sent == []
    assert client.post("/api/cosign/cs_nope/reject", headers={"Authorization": "Bearer s3cret"}).status_code == 404
    assert client.get("/api/cosign/approvals?status=rejected").json()["approvals"][0]["note"] == "too big"
    assert client.get("/api/cosign/audit").json()["audit"][0]["event"] == "rejected"
