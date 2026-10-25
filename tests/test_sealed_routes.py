"""HTTP API and page for Sealed (agent/routes/sealed.py). No network: W1/W2 calls are monkeypatched."""

import json

import pytest
from fastapi.testclient import TestClient

from agent.routes import sealed as routes
from agent.sealed import store
from agent.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SEALED_RUNS_DIR", str(tmp_path))
    return TestClient(app)


@pytest.fixture
def sample():
    return json.loads(store.SAMPLE_RUN.read_text(encoding="utf-8"))


def test_page_and_static_assets(client):
    page = client.get("/sealed")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "can&#39;t cheat" in page.text or "can't cheat" in page.text
    for asset in ("app.js", "style.css"):
        assert client.get(f"/sealed/static/{asset}").status_code == 200


def test_demo_returns_a_run_record(client):
    res = client.get("/sealed/api/demo")
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "sealed.run/v1"
    assert body["preregistration"]["commitment"]
    assert body["iterations"]


def test_demo_falls_back_to_sample_when_store_is_a_stub(client, monkeypatch):
    def stub(env="epidemic"):
        raise NotImplementedError

    monkeypatch.setattr(store, "load_demo", stub)
    res = client.get("/sealed/api/demo")
    assert res.status_code == 200
    assert res.json()["run_id"] == "sample-epidemic-0001"


def test_envs_lists_epidemic(client):
    names = [v["name"] for v in client.get("/sealed/api/envs").json()]
    assert "epidemic" in names


@pytest.mark.parametrize("path", ["/sealed/api/runs/bad.id", "/sealed/api/runs/bad.id/bundle", "/sealed/api/runs/" + "x" * 65])
def test_bad_run_id_is_400(client, path):
    res = client.get(path)
    assert res.status_code == 400
    assert "error" in res.json()


def test_unknown_run_is_404(client):
    for path in ("/sealed/api/runs/nope-123", "/sealed/api/runs/nope-123/bundle"):
        res = client.get(path)
        assert res.status_code == 404
        assert "error" in res.json()
    assert client.post("/sealed/api/runs/nope-123/anchor").status_code == 404


def test_stored_run_list_get_and_bundle(client, sample):
    store.RunStore().save(sample)
    listed = client.get("/sealed/api/runs").json()
    assert [s["run_id"] for s in listed] == [sample["run_id"]]
    got = client.get(f"/sealed/api/runs/{sample['run_id']}")
    assert got.status_code == 200 and got.json()["run_id"] == sample["run_id"]
    bundle = client.get(f"/sealed/api/runs/{sample['run_id']}/bundle")
    assert bundle.status_code == 200
    assert "attachment" in bundle.headers["content-disposition"]
    assert json.loads(bundle.text)["schema"] == "sealed.run/v1"


def test_anchor_saves_the_updated_record(client, sample, monkeypatch):
    store.RunStore().save(sample)

    async def fake_anchor_pending(record):
        record = dict(record)
        record["status"] = "done"
        record["error"] = "anchored by test"
        return record

    monkeypatch.setattr(routes.chain, "anchor_pending", fake_anchor_pending)
    res = client.post(f"/sealed/api/runs/{sample['run_id']}/anchor")
    assert res.status_code == 200 and res.json()["error"] == "anchored by test"
    assert store.RunStore().load(sample["run_id"])["error"] == "anchored by test"


@pytest.mark.parametrize(
    "body",
    [
        {"env": "nope", "iterations": 4, "proposer": "scripted"},
        {"env": "epidemic", "iterations": 4, "proposer": "gpt"},
        {"env": "epidemic", "iterations": 0, "proposer": "scripted"},
        {"env": "epidemic", "iterations": 13, "proposer": "scripted"},
        {"env": "epidemic", "iterations": "8", "proposer": "scripted"},
        {"env": "epidemic", "iterations": 8, "proposer": "scripted", "anchor": "yes"},
        [],
    ],
)
def test_start_run_validation(client, monkeypatch, body):
    def must_not_run(*args, **kwargs):
        raise AssertionError("start_background_run called for an invalid body")

    monkeypatch.setattr(routes.harness, "start_background_run", must_not_run)
    res = client.post("/sealed/api/runs", json=body)
    assert res.status_code == 400
    assert "error" in res.json()


def test_start_run_returns_202(client, monkeypatch):
    calls = []

    def fake_start(env, *, iterations, proposer, anchor):
        calls.append((env, iterations, proposer, anchor))
        return "20261025T133000Z-epidemic-test"

    monkeypatch.setattr(routes.harness, "start_background_run", fake_start)
    res = client.post("/sealed/api/runs", json={"env": "epidemic", "iterations": 3, "proposer": "scripted", "anchor": False})
    assert res.status_code == 202
    assert res.json() == {"run_id": "20261025T133000Z-epidemic-test"}
    assert calls == [("epidemic", 3, "scripted", False)]


def test_start_run_stub_gives_501(client, monkeypatch):
    def stub(*args, **kwargs):
        raise NotImplementedError("harness not landed")

    monkeypatch.setattr(routes.harness, "start_background_run", stub)
    res = client.post("/sealed/api/runs", json={"env": "epidemic", "iterations": 3, "proposer": "scripted"})
    assert res.status_code == 501
    assert "error" in res.json()


def test_verify_returns_the_verify_report(client, sample, monkeypatch):
    report = {"ok": True, "code_match": True, "commitment_recomputed": "a", "commitment_recorded": "a", "commitment_onchain": "a",
              "commit_match": True, "result_recomputed": "b", "result_recorded": "b", "result_onchain": "b", "result_match": True,
              "onchain_status": "confirmed", "reasons": []}
    seen = {}
    fetched = []

    async def fake_fetch(signature):
        fetched.append(signature)
        return f"memo-for-{signature}"

    def fake_verify(record, env, *, commit_memo_onchain, reveal_memo_onchain):
        seen.update(run_id=record["run_id"], env=env.name, commit=commit_memo_onchain, reveal=reveal_memo_onchain)
        return report

    sample["preregistration"]["chain"].update(status="confirmed", signature="SIGCOMMIT")
    monkeypatch.setattr(routes.chain, "fetch_memo", fake_fetch)
    monkeypatch.setattr(routes.prereg, "verify_bundle", fake_verify)
    res = client.post("/sealed/api/verify", json={"record": sample})
    assert res.status_code == 200
    assert res.json() == report
    assert fetched == ["SIGCOMMIT"]  # the reveal receipt is pending, so it is not fetched
    assert seen == {"run_id": sample["run_id"], "env": "epidemic", "commit": "memo-for-SIGCOMMIT", "reveal": None}


def test_verify_rejects_bad_bodies(client):
    assert client.post("/sealed/api/verify", json={"nope": 1}).status_code == 400
    assert client.post("/sealed/api/verify", json={"record": {"env": "nope"}}).status_code == 400


def test_verify_real_bundle_detects_tampering(client, sample):
    honest = client.post("/sealed/api/verify", json={"record": sample}).json()
    assert honest["commit_match"] and honest["result_match"]
    sample["iterations"][4]["candidate"]["surge_beds"] += 1
    tampered = client.post("/sealed/api/verify", json={"record": sample}).json()
    assert tampered["ok"] is False and tampered["result_match"] is False
