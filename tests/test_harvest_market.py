"""Harvest market engine: LMSR book, settlement, hash-chained ledger, fund, scoring, JSON API and R1.

No network: anchoring is off (HARVEST_ANCHOR=0) and the house-agent loop is off.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.harvest import fund, ledger, lmsr, market, scoring
from agent.harvest.market import FEE_RATE, Book, MarketError, decide
from agent.routes import harvest as routes
from agent.server import app

PERSONA = {"id": "climate-pessimist", "name": "Climate Pessimist", "color": "#d9480f", "world_model": "heat -> shocks",
           "beliefs": [], "heuristic": {"type": "momentum"}, "soft_lean": 0.15, "risk": 0.25, "edge": 0.03}
OTHER = {**PERSONA, "id": "adaptation-optimist", "name": "Adaptation Optimist", "soft_lean": -0.1}


def _q(qid, cutoff, split_title="Will wheat be >10% higher?"):
    return {"qid": qid, "spec_id": qid.split("@")[0], "kind": "hard", "title": split_title, "region": "Italy", "crop": "durum wheat",
            "adverse_if": "yes", "cutoff": cutoff, "series": "PWHEAMTUSDM", "base_month": cutoff, "base_value": 300.0,
            "resolve_month": "2026-12", "target": 330.0, "rule": None, "prior": 0.5}


def synthetic_result():
    """Dev cutoff (both personas), holdout cutoff (optimist better), live cutoff with one open market."""
    def cut(c, split, outcomes, fc):
        qs = [_q(q, c) for q in outcomes]
        return {"cutoff": c, "split": split, "questions": qs, "windows": {},
                "forecasts": [{"persona_id": pid, "qid": q, "p_yes": p, "rationale": "", "source": "fallback"} for (pid, q), p in fc.items()],
                "trades": [{"seq": 1, "trader": "agent:adaptation-optimist", "qid": next(iter(outcomes)), "side": "NO", "spend": 10.0, "fee": 0.2,
                            "shares": 20.0, "price_before": 0.5, "price_after": 0.45, "at": "", "cutoff": c}] if split != "live" else [],
                "final_prices": {q: 0.5 for q in outcomes},
                "resolutions": [{"qid": q, "outcome": o, "resolved_value": 1.0, "resolve_month": "x"} for q, o in outcomes.items()],
                "payouts": [{"trader": "agent:adaptation-optimist", "qid": next(iter(outcomes)), "shares": 20.0, "amount": 20.0}] if split != "live" else []}
    dev = cut("2023-03", "dev", {"a@2023-03": 1}, {("climate-pessimist", "a@2023-03"): 0.8, ("adaptation-optimist", "a@2023-03"): 0.6})
    hold = cut("2025-09", "holdout", {"a@2025-09": 0, "b@2025-09": 0},
               {("climate-pessimist", "a@2025-09"): 0.9, ("climate-pessimist", "b@2025-09"): 0.7,
                ("adaptation-optimist", "a@2025-09"): 0.2, ("adaptation-optimist", "b@2025-09"): 0.1})
    live = cut("2026-06", "live", {"a@2026-06": None}, {("climate-pessimist", "a@2026-06"): 0.8, ("adaptation-optimist", "a@2026-06"): 0.3})
    live["final_prices"] = {"a@2026-06": 0.4}
    return {"v": 1, "generated_at": "", "model": "test", "mask": {"commodities": {}, "seed": 1}, "personas": [PERSONA, OTHER],
            "cutoffs": [dev, hold, live], "commitments": [], "scores": {}}


SOFT = [{"id": "it-drought-2027", "kind": "soft", "title": "Will Italy declare a national drought emergency in 2027?", "deadline": "2027-12-31",
         "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "prior": 0.35, "why": ""}]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.delenv("PUBLIC_MODE", raising=False)
    result = synthetic_result()
    monkeypatch.setattr(market, "load_replay", lambda: result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: SOFT)
    monkeypatch.setattr(routes, "replay_result", lambda: result)
    market.reset_live_book()
    for limiter in routes.LIMITERS:
        limiter.reset()
    yield
    market.reset_live_book()


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------- book

def test_buy_moves_price_and_conserves_spend():
    book = Book()
    m = book.open("q", prior=0.3)
    assert book.price("q") == pytest.approx(0.3)
    q_yes, q_no = m["q_yes"], m["q_no"]
    t = book.buy("alice", "q", "YES", 100)
    assert t["price_after"] > t["price_before"] == pytest.approx(0.3)
    assert t["fee"] == pytest.approx(FEE_RATE * 100)
    assert lmsr.cost_to_buy(q_yes, q_no, m["b"], "YES", t["shares"]) == pytest.approx(100 - t["fee"])
    assert book.balance("alice") == pytest.approx(900)
    assert book.fund == pytest.approx(2.0)
    t2 = book.buy("bob", "q", "NO", 50)
    assert t2["price_after"] < t2["price_before"]
    assert [h[0] for h in m["history"]] == [0, 1, 2]


def test_resolve_pays_one_credit_per_winning_share():
    book = Book()
    book.open("q")
    yes = book.buy("alice", "q", "YES", 100)
    book.buy("bob", "q", "NO", 100)
    payouts = book.resolve("q", 1)
    assert payouts == [{"trader": "alice", "qid": "q", "shares": yes["shares"], "amount": yes["shares"]}]
    assert book.balance("alice") == pytest.approx(900 + yes["shares"])
    assert book.balance("bob") == pytest.approx(900)
    with pytest.raises(MarketError):
        book.buy("alice", "q", "YES", 1)


def test_insufficient_credits_and_bad_inputs_raise():
    book = Book()
    book.open("q")
    with pytest.raises(MarketError, match="insufficient"):
        book.buy("alice", "q", "YES", 1000.01)
    for args in (("alice", "q", "YES", 0), ("alice", "q", "MAYBE", 5), ("alice", "nope", "YES", 5)):
        with pytest.raises(MarketError):
            book.buy(*args)


def test_decide_follows_edge_and_caps_spend():
    assert decide(PERSONA, 0.52, 0.5, 1000) is None
    assert decide(PERSONA, 0.8, 0.5, 1000) == {"side": "YES", "spend": 75.0}
    assert decide(PERSONA, 0.1, 0.5, 1000)["side"] == "NO"
    assert decide({**PERSONA, "risk": 5.0}, 0.99, 0.01, 1000)["spend"] == 200.0


# ---------------------------------------------------------------- ledger

def test_ledger_chain_verifies_and_detects_tampering(tmp_path):
    for i in range(3):
        ledger.append(kind="bet", trader=f"user:{i}", payload={"qid": "q", "side": "YES", "spend": 1.5 + i})
    report = ledger.verify()
    assert report["ok"] and report["count"] == 3 and report["broken_at"] is None
    assert report["head"] == ledger.all_entries()[-1]["hash"]
    with sqlite3.connect(tmp_path / "harvest.sqlite3") as conn:
        conn.execute("UPDATE entries SET payload = ? WHERE seq = 2", ('{"qid":"q","side":"NO","spend":2.5}',))
    bad = ledger.verify()
    assert not bad["ok"] and bad["broken_at"] == 2


def test_anchorer_batches_ledger_and_users(monkeypatch):
    monkeypatch.setenv("HARVEST_ANCHOR", "1")
    monkeypatch.setenv("HARVEST_ANCHOR_EVERY", "2")
    sent = []

    async def fake_anchor(memo, **_):
        sent.append(memo)
        return {"status": "pending", "memo": memo, "signature": None, "explorer_url": None, "cluster": "devnet", "at": "", "error": "pending on-chain: test"}

    monkeypatch.setattr(ledger.chain, "anchor", fake_anchor)
    ledger.append(kind="join", trader="user:a", payload={"nickname": "a"})
    ledger.append(kind="bet", trader="user:a", payload={})
    import asyncio

    done = asyncio.run(ledger.Anchorer().maybe_anchor())
    head = ledger.all_entries()[-1]["hash"]
    assert sent == [ledger.memo_for(2, head), ledger.users_memo(1, head)]
    assert [d["status"] for d in done] == ["pending", "pending"]
    assert ledger.verify()["anchors_ok"]


# ---------------------------------------------------------------- fund and scoring

def test_fund_shares_sum_to_one_and_follow_risk():
    markets = [{"qid": "a", "region": "Italy", "crop": "wheat", "adverse_if": "yes", "p_yes": 0.8},
               {"qid": "b", "region": "Italy", "crop": "wheat", "adverse_if": "yes", "p_yes": 0.6},
               {"qid": "c", "region": "Spain", "crop": "olive oil", "adverse_if": "no", "p_yes": 0.9}]
    state = fund.allocate(markets, 50.0)
    assert sum(a["share"] for a in state["allocations"]) == pytest.approx(1.0)
    assert sum(a["amount"] for a in state["allocations"]) == pytest.approx(50.0)
    top = state["allocations"][0]
    assert (top["region"], top["risk"], top["questions"]) == ("Italy", pytest.approx(0.7), ["a", "b"])
    assert state["memo"].startswith("harvest:v1:fund:50.00:") and len(state["memo"].split(":")[-1]) == 32


def test_leaderboard_ranks_by_holdout_brier():
    lb = scoring.leaderboard(synthetic_result(), external=[{"persona_id": "ext:x", "name": "X"}])
    ids = [m["persona_id"] for m in lb["world_models"]]
    assert ids == ["adaptation-optimist", "climate-pessimist", "ext:x"]
    top = lb["world_models"][0]
    assert top["rank"] == 1 and top["brier_holdout"] == pytest.approx((0.04 + 0.01) / 2)
    assert top["pnl_holdout"] == pytest.approx(10.0)
    assert lb["world_models"][2]["origin"] == "external"
    assert lb["baselines"]["coin"] == 0.25 and lb["baselines"]["market"] == pytest.approx(0.25)
    assert "Adaptation Optimist world model beat the market" in scoring.insight(lb)


# ---------------------------------------------------------------- live book and API

def test_live_book_seeds_live_and_soft_markets_and_replays_ledger():
    book = market.live_book()
    assert set(book.markets) == {"a@2026-06", "it-drought-2027"}
    assert book.price("a@2026-06") == pytest.approx(0.4)
    assert len(book.agent_forecasts["it-drought-2027"]) == 2
    book.record(kind="join", trader="user:a", payload={"nickname": "a", "token_sha256": market.token_sha("tok")})
    book.place_bet("user:a", "a@2026-06", "YES", 40)
    price = book.price("a@2026-06")
    market.reset_live_book()
    again = market.live_book()
    assert again is not book and again.price("a@2026-06") == pytest.approx(price)
    assert again.trader_for("tok") == "user:a" and again.balance("user:a") == pytest.approx(960)


def test_house_step_trades_towards_forecast():
    book = market.live_book()
    trade = book.house_step()
    assert trade is not None and trade["trader"].startswith("agent:")


def test_state_and_since(client):
    r = client.get("/harvest/api/state")
    assert r.status_code == 200
    body = r.json()
    assert {"personas", "markets", "recent_trades", "leaderboard", "fund", "users", "ledger_head", "insight"} <= set(body)
    tok = client.post("/harvest/api/join", json={"nickname": "Ana"}).json()["token"]
    bet = client.post("/harvest/api/bet", json={"token": tok, "qid": "it-drought-2027", "side": "YES", "spend": 20}).json()
    seq = bet["trade"]["seq"]
    assert [t["seq"] for t in client.get(f"/harvest/api/state?since={seq - 1}").json()["recent_trades"]] == [seq]
    assert client.get(f"/harvest/api/state?since={seq}").json()["recent_trades"] == []
    assert client.get("/harvest/api/state").json()["users"] == 1
    for path in ("/harvest/api/markets", "/harvest/api/fund", "/harvest/api/geo", "/harvest/api/ledger", "/harvest/api/leaderboard", "/harvest/api/verify", "/harvest/api/holdout"):
        assert client.get(path).status_code == 200, path


def test_r1_external_agent_flow(client):
    doc = client.get("/harvest/api/agents").json()
    assert any(e["path"] == "/harvest/api/agents/register" for e in doc["endpoints"])
    reg = client.post("/harvest/api/agents/register", json={"name": "Drought Bear", "world_model": "Heat cuts southern yields.", "owner": "judges"})
    assert reg.status_code == 200
    agent = reg.json()
    assert agent["agent_id"].startswith("ext:drought-bear-") and agent["credits"] == 1000 and len(agent["world_model_sha256"]) == 64
    assert agent["credits_have_no_value"] is True and doc["credits_have_no_value"] is True
    markets = client.get("/harvest/api/agents/markets").json()["markets"]
    qid = next(m["qid"] for m in markets if m["kind"] == "hard")
    before = next(m["p_yes"] for m in markets if m["qid"] == qid)
    r = client.post("/harvest/api/agents/bet", json={"token": agent["token"], "qid": qid, "side": "YES", "spend": 50, "p_yes": 0.7, "rationale": "heat"})
    assert r.status_code == 200, r.text
    assert r.json()["market"]["p_yes"] > before and r.json()["balance"] == pytest.approx(950)
    lb = client.get("/harvest/api/leaderboard").json()
    ext = [m for m in lb["world_models"] if m["persona_id"] == agent["agent_id"]]
    assert ext and ext[0]["origin"] == "external"
    detail = client.get(f"/harvest/api/agents/{agent['agent_id']}").json()
    assert detail["world_model_sha256"] == agent["world_model_sha256"] and detail["positions"][0]["qid"] == qid
    kinds = [e["kind"] for e in client.get("/harvest/api/ledger").json()["entries"]]
    assert kinds == ["agent", "bet"]
    assert client.post("/harvest/api/agents/bet", json={"token": "nope", "qid": qid, "side": "YES", "spend": 5}).status_code == 401


def test_forecast_round_scores_against_world_models(client):
    tok = client.post("/harvest/api/join", json={"nickname": "Ben"}).json()["token"]
    qs = [q["qid"] for q in client.get("/harvest/api/holdout").json()["questions"]]
    assert qs == ["a@2025-09", "b@2025-09"]
    r = client.post("/harvest/api/forecast", json={"token": tok, "answers": [{"qid": q, "p_yes": 0.01} for q in qs]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rank_vs_world_models"] == 1 and body["brier"] == pytest.approx(0.0001)
    assert len(body["reveal"]) == 2
    humans = client.get("/harvest/api/leaderboard").json()["humans"]
    assert humans[0]["nickname"] == "Ben" and humans[0]["brier"] == pytest.approx(0.0001)


def test_bet_errors_are_clear(client):
    tok = client.post("/harvest/api/join", json={"nickname": "Cy"}).json()["token"]
    assert client.post("/harvest/api/bet", json={"token": tok, "qid": "nope", "side": "YES", "spend": 5}).status_code == 404
    assert client.post("/harvest/api/bet", json={"token": tok, "qid": "it-drought-2027", "side": "UP", "spend": 5}).status_code == 422
    r = client.post("/harvest/api/bet", json={"token": tok, "qid": "it-drought-2027", "side": "NO", "spend": 10_000})
    assert r.status_code == 200 and r.json()["trade"]["spend"] == 500
