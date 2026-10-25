"""Sealed Verify (agent/verify, agent/routes/merchants.py). No network: probes are monkeypatched."""

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agent.payments.config import SOLANA_DEVNET, USDC_DEVNET_MINT
from agent.routes import merchants as routes
from agent.server import app
from agent.verify import ledger, merchants, prober, probes, shopper
from agent.verify.ratelimit import RateLimiter

PAY_TO = "3VDpWHVvVpaQ6YWiFs7HhAyTXBZPWLB8KcLyp6ARSHG1"
OFFER_V2 = {
    "x402Version": 2,
    "error": "Payment required",
    "resource": {"url": "http://127.0.0.1:3001/premium-data", "description": "One premium fact"},
    "accepts": [{"scheme": "exact", "network": SOLANA_DEVNET, "asset": USDC_DEVNET_MINT, "amount": "10000", "payTo": PAY_TO, "maxTimeoutSeconds": 300}],
}


# ---------- x402 offer parsing ----------


def test_parses_v2_payment_required_header():
    header = base64.b64encode(json.dumps(OFFER_V2).encode()).decode()
    offer = probes.parse_x402_offer(402, {"Payment-Required": header}, b"{}")
    assert offer["amount"] == "10000" and offer["amount_label"] == "0.01 USDC"
    assert offer["network_label"] == "Solana devnet" and offer["pay_to"] == PAY_TO
    assert offer["asset"] == USDC_DEVNET_MINT and offer["version"] == 2


def test_parses_v1_json_body():
    body = {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "solana-devnet", "asset": USDC_DEVNET_MINT, "maxAmountRequired": "2500", "payTo": PAY_TO}]}
    offer = probes.parse_x402_offer(402, {}, json.dumps(body).encode())
    assert offer["amount_label"] == "0.0025 USDC" and offer["version"] == 1


@pytest.mark.parametrize(
    ("status", "headers", "body", "message"),
    [
        (200, {}, b"{}", "expected HTTP 402"),
        (402, {}, b"not json", "JSON body"),
        (402, {}, json.dumps({"accepts": []}).encode(), "no payment options"),
        (402, {}, json.dumps({"accepts": [{"scheme": "exact", "network": "n", "asset": "a", "amount": "5"}]}).encode(), "payTo"),
        (402, {}, json.dumps({"accepts": [{"scheme": "exact", "network": "n", "asset": "a", "payTo": PAY_TO, "amount": "-3"}]}).encode(), "positive integer"),
    ],
)
def test_rejects_invalid_offers(status, headers, body, message):
    with pytest.raises(probes.ParseError, match=message):
        probes.parse_x402_offer(status, headers, body)


def test_x402_probe_refuses_private_hosts():
    result = asyncio.run(probes.check_x402("http://127.0.0.1:9/pay"))
    assert result.status == "FAIL" and "non-public" in result.reason


def test_x402_probe_reports_unreachable_for_seeds():
    result = asyncio.run(probes.check_x402("http://127.0.0.1:9/pay", allow_private=True, timeout_s=2))
    assert result.status == "UNREACHABLE"


# ---------- Solana Pay ----------


def test_parses_solana_pay_transfer_url():
    url = probes.solana_pay_url(PAY_TO, label="Kings Café")
    parsed = probes.parse_solana_pay_url(url)
    assert parsed["recipient"] == PAY_TO and parsed["amount"] == "0.01"
    assert parsed["spl_token"] == USDC_DEVNET_MINT and parsed["label"] == "Kings Café"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("bitcoin:abc", "starts with solana:"),
        ("solana:notanaddress?amount=1", "not a Solana address"),
        ("solana:https%3A%2F%2Fexample.com%2Ftx", "transaction-request"),
        (f"solana:{PAY_TO}?amount=-1", "non-negative"),
        (f"solana:{PAY_TO}?amount=1e3", "plain"),
        (f"solana:{PAY_TO}?amount=1&spl-token=xyz", "mint address"),
    ],
)
def test_rejects_bad_solana_pay_urls(url, message):
    with pytest.raises(probes.ParseError, match=message):
        probes.parse_solana_pay_url(url)


