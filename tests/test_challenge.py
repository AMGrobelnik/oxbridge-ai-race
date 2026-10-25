"""Beat the Seal: scoring, input validation, the hash chain and verify, rate limits, anchoring, routes.

No network: Gemini and devnet calls are monkeypatched.
"""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.challenge import game, ledger
from agent.challenge.ratelimit import RateLimiter
from agent.routes import challenge as routes
from agent.sealed import chain
from agent.server import app

BASE = game.environment().baseline()
GENERALISES = {**BASE, "risk_on_sol_weight": 0.4, "staked_share": 1.0, "rebalance_band": 0.2}
OVERFIT = {**BASE, "rule": "mean_reversion", "risk_on_sol_weight": 1.0, "risk_off_sol_weight": 0.2, "stop_loss_pct": 10, "lookback_days": 10}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CHALLENGE_DB", str(tmp_path / "challenge.sqlite3"))
    monkeypatch.setenv("CHALLENGE_ANCHOR", "0")
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    for limiter in (routes.submit_limiter, routes.parse_limiter, routes.score_limiter, routes.global_submit_limiter):
        limiter.reset()


@pytest.fixture
def client():
    return TestClient(app)


def submit(client, nickname, params, ip="10.0.0.1"):
    return client.post("/challenge/api/submit", json={"nickname": nickname, "params": params}, headers={"CF-Connecting-IP": ip})


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice", "alice"),
        ("  Bob   the  Builder ", "Bob the Builder"),
        ("<script>alert(1)</script>", "scriptalert1script"),
        ('<img src=x onerror="x">', "img srcx onerrorx"),
        ("x" * 40, "x" * 24),
        ("ｆｕｌｌｗｉｄｔｈ", "fullwidth"),
        ("o'neil-2.0_!", "o'neil-2.0_!"),
    ],
)
def test_nickname_is_sanitised(raw, expected):
    nick = game.sanitise_nickname(raw)
    assert nick == expected
    assert len(nick) <= 24 and "<" not in nick and ">" not in nick and '"' not in nick


@pytest.mark.parametrize("raw", ["", "   ", "<>&", None, 42, ["a"]])
def test_bad_nicknames_are_rejected(raw):
    with pytest.raises(game.InvalidInput):
        game.sanitise_nickname(raw)


def test_nick_hash_ignores_case():
    assert game.nick_sha256("Alice") == game.nick_sha256("alice") != game.nick_sha256("bob")


def test_validate_params_accepts_the_schema_and_normalises_numbers():
    params = game.validate_params({**BASE, "lookback_days": 20.0, "risk_on_sol_weight": 0.123456789})
    assert params["lookback_days"] == 20 and isinstance(params["lookback_days"], int)
    assert params["risk_on_sol_weight"] == 0.1235


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {**BASE, "rule": "yolo"},
        {**BASE, "extra": 1},
        {**BASE, "risk_on_sol_weight": 1.5},
        {**BASE, "lookback_days": 2.5},
        {**BASE, "stop_loss_pct": True},
        {**BASE, "staked_share": "0.5"},
        {k: v for k, v in BASE.items() if k != "rebalance_band"},
    ],
)
def test_validate_params_rejects_anything_off_schema(bad):
    with pytest.raises(game.InvalidInput):
        game.validate_params(bad)


def test_coerce_params_clamps_model_output():
    params = game.coerce_params({"rule": "Mean Reversion", "risk_on_sol_weight": 3, "lookback_days": 7.6, "stop_loss_pct": "10%", "junk": 1}, BASE)
    assert params["rule"] == "mean_reversion"
    assert params["risk_on_sol_weight"] == 1 and params["lookback_days"] == 8 and params["stop_loss_pct"] == 10
    assert "junk" not in params and game.environment().validate(params) == []


def test_heuristic_parser_reads_the_example_sentence():
    params, summary = game.heuristic_params("buy dips hard, stop at 10%", BASE)
    assert params["rule"] == "mean_reversion"
    assert params["risk_on_sol_weight"] == 1 and params["stop_loss_pct"] == 10
    assert "stop at 10%" in summary


