"""Climate context for LIVE forecasts only (the sealed replay stays prices-only so the holdout is comparable).

Two monthly NOAA series, fetched once into the committed fixture data/climate.json:
- oni: Oceanic Nino Index (3-month running SST anomaly, Nino 3.4), keyed by the season's centre month.
- europe_land: Climate at a Glance, Europe land temperature anomaly vs 1901-2000 (degrees C).

Refresh with `python -m agent.harvest.climate` (network, timeouts, IPv4). The server only reads the fixture.
"""

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CLIMATE = Path(__file__).resolve().parent / "data" / "climate.json"
FETCH_TIMEOUT_S = 30.0
FIRST_YEAR = 2000
SOURCES: dict[str, dict[str, str]] = {
    "oni": {
        "name": "Oceanic Nino Index (NOAA CPC)", "units": "degrees C SST anomaly, 3-month running mean",
        "url": "https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt",
    },
    "europe_land": {
        "name": "Europe land temperature anomaly (NOAA NCEI Climate at a Glance)", "units": "degrees C vs 1901-2000",
        "url": "https://www.ncei.noaa.gov/access/monitoring/climate-at-a-glance/global/time-series/europe/land/1/0/2000-2026/data.csv",
    },
}
_SEASON_CENTRE = {s: i + 1 for i, s in enumerate(("DJF", "JFM", "FMA", "MAM", "AMJ", "MJJ", "JJA", "JAS", "ASO", "SON", "OND", "NDJ"))}

logger = logging.getLogger("agent")


def parse_oni(text: str) -> list[tuple[str, float]]:
    """`SEAS YR TOTAL ANOM` rows -> [(YYYY-MM of the season's centre month, anomaly)]."""
    out: list[tuple[str, float]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4 or parts[0] not in _SEASON_CENTRE:
            continue
        try:
            year, anom = int(parts[1]), float(parts[3])
        except ValueError:
            continue
        if year >= FIRST_YEAR:
            out.append((f"{year:04d}-{_SEASON_CENTRE[parts[0]]:02d}", anom))
    return out


def parse_cag(text: str) -> list[tuple[str, float]]:
    """Climate at a Glance CSV (`#` header lines, then `Date,Anomaly` with Date=YYYYMM)."""
    out: list[tuple[str, float]] = []
    for line in text.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2 or not parts[0].isdigit() or len(parts[0]) != 6:
            continue
        try:
            value = float(parts[1])
        except ValueError:
            continue
        if value > -900:  # -999 marks missing
            out.append((f"{parts[0][:4]}-{parts[0][4:]}", value))
    return out


_PARSERS = {"oni": parse_oni, "europe_land": parse_cag}


def _fetch(url: str, timeout_s: float = FETCH_TIMEOUT_S) -> str:
    import httpx

    # noaa.gov hangs over IPv6 from some networks: bind the client to IPv4.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
    with httpx.Client(transport=transport, timeout=timeout_s, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def refresh(path: Path = CLIMATE) -> dict[str, Any]:
    """Fetch both series and write the fixture. A failed source keeps its previous points and is listed in `missing`."""
    old = load_climate(path) or {"series": {}}
    data: dict[str, Any] = {"fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "series": {}, "missing": []}
    for sid, meta in SOURCES.items():
        try:
            points = _PARSERS[sid](_fetch(meta["url"]))
            if not points:
                raise ValueError("no rows parsed")
            data["series"][sid] = {**meta, "points": [list(p) for p in points]}
        except Exception as err:  # noqa: BLE001 - one dead source must not stop the other
            logger.warning("climate source %s failed: %s", sid, err)
            data["missing"].append(sid)
            if sid in old["series"]:
                data["series"][sid] = old["series"][sid]
    path.write_text(_dumps(data))
    return data


def _dumps(data: dict[str, Any]) -> str:
    """Indented JSON with one [month, value] point per line, so refreshes diff cleanly."""
    text = json.dumps(data, indent=1)
    return re.sub(r'\[\s+("\d{4}-\d{2}"),\s+(-?[\d.eE+-]+)\s+\]', r"[\1, \2]", text) + "\n"


def load_climate(path: Path = CLIMATE) -> dict[str, Any] | None:
    """The committed fixture with points as (month, value) tuples, or None if missing or unreadable."""
    try:
        data = json.loads(path.read_text())
        for s in data["series"].values():
            s["points"] = [(str(m), float(v)) for m, v in s["points"]]
        return dict(data)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def context_lines(data: dict[str, Any] | None, months: int = 12) -> list[str]:
    """Prompt lines: the last `months` points of each series, plus the latest reading."""
    if not data:
        return []
    lines: list[str] = []
    for s in data.get("series", {}).values():
        pts = s["points"][-months:]
        if not pts:
            continue
        lines.append(f"{s['name']} ({s['units']}), latest {pts[-1][0]} = {pts[-1][1]:+.2f}: "
                     + ", ".join(f"{m}={v:+.2f}" for m, v in pts))
    return lines


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    d = refresh()
    print({k: len(v["points"]) for k, v in d["series"].items()}, "missing:", d["missing"])
