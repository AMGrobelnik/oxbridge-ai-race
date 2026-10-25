"""Live agriculture news feed: keyless RSS/Atom poller, keyword tagging and a SQLite store.

A daemon thread (started lazily by `ensure_poller`, like the house loop) pulls every source in
SOURCES each NEWS_POLL_S seconds (default 600), 15 s timeout per source. Items are deduped by a
hash of their URL (and of their normalised title, since Google News repeats a story across
queries), tagged with commodities, regions and the question-bank spec ids they bear on, and
stored in the `news` table of the .harvest DB, which keeps the newest NEWS_KEEP (500) items.
Headlines that match no commodity are dropped.

Optionally, items that bear on a market get a one-line "why it matters" and a direction hint
(up/down/neutral for the commodity price, or for YES on a soft market) from one batched Gemini
call per poll, capped by NEWS_LLM_MAX_CALLS (default 40 per process); any failure leaves them
untagged. HARVEST_OFFLINE=1 or NEWS_POLL_S=0 disables polling.

The sealed replay never reads this module; only the live re-forecast and the API do.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET  # noqa: S405 - size-capped feeds from a fixed source list
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus

import httpx

from . import forecaster, ledger

logger = logging.getLogger("agent")

FETCH_TIMEOUT_S = 15.0
MAX_FEED_BYTES = 3_000_000
NEWS_KEEP = 500
LLM_BATCH = 20
USER_AGENT = "Mozilla/5.0 (compatible; CrophecyNews/1.0; +https://github.com/AMGrobelnik)"
ATOM = "{http://www.w3.org/2005/Atom}"

GOOGLE_QUERIES = (
    "wheat price", "durum wheat Italy", "maize corn price Europe", "olive oil prices", "Spain olive harvest",
    "cocoa price", "coffee prices", "sugar price", "sunflower oil price", "rice prices", "Europe drought crops",
    "EU farm policy", "grain export ban", "Black Sea grain", "farmland foreign investment Europe",
)


def google_news(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote_plus(query + ' when:7d')}&hl=en-GB&gl=GB&ceid=GB:en"


FEEDS: tuple[tuple[str, str], ...] = (
    ("World Grain", "https://www.world-grain.com/rss/articles"),
    ("Farm Progress", "https://www.farmprogress.com/rss.xml"),
    ("Agriland", "https://www.agriland.ie/feed/"),
)
SOURCES: tuple[tuple[str, str], ...] = tuple((f"Google News: {q}", google_news(q)) for q in GOOGLE_QUERIES) + FEEDS

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url_hash TEXT NOT NULL UNIQUE,
  title_hash TEXT NOT NULL,
  title TEXT NOT NULL,
  source TEXT NOT NULL,
  url TEXT NOT NULL,
  published_at TEXT,
  fetched_at TEXT NOT NULL,
  commodities TEXT NOT NULL,
  regions TEXT NOT NULL,
  specs TEXT NOT NULL,
  summary TEXT,
  direction TEXT,
  enriched INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS news_title_hash ON news(title_hash);
"""


def _rx(*words: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(words) + r")\b", re.IGNORECASE)


COMMODITIES: dict[str, re.Pattern[str]] = {
    "wheat": _rx("wheat", "durum", "grains?", "cereals?"),
    "maize": _rx("maize", "corn"),
    "olive oil": _rx("olive oil", "olives?", "olive harvest", "olive groves?"),
    "coffee": _rx("coffee", "arabica", "robusta"),
    "cocoa": _rx("cocoa", "chocolate"),
    "sugar": _rx("sugar", "sugar ?beets?", "sugarcane"),
    "sunflower oil": _rx("sunflower", "sunflower oil", "sunflower seeds?", "vegetable oils?"),
    "rice": _rx("rice", "paddy"),
    "farmland": _rx("farmland", "agricultural land", "land purchases?", "land grab"),
    "water": _rx("water rationing", "water restrictions?", "water shortages?", "reservoirs?", "drought", "heatwaves?", "heat waves?"),
    "farm policy": re.compile(r"\bCAP\b|(?i:\b(?:common agricultural policy|farm policy|farm subsid(?:y|ies)|agricultural policy)\b)"),
}