def test_sentence_parser_uses_gemini_and_validates(monkeypatch):
    async def fake(prompt):
        assert "buy dips" in prompt
        return json.dumps({"params": {"rule": "momentum", "risk_on_sol_weight": 9, "lookback_days": 14}, "summary": "trend"})

    monkeypatch.setattr(game, "_ask_fast", fake)
    out = asyncio.run(game.params_from_sentence("buy dips", BASE))
    assert out["source"] == "gemini" and out["params"]["rule"] == "momentum"
    assert out["params"]["risk_on_sol_weight"] == 1 and game.environment().validate(out["params"]) == []


def test_sentence_parser_falls_back_to_keywords(monkeypatch):
    async def broken(prompt):
        raise RuntimeError("no key")

    monkeypatch.setattr(game, "_ask_fast", broken)
    out = asyncio.run(game.params_from_sentence("follow the trend, stake everything", BASE))
    assert out["source"] == "keywords" and out["params"]["rule"] == "momentum" and out["params"]["staked_share"] == 1


# ---------------------------------------------------------------- scoring

def test_reveal_scores_dev_and_sealed_with_the_sealed_gate():
    base = game.reveal(BASE)
    assert base["verdict"] == "GENERALISES"
    assert set(base["per_shift"]) == {"base", "crash", "usdc_depeg", "fee_spike", "vol_spike"}
    assert base["sealed_score"] == pytest.approx(sum(base["per_shift"].values()) / 5, abs=1e-3)
    assert base["drop"] == pytest.approx(base["dev_score"] - base["sealed_score"], abs=1e-3)
    assert game.reveal(GENERALISES)["verdict"] == "GENERALISES"
    overfit = game.reveal(OVERFIT)
    assert overfit["verdict"] == "OVERFIT" and overfit["reasons"] and overfit["drop"] > 0
    assert game.reveal(OVERFIT) == overfit  # deterministic


def test_dev_report_matches_the_plugin():
    assert game.dev_report(BASE)["dev_score"] == game.environment().dev_report(BASE)["score"]


# ---------------------------------------------------------------- chain and verify

def test_chain_links_and_verifies(client):
    for i, params in enumerate([BASE, OVERFIT, GENERALISES]):
        assert submit(client, f"p{i}", params).status_code == 200
    entries = ledger.all_entries()
    assert [e["seq"] for e in entries] == [1, 2, 3]
    assert entries[0]["prev"] == ledger.GENESIS
    assert all(entries[i]["prev"] == entries[i - 1]["hash"] for i in (1, 2))
    assert all(ledger.entry_hash(e) == e["hash"] for e in entries)
    report = client.get("/challenge/verify?rescore=10").json()
    assert report["ok"] and report["length"] == 3 and report["head_recomputed"] == entries[-1]["hash"]
    assert [r["match"] for r in report["rescored"]] == [True, True, True]


def test_verify_detects_an_edited_score(client):
    for i in range(3):
        submit(client, f"p{i}", BASE)
    with sqlite3.connect(ledger.db_path()) as conn:
        conn.execute("UPDATE submissions SET sealed_score = 999 WHERE seq = 2")
    report = client.get("/challenge/verify").json()
    assert not report["ok"] and report["broken_at"] == 2


def test_verify_rescoring_catches_a_reforged_chain(client):
    submit(client, "p", BASE)
    with sqlite3.connect(ledger.db_path()) as conn:
        conn.execute("UPDATE submissions SET sealed_score = 120 WHERE seq = 1")
    entry = ledger.all_entries()[0]
    with sqlite3.connect(ledger.db_path()) as conn:  # recompute the hash so the chain itself looks fine
        conn.execute("UPDATE submissions SET hash = ? WHERE seq = 1", (ledger.entry_hash(entry),))
    assert client.get("/challenge/verify").json()["ok"]
    report = client.get("/challenge/verify?rescore=5").json()
    assert not report["ok"] and report["rescored"] == [{"seq": 1, "match": False}]


