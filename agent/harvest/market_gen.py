"""Markets from the news: a drafting agent proposes new markets from fresh headlines; an admin approves them.

Every MARKET_GEN_S seconds (default 900) the job reads news items newer than its cursor and asks Gemini (one call,
capped by MARKET_GEN_MAX_CALLS, default 60) for up to two candidate markets: question, kind hard/soft, the FRED
series and rule for hard ones, a deadline, resolution source, region, crop and the news ids behind it. Candidates
are validated, deduped against live markets and earlier drafts, and stored in `market_drafts` as drafts.

POST /harvest/api/markets/drafts/{id}/approve (admin) opens the draft in the live book: house agents get a
world-model lean on the drafting agent's estimate (the live re-forecast then replaces it), and the market opens at
their consensus (0.5 without agents). Approved drafts are re-opened from this table when the book is rebuilt, before
the ledger replays, so their bets and resolution replay too. A failed LLM call drafts nothing and retries next run.
"""

import json
import logging
import re
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import ledger, llm_jobs, news, prices, questions
from .llm_jobs import Ask

if TYPE_CHECKING:
    from .market import LiveBook

logger = logging.getLogger("agent")

MAX_NEWS = 25
MAX_PER_RUN = 2
DUP_JACCARD = 0.5
RULE_TYPES = ("pct_change_gt", "pct_change_lt", "level_gt", "level_lt")
STOP = frozenset("will the be by in of a an to on at for and or than more less above below its this that from with into end any".split())
job = llm_jobs.Job("market_gen", interval_s=900.0, max_calls=60, first_delay_s=60.0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question TEXT NOT NULL,
  kind TEXT NOT NULL,
  series TEXT,
  rule TEXT,
  deadline TEXT,
  resolution_source TEXT NOT NULL,
  region TEXT NOT NULL,
  crop TEXT NOT NULL,
  adverse_if TEXT NOT NULL,
  prior REAL NOT NULL,
  news_ids TEXT NOT NULL,
  why TEXT NOT NULL,
  status TEXT NOT NULL,
  qid TEXT,
  instance TEXT,
  open_price REAL,
  created_at TEXT NOT NULL,
  approved_at TEXT
);
CREATE TABLE IF NOT EXISTS market_gen_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _conn() -> Any:
    return ledger.connect()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "question": r["question"], "kind": r["kind"], "series": r["series"], "rule": json.loads(r["rule"]) if r["rule"] else None,
            "deadline": r["deadline"], "resolution_source": r["resolution_source"], "region": r["region"], "crop": r["crop"],
            "adverse_if": r["adverse_if"], "prior": r["prior"], "news_ids": json.loads(r["news_ids"]), "why": r["why"], "status": r["status"],
            "qid": r["qid"], "open_price": r["open_price"], "created_at": r["created_at"], "approved_at": r["approved_at"]}


def drafts(*, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM market_drafts", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        conn.executescript(SCHEMA)
        return [_row(r) for r in conn.execute(sql, args)]


def _cursor() -> int:
    with _conn() as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT value FROM market_gen_state WHERE key = 'news_cursor'").fetchone()
    return int(row["value"]) if row else 0


def _set_cursor(n: int) -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO market_gen_state (key, value) VALUES ('news_cursor', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(n),))


# ---------------------------------------------------------------- dedupe
def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in STOP}


