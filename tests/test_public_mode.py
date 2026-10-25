"""PUBLIC_MODE=1 guard (agent/routes/public_mode.py): admin routes need X-Admin-Token, public routes work."""

import pytest
from fastapi.testclient import TestClient

from agent.challenge.ratelimit import RateLimiter
from agent.routes import challenge as challenge_routes
from agent.routes import public_mode
from agent.sealed import harness
from agent.server import app

ADMIN = "test-admin-token"
BASE = {"rule": "hold", "lookback_days": 20, "entry_threshold_pct": 5, "risk_on_sol_weight": 0.5,
        "risk_off_sol_weight": 0.5, "staked_share": 0, "rebalance_band": 0.1, "stop_loss_pct": 0}

ADMIN_ROUTES = [
    ("POST", "/sealed/api/runs"),
    ("POST", "/sealed/api/runs/sample-epidemic-0001/anchor"),
    ("POST", "/sealed/api/verify"),
    ("POST", "/sealed/api/guardrail/demo"),
    ("POST", "/api/cosign/cs_000000000000/approve"),
    ("POST", "/api/cosign/cs_000000000000/reject"),
    ("GET", "/api/cosign/pending"),
    ("GET", "/api/cosign/audit"),
    ("POST", "/api/buy"),
    ("GET", "/api/buy"),
    ("GET", "/premium-data"),
    ("POST", "/api/chat"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
    ("DELETE", "/challenge"),
    ("PUT", "/challenge/api/submit"),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_MODE", "1")
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("CHALLENGE_DB", str(tmp_path / "c.sqlite3"))
    monkeypatch.setenv("CHALLENGE_ANCHOR", "0")
    monkeypatch.setenv("COSIGN_DIR", str(tmp_path / "cosign"))
    monkeypatch.setenv("SEALED_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(public_mode, "request_limiter", RateLimiter(10_000))
    for limiter in (challenge_routes.submit_limiter, challenge_routes.parse_limiter, challenge_routes.score_limiter):
        limiter.reset()

    def no_runs(*args, **kwargs):
        raise AssertionError("a public request must never start a Sealed run")

    monkeypatch.setattr(harness, "start_background_run", no_runs)
    return TestClient(app)


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_public_request_to_admin_route_gets_401(client, method, path):
    res = client.request(method, path, json={"env": "strategy", "proposer": "scripted"})
    assert res.status_code == 401
    assert "X-Admin-Token" in res.json()["error"]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES[:7])
def test_wrong_admin_token_gets_403(client, method, path):
    res = client.request(method, path, json={}, headers={"X-Admin-Token": "nope"})
    assert res.status_code == 403


def test_admin_routes_are_closed_when_no_admin_token_is_configured(client, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN")
    assert client.post("/sealed/api/runs", json={}, headers={"X-Admin-Token": ""}).status_code == 403
    assert client.get("/api/cosign/pending", headers={"X-Admin-Token": "anything"}).status_code == 403


def test_right_admin_token_passes_through(client):
    res = client.get("/api/cosign/pending", headers={"X-Admin-Token": ADMIN})
    assert res.status_code == 200 and "pending" in res.json()
    res = client.post("/api/cosign/cs_000000000000/reject", json={}, headers={"X-Admin-Token": ADMIN})
    assert res.status_code not in (401, 403)


def test_verifier_routes_still_work(client, monkeypatch):
    monkeypatch.setenv("AGENT_STUDIO_VERIFIER_TOKEN", "verifier-secret")
    res = client.get("/agent-studio/v1/health", headers={"Authorization": "Bearer verifier-secret"})
    assert res.status_code == 200 and res.json()["status"] == "ready"
    # The verifier route keeps its own auth: no admin token involved, its own 401 shape.
    res = client.get("/agent-studio/v1/health")
    assert res.status_code == 401 and res.json()["error"]["code"] == "unauthorized"
    res = client.post("/agent-studio/v1/tasks", json={}, headers={"Authorization": "Bearer verifier-secret"})
    assert res.status_code not in (401, 403) and "protocol" in res.json()


@pytest.mark.parametrize(
    "path",
    ["/", "/race", "/challenge", "/challenge/board", "/challenge/qr", "/challenge/qr.svg", "/challenge/verify",
     "/challenge/api/config", "/challenge/api/stats", "/challenge/api/ledger", "/challenge/static/style.css",
     "/sealed", "/sealed/api/envs", "/sealed/api/demo", "/sealed/api/runs"],
)
def test_public_pages_and_read_only_sealed_gets_work(client, path):
    res = client.get(path)
    assert res.status_code == 200, (path, res.text[:200])
    assert res.headers["x-content-type-options"] == "nosniff"


def test_public_can_play_the_challenge(client):
    assert client.post("/challenge/api/score", json={"params": BASE}).status_code == 200
    res = client.post("/challenge/api/submit", json={"nickname": "judge", "params": BASE})
    assert res.status_code == 200 and res.json()["entry"]["seq"] == 1


def test_public_requests_have_a_per_ip_cap(client, monkeypatch):
    monkeypatch.setattr(public_mode, "request_limiter", RateLimiter(3))
    codes = [client.get("/challenge/api/stats", headers={"CF-Connecting-IP": "9.9.9.9"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert client.get("/challenge/api/stats", headers={"CF-Connecting-IP": "8.8.8.8"}).status_code == 200
    # A bogus admin header does not bypass the cap; a valid one does.
    assert client.get("/challenge/api/stats", headers={"CF-Connecting-IP": "9.9.9.9", "X-Admin-Token": "x"}).status_code == 429
    assert client.get("/challenge/api/stats", headers={"CF-Connecting-IP": "9.9.9.9", "X-Admin-Token": ADMIN}).status_code == 200


def test_guard_is_off_without_public_mode(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_MODE")
    res = client.get("/api/cosign/pending")
    assert res.status_code == 200
    assert "x-content-type-options" not in res.headers


@pytest.mark.parametrize(
    ("method", "path", "public"),
    [
        ("GET", "/sealed/api/runs/abc/bundle", True),
        ("HEAD", "/challenge", True),
        ("POST", "/challenge/api/submit", True),
        ("POST", "/challenge/api/score", True),
        ("POST", "/challenge/verify", False),
        ("POST", "/agent-studio/v1/tasks", True),
        ("GET", "/api/cosign/approvals", False),
        ("POST", "/cosign/x/approve", False),
        ("GET", "/challenges", False),
        ("GET", "/sealedx", False),
        ("PATCH", "/sealed", False),
    ],
)
def test_route_classification(method, path, public):
    assert public_mode.is_public(method, path) is public
