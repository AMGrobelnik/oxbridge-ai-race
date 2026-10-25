"""PUBLIC_MODE=1 guard (agent/routes/public_mode.py): admin routes need X-Admin-Token, public routes work."""

import pytest
from fastapi.testclient import TestClient

from agent.challenge.ratelimit import RateLimiter
from agent.routes import challenge as challenge_routes
from agent.routes import merchants as map_routes
from agent.routes import public_mode
from agent.sealed import harness
from agent.verify import prober
from agent.server import app

ADMIN = "test-admin-token"
BASE = {"rule": "hold", "lookback_days": 20, "entry_threshold_pct": 5, "risk_on_sol_weight": 0.5,
        "risk_off_sol_weight": 0.5, "staked_share": 0, "rebalance_band": 0.1, "stop_loss_pct": 0}

ADMIN_ROUTES = [
    ("POST", "/sealed/api/runs"),
    ("POST", "/sealed/api/runs/sample-epidemic-0001/anchor"),
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
    monkeypatch.setattr(public_mode, "verify_limiter", RateLimiter(10_000))
    monkeypatch.setattr(prober, "_service", prober.VerifyService(tmp_path / "verify", anchor_enabled=False))
    for limiter in (map_routes.onboard_ip_limiter, map_routes.onboard_global_limiter, map_routes.reverify_ip_limiter, map_routes.reverify_merchant_limiter):
        limiter.reset()
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
        ("GET", "/map", True),
        ("GET", "/map/static/map.js", True),
        ("GET", "/map/api/state", True),
        ("GET", "/map/verify", True),
        ("POST", "/map/api/onboard", True),
        ("POST", "/map/api/reverify", True),
        ("POST", "/sealed/api/verify", True),
        ("GET", "/map/api/other", False),
        ("POST", "/map/api/state", False),
        ("DELETE", "/map", False),
        ("GET", "/mapx", False),
        ("POST", "/sealed/api/runs", False),
        ("POST", "/sealed/api/verify/x", False),
    ],
)
def test_route_classification(method, path, public):
    assert public_mode.is_public(method, path) is public


@pytest.mark.parametrize("path", ["/map", "/map/static/map.js", "/map/static/map.css", "/map/api/state", "/map/api/ledger", "/map/verify"])
def test_public_map_reads_work(client, path):
    res = client.get(path)
    assert res.status_code == 200, (path, res.text[:200])
    assert res.headers["x-content-type-options"] == "nosniff"


def test_public_can_use_map_posts_without_admin_token(client):
    # Validation answers (400/404), not the admin guard (401): no probe runs for these inputs.
    res = client.post("/map/api/onboard", json={"name": "<b>", "target": "x", "lat": 0, "lng": 0})
    assert res.status_code == 400 and "X-Admin-Token" not in res.text
    assert client.post("/map/api/reverify", json={"merchant_id": "nope"}).status_code == 404


def test_public_sealed_verify_is_open_and_capped(client, monkeypatch):
    monkeypatch.setattr(public_mode, "verify_limiter", RateLimiter(2))
    codes = [client.post("/sealed/api/verify", json={}, headers={"CF-Connecting-IP": "7.7.7.7"}).status_code for _ in range(3)]
    assert codes == [400, 400, 429]
    assert client.post("/sealed/api/verify", json={}, headers={"CF-Connecting-IP": "6.6.6.6"}).status_code == 400
    # The admin routes next to it stay closed.
    assert client.post("/sealed/api/runs", json={}).status_code == 401
    assert client.post("/sealed/api/runs/sample-epidemic-0001/anchor", json={}).status_code == 401


def test_client_ip_trusts_forwarding_headers_only_from_the_local_proxy():
    from starlette.requests import Request

    def req(peer: str, headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": (peer, 1234)})

    from agent.challenge.ratelimit import client_ip
    assert client_ip(req("127.0.0.1", {"CF-Connecting-IP": "5.6.7.8"})) == "5.6.7.8"
    assert client_ip(req("127.0.0.1", {})) == "127.0.0.1"
    assert client_ip(req("8.8.4.4", {"CF-Connecting-IP": "5.6.7.8"})) == "8.8.4.4"


def test_default_public_get_limit_fits_a_venue_nat():
    assert public_mode.request_limiter.limit >= 3000