def test_verify_detects_a_forged_anchor(client):
    submit(client, "p", BASE)
    head = ledger.all_entries()[0]["hash"]
    ledger.record_anchor(1, head, chain.pending_receipt(ledger.memo_for(1, head), "pending on-chain: test"))
    assert client.get("/challenge/verify").json()["ok"]
    ledger.record_anchor(1, "f" * 64, chain.pending_receipt(ledger.memo_for(1, "f" * 64), "x"))
    report = client.get("/challenge/verify").json()
    assert not report["ok"] and report["anchors"][-1]["match"] is False


def test_public_ledger_hides_nicknames(client):
    submit(client, "secret name", BASE)
    body = client.get("/challenge/api/ledger").json()
    assert body["genesis"] == ledger.GENESIS and len(body["entries"]) == 1
    assert "nickname" not in body["entries"][0] and "secret name" not in json.dumps(body)


# ---------------------------------------------------------------- anchoring

def test_anchor_batches_every_n_with_the_existing_pending_fallback(monkeypatch):
    from agent.payments.config import PaymentsConfigError

    async def unfunded(memo, to, amount, timeout_s):
        raise PaymentsConfigError("no key")

    monkeypatch.setattr(chain, "_send", unfunded)
    monkeypatch.setenv("CHALLENGE_ANCHOR", "1")
    monkeypatch.setenv("CHALLENGE_ANCHOR_EVERY", "2")
    anchorer = ledger.Anchorer()
    result = game.reveal(BASE)
    ledger.append(nickname="a", nick_sha256=game.nick_sha256("a"), params=BASE, result=result)
    assert asyncio.run(anchorer.maybe_anchor()) is None  # 1 < 2 and the interval has not passed
    ledger.append(nickname="b", nick_sha256=game.nick_sha256("b"), params=BASE, result=result)
    receipt = asyncio.run(anchorer.maybe_anchor())
    head = ledger.all_entries()[-1]["hash"]
    assert receipt["memo"] == f"sealed:v1:challenge:2:{head}"
    assert receipt["status"] == "pending" and receipt["error"].startswith("pending on-chain")
    assert ledger.stats()["last_anchor"]["status"] == "pending"
    assert asyncio.run(anchorer.maybe_anchor()) is None  # nothing new since count 2
    assert ledger.verify()["ok"]


def test_anchor_fires_on_the_interval_and_records_confirmed(monkeypatch):
    sent = []

    async def ok(memo, *, to=None, amount_usdc=None, timeout_s=45):
        sent.append(memo)
        return chain.confirmed_receipt(memo, "5" * 64)

    monkeypatch.setattr(chain, "anchor", ok)
    monkeypatch.setenv("CHALLENGE_ANCHOR", "1")
    anchorer = ledger.Anchorer()
    ledger.append(nickname="a", nick_sha256=game.nick_sha256("a"), params=BASE, result=game.reveal(BASE))
    assert anchorer.due() is None
    anchorer._last_attempt -= anchorer.interval_s + 1
    asyncio.run(anchorer.maybe_anchor())
    assert sent and sent[0].startswith("sealed:v1:challenge:1:")
    assert ledger.stats()["last_confirmed_anchor"]["explorer_url"].endswith("?cluster=devnet")


def test_anchoring_can_be_disabled(monkeypatch):
    monkeypatch.setattr(chain, "anchor", None)  # would raise if called
    anchorer = ledger.Anchorer()
    ledger.append(nickname="a", nick_sha256=game.nick_sha256("a"), params=BASE, result=game.reveal(BASE))
    anchorer._last_attempt -= 10_000
    assert asyncio.run(anchorer.maybe_anchor()) is None


# ---------------------------------------------------------------- rate limits