REGIONS: dict[str, re.Pattern[str]] = {
    "EU": re.compile(r"\bEU\b|(?i:\b(?:european union|europe|european|brussels|eurozone)\b)"),
    "Italy": _rx("italy", "italian"),
    "France": _rx("france", "french"),
    "Spain": _rx("spain", "spanish", "andalusia", "jaén", "jaen"),
    "Greece": _rx("greece", "greek", "athens", "crete"),
    "Romania": _rx("romania", "romanian"),
    "Germany": _rx("germany", "german"),
    "UK": re.compile(r"\bUK\b|(?i:\b(?:britain|british|england|scotland)\b)"),
    "Russia": _rx("russia", "russian"),
    "Ukraine": _rx("ukraine", "ukrainian"),
    "Black Sea": _rx("black sea"),
    "West Africa": _rx("ivory coast", "côte d.ivoire", "cote d.ivoire", "ghana", "west africa", "nigeria"),
    "Brazil": _rx("brazil", "brazilian"),
    "US": re.compile(r"\bU\.?S\.?\b|(?i:\b(?:united states|american|usda)\b)"),
    "India": _rx("india", "indian"),
    "China": _rx("china", "chinese"),
    "Gulf": _rx("saudi", "saudi arabia", "uae", "emirati", "abu dhabi", "qatar", "gulf"),
    "Vietnam": _rx("vietnam", "vietnamese"),
    "Argentina": _rx("argentina", "argentine"),
    "Australia": _rx("australia", "australian"),
}
SERIES_COMMODITY = {
    "PWHEAMTUSDM": "wheat", "PMAIZMTUSDM": "maize", "POLVOILUSDM": "olive oil", "PCOFFOTMUSDM": "coffee",
    "PCOCOUSDM": "cocoa", "PSUGAISAUSDM": "sugar", "PSUNOUSDM": "sunflower oil", "PRICENPQUSDM": "rice",
}
EXPORT_BAN = _rx("export bans?", "export restrictions?", "export quotas?", "grain corridor", "black sea", "export curbs?", "shipping halt")
SOVEREIGN = _rx("sovereign", "foreign (?:buyers?|investors?|funds?|ownership)", "state-linked", "restrict(?:s|ion|ions)?", "ban(?:s|ned)?")
HEAT = _rx("heat", "heatwaves?", "climate", "drought", "adaptation")


def _soft_rules() -> dict[str, Callable[[str, set[str], set[str]], bool]]:
    """Soft-market spec id -> rule(text, commodities, regions)."""
    return {
        "it-drought-2027": lambda t, c, r: "Italy" in r and "water" in c,
        "eu-sovereign-land-2028": lambda t, c, r: "farmland" in c and bool(SOVEREIGN.search(t)) and bool(r & {"EU", "Gulf", "Romania"}),
        "gulf-eu-farmland-2027": lambda t, c, r: "farmland" in c and "Gulf" in r,
        "es-olive-output-2027": lambda t, c, r: "olive oil" in c and "Spain" in r,
        "gr-water-rationing-2027": lambda t, c, r: "Greece" in r and "water" in c,
        "eu-cap-heat-2028": lambda t, c, r: "farm policy" in c and ("EU" in r or bool(HEAT.search(t))),
        "ru-grain-ban-2027": lambda t, c, r: bool(EXPORT_BAN.search(t)) and (bool(r & {"Russia", "Ukraine", "Black Sea"}) or "wheat" in c),
        "fr-wheat-crop-2027": lambda t, c, r: "wheat" in c and "France" in r,
    }


_bank_cache: dict[str, Any] = {}


def _bank() -> list[dict[str, Any]]:
    if "bank" not in _bank_cache:
        from . import questions

        try:
            _bank_cache["bank"] = [dict(s) for s in questions.load_bank()]
        except (OSError, ValueError):
            _bank_cache["bank"] = []
    return _bank_cache["bank"]


def tag(text: str, bank: Iterable[dict[str, Any]] | None = None) -> tuple[list[str], list[str], list[str]]:
    """Commodities, regions and question-bank spec ids that `text` (a headline) bears on."""
    commodities = {name for name, rx in COMMODITIES.items() if rx.search(text)}
    regions = {name for name, rx in REGIONS.items() if rx.search(text)}
    soft = _soft_rules()
    specs = []
    for spec in _bank() if bank is None else bank:
        sid = spec["id"]
        if spec.get("kind") == "hard":
            if SERIES_COMMODITY.get(spec.get("rule", {}).get("series") or spec.get("series") or "") in commodities:
                specs.append(sid)
        elif sid in soft and soft[sid](text, commodities, regions):
            specs.append(sid)
    return sorted(commodities), sorted(regions), specs


