"""Model registry, leakage guard, persona x model agents and the provider-agnostic call (offline)."""

import asyncio
import json

import httpx
import pytest

from agent.harvest import byo, forecaster, models, personas, prices, replay, scoring
from tests.test_harvest_replay import SPECS, _fixture


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.setattr(forecaster, "CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(forecaster, "_cache", None)


def test_registry_rows_are_pinned_and_sourced() -> None:
    rows = models.models()
    assert len(rows) >= 4 and len({m["key"] for m in rows}) == len(rows)
    assert len({m["family"] for m in rows}) >= 3
    for m in rows:
        assert m["provider"] in models.ENDPOINTS or m["provider"] == "gemini"
        assert m["env"].endswith("_API_KEY") and m["source"].startswith("https://")
        assert "latest" not in m["model_id"]  # pinned by a concrete id, never an alias that moves
    assert models.byo_model() in {m["key"] for m in rows}


def test_every_model_cutoff_is_a_month_before_the_holdout() -> None:
    """The hard guard: fails if any model's cutoff + 1 month (or a narrative source + 1 month) is after HOLDOUT_FROM."""
    for m in models.models():
        assert prices.add_months(m["knowledge_cutoff"][:7], 1) <= replay.HOLDOUT_FROM, m
    for p in personas.load_personas():
        if p.get("knowledge_cutoff"):
            assert prices.add_months(p["knowledge_cutoff"][:7], 1) <= replay.HOLDOUT_FROM, p["id"]
    assert replay.HOLDOUT_FROM == models.holdout_from() >= models.earliest_holdout(personas=personas.load_personas())
    assert models.earliest_holdout(personas=personas.load_personas()) == "2025-09"
    late = [*models.models(), {"key": "late", "knowledge_cutoff": "2025-09"}]
    with pytest.raises(models.LeakageError, match="late"):
        models.check_holdout(late, replay.HOLDOUT_FROM)
    with pytest.raises(models.LeakageError, match="persona x"):
        models.check_holdout(models.models(), replay.HOLDOUT_FROM, personas=[{"id": "x", "knowledge_cutoff": "2025-09-02"}])
    assert "latest: Jan 2025" in models.cutoff_line([{"knowledge_cutoff": "2024-06"}, {"knowledge_cutoff": "2025-01"}])


def test_run_refuses_a_model_that_saw_the_holdout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = {**models.models()[0], "key": "leaky", "knowledge_cutoff": "2026-01"}
    real = models.registry()
    monkeypatch.setattr(models, "registry", lambda: {**real, "models": [*real["models"], fake]})
    with pytest.raises(models.LeakageError):
        asyncio.run(replay.run(fixture=_fixture(), specs=SPECS, personas=personas.load_personas(), offline=True, model_keys=["leaky"]))


def test_persona_by_model_replay_has_summary_tables() -> None:
    keys = [m["key"] for m in models.models()][:2]
    res = asyncio.run(replay.run(fixture=_fixture(), specs=SPECS, personas=personas.load_personas(), offline=True, model_keys=keys))
    n = len(personas.load_personas())
    assert len(res["personas"]) == 2 * n and {p["model"] for p in res["personas"]} == set(keys)
    assert all(p["id"] == models.agent_id(p["persona"], p["model"]) for p in res["personas"])
    lb = scoring.leaderboard(res)
    assert [r["key"] for r in lb["models"]] == keys and lb["holdout_from"] == replay.HOLDOUT_FROM
    assert lb["cutoff_line"].startswith("Every scored question resolves after every model's training cutoff (latest: ")
    assert {g["key"] for g in lb["by_model"]} == set(keys) and all(g["n_agents"] == n for g in lb["by_model"])
    assert len(lb["by_persona"]) == n and all(g["brier_holdout_lo"] <= g["brier_holdout"] <= g["brier_holdout_hi"] for g in lb["by_persona"])
    narr = [r for r in lb["world_models"] if r["origin"] == "narrative"]
    assert len(narr) == 2 * 8 and all(r["sources"] and "not endorsed" in r["disclaimer"] and r["brier_holdout_lo"] is not None for r in narr)
    assert {r["origin"] for r in lb["by_persona"]} == {"house", "narrative"}


def test_cache_key_separates_models() -> None:
    p = personas.load_personas()[0]
    a, b = models.expand([p], [m["key"] for m in models.models()][:2])
    assert forecaster.cache_key(a, "2025-09", "x") != forecaster.cache_key(b, "2025-09", "x")
    assert forecaster.cache_key(p, "2025-09", "x") != forecaster.cache_key(a, "2025-09", "x")


def test_byo_runs_on_the_pinned_model() -> None:
    persona = byo.make_persona("byo-x", "T", "Prices follow the weather in the main growing regions.", personas.load_personas())
    assert persona["model"] == models.byo_model()
    assert byo.world_model_sha("a theory") != byo.world_model_sha("another theory")


def test_ask_retries_then_extracts_json(monkeypatch: pytest.MonkeyPatch) -> None:
    key = next(m["key"] for m in models.models() if m["provider"] == "openrouter")
    calls: list[int] = []

    async def once(m, prompt, timeout_s):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.HTTPStatusError("rate", request=httpx.Request("POST", "http://x"), response=httpx.Response(429))
        if len(calls) == 2:
            return "not json"
        return 'Sure:\n```json\n{"forecasts": []}\n```'

    monkeypatch.setattr(models, "_once", once)
    monkeypatch.setattr(models, "BACKOFF_S", 0.0)
    assert json.loads(asyncio.run(models.ask(key, "p"))) == {"forecasts": []}
    assert len(calls) == 3


def test_ask_gives_up_with_an_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    key = models.models()[0]["key"]

    async def once(m, prompt, timeout_s):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(models, "_once", once)
    monkeypatch.setattr(models, "BACKOFF_S", 0.0)
    with pytest.raises(httpx.HTTPError):
        asyncio.run(models.ask(key, "p"))


def test_ask_does_not_retry_a_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    key = models.models()[0]["key"]
    calls: list[int] = []

    async def once(m, prompt, timeout_s):
        calls.append(1)
        raise httpx.HTTPStatusError("bad", request=httpx.Request("POST", "http://x"), response=httpx.Response(400))

    monkeypatch.setattr(models, "_once", once)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(models.ask(key, "p"))
    assert len(calls) == 1


def test_live_book_trades_one_model_only() -> None:
    from agent.harvest import market

    keys = [m["key"] for m in models.models()][:2]
    res = asyncio.run(replay.run(fixture=_fixture(), specs=SPECS, personas=personas.load_personas(), offline=True, model_keys=keys))
    roster = market.live_roster(res["personas"], dict(res))
    assert len(roster) == len(personas.load_personas()) and len({p["model"] for p in roster}) == 1
    assert roster[0]["model"] == scoring.group_table(dict(res), "model")[0]["key"]
