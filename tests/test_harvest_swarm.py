"""Agent swarm: names, pacing, baseline beliefs, bet sizing, reply parsing and the /harvest/api/swarm counter. Offline."""

import re
import time
from collections import deque
from datetime import UTC, datetime, timedelta

import pytest

from agent.harvest import forecaster, market, swarm
from agent.routes import harvest_swarm


def test_every_agent_is_tagged_and_fits_the_name_limit():
    agents = swarm.build_agents(swarm.MODELS, swarm.load_all_personas()) + swarm.build_baselines()
    assert len(agents) > len(swarm.MODELS)
    for a in agents:
        assert a.name.startswith(swarm.ROBOT + " ")
        assert len(a.name) <= swarm.NAME_MAX
        assert "…" not in a.name
        assert a.world_model.startswith("AI agent (not a human)")
        assert len(a.world_model) <= swarm.WM_MAX
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,23}", a.username)
    llm = [a for a in agents if a.kind == "llm"]
    assert all(a.model and a.model.label in a.name for a in llm)
    assert len({a.key for a in agents}) == len({a.username for a in agents}) == len(agents)


def test_pacer_spreads_budget_and_respects_rpm():
    now = 1000.0
    p = swarm.Pacer(rpm=10, budget=1000, until=now + 600)
    assert p.gap(now) == pytest.approx(6.0)
    p2 = swarm.Pacer(rpm=60, budget=10, until=now + 600)
    assert p2.gap(now) == pytest.approx(60.0)
    p2.take(now)
    assert p2.ready_in(now) == pytest.approx(600 / 9)
    p2.backoff(500, now)
    assert p2.ready_in(now) == pytest.approx(500)
    p3 = swarm.Pacer(rpm=60, budget=1, until=now + 600)
    p3.take(now)
    assert p3.ready_in(now) == float("inf")


def _market(p: float, **kw):
    return {"qid": "wheat-up10-6m@2026-09", "title": "Will wheat be above $272/mt by 2027-03?", "kind": "hard", "p_yes": p,
            "visible_history": [["m", 100 + i] for i in range(13)], "target_index": 112.0, "closes": "2027-03", **kw}


def test_baseline_beliefs_follow_their_rules():
    now = time.time()
    hist = deque([(now - 300, 0.40), (now - 150, 0.45), (now - 5, 0.50)])
    mom, slow, mr, _mr2, clim, _humble, contra, flow = swarm.build_baselines()
    b = swarm.baseline_belief(mom, _market(0.50), hist, now)
    assert b is not None and b[0] > 0.5
    b = swarm.baseline_belief(flow, _market(0.50), hist, now)
    assert b is not None and b[0] < 0.5
    b = swarm.baseline_belief(mr, _market(0.50), hist, now)
    assert b is not None and b[0] < 0.5
    b = swarm.baseline_belief(contra, _market(0.90), hist, now)
    assert b is not None and b[0] < 0.9
    c = swarm.baseline_belief(clim, _market(0.5), hist, now)
    assert c is not None and 0.03 <= c[0] <= 0.97
    assert swarm.climatology_p({**_market(0.5), "kind": "soft"}) == 0.3
    assert swarm.baseline_belief(slow, _market(0.5), deque(), now) is None


def test_bet_sizing_scales_with_edge_and_caps_bankroll():
    a = swarm.build_baselines()[0]
    assert swarm.size_bet(a, 0.51, 0.50) is None
    side, spend = swarm.size_bet(a, 0.70, 0.50)  # type: ignore[misc]
    assert side == "YES" and 1.0 <= spend <= 0.04 * a.bankroll
    side, spend = swarm.size_bet(a, 0.10, 0.90)  # type: ignore[misc]
    assert side == "NO" and spend == pytest.approx(min(0.04 * a.bankroll, 60.0))
    a.bankroll = 0.5
    assert swarm.size_bet(a, 0.9, 0.1) is None


def test_reply_parsing_strips_fences_and_thinking():
    text = '<think>hmm {not json}</think>```json\n{"forecasts": [{"qid": "Q1", "p_yes": 0.7, "rationale": "dry"}]}\n```'
    parsed = forecaster._parse(swarm.extract_json(text), 1)
    assert parsed["Q1"][0] == pytest.approx(0.7)
    with pytest.raises(ValueError):
        swarm.extract_json("no json here")


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setattr(market, "load_replay", lambda: None)
    monkeypatch.setattr(market, "load_soft_specs", lambda: [{"id": "q1", "title": "Q?", "prior": 0.4}])
    market.reset_live_book()
    yield market.live_book()
    market.reset_live_book()


def test_swarm_counter_counts_swarm_agents_and_last_hour_bets(book):
    book.record(kind="agent", trader="ext:bot-1", payload={"name": "\U0001f916 baseline · momentum fast", "owner": swarm.SWARM_OWNER})
    book.record(kind="agent", trader="ext:other", payload={"name": "someone", "owner": "team-x"})
    book.place_bet("ext:bot-1", "q1", "YES", 5)
    book.place_bet("ext:bot-1", "q1", "NO", 5)
    book.place_bet("ext:other", "q1", "YES", 5)
    view = harvest_swarm.swarm_view(book)
    assert view["agents"] == 1 and view["bets_last_hour"] == 2 and view["all_bets_last_hour"] == 3
    assert view["recent"][0]["name"].startswith("\U0001f916")
    later = harvest_swarm.swarm_view(book, now=datetime.now(UTC) + timedelta(hours=2))
    assert later["bets_last_hour"] == 0


def test_parse_reply_recovers_rows_from_broken_json():
    text = '{"forecasts": [{"qid": "Q1", "p_yes": 0.62, "rationale": "heat "risk" high"}, {"qid": "Q2", "p_yes": 0.2, "rationale": "calm"}, {"qid": "Q9", "p_yes": 0.5}'
    parsed = swarm.parse_reply(text, 2)
    assert parsed["Q1"][0] == pytest.approx(0.62) and parsed["Q2"] == (pytest.approx(0.2), "calm") and "Q9" not in parsed
    with pytest.raises(ValueError):
        swarm.parse_reply("I cannot answer", 2)


def test_beliefs_survive_a_restart(tmp_path):
    def make():
        return swarm.Swarm(base="http://x", until=time.time() + 60, stop_file=tmp_path / "stop", state_file=tmp_path / "accounts.json",
                           models=swarm.MODELS[:1], bets_per_min=10)
    first = make()
    a = next(x for x in first.agents if x.kind == "llm")
    a.beliefs, a.believed_at = {"q1": (0.7, "dry")}, 123.0
    first._save_beliefs()
    second = make()
    assert second.load_beliefs() == 1
    b = next(x for x in second.agents if x.key == a.key)
    assert b.beliefs == {"q1": (0.7, "dry")} and b.believed_at == 123.0
