"""Strategy-first demo page: per-env frozen demos and the guardrail (co-sign) demo payment."""

import pytest
from fastapi.testclient import TestClient

from agent.sealed import store
from agent.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEALED_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("COSIGN_DIR", str(tmp_path / "cosign"))
    monkeypatch.setenv("SEALED_ANCHOR_TO", "11111111111111111111111111111111")
    return TestClient(app)


@pytest.mark.parametrize("env", ["strategy", "tokenomics", "epidemic"])
def test_every_env_has_a_frozen_demo_with_an_overfit_champion(env):
    rec = store.load_demo(env)
    assert rec["env"] == env and rec["mode"] == "cached" and rec["status"] == "done"
    rv = rec["reveal"]
    assert rv["verdict"] == "OVERFIT"
    assert rv["selected_iteration"] is not None and rv["selected_iteration"] != rv["dev_champion_iteration"]
    assert rec["audit"]["rounds"] and len(rec["audit"]["rounds"]) == len(rec["iterations"])


def test_demo_defaults_to_strategy_and_takes_an_env(client):
    assert client.get("/sealed/api/demo").json()["env"] == "strategy"
    assert client.get("/sealed/api/demo", params={"env": "tokenomics"}).json()["env"] == "tokenomics"
    assert client.get("/sealed/api/demo", params={"env": "nope"}).status_code == 400


def test_page_leads_with_the_strategy_headline(client):
    page = client.get("/sealed").text
    assert "Sealed Backtest: AI quant research that can&#39;t fool itself" in page or "Sealed Backtest: AI quant research that can't fool itself" in page
    for env in ("strategy", "tokenomics", "epidemic"):
        assert f'data-env="{env}"' in page


def test_guardrail_demo_queues_one_over_cap_payment(client):
    body = {"env": "strategy", "run_id": "20261025T131332Z-strategy-e5a1", "iteration": 1}
    first = client.post("/sealed/api/guardrail/demo", json=body).json()
    approval = first["approval"]
    assert approval["status"] == "pending" and approval["amount_usdc"] > first["capUsd"]
    assert approval["memo"] == "sealed:v1:deploy:20261025T131332Z-strategy-e5a1:1"
    again = client.post("/sealed/api/guardrail/demo", json=body).json()
    assert again["approval"]["id"] == approval["id"]
    assert [a["id"] for a in client.get("/api/cosign/pending").json()["pending"]] == [approval["id"]]
    assert client.post(f"/api/cosign/{approval['id']}/reject", json={"by": "test"}).json()["status"] == "rejected"
    fresh = client.post("/sealed/api/guardrail/demo", json=body).json()["approval"]
    assert fresh["id"] != approval["id"] and fresh["status"] == "pending"


def test_guardrail_demo_rejects_bad_input(client):
    assert client.post("/sealed/api/guardrail/demo", json={"env": "strategy", "run_id": "bad id!", "iteration": 1}).status_code == 400
    assert client.post("/sealed/api/guardrail/demo", json={"env": "strategy", "run_id": "r1", "iteration": True}).status_code == 400
    assert client.post("/sealed/api/guardrail/demo", json={"env": "nope", "run_id": "r1", "iteration": 1}).status_code == 400