# ---------------------------------------------------------------- parsing
def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _iso(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(content: bytes, source: str) -> list[dict[str, Any]]:
    """RSS 2.0 or Atom -> [{title, source, url, published_at}]. Google News titles 'Headline - Outlet' become
    title 'Headline' and source 'Outlet'. Raises ValueError on unparseable XML."""
    if len(content) > MAX_FEED_BYTES:
        raise ValueError("feed too large")
    if b"<!ENTITY" in content[:4096]:
        raise ValueError("feed declares entities")
    try:
        root = ET.fromstring(content)  # noqa: S314 - size-capped, entity declarations refused above
    except ET.ParseError as err:
        raise ValueError(f"bad feed XML: {err}") from err
    out = []
    for item in root.iter("item"):
        link = _text(item.find("link")) or _text(item.find("guid"))
        out.append({"title": _text(item.find("title")), "url": link, "published_at": _iso(_text(item.find("pubDate"))),
                    "outlet": _text(item.find("source"))})
    for entry in root.iter(f"{ATOM}entry"):
        link_el = entry.find(f"{ATOM}link[@rel='alternate']")
        if link_el is None:
            link_el = entry.find(f"{ATOM}link")
        when = _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
        out.append({"title": _text(entry.find(f"{ATOM}title")), "url": (link_el.get("href") or "") if link_el is not None else "",
                    "published_at": _iso(when), "outlet": ""})
    items = []
    for it in out:
        title, outlet = it["title"], it.pop("outlet")
        if outlet and title.endswith(f" - {outlet}"):
            title = title[: -len(outlet) - 3].strip()
        if not title or not it["url"].startswith(("http://", "https://")):
            continue
        items.append({**it, "title": title[:300], "source": outlet or ("Google News" if source.startswith("Google News") else source)})
    return items


# ---------------------------------------------------------------- store
def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def connect() -> Any:
    return ledger.connect()


def _ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def store(items: Iterable[dict[str, Any]], *, keep: int = NEWS_KEEP) -> int:
    """Tags and inserts new on-topic items (deduped by URL hash and normalised title; items with no commodity are
    dropped), trims to the newest `keep`. Returns the number added."""
    added = 0
    fetched = ledger.now_iso()
    with connect() as conn:
        _ensure(conn)
        for it in items:
            uh, th = _hash(it["url"]), _hash(_norm_title(it["title"]))
            if conn.execute("SELECT 1 FROM news WHERE url_hash = ? OR title_hash = ?", (uh, th)).fetchone():
                continue
            commodities, regions, specs = tag(it["title"])
            if not commodities and not specs:
                continue  # off-topic: keep the 500 slots for headlines that bear on a commodity
            conn.execute(
                "INSERT INTO news (url_hash, title_hash, title, source, url, published_at, fetched_at, commodities, regions, specs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uh, th, it["title"], it["source"], it["url"], it.get("published_at"), fetched,
                 json.dumps(commodities), json.dumps(regions), json.dumps(specs)))
            added += 1
        conn.execute("DELETE FROM news WHERE id NOT IN (SELECT id FROM news ORDER BY id DESC LIMIT ?)", (keep,))
    return added


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "title": r["title"], "source": r["source"], "url": r["url"], "published_at": r["published_at"],
            "fetched_at": r["fetched_at"], "commodities": json.loads(r["commodities"]), "regions": json.loads(r["regions"]),
            "specs": json.loads(r["specs"]), "summary": r["summary"], "direction": r["direction"]}


def query(*, since: int | None = None, commodity: str | None = None, spec: str | None = None, limit: int = 50) -> tuple[list[dict[str, Any]], int]:
    """Newest first, optionally only ids > since, a commodity, or a spec id. Returns (items, latest_id)."""
    limit = max(1, min(int(limit), NEWS_KEEP))
    sql, args = "SELECT * FROM news WHERE id > ?", [int(since or 0)]
    if commodity:
        sql += " AND EXISTS (SELECT 1 FROM json_each(news.commodities) WHERE lower(value) = lower(?))"
        args.append(commodity.strip())
    if spec:
        sql += " AND EXISTS (SELECT 1 FROM json_each(news.specs) WHERE value = ?)"
        args.append(spec.strip())
    sql += " ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        _ensure(conn)
        rows = [_row(r) for r in conn.execute(sql, args)]
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM news").fetchone()[0]
    return rows, int(latest)


def headlines_for(spec_ids: Iterable[str], per_spec: int = 6) -> dict[str, list[str]]:
    """spec id -> up to `per_spec` most recent headline lines ('date, source: title [direction: why]'). Never raises."""
    out: dict[str, list[str]] = {}
    try:
        for sid in dict.fromkeys(spec_ids):
            rows, _ = query(spec=sid, limit=per_spec)
            lines = []
            for r in rows:
                note = f" [{r['direction']}: {r['summary']}]" if r.get("summary") and r.get("direction") else ""
                lines.append(f"{(r['published_at'] or r['fetched_at'])[:10]}, {r['source']}: {r['title']}{note}")
            if lines:
                out[sid] = lines
    except (sqlite3.Error, OSError, ValueError) as err:
        logger.warning("harvest news: headlines unavailable: %s", type(err).__name__)
    return out