def _fake_rpc(account, token_accounts):
    async def rpc(_client, _url, method, _params):
        if method == "getAccountInfo":
            return {"value": account}
        return {"value": [{"account": {"data": {"parsed": {"info": {"state": "initialized"}}}}}] * token_accounts}

    return rpc


@pytest.mark.parametrize(
    ("account", "tokens", "status", "words"),
    [
        ({"lamports": 5}, 1, "PASS", "payable"),
        (None, 1, "PASS", "token account only"),
        ({"lamports": 5}, 0, "FAIL", "no USDC token account"),
        (None, 0, "FAIL", "does not exist"),
    ],
)
def test_solana_pay_probe_outcomes(monkeypatch, account, tokens, status, words):
    monkeypatch.setattr(probes, "_rpc", _fake_rpc(account, tokens))
    result = asyncio.run(probes.check_solana_pay(probes.solana_pay_url(PAY_TO)))
    assert result.status == status and words in result.reason


def test_solana_pay_probe_unreachable_rpc(monkeypatch):
    async def down(*_args):
        raise RuntimeError("RPC getAccountInfo error: boom")

    monkeypatch.setattr(probes, "_rpc", down)
    result = asyncio.run(probes.check_solana_pay(probes.solana_pay_url(PAY_TO)))
    assert result.status == "UNREACHABLE"


def test_micropay_degrades_to_pending_without_a_wallet(monkeypatch):
    monkeypatch.delenv("AGENT_PRIVATE_KEY", raising=False)
    monkeypatch.setattr("agent.payments.transfer.load_agent_keypair", lambda: (_ for _ in ()).throw(__import__("agent.payments.config", fromlist=["x"]).PaymentsConfigError("unset")))
    result = asyncio.run(probes.micropay(PAY_TO, memo="cmap:v1:shop:1:x"))
    assert result.status == "PENDING" and result.reason.startswith("pending on-chain")


# ---------- ledger chain and schedule commitment ----------


def _entries(n):
    entries = []
    for i in range(n):
        ledger.append_entry(entries, {"at": f"2026-10-25T13:0{i}:00Z", "round": 1, "merchant_id": f"m{i}", "merchant_name": f"M{i}", "probe": "x402", "status": "PASS", "reason": "ok", "detail": {"a": i}, "verdict": "fine", "verdict_by": "rules"})
    return entries


def test_ledger_chain_links_and_verifies():
    entries = _entries(3)
    assert entries[0]["prev"] == ledger.GENESIS and entries[1]["prev"] == entries[0]["hash"]
    report = ledger.verify_chain(entries)
    assert report["ok"] and report["head"] == entries[-1]["hash"] and report["count"] == 3
    assert ledger.probe_memo(3, report["head"]) == f"cmap:v1:probe:3:{entries[-1]['hash']}"


def test_ledger_detects_tampering_and_deletion():
    entries = _entries(3)
    entries[1]["status"] = "FAIL"
    assert ledger.verify_chain(entries)["first_bad_seq"] == 2
    entries = _entries(3)
    del entries[1]
    report = ledger.verify_chain(entries)
    assert not report["ok"] and report["first_bad_seq"] == 2


def _round(salt="ab" * 16):
    schedule = ledger.build_schedule(4, [{"id": "a", "target": "solana:x"}, {"id": "b", "target": "https://b"}], [{"merchant_id": "b", "at": "2026-10-25T13:01:00Z", "type": "x402"}, {"merchant_id": "a", "at": "2026-10-25T13:00:30Z", "type": "solana_pay"}])
    commitment = ledger.schedule_commitment(schedule, salt)
    return {"round": 4, "kind": "scheduled", "commitment": commitment, "memo": ledger.sched_memo(4, commitment), "schedule": schedule, "salt": salt}


def test_schedule_commitment_recomputes():
    rec = _round()
    assert rec["memo"].startswith("cmap:v1:sched:4:")
    assert ledger.verify_round(rec)["ok"] is True
    assert ledger.schedule_commitment(rec["schedule"], "cd" * 16) != rec["commitment"]


def test_schedule_tampering_is_detected():
    rec = _round()
    rec["schedule"]["probes"][0]["at"] = "2026-10-25T14:00:00Z"
    assert ledger.verify_round(rec)["ok"] is False
    sealed = {**_round(), "schedule": None, "salt": None}
    assert ledger.verify_round(sealed)["ok"] is None