def similar(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def _same_rule(d: dict[str, Any], q: dict[str, Any]) -> bool:
    r1, r2 = d.get("rule") or {}, q.get("rule") or {}
    if not r1 or not r2 or d.get("series") != q.get("series") or r1.get("type") != r2.get("type"):
        return False
    if r1.get("horizon_months") != r2.get("horizon_months"):
        return False
    if "level" in r1 and "level" in r2:
        return abs(float(r1["level"]) - float(r2["level"])) <= 0.05 * abs(float(r2["level"]))
    return abs(float(r1.get("threshold", 0)) - float(r2.get("threshold", 0))) < 0.03


def is_duplicate(d: dict[str, Any], existing: list[dict[str, Any]]) -> bool:
    """True when `d` restates an existing market or draft: similar wording, or the same series and rule."""
    return any(similar(d["question"], e.get("title") or e.get("question") or "") >= DUP_JACCARD or (d["kind"] == "hard" and _same_rule(d, e))
               for e in existing)


def existing_markets(book: "LiveBook") -> list[dict[str, Any]]:
    with book.lock:
        live = [{"title": q.get("title", qid), "kind": q.get("kind"), "series": q.get("series"), "rule": q.get("rule")} for qid, q in book.meta.items()]
    return live + [d for d in drafts(limit=500) if d["status"] != "rejected"]


# ---------------------------------------------------------------- validation
def _fixture() -> dict[str, Any] | None:
    try:
        return dict(prices.load_fixture())
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-") or "market"


def _spec(d: dict[str, Any], draft_id: int) -> dict[str, Any]:
    spec = {"id": f"news{draft_id}-{_slug(d['question'])}", "kind": d["kind"], "title": d["question"], "region": d["region"], "crop": d["crop"],
            "adverse_if": d["adverse_if"], "why": d["why"], "prior": d["prior"]}
    if d["kind"] == "hard":
        spec |= {"series": d["series"], "rule": d["rule"]}
    else:
        spec["deadline"] = d["deadline"]
    return spec


def instance_for(d: dict[str, Any], draft_id: int, fixture: dict[str, Any] | None) -> dict[str, Any]:
    """The QuestionInstance a draft opens as. Hard: instantiated at the series' latest month (precise title, target)."""
    spec = _spec(d, draft_id)
    if d["kind"] == "hard":
        if not fixture or d["series"] not in fixture["series"] or not fixture["series"][d["series"]]["points"]:
            raise ValueError(f"no price data for {d['series']}")
        cutoff = fixture["series"][d["series"]]["points"][-1][0]
        q = dict(questions.instantiate(spec, cutoff, fixture))  # type: ignore[arg-type]
        q["prior"] = d["prior"]
        return q
    return {"qid": spec["id"], "spec_id": spec["id"], "kind": "soft", "title": d["question"], "region": d["region"], "crop": d["crop"],
            "adverse_if": d["adverse_if"], "cutoff": None, "series": None, "base_month": None, "base_value": None,
            "resolve_month": d["deadline"], "target": None, "rule": None, "prior": d["prior"]}


def clean(raw: Any, news_ids: set[int], *, today: date | None = None) -> dict[str, Any] | None:
    """A validated draft from one model candidate, or None."""
    if not isinstance(raw, dict):
        return None
    today = today or datetime.now(UTC).date()
    question = " ".join(str(raw.get("question") or "").split())[:200]
    kind = str(raw.get("kind") or "").lower()
    if len(question) < 15 or not question.endswith("?") or kind not in ("hard", "soft"):
        return None
    ids = []
    for x in raw.get("news_ids") or []:
        try:
            if int(x) in news_ids:
                ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if not ids:
        return None
    try:
        prior = min(max(float(raw.get("prior", 0.5)), 0.05), 0.95)
    except (TypeError, ValueError):
        prior = 0.5
    d: dict[str, Any] = {"question": question, "kind": kind, "series": None, "rule": None, "deadline": None,
                         "resolution_source": " ".join(str(raw.get("resolution_source") or "").split())[:200],
                         "region": " ".join(str(raw.get("region") or "EU").split())[:40], "crop": " ".join(str(raw.get("crop") or "all crops").split()).lower()[:40],
                         "adverse_if": "no" if str(raw.get("adverse_if", "yes")).lower() == "no" else "yes", "prior": round(prior, 3),
                         "news_ids": sorted(set(ids)), "why": " ".join(str(raw.get("why") or "").split())[:300]}
    if kind == "hard":
        series, rule = str(raw.get("series") or ""), raw.get("rule")
        if series not in prices.SERIES or not isinstance(rule, dict) or rule.get("type") not in RULE_TYPES:
            return None
        try:
            horizon = int(rule.get("horizon_months"))
            r: dict[str, Any] = {"type": rule["type"], "horizon_months": horizon}
            if rule["type"].startswith("pct_change"):
                r["threshold"] = round(float(rule.get("threshold")), 4)
            else:
                r["level"] = round(float(rule.get("level")), 4)
        except (TypeError, ValueError):
            return None
        if not 1 <= horizon <= 12:
            return None
        if r["type"] == "pct_change_gt" and not 0 < r["threshold"] <= 1:
            return None
        if r["type"] == "pct_change_lt" and not -0.9 <= r["threshold"] < 0:
            return None
        if "level" in r and r["level"] <= 0:
            return None
        d |= {"series": series, "rule": r, "resolution_source": f"FRED {series} (IMF primary commodity prices), {prices.FRED_CSV.format(series)}"}
    else:
        try:
            deadline = date.fromisoformat(str(raw.get("deadline") or "")[:10])
        except ValueError:
            return None
        if not today < deadline <= today + timedelta(days=3 * 366) or not d["resolution_source"]:
            return None
        d["deadline"] = deadline.isoformat()
    return d


# ---------------------------------------------------------------- drafting
def build_prompt(items: list[dict[str, Any]], existing: list[dict[str, Any]], fixture: dict[str, Any] | None, today: str) -> str:
    series_lines = []
    for sid, (name, unit) in prices.SERIES.items():
        pts = (fixture or {}).get("series", {}).get(sid, {}).get("points") or []
        last = f", latest {pts[-1][0]} = {pts[-1][1]:.2f}" if pts else ""
        series_lines.append(f"- {sid}: {name} ({unit}){last}")
    return "\n".join([
        "You draft new binary markets for The Crophecy, a play-credit prediction market on European agriculture under heat, drought and politics.",
        f"Today: {today}. Read the fresh headlines and propose at most {MAX_PER_RUN} NEW markets they motivate (zero is fine).",
        "Each must be clearly resolvable, matter for European food or farming, and not restate an existing market.",
        "kind 'hard' = resolves from a monthly FRED price series with a rule; kind 'soft' = a policy/geopolitics event resolved from news by a deadline.",
        "FRED series (monthly):", *series_lines,
        'Hard rule formats: {"type": "pct_change_gt", "threshold": 0.1, "horizon_months": 6} (more than 10% above the latest value),',
        '{"type": "pct_change_lt", "threshold": -0.1, "horizon_months": 3}, {"type": "level_gt" | "level_lt", "level": 300, "horizon_months": 6}. horizon 1-12.',
        "Soft markets need a deadline YYYY-MM-DD within 3 years and a resolution_source (who would announce it).",
        "EXISTING MARKETS (do not duplicate):", *[f"- {e.get('title') or e.get('question')}" for e in existing[:80]],
        "FRESH HEADLINES (id, date, source: title [commodities; regions]):",
        *[f"{it['id']}: {(it.get('published_at') or it.get('fetched_at') or '')[:10]}, {it['source']}: {it['title']} "
          f"[{', '.join(it.get('commodities') or [])}; {', '.join(it.get('regions') or [])}]" for it in items],
        'Answer only with JSON: {"markets": [{"question": "Will ...?", "kind": "hard|soft", "series": "PWHEAMTUSDM", "rule": {...}, '
        '"deadline": "YYYY-MM-DD", "resolution_source": "...", "region": "Spain", "crop": "olive oil", "adverse_if": "yes|no", '
        '"prior": 0.3, "news_ids": [123], "why": "one sentence"}]}',
    ])


def add_draft(d: dict[str, Any]) -> dict[str, Any]:
    with _conn() as conn:
        conn.executescript(SCHEMA)
        cur = conn.execute(
            "INSERT INTO market_drafts (question, kind, series, rule, deadline, resolution_source, region, crop, adverse_if, prior, news_ids, why, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'draft',?)",
            (d["question"], d["kind"], d["series"], json.dumps(d["rule"]) if d["rule"] else None, d["deadline"], d["resolution_source"], d["region"],
             d["crop"], d["adverse_if"], d["prior"], json.dumps(d["news_ids"]), d["why"], ledger.now_iso()))
        row = conn.execute("SELECT * FROM market_drafts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row(row)


def draft_once(book: "LiveBook", ask: Ask | None = None) -> list[dict[str, Any]]:
    """Drafts markets from news newer than the cursor. Returns the stored drafts."""
    items, latest = news.query(since=_cursor(), limit=MAX_NEWS)
    if not items:
        return []
    if not job.spend():
        return []
    fixture = _fixture()
    existing = existing_markets(book)
    data = llm_jobs.ask_json(build_prompt(items, existing, fixture, datetime.now(UTC).strftime("%Y-%m-%d")), ask)
    _set_cursor(latest)
    ids = {it["id"] for it in items}
    out = []
    for raw in (data.get("markets") or [])[:MAX_PER_RUN]:
        d = clean(raw, ids)
        if d is None or is_duplicate(d, existing):
            continue
        if d["kind"] == "hard":
            try:
                d["question"] = instance_for(d, 0, fixture)["title"]  # the precise title (base, target, resolve month)
            except ValueError:
                continue
            if is_duplicate(d, existing):
                continue
        stored = add_draft(d)
        existing.append(stored)
        out.append(stored)
    logger.info("harvest market drafts: %d new from %d headlines", len(out), len(items))
    return out


def _step() -> None:
    from . import market

    draft_once(market.live_book())


def ensure_loop() -> bool:
    return job.ensure(_step)


# ---------------------------------------------------------------- approval
def _install(book: "LiveBook", q: dict[str, Any], price: float | None = None) -> float:
    """Adds the market to the book with world-model leans on its prior; returns the open price (the agents' consensus)."""
    from .market import clamp

    qid, prior = q["qid"], float(q.get("prior") or 0.5)
    seeds = []
    for p in book.personas:
        lean = float(p.get("soft_lean", 0.0))
        p_yes = clamp(prior + (lean if q.get("adverse_if") == "yes" else -lean), 0.02, 0.98)
        seeds.append(book._forecast_view({"persona_id": p["id"], "qid": qid, "p_yes": round(p_yes, 4),
                                          "rationale": "world-model lean on the news agent's estimate", "source": "fallback"}))
    if price is None:
        price = round(sum(s["p_yes"] for s in seeds) / len(seeds), 4) if seeds else 0.5
    book.meta[qid] = q  # type: ignore[assignment]
    book.open(qid, prior=price)
    if seeds and qid not in book.agent_forecasts:
        book.agent_forecasts[qid] = seeds
    return price


def approve(book: "LiveBook", draft_id: int) -> dict[str, Any]:
    """Opens a draft in the live book. Raises LookupError (unknown), ValueError (not a draft, or cannot open)."""
    with _conn() as conn:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT * FROM market_drafts WHERE id = ?", (draft_id,)).fetchone()
    if row is None:
        raise LookupError(f"unknown draft {draft_id}")
    d = _row(row)
    if d["status"] != "draft":
        raise ValueError(f"draft {draft_id} is already {d['status']}")
    q = instance_for(d, draft_id, _fixture())
    with book.lock:
        if q["qid"] in book.markets:
            raise ValueError(f"market {q['qid']} already exists")
        price = _install(book, q)
        book.version += 1
    with _conn() as conn:
        conn.execute("UPDATE market_drafts SET status = 'approved', qid = ?, instance = ?, open_price = ?, approved_at = ? WHERE id = ?",
                     (q["qid"], json.dumps(q), price, ledger.now_iso(), draft_id))
    logger.info("harvest market draft %d approved as %s at %.2f", draft_id, q["qid"], price)
    return {"id": draft_id, "qid": q["qid"], "title": q["title"], "open_price": price, "status": "approved"}


def reopen_approved(book: "LiveBook") -> int:
    """Re-opens approved drafts in a freshly seeded book (before the ledger replays). Returns how many."""
    with _conn() as conn:
        conn.executescript(SCHEMA)
        rows = conn.execute("SELECT instance, open_price FROM market_drafts WHERE status = 'approved' AND instance IS NOT NULL ORDER BY approved_at, id").fetchall()
    n = 0
    for r in rows:
        q = json.loads(r["instance"])
        if q["qid"] in book.markets:
            continue
        _install(book, q, None if r["open_price"] is None else float(r["open_price"]))
        n += 1
    return n