# ---------------------------------------------------------------- fetching
def fetch_source(name: str, url: str, client: httpx.Client) -> list[dict[str, Any]]:
    r = client.get(url)
    r.raise_for_status()
    return parse_feed(r.content, name)


def poll_once(sources: Iterable[tuple[str, str]] = SOURCES) -> dict[str, int]:
    """Fetches every source once and stores what is new. Returns source -> items parsed (-1 on failure)."""
    seen: dict[str, int] = {}
    batch: list[dict[str, Any]] = []
    with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        for name, url in sources:
            try:
                items = fetch_source(name, url, client)
                seen[name] = len(items)
                batch.extend(items)
            except (httpx.HTTPError, ValueError) as err:
                seen[name] = -1
                logger.info("harvest news: %s failed: %s", name, type(err).__name__)
    batch.sort(key=lambda it: it.get("published_at") or "")  # oldest first, so ids grow with recency
    added = store(batch)
    _status.update(last_poll=ledger.now_iso(), last_added=added, sources=seen)
    return seen


# ---------------------------------------------------------------- optional LLM enrichment
def llm_max_calls() -> int:
    try:
        return int(os.environ.get("NEWS_LLM_MAX_CALLS") or 40)
    except ValueError:
        return 40


def enrich_once(ask: Callable[[str], str] | None = None) -> int:
    """One batched LLM call adding summary + direction to unenriched market-relevant items. Returns how many were updated."""
    if _status["llm_calls"] >= llm_max_calls():
        return 0
    with connect() as conn:
        _ensure(conn)
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM news WHERE enriched = 0 AND specs != '[]' ORDER BY id DESC LIMIT ?", (LLM_BATCH,))]
    if not rows:
        return 0
    if ask is None:
        from . import live_forecast

        ask = live_forecast.gemini_ask
    lines = [f'{r["id"]}: {r["title"]} (commodities: {", ".join(r["commodities"]) or "-"}; regions: {", ".join(r["regions"]) or "-"})' for r in rows]
    prompt = "\n".join([
        "You are a commodity-market analyst for a European agriculture prediction market.",
        "For each headline give one short line (<=140 chars) on why it matters for European agricultural prices or farm risk,",
        "and a direction hint for the price of the commodity it mentions: up, down or neutral.",
        "Headlines:", *lines,
        'Answer only with JSON: {"items": [{"id": 123, "why": "...", "direction": "up|down|neutral"}]}',
    ])
    _status["llm_calls"] += 1
    text = ask(prompt)
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    ids = {r["id"] for r in rows}
    updated = 0
    with connect() as conn:
        for it in data.get("items", []) if isinstance(data, dict) else []:
            try:
                nid = int(it["id"])
            except (KeyError, TypeError, ValueError):
                continue
            direction = str(it.get("direction", "")).lower().strip()
            why = " ".join(str(it.get("why", "")).split())[:200]
            if nid in ids and direction in ("up", "down", "neutral") and why:
                conn.execute("UPDATE news SET summary = ?, direction = ?, enriched = 1 WHERE id = ?", (why, direction, nid))
                updated += 1
        conn.execute(f"UPDATE news SET enriched = 1 WHERE id IN ({','.join('?' * len(ids))}) AND enriched = 0", tuple(ids))
    return updated


# ---------------------------------------------------------------- poller
_lock = threading.Lock()
_status: dict[str, Any] = {"thread": None, "last_poll": None, "last_added": 0, "sources": {}, "llm_calls": 0}


def interval_s() -> float:
    try:
        return float(os.environ.get("NEWS_POLL_S") or 600)
    except ValueError:
        return 600.0


def enabled() -> bool:
    return not forecaster.offline_env() and interval_s() > 0


def _loop() -> None:
    while True:
        try:
            poll_once()
        except Exception:  # noqa: BLE001 - one bad poll never stops the feed
            logger.exception("harvest news poll failed")
        if os.environ.get("GEMINI_API_KEY") and os.environ.get("NEWS_LLM", "1") not in ("0", "false"):
            try:
                n = enrich_once()
                if n:
                    logger.info("harvest news: enriched %d items", n)
            except Exception as err:  # noqa: BLE001 - enrichment is optional
                logger.warning("harvest news enrichment failed: %s", type(err).__name__)
        time.sleep(max(interval_s(), 30.0))


def ensure_poller() -> bool:
    """Starts the news poller thread once per process (no-op offline). Returns True when it is running."""
    if not enabled():
        return False
    with _lock:
        t = _status["thread"]
        if t is not None and t.is_alive():
            return True
        t = threading.Thread(target=_loop, name="harvest-news", daemon=True)
        _status["thread"] = t
        t.start()
    return True


def status() -> dict[str, Any]:
    return {k: v for k, v in _status.items() if k != "thread"} | {"running": bool(_status["thread"] and _status["thread"].is_alive())}
