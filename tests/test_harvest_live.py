"""Live market engine: deeper live liquidity, climate fixture, live re-forecasting. Offline: the LLM is stubbed."""

import json
import random

import pytest

from agent.harvest import climate, forecaster, live_forecast, market, replay
from agent.harvest.market import DEFAULT_B, Book

PERSONA = {"id": "climate-pessimist", "name": "Climate Pessimist", "color": "#d9480f", "world_model": "heat -> shocks",
           "beliefs": ["droughts cut yields"], "heuristic": {"type": "momentum"}, "soft_lean": 0.15, "risk": 0.25, "edge": 0.03}
OTHER = {**PERSONA, "id": "adaptation-optimist", "name": "Adaptation Optimist", "soft_lean": -0.1}
HARD = {"qid": "a@2026-06", "spec_id": "a", "kind": "hard", "title": "Will wheat be more than 10% higher by 2026-12?", "region": "Italy",
        "crop": "durum wheat", "adverse_if": "yes", "cutoff": "2026-06", "series": "PWHEAMTUSDM", "base_month": "2026-06",
        "base_value": 300.0, "resolve_month": "2026-12", "target": 330.0, "rule": None, "prior": 0.5}
SOFT = [{"id": "it-drought-2027", "kind": "soft", "title": "Will Italy declare a national drought emergency in 2027?", "deadline": "2027-12-31",
         "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "prior": 0.35, "why": ""}]


def _result():
    live = {"cutoff": "2026-06", "split": "live", "questions": [HARD], "windows": {}, "trades": [], "resolutions": [], "payouts": [],
            "forecasts": [{"persona_id": p, "qid": HARD["qid"], "p_yes": 0.4, "rationale": "replay belief", "source": "cache"}
                          for p in ("climate-pessimist", "adaptation-optimist")],
            "final_prices": {HARD["qid"]: 0.4}}
    return {"personas": [PERSONA, OTHER], "cutoffs": [live]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_DB", str(tmp_path / "harvest.sqlite3"))
    monkeypatch.setenv("HARVEST_ANCHOR", "0")
    monkeypatch.setenv("HARVEST_HOUSE_TICK_S", "0")
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.delenv("LIVE_B", raising=False)
    monkeypatch.setattr(market, "load_replay", _result)
    monkeypatch.setattr(market, "load_soft_specs", lambda: SOFT)
    monkeypatch.setattr(live_forecast, "gemini_ask", lambda prompt: pytest.fail("network LLM called in a test"))
    market.reset_live_book()
    live_forecast.reset()
    yield
    market.reset_live_book()
    live_forecast.reset()


def _move(b: float, prior: float = 0.5, spend: float = 25.0) -> float:
    book = Book()
    book.open("q", prior=prior, b=b)
    t = book.buy("u", "q", "YES", spend)
    return (t["price_after"] - t["price_before"]) * 100


# ---------------------------------------------------------------- liquidity
def test_live_book_is_deeper_and_keeps_seeded_prices():
    book = market.live_book()
    assert book.markets[HARD["qid"]]["b"] == 400.0
    assert book.price(HARD["qid"]) == pytest.approx(0.4)
    assert book.price("it-drought-2027") == pytest.approx(0.35)
    assert 2.5 <= _move(400.0) <= 5.0
    assert 2.5 <= _move(400.0, prior=0.3) <= 5.0
    trade, _ = book.place_bet("user:x", HARD["qid"], "YES", 25.0)
    assert (trade["price_after"] - trade["price_before"]) * 100 < 5.0


def test_live_b_env_and_replay_keeps_default(monkeypatch):
    monkeypatch.setenv("LIVE_B", "250")
    assert market.live_book().markets[HARD["qid"]]["b"] == 250.0
    monkeypatch.setenv("LIVE_B", "nonsense")
    assert market.live_b() == 400.0
    assert DEFAULT_B == 100.0 and Book().open("r").get("b") == DEFAULT_B
    assert _move(DEFAULT_B) > 9.0
    import inspect

    assert "market.Book()" in inspect.getsource(replay.run)  # the replay builds plain Books at DEFAULT_B


# ---------------------------------------------------------------- climate
def test_climate_fixture_loads_both_series():
    data = climate.load_climate()
    assert data is not None and not data["missing"]
    for sid in ("oni", "europe_land"):
        pts = data["series"][sid]["points"]
        assert len(pts) > 200 and pts[0][0] == "2000-01" and isinstance(pts[-1][1], float)
    lines = climate.context_lines(data, months=6)
    assert len(lines) == 2 and "Nino" in lines[0] and "Europe" in lines[1]


def test_climate_parsers_and_missing_file(tmp_path):
    oni = " SEAS  YR   TOTAL   ANOM\n  DJF 1999  24.0  -1.5\n  DJF 2026  26.5   0.20\n  NDJ 2026  28.1   1.90\n"
    assert climate.parse_oni(oni) == [("2026-01", 0.2), ("2026-12", 1.9)]
    cag = "# Title: x\n# Units: C\nDate,Anomaly\n202601,1.25\n202602,-999\n"
    assert climate.parse_cag(cag) == [("2026-01", 1.25)]
    assert climate.load_climate(tmp_path / "nope.json") is None


def test_climate_refresh_keeps_working_source(tmp_path, monkeypatch):
    path = tmp_path / "climate.json"

    def fetch(url, timeout_s=30.0):
        if "cpc" in url:
            return "  JJA 2026  29.0  1.80\n"
        raise OSError("down")

    monkeypatch.setattr(climate, "_fetch", fetch)
    data = climate.refresh(path)
    assert data["missing"] == ["europe_land"]
    assert climate.load_climate(path)["series"]["oni"]["points"] == [("2026-07", 1.8)]


# ---------------------------------------------------------------- live re-forecasting
def _answer(p_hard: float, p_soft: float) -> str:
    return json.dumps({"forecasts": [{"qid": "Q1", "p_yes": p_hard, "rationale": "heat dome over the Po valley"},
                                     {"qid": "Q2", "p_yes": p_soft, "rationale": "reservoirs at record lows"}]})


def test_live_prompt_has_climate_prices_and_market_prices():
    book = market.live_book()
    prompt, qs = live_forecast.build(book, PERSONA)
    assert [q["qid"] for q in qs] == [HARD["qid"], "it-drought-2027"]
    assert "Climate context" in prompt and "Oceanic Nino" in prompt
    assert "Wheat (USD/mt):" in prompt and "market 0.40" in prompt and "market 0.35" in prompt
    assert "Q2: Will Italy declare" in prompt


def test_reforecast_updates_persona_and_house_trades_toward_it():
    book = market.live_book()
    seen = []
    n = live_forecast.reforecast_once(book, PERSONA, lambda prompt: seen.append(prompt) or _answer(0.85, 0.9))
    assert n == 2 and seen
    mine = [f for f in book.agent_forecasts[HARD["qid"]] if f["persona_id"] == "climate-pessimist"]
    assert len(mine) == 1 and mine[0]["p_yes"] == 0.85 and mine[0]["source"] == "live" and mine[0]["at"]
    assert mine[0]["rationale"] == "heat dome over the Po valley"
    assert book.live_reforecast_at["climate-pessimist"] == mine[0]["at"]
    # the other persona's replay belief is untouched
    assert any(f["persona_id"] == "adaptation-optimist" and f["p_yes"] == 0.4 for f in book.agent_forecasts[HARD["qid"]])
    before = book.price(HARD["qid"])
    trades = [t for t in (book.house_step(random.Random(i)) for i in range(40)) if t]
    mine_t = [t for t in trades if t["trader"] == "agent:climate-pessimist" and t["qid"] == HARD["qid"]]
    assert mine_t and all(t["side"] == "YES" for t in mine_t)
    assert mine_t[0]["rationale"] == "heat dome over the Po valley"
    assert book.price(HARD["qid"]) > before


@pytest.mark.parametrize("answer", ["not json", json.dumps({"forecasts": []}), RuntimeError("boom")])
def test_failure_keeps_old_forecasts(answer):
    book = market.live_book()
    old = json.dumps(book.agent_forecasts, sort_keys=True)

    def ask(prompt):
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(Exception):  # noqa: B017 - any failure type; the background runner swallows it
        live_forecast.reforecast_once(book, PERSONA, ask)
    live_forecast._run(book, PERSONA, ask)  # the runner never raises
    assert json.dumps(book.agent_forecasts, sort_keys=True) == old
    assert book.live_reforecast_at == {}


def test_scheduler_round_robin_interval_and_offline(monkeypatch):
    book = market.live_book()
    assert live_forecast.maybe_reforecast(book, now=1000.0, ask=lambda p: _answer(0.7, 0.7)) is False  # offline
    monkeypatch.setenv("HARVEST_OFFLINE", "0")
    monkeypatch.setenv("LIVE_REFORECAST_S", "240")
    ask = lambda p: _answer(0.7, 0.6)  # noqa: E731
    assert live_forecast.maybe_reforecast(book, now=1000.0, ask=ask) is False  # first one waits FIRST_DELAY_S
    assert live_forecast.maybe_reforecast(book, now=1031.0, ask=ask, wait=True) is True
    assert set(book.live_reforecast_at) == {"climate-pessimist"}
    assert live_forecast.maybe_reforecast(book, now=1100.0, ask=ask) is False  # not due yet
    assert live_forecast.maybe_reforecast(book, now=1272.0, ask=ask, wait=True) is True
    assert set(book.live_reforecast_at) == {"climate-pessimist", "adaptation-optimist"}
    monkeypatch.setenv("LIVE_REFORECAST_MAX_CALLS", "2")
    assert live_forecast.maybe_reforecast(book, now=9999.0, ask=ask) is False  # budget spent


def test_replay_prompt_stays_prices_only():
    import inspect

    assert "climate" not in inspect.getsource(forecaster.build_prompt).lower()
    assert "climate" not in inspect.getsource(replay).lower().replace("climate-pessimist", "")


def test_live_prompt_has_news_headlines_and_replay_prompt_has_none():
    from agent.harvest import news

    news.store([{"title": "Italy declares drought emergency as durum fields wither", "url": "https://example.com/it",
                 "source": "Grain wire", "published_at": "2026-10-22T12:00:00Z"},
                {"title": "Cocoa prices slide as Ivory Coast crop improves", "url": "https://example.com/cocoa",
                 "source": "Wire", "published_at": "2026-10-23T12:00:00Z"}])
    prompt, qs = live_forecast.build(market.live_book(), PERSONA)
    q2 = prompt.split("Q2: ", 1)[1]
    assert "Recent news, newest first:" in q2 and "2026-10-22, Grain wire: Italy declares drought emergency" in q2
    assert "Cocoa prices slide" not in prompt  # not relevant to these markets
    assert "Weigh the recent news" in prompt
    import inspect

    assert "news" not in inspect.getsource(forecaster.build_prompt).lower()
    assert "news" not in inspect.getsource(replay).lower()