def test_rate_limiter_window():
    limiter = RateLimiter(2, window_s=60)
    assert limiter.hit("ip", now=0) == 0 and limiter.hit("ip", now=1) == 0
    assert limiter.hit("ip", now=2) == pytest.approx(58)
    assert limiter.hit("other", now=2) == 0
    assert limiter.hit("ip", now=60.5) == 0


def test_submissions_are_rate_limited_per_ip(client, monkeypatch):
    monkeypatch.setattr(routes, "submit_limiter", RateLimiter(2))
    assert submit(client, "a", BASE, ip="1.1.1.1").status_code == 200
    assert submit(client, "a", BASE, ip="1.1.1.1").status_code == 200
    blocked = submit(client, "a", BASE, ip="1.1.1.1")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert submit(client, "b", BASE, ip="2.2.2.2").status_code == 200
    assert ledger.stats()["sealed"] == 3


def test_default_submit_limit_is_20_per_minute(client):
    codes = [submit(client, "a", BASE, ip="3.3.3.3").status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


# ---------------------------------------------------------------- routes

def test_pages_and_assets(client):
    for path in ("/challenge", "/challenge/board"):
        res = client.get(path)
        assert res.status_code == 200 and "Beat the Seal" in res.text
    for asset in ("style.css", "common.js", "challenge.js", "board.js"):
        assert client.get(f"/challenge/static/{asset}").status_code == 200


def test_qr_uses_public_base_url(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://beat.example.org/")
    svg = client.get("/challenge/qr.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml") and "<svg" in svg.text
    page = client.get("/challenge/qr")
    assert "https://beat.example.org/challenge" in page.text and "<svg" in page.text
    assert client.get("/challenge/api/config").json()["public_url"] == "https://beat.example.org/challenge"


def test_score_endpoint(client):
    res = client.post("/challenge/api/score", json={"params": BASE})
    assert res.status_code == 200 and res.json()["dev_score"] == game.dev_report(BASE)["dev_score"]
    assert client.post("/challenge/api/score", json={"params": {**BASE, "rule": "x"}}).status_code == 422
    assert client.post("/challenge/api/score", content=b"not json").status_code == 400
    assert client.post("/challenge/api/score", content=b"{" + b" " * 5000 + b"}").status_code == 413


def test_parse_endpoint_falls_back_without_gemini(client, monkeypatch):
    async def broken(prompt):
        raise RuntimeError("quota")

    monkeypatch.setattr(game, "_ask_fast", broken)
    res = client.post("/challenge/api/parse", json={"sentence": "buy dips hard, stop at 10%"})
    body = res.json()
    assert res.status_code == 200 and body["source"] == "keywords" and body["params"]["stop_loss_pct"] == 10
    assert "dev_score" in body
    assert client.post("/challenge/api/parse", json={"sentence": "   "}).status_code == 422


def test_submit_rejects_bad_input(client):
    assert submit(client, "", BASE).status_code == 422
    assert submit(client, "ok", {**BASE, "staked_share": 2}).status_code == 422
    assert ledger.stats()["sealed"] == 0


def test_leaderboard_wall_and_counters(client):
    submit(client, "Alice", BASE)
    submit(client, "alice", GENERALISES)  # same player (case-folded), better sealed score
    submit(client, "Bob", OVERFIT)
    res = submit(client, "<b>Carol</b>", GENERALISES).json()
    assert res["nickname"] == "bCarolb" and res["rank"] in (1, 2)
    state = client.get("/challenge/api/stats").json()
    assert state["stats"]["players"] == 3 and state["stats"]["sealed"] == 4
    assert state["stats"]["generalises"] == 3 and state["stats"]["overfit"] == 1
    board = state["leaderboard"]
    assert all(e["verdict"] == "GENERALISES" for e in board)
    assert [e["nickname"] for e in board].count("alice") + [e["nickname"] for e in board].count("Alice") == 1
    assert board == sorted(board, key=lambda e: -e["sealed_score"])
    assert [e["nickname"] for e in state["overfit_wall"]] == ["Bob"]
    assert state["stats"]["chain_head"] == ledger.all_entries()[-1]["hash"]
