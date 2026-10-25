"""Harvest data fixture and question bank (SPEC.md sections 2 and 3). No network."""

from agent.harvest import prices, questions
from agent.harvest.types import PriceFixture, QuestionSpec


def _fixture() -> PriceFixture:
    months = [prices.add_months("2022-01", i) for i in range(12)]
    a = {"id": "A", "name": "Wheat", "unit": "USD/mt", "source_url": "x", "points": [(m, 100.0 + 10 * i) for i, m in enumerate(months)]}
    b = {"id": "B", "name": "B", "unit": "USD/mt", "source_url": "x", "points": [(m, 50.0) for m in months[:10]]}
    return {"v": 1, "source": "test", "fetched_at": "now", "series": {"A": a, "B": b}}  # type: ignore[typeddict-item]


def _spec(rule: dict, series: str = "A") -> QuestionSpec:
    return {"id": "q", "kind": "hard", "title": "t", "series": series, "rule": rule,  # type: ignore[typeddict-item]
            "region": "Italy", "crop": "wheat", "adverse_if": "yes", "why": "w"}


def test_add_months_and_window_never_leaks_future() -> None:
    f = _fixture()
    assert prices.add_months("2022-11", 3) == "2023-02"
    for upto in ["2021-06", "2022-01", "2022-05", "2022-12", "2030-01"]:
        w = prices.window(f, "A", upto, months=4)
        assert all(m <= upto for m, _ in w)
        assert len(w) <= 4
    assert prices.window(f, "A", "2022-05", months=3) == [("2022-03", 120.0), ("2022-04", 130.0), ("2022-05", 140.0)]
    assert prices.window(f, "A", "2021-12") == []


def test_value_at_and_last_month() -> None:
    f = _fixture()
    assert prices.value_at(f, "A", "2022-02") == 110.0
    assert prices.value_at(f, "A", "2025-01") is None
    assert prices.value_at(f, "ZZZ", "2022-02") is None
    assert prices.last_month(f) == "2022-10"


def test_parse_csv_drops_missing_and_old() -> None:
    text = "observation_date,A\n2014-12-01,1\n2015-01-01,2.5\n2015-02-01,.\n2015-03-01,3\n"
    assert prices.parse_csv(text, "A") == [("2015-01", 2.5), ("2015-03", 3.0)]


def test_resolve_all_rule_types() -> None:
    f = _fixture()  # A: 2022-01 100, +10/month; base at 2022-03 = 120, at 2022-06 = 170
    cases = [
        ({"type": "pct_change_gt", "threshold": 0.10, "horizon_months": 3}, 1),  # 150 > 132
        ({"type": "pct_change_gt", "threshold": 0.50, "horizon_months": 3}, 0),  # 150 > 180 no
        ({"type": "pct_change_lt", "threshold": -0.10, "horizon_months": 3}, 0),  # 150 < 108 no
        ({"type": "pct_change_lt", "threshold": 0.50, "horizon_months": 3}, 1),  # 150 < 180
        ({"type": "level_gt", "level": 140, "horizon_months": 3}, 1),
        ({"type": "level_gt", "level": 150, "horizon_months": 3}, 0),  # strict
        ({"type": "level_lt", "level": 160, "horizon_months": 3}, 1),
        ({"type": "level_lt", "level": 150, "horizon_months": 3}, 0),  # strict
    ]
    for rule, want in cases:
        inst = questions.instantiate(_spec(rule), "2022-03", f)
        assert inst["qid"] == "q@2022-03" and inst["base_value"] == 120.0 and inst["resolve_month"] == "2022-06"
        res = questions.resolve(inst, f)
        assert res["outcome"] == want, rule
        assert res["resolved_value"] == 150.0
        assert questions.resolve(inst, f) == res  # pure


def test_resolve_none_past_data_and_for_soft() -> None:
    f = _fixture()
    inst = questions.instantiate(_spec({"type": "level_gt", "level": 1, "horizon_months": 6}), "2022-06", f)
    assert inst["resolve_month"] == "2022-12"
    assert questions.resolve(inst, f)["outcome"] is None  # last common month is 2022-10
    soft: QuestionSpec = {"id": "s", "kind": "soft", "title": "Soft?", "region": "EU", "crop": "x",
                          "adverse_if": "yes", "why": "w", "deadline": "2027-12-31", "prior": 0.3}
    si = questions.instantiate(soft, None, f)
    assert si["qid"] == "s" and si["cutoff"] is None and si["series"] is None and si["prior"] == 0.3
    assert questions.resolve(si, f)["outcome"] is None


def test_title_rendered_with_numbers() -> None:
    f = _fixture()
    inst = questions.instantiate(_spec({"type": "pct_change_gt", "threshold": 0.10, "horizon_months": 6}), "2022-03", f)
    assert inst["target"] is not None and abs(inst["target"] - 132.0) < 1e-9
    assert "$120/mt" in inst["title"] and "$132/mt" in inst["title"] and "2022-09" in inst["title"]


def test_bank_shape() -> None:
    bank = questions.load_bank()
    hard, soft = questions.hard_specs(bank), questions.soft_specs(bank)
    assert 12 <= len(hard) <= 20 and len(soft) >= 8
    ids = [s["id"] for s in bank]
    assert len(ids) == len(set(ids))
    assert {s["series"] for s in hard} == set(prices.SERIES)
    for s in hard:
        r = s["rule"]
        assert r["horizon_months"] in (3, 6)
        assert ("threshold" in r) if r["type"].startswith("pct") else ("level" in r)
    for s in soft:
        assert s["deadline"] and 0 < s["prior"] < 1
    for s in bank:
        assert s["adverse_if"] in ("yes", "no") and s["region"] and s["crop"] and s["why"]


def test_real_fixture() -> None:
    f = prices.load_fixture()
    assert set(f["series"]) == set(prices.SERIES)
    assert prices.last_month(f) >= "2026-06"
    for s in f["series"].values():
        assert s["points"][0][0] == prices.START_MONTH
        months = [m for m, _ in s["points"]]
        assert months == sorted(months)
    bank = questions.load_bank()
    for spec in questions.hard_specs(bank):
        inst = questions.instantiate(spec, "2022-03", f)
        assert inst["base_value"] is not None
        assert questions.resolve(inst, f)["outcome"] in (0, 1)
