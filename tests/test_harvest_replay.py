"""Offline tests for Harvest personas, masked forecaster and the time-machine replay (agents-replay)."""

import asyncio
import copy
import math
import re

import pytest

from agent.harvest import forecaster, personas, prices, questions, replay
from agent.harvest.types import PriceFixture, QuestionSpec

NAME_RE = re.compile(r"\b(" + "|".join(re.escape(n.split(" (")[0]) for n, _ in prices.SERIES.values()) + r")\b", re.IGNORECASE)
SPECS: list[QuestionSpec] = [
    {"id": "wheat-up10-6m", "kind": "hard", "title": "Wheat up 10%?", "series": "PWHEAMTUSDM", "rule": {"type": "pct_change_gt", "threshold": 0.10, "horizon_months": 6}, "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "why": "t"},
    {"id": "olive-above-3m", "kind": "hard", "title": "Olive above?", "series": "POLVOILUSDM", "rule": {"type": "level_gt", "level": 5000, "horizon_months": 3}, "region": "Spain", "crop": "olive oil", "adverse_if": "yes", "why": "t"},
    {"id": "it-drought-2027", "kind": "soft", "title": "Will Italy declare a drought emergency in 2027?", "region": "Italy", "crop": "durum wheat", "adverse_if": "yes", "prior": 0.4, "why": "t", "deadline": "2027-12-31"},
]


def _fixture() -> PriceFixture:
    series = {}
    for k, (sid, base) in enumerate([("PWHEAMTUSDM", 250.0), ("POLVOILUSDM", 5000.0)]):
        pts, month = [], "2021-01"
        for i in range(63):  # 2021-01 .. 2026-03
            pts.append((month, round(base * (1 + 0.25 * math.sin(i / (4 + k)) + 0.004 * i), 4)))
            month = prices.add_months(month, 1)
        name, unit = prices.SERIES[sid]
        series[sid] = {"id": sid, "name": name, "unit": unit, "source_url": "x", "points": pts}
    return {"v": 1, "source": "synthetic", "fetched_at": "x", "series": series}  # type: ignore[typeddict-item]


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HARVEST_OFFLINE", "1")
    monkeypatch.setattr(forecaster, "CACHE", tmp_path / "cache.json")
    monkeypatch.setattr(forecaster, "_cache", None)


def _run(fx: PriceFixture):
    return asyncio.run(replay.run(fixture=fx, specs=SPECS, personas=personas.load_personas(), offline=True))


def test_eight_personas_with_masked_text() -> None:
    ps = [p for p in personas.load_personas() if p.get("origin", "house") == "house"]
    assert [p["id"] for p in ps] == ["climate-pessimist", "efficient-market-skeptic", "geopolitics-hawk", "adaptation-optimist", "macro-dollar", "land-grab-realist", "agronomist", "crowd-follower"]
    assert len({p["color"] for p in ps}) == 8
    for p in ps:
        assert 3 <= len(p["beliefs"]) <= 5
        text = p["world_model"] + " ".join(p["beliefs"])
        assert not re.search(r"20\d\d", text) and not NAME_RE.search(text)


def test_masked_prompt_has_no_years_or_names() -> None:
    fx = _fixture()
    mask = forecaster.make_mask(list(fx["series"]), 7)
    for cutoff in ("2022-03", "2025-09"):
        qs = [questions.instantiate(s, cutoff, fx) for s in SPECS if s["kind"] == "hard"]
        assert any(re.search(r"20\d\d", q["title"]) for q in qs)  # the raw titles do leak
        for p in personas.load_personas():
            prompt = forecaster.build_prompt(p, qs, fx, cutoff, mask)
            assert not re.search(r"20\d\d", prompt), prompt
            assert not NAME_RE.search(prompt), NAME_RE.search(prompt)
            assert "Commodity " in prompt and "t0=" in prompt and "t-12=100.0" in prompt


def test_mask_window_rebases_and_relabels() -> None:
    win = forecaster.mask_window([("2025-07", 50.0), ("2025-08", 55.0), ("2025-09", 75.0)], "2025-09")
    assert win == [("t-2", 100.0), ("t-1", 110.0), ("t0", 150.0)]


def test_unmask_round_trips() -> None:
    mask = forecaster.make_mask(list(prices.SERIES), 3)
    assert sorted(mask["commodities"].values()) == [f"Commodity {c}" for c in "ABCDEFGH"]
    assert forecaster.make_mask(list(prices.SERIES), 3) == mask
    text = "Wheat and Olive oil rally while Cocoa slumps."
    masked = forecaster.mask_text(text, mask)
    assert not NAME_RE.search(masked)
    assert forecaster.unmask(masked, mask) == text


def test_fallback_deterministic_and_differs_across_personas() -> None:
    fx = _fixture()
    q = questions.instantiate(SPECS[0], "2024-06", fx)
    ps = personas.load_personas()
    a = [forecaster.fallback(p, q, fx, "2024-06")["p_yes"] for p in ps]
    b = [forecaster.fallback(p, q, fx, "2024-06")["p_yes"] for p in ps]
    assert a == b and len(set(a)) >= 6
    assert all(0.01 <= p <= 0.99 for p in a)
    soft = questions.instantiate(SPECS[2], None, fx)
    f = forecaster.fallback(ps[0], soft, fx, "2024-06")
    assert f["p_yes"] == pytest.approx(0.4 + ps[0]["soft_lean"])


def test_replay_splits_and_scores() -> None:
    res = _run(_fixture())
    splits = {c["cutoff"]: c["split"] for c in res["cutoffs"]}
    assert splits["2022-03"] == "dev" and splits["2025-09"] == "holdout" and splits["2026-03"] == "live"
    live = [c for c in res["cutoffs"] if c["split"] == "live"]
    assert live and all(not c["resolutions"] for c in live)
    assert any(c["trades"] for c in res["cutoffs"])
    assert [c["kind"] for c in res["commitments"]] == ["models", "holdout"]
    assert all(c["receipt"]["status"] == "pending" for c in res["commitments"])
    assert len(res["scores"]["world_models"]) == len(personas.load_personas()) == 16
    assert all(m["brier_holdout"] is not None for m in res["scores"]["world_models"])


def test_holdout_commit_precedes_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    committed: list[str] = []
    real_commit, real_resolve = replay._commit, questions.resolve

    async def commit(kind, sha, ref, anchor):
        committed.append(kind)
        return await real_commit(kind, sha, ref, anchor)

    def resolve(instance, fixture):
        if (instance["cutoff"] or "") >= replay.HOLDOUT_FROM:
            assert "holdout" in committed, "holdout resolved before its commitment"
        return real_resolve(instance, fixture)

    monkeypatch.setattr(replay, "_commit", commit)
    monkeypatch.setattr(questions, "resolve", resolve)
    res = _run(_fixture())
    assert committed == ["models", "holdout"]
    assert any(r["outcome"] is not None for c in res["cutoffs"] if c["split"] == "holdout" for r in c["resolutions"])


def test_verify_commitments_clean_tamper_and_windows() -> None:
    fx = _fixture()
    res = _run(fx)
    clean = asyncio.run(replay.verify_commitments(res, fx))
    assert clean["commitments_ok"] and clean["windows_ok"] and clean["ok"]
    tampered = asyncio.run(replay.verify_commitments(res, fx, tamper=True))
    assert not tampered["commitments_ok"] and tampered["windows_ok"]
    bad = copy.deepcopy(fx)
    pts = bad["series"]["PWHEAMTUSDM"]["points"]
    i = next(k for k, (m, _) in enumerate(pts) if m == "2025-08")  # inside the 2025-09 holdout window
    pts[i] = (pts[i][0], pts[i][1] * 1.05)
    broken = asyncio.run(replay.verify_commitments(res, bad))
    assert not broken["windows_ok"] and broken["commitments_ok"]


def test_freeze_round_trip(tmp_path) -> None:
    res = _run(_fixture())
    path = replay.freeze(res, tmp_path / "demo.json")
    assert replay.load_demo(path)["commitments"][1]["sha256"] == res["commitments"][1]["sha256"]


def test_narrative_personas_are_sourced_before_the_holdout() -> None:
    narr = [p for p in personas.load_personas() if p.get("origin") == "narrative"]
    assert len(narr) == 8
    for p in narr:
        assert "not endorsed" in p["disclaimer"] and p["knowledge_cutoff"] <= "2025-07-31"
        assert p["sources"] and all(s["url"].startswith("http") and s["date"][:7] <= p["knowledge_cutoff"][:7] for s in p["sources"])
        assert prices.add_months(p["knowledge_cutoff"][:7], 1) <= replay.HOLDOUT_FROM
        text = p["world_model"] + " ".join(p["beliefs"])
        assert not re.search(r"20\d\d", text) and not NAME_RE.search(text)
