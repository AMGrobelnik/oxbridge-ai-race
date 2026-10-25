"""FRED / IMF commodity price fixture (SPEC.md section 2). Owner: data-questions."""

from pathlib import Path

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
    raise NotImplementedError


def load_fixture(path: Path = FIXTURE) -> PriceFixture:
    raise NotImplementedError


def window(fixture: PriceFixture, series_id: str, upto: str, months: int = 24) -> list[tuple[str, float]]:
    """The last `months` points with month <= upto. The only way agents see prices."""
    raise NotImplementedError


def value_at(fixture: PriceFixture, series_id: str, month: str) -> float | None:
    raise NotImplementedError


def last_month(fixture: PriceFixture) -> str:
    """Latest month present in every series."""
    raise NotImplementedError