def test_probes_must_follow_the_sealed_schedule():
    rec = _round()
    done = [{"round": 4, "merchant_id": "a", "probe": "solana_pay"}]
    assert ledger.probes_follow_schedule(rec, done) is True
    assert ledger.probes_follow_schedule(rec, done + [{"round": 4, "merchant_id": "a", "probe": "micropay"}]) is False


# ---------- merchants: validation and pin status ----------


NEW_WALLET = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"


def _payload(**kw):
    return {"name": "Queens' Lane Coffee", "target": NEW_WALLET, "lat": 52.2025, "lng": 0.1150, "place": "Queens' Lane", **kw}


def test_onboarding_builds_a_solana_pay_link_from_a_wallet():
    m = merchants.build_onboarded(_payload(), [])
    assert m["kind"] == "solana_pay" and m["target"].startswith(f"solana:{NEW_WALLET}?amount=0.01&spl-token={USDC_DEVNET_MINT}")
    assert not m["seed"] and m["id"].startswith("m-")


def test_onboarding_sanitises_text():
    m = merchants.build_onboarded(_payload(name="  Tab\tand​zero   width  "), [])
    assert m["name"] == "Tab andzero width"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"name": "<script>alert(1)</script>"}, "may only use"),
        ({"name": "x"}, "2-48"),
        ({"lat": 123}, "lat must be between"),
        ({"lng": "nan"}, "lng must be between"),
        ({"lat": True}, "lat must be a number"),
        ({"target": "ftp://example.com/x"}, "target must be"),
        ({"target": "https://user:pw@example.com/pay"}, "credentials"),
        ({"target": "solana:bad"}, "Solana Pay URL rejected"),
        ({"target": "https://a.example/" + "x" * 400}, "max 300"),
        ({"target": "javascript:alert(1)"}, "target must be"),
    ],
)
def test_onboarding_rejects_bad_input(override, message):
    with pytest.raises(merchants.InvalidMerchant, match=message):
        merchants.build_onboarded(_payload(**override), [])


def test_onboarding_rejects_duplicates():
    first = merchants.build_onboarded(_payload(), [])
    with pytest.raises(merchants.InvalidMerchant, match="already on the map"):
        merchants.build_onboarded(_payload(name="Another Name"), [first])


