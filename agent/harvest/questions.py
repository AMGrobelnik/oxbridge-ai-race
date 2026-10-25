"""Question bank, instantiation at a cutoff and resolution (SPEC.md section 3). Owner: data-questions."""

import json
from pathlib import Path

from .prices import SERIES, add_months, last_month, value_at
from .types import PriceFixture, QuestionInstance, QuestionSpec, Resolution, Rule

BANK = Path(__file__).resolve().parent / "data" / "questions.json"


def load_bank(path: Path = BANK) -> list[QuestionSpec]:
    bank: list[QuestionSpec] = json.loads(path.read_text())
    return bank


def fmt_price(series_id: str, value: float) -> str:
    """'$331/mt' for USD/mt series, '268 USc/lb' for cents-per-pound ones."""
    unit = SERIES.get(series_id, ("", "USD/mt"))[1]
    if unit.startswith("USD/"):
        return f"${value:,.0f}/{unit.split('/', 1)[1]}"
    return f"{value:,.1f} {unit}" if value < 100 else f"{value:,.0f} {unit}"


def _target(rule: Rule, base: float | None) -> float | None:
    if rule["type"].startswith("pct_change"):
        return None if base is None else base * (1 + rule["threshold"])
    return float(rule["level"])


def hard_specs(bank: list[QuestionSpec]) -> list[QuestionSpec]:
    return [s for s in bank if s["kind"] == "hard"]


def soft_specs(bank: list[QuestionSpec]) -> list[QuestionSpec]:
    return [s for s in bank if s["kind"] == "soft"]


def instantiate(spec: QuestionSpec, cutoff: str | None, fixture: PriceFixture) -> QuestionInstance:
    """Hard: qid f"{id}@{cutoff}", base = value at cutoff, target per rule. Soft: qid = id, cutoff None."""
    common = {
        "spec_id": spec["id"],
        "kind": spec["kind"],
        "region": spec["region"],
        "crop": spec["crop"],
        "adverse_if": spec["adverse_if"],
    }
    if spec["kind"] == "soft" or "rule" not in spec:
        return {  # type: ignore[return-value]
            **common,
            "qid": spec["id"],
            "title": spec["title"],
            "cutoff": None,
            "series": None,
            "base_month": None,
            "base_value": None,
            "resolve_month": None,
            "target": None,
            "rule": None,
            "prior": float(spec.get("prior", 0.5)),
        }
    if cutoff is None:
        raise ValueError(f"hard question {spec['id']} needs a cutoff")
    series_id = spec["series"]
    rule = spec["rule"]
    base = value_at(fixture, series_id, cutoff)
    resolve_month = add_months(cutoff, rule["horizon_months"])
    target = _target(rule, base)
    name = SERIES.get(series_id, (series_id, ""))[0].split(" (")[0].lower()
    direction = "above" if rule["type"].endswith("_gt") else "below"
    if base is None or target is None:
        title = spec["title"]
    else:
        title = (
            f"Will {name} (now {fmt_price(series_id, base)}) be {direction} "
            f"{fmt_price(series_id, target)} by {resolve_month}?"
        )
    return {  # type: ignore[return-value]
        **common,
        "qid": f"{spec['id']}@{cutoff}",
        "title": title,
        "cutoff": cutoff,
        "series": series_id,
        "base_month": cutoff,
        "base_value": base,
        "resolve_month": resolve_month,
        "target": target,
        "rule": rule,
        "prior": 0.5,
    }


def resolve(instance: QuestionInstance, fixture: PriceFixture) -> Resolution:
    """Pure. outcome None when resolve_month is past the fixture or the question is soft."""
    qid, month, target, rule = instance["qid"], instance["resolve_month"], instance["target"], instance["rule"]
    open_res: Resolution = {"qid": qid, "outcome": None, "resolved_value": None, "resolve_month": month}
    if instance["kind"] == "soft" or rule is None or month is None or target is None or instance["series"] is None:
        return open_res
    if month > last_month(fixture):
        return open_res
    value = value_at(fixture, instance["series"], month)
    if value is None:
        return open_res
    hit = value > target if rule["type"].endswith("_gt") else value < target
    return {"qid": qid, "outcome": int(hit), "resolved_value": value, "resolve_month": month}
