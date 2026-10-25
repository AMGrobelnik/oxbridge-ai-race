"""FRED / IMF commodity price fixture (SPEC.md section 2). Owner: data-questions."""

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .types import PriceFixture

DATA_DIR = Path(__file__).resolve().parent / "data"
FIXTURE = DATA_DIR / "prices.json"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
SERIES: dict[str, tuple[str, str]] = {
    "PWHEAMTUSDM": ("Wheat", "USD/mt"),
    "PMAIZMTUSDM": ("Maize", "USD/mt"),
    "POLVOILUSDM": ("Olive oil", "USD/mt"),
    "PCOFFOTMUSDM": ("Coffee (other milds)", "USc/lb"),
    "PCOCOUSDM": ("Cocoa", "USD/mt"),
    "PSUGAISAUSDM": ("Sugar", "USc/lb"),
    "PSUNOUSDM": ("Sunflower oil", "USD/mt"),
    "PRICENPQUSDM": ("Rice", "USD/mt"),
}
START_MONTH = "2015-01"


def add_months(month: str, n: int) -> str:
    """'2022-03' + 6 -> '2022-09'."""
    year, mon = (int(x) for x in month.split("-"))
    idx = year * 12 + (mon - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


async def fetch_fixture(path: Path = FIXTURE) -> PriceFixture:
    """Download every SERIES CSV from FRED, keep months >= START_MONTH, write and return the fixture."""
    series = {}
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for sid, (name, unit) in SERIES.items():
            url = FRED_CSV.format(sid)
            resp = await client.get(url)
            resp.raise_for_status()
            points = parse_csv(resp.text, sid)
            series[sid] = {"id": sid, "name": name, "unit": unit, "source_url": url, "points": points}
    fixture: PriceFixture = {
        "v": 1,
        "source": "IMF primary commodity prices via FRED",
        "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "series": series,  # type: ignore[typeddict-item]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fixture, indent=1) + "\n")
    return fixture


def parse_csv(text: str, series_id: str) -> list[tuple[str, float]]:
    """FRED CSV (observation_date,<ID>) -> [(YYYY-MM, value)], '.' dropped, months >= START_MONTH."""
    out: list[tuple[str, float]] = []
    for row in csv.DictReader(io.StringIO(text)):
        date = row.get("observation_date") or row.get("DATE") or ""
        raw = (row.get(series_id) or "").strip()
        if not date or raw in ("", "."):
            continue
        month = date[:7]
        if month < START_MONTH:
            continue
        try:
            out.append((month, round(float(raw), 4)))
        except ValueError:
            continue
    out.sort()
    return out


def load_fixture(path: Path = FIXTURE) -> PriceFixture:
    fixture = json.loads(path.read_text())
    for s in fixture["series"].values():
        s["points"] = [(m, float(v)) for m, v in s["points"]]
    return fixture


def window(fixture: PriceFixture, series_id: str, upto: str, months: int = 24) -> list[tuple[str, float]]:
    """The last `months` points with month <= upto. The only way agents see prices."""
    pts = [(m, v) for m, v in fixture["series"][series_id]["points"] if m <= upto]
    return pts[-months:] if months > 0 else []


def value_at(fixture: PriceFixture, series_id: str, month: str) -> float | None:
    s = fixture["series"].get(series_id)
    if s is None:
        return None
    for m, v in s["points"]:
        if m == month:
            return float(v)
    return None


def last_month(fixture: PriceFixture) -> str:
    """Latest month present in every series."""
    return min(s["points"][-1][0] for s in fixture["series"].values() if s["points"])