def test_pin_status_colours():
    now = datetime.now(UTC)
    fresh = (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    shop = {"kind": "x402"}
    assert merchants.pin_status(shop, {"status": "PASS", "at": fresh}, now) == "green"
    assert merchants.pin_status(shop, {"status": "PASS", "at": old}, now) == "amber"
    assert merchants.pin_status(shop, {"status": "PENDING", "at": fresh}, now) == "amber"
    assert merchants.pin_status(shop, {"status": "UNREACHABLE", "at": fresh}, now) == "red"
    assert merchants.pin_status(shop, None, now) == "grey"
    assert merchants.pin_status({"kind": "physical"}, None, now) == "grey"


def test_seeds_include_broken_and_physical_merchants():
    seeds = merchants.seed_merchants()
    assert 6 <= len(seeds) <= 8
    assert all(s["note"].startswith("demo merchant") for s in seeds)
    targets = " ".join(s["target"] or "" for s in seeds)
    assert merchants.PAYER in targets and merchants.SELLER in targets and "/premium-data" in targets


def test_rate_limiter_window():
    limiter = RateLimiter(2, 60)
    assert limiter.hit("ip", now=0) == 0 and limiter.hit("ip", now=1) == 0
    assert limiter.hit("ip", now=2) > 0
    assert limiter.hit("other", now=2) == 0
    assert limiter.hit("ip", now=61) == 0


def test_rules_fallback_when_gemini_is_off(monkeypatch):
    monkeypatch.setenv("VERIFY_GEMINI", "0")
    seeds = merchants.seed_merchants()
    plan, by = asyncio.run(shopper.plan(seeds, {s["id"]: True for s in seeds}))
    assert by == "rules"
    assert plan["kings-parade-data"] == "x402" and plan["trinity-st-books"] == "micropay" and plan["market-hill-cafe"] == "solana_pay"
    assert "silver-st-bakery" not in plan
    lines, by = asyncio.run(shopper.verdicts([("Shop", {"probe": "x402", "status": "UNREACHABLE", "reason": "no answer within 6s"})]))
    assert by == "rules" and "Couldn't reach Shop" in lines[0]


# ---------- service and routes ----------


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("VERIFY_GEMINI", "0")

    async def fake_x402(url, *, allow_private=False, timeout_s=6):
        ok = "premium-data" in url
        return probes.ProbeResult("x402", "PASS" if ok else "UNREACHABLE", "402 offer: 0.01 USDC" if ok else "connection failed")

    async def fake_sp(url, **_kw):
        return probes.ProbeResult("solana_pay", "PASS", "payable")

    async def fake_micropay(recipient, *, memo, **_kw):
        return probes.ProbeResult("micropay", "PENDING", "pending on-chain: agent wallet not funded (devnet SOL/USDC)")

    monkeypatch.setattr(probes, "check_x402", fake_x402)
    monkeypatch.setattr(probes, "check_solana_pay", fake_sp)
    monkeypatch.setattr(probes, "micropay", fake_micropay)
    svc = prober.VerifyService(tmp_path, anchor_enabled=False)
    monkeypatch.setattr(prober, "_service", svc)
    for limiter in (routes.onboard_ip_limiter, routes.onboard_global_limiter, routes.reverify_ip_limiter, routes.reverify_merchant_limiter):
        limiter.reset()
    return svc


def test_round_seals_probes_and_verifies(service):
    rec = asyncio.run(service.run_round(kind="scheduled", window_s=(0, 0)))
    assert rec["revealed_at"] and rec["probe_count"] == 6
    pins = {p["id"]: p for p in service.public_state()["merchants"]}
    assert pins["kings-parade-data"]["status"] == "green"
    assert pins["dead-link-diner"]["status"] == "red"
    assert pins["trinity-st-books"]["status"] == "amber"
    assert pins["silver-st-bakery"]["status"] == "grey"
    report = service.verify()
    assert report["ok"] and report["rounds_checked"] == 1 and report["chain"]["count"] == 6
    service.state["entries"][2]["verdict"] = "edited"
    assert service.verify()["ok"] is False


def test_routes_page_state_and_verify(service):
    client = TestClient(app)
    page = client.get("/map")
    assert page.status_code == 200 and "Sealed Verify" in page.text and "leaflet" in page.text
    assert client.get("/map/static/map.js").status_code == 200
    assert client.get("/map/static/../server.py").status_code == 404
    state = client.get("/map/api/state").json()
    assert state["counters"]["merchants"] == 7 and state["counters"]["probes"] == 0
    assert client.get("/map/verify").json()["ok"] is True


def test_onboard_route_probes_and_rate_limits(service):
    client = TestClient(app)
    res = client.post("/map/api/onboard", json=_payload())
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["merchant"]["status"] == "green" and body["payment_link"].startswith("solana:")
    assert client.get("/map/api/state").json()["counters"]["onboarded_today"] == 1
    assert client.post("/map/api/onboard", json=_payload(name="<b>")).status_code == 400
    assert client.post("/map/api/onboard", json=_payload(name="Third Shop", target="https://example.com/x")).status_code in (201, 400)
    limited = client.post("/map/api/onboard", json=_payload(name="Fourth Shop"))
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_reverify_route_validates_and_rate_limits(service):
    client = TestClient(app)
    assert client.post("/map/api/reverify", json={"merchant_id": "../etc"}).status_code == 400
    assert client.post("/map/api/reverify", json={"merchant_id": "nope"}).status_code == 404
    assert client.post("/map/api/reverify", json={"merchant_id": "silver-st-bakery"}).status_code == 400
    ok = client.post("/map/api/reverify", json={"merchant_id": "kings-parade-data"})
    assert ok.status_code == 200 and ok.json()["merchant"]["status"] == "green"
    assert client.post("/map/api/reverify", json={"merchant_id": "kings-parade-data"}).status_code == 429
    assert client.post("/map/api/reverify", content=b"[1]", headers={"content-type": "application/json"}).status_code == 400
