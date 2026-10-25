"""Market resolution: a FRED oracle for hard (price-rule) markets, an evidence agent for soft ones, admin confirmation.

Hard markets: a background job (RESOLVE_ORACLE_S, default 600 s) looks at open hard markets whose resolve month
has ended, re-fetches their FRED series (at most once an hour per series, 20 s timeout) and, once the resolve
month's value exists, resolves them through questions.resolve + Book.resolve: winners are paid, a `resolve`
ledger entry records the outcome, value, payouts and each house agent's Brier on the market (the live scores).

Soft markets cannot auto-resolve. The evidence agent (RESOLVE_EVIDENCE_S, default 1800 s, one market per run,
capped by RESOLVE_EVIDENCE_MAX_CALLS, default 48) reads the market's tagged news and proposes YES / NO / NOT_YET
with cited headlines and a confidence. Proposals are stored in `resolution_proposals` and never applied
automatically: POST /harvest/api/resolve/{qid} with the admin token applies or rejects one.
HARVEST_OFFLINE=1 disables both jobs and every network call.
"""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx

from . import forecaster, ledger, llm_jobs, news, prices, questions
from .llm_jobs import Ask
from .market import MarketError

if TYPE_CHECKING:
    from .market import LiveBook

logger = logging.getLogger("agent")

FRED_TIMEOUT_S = 20.0
FRED_MIN_S = 3600.0
EVIDENCE_NEWS = 12
VERDICTS = ("YES", "NO", "NOT_YET")
oracle_job = llm_jobs.Job("resolve_oracle", interval_s=600.0, max_calls=0, first_delay_s=30.0)
evidence_job = llm_jobs.Job("resolve_evidence", interval_s=1800.0, max_calls=48, first_delay_s=90.0)
Fetch = Callable[[str], list[tuple[str, float]]]

SCHEMA = """
CREATE TABLE IF NOT EXISTS resolution_proposals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  qid TEXT NOT NULL,
  verdict TEXT NOT NULL,
  confidence REAL NOT NULL,
  rationale TEXT NOT NULL,
  evidence TEXT NOT NULL,
  status TEXT NOT NULL,
  at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT
);
CREATE INDEX IF NOT EXISTS resolution_proposals_qid ON resolution_proposals(qid, id);
"""

_series: dict[str, dict[str, Any]] = {}
_series_lock = threading.Lock()


def _conn() -> Any:
    return ledger.connect()


def this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


# ---------------------------------------------------------------- applying a resolution
def apply_resolution(book: "LiveBook", qid: str, outcome: int, *, by: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolves `qid` in the book (pays winners) and appends the `resolve` ledger entry, under the book lock.
    The entry carries each house agent's Brier on this market, which the live scores are built from."""
    with book.lock:
        m = book.markets.get(qid)
        if m is None:
            raise MarketError(f"unknown market {qid!r}")
        if m["status"] != "open":
            raise MarketError(f"market {qid!r} is already resolved")
        price = book.price(qid)
        briers = {f["persona_id"]: round((float(f["p_yes"]) - outcome) ** 2, 6) for f in book.agent_forecasts.get(qid, [])}
        payouts = book.resolve(qid, outcome)
        payload = {"qid": qid, "outcome": outcome, "by": by, "price_at_resolve": round(price, 6), "agent_briers": briers,
                   "payouts": [{"trader": p["trader"], "amount": round(p["amount"], 6)} for p in payouts], **(extra or {})}
        try:
            entry = ledger.append(kind="resolve", trader=by, payload=payload)
        except BaseException:
            for p in payouts:
                book.accounts[p["trader"]] -= p["amount"]
            m["status"], m["outcome"] = "open", None
            raise
        book.ledger_head = (entry["seq"], entry["hash"])
        book.version += 1
    logger.info("harvest resolve: %s -> %s by %s, %d payouts", qid, "YES" if outcome else "NO", by, len(payouts))
    return {"qid": qid, "outcome": outcome, "payouts": payload["payouts"], "entry": {"seq": entry["seq"], "hash": entry["hash"]}}


def resolved_entries() -> dict[str, dict[str, Any]]:
    """qid -> the payload (plus at, seq) of its resolve ledger entry."""
    out: dict[str, dict[str, Any]] = {}
    with _conn() as conn:
        for r in conn.execute("SELECT seq, payload, at FROM entries WHERE kind = 'resolve' ORDER BY seq ASC"):
            p = json.loads(r["payload"])
            out[str(p.get("qid"))] = {**p, "at": r["at"], "seq": r["seq"]}
    return out


def live_scores(entries: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """persona_id -> {brier_live, n_live}: mean Brier of each house agent's belief at resolution on resolved live markets."""
    acc: dict[str, list[float]] = {}
    for p in (resolved_entries() if entries is None else entries).values():
        for pid, b in (p.get("agent_briers") or {}).items():
            acc.setdefault(pid, []).append(float(b))
    return {pid: {"brier_live": sum(v) / len(v), "n_live": len(v)} for pid, v in acc.items()}


# ---------------------------------------------------------------- hard oracle (FRED)
def fetch_series(sid: str) -> list[tuple[str, float]]:
    if forecaster.offline_env():
        raise RuntimeError("HARVEST_OFFLINE=1: FRED disabled")
    with httpx.Client(timeout=FRED_TIMEOUT_S, follow_redirects=True) as client:
        r = client.get(prices.FRED_CSV.format(sid))
        r.raise_for_status()
    return prices.parse_csv(r.text, sid)


def series_points(sid: str, *, fetch: Fetch = fetch_series, now: float | None = None) -> list[tuple[str, float]] | None:
    """The series' points, re-fetched from FRED at most once per FRED_MIN_S (a failure also waits that long)."""
    now = time.monotonic() if now is None else now
    with _series_lock:
        cached = _series.get(sid)
        if cached is not None and now - cached["mono"] < FRED_MIN_S:
            return cached["points"]
        _series[sid] = {**(cached or {"points": None}), "mono": now, "checked_at": ledger.now_iso()}
    try:
        pts = fetch(sid)
        with _series_lock:
            _series[sid].update(points=pts, error=None, last_month=pts[-1][0] if pts else None)
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as err:
        logger.info("harvest oracle: FRED %s failed: %s", sid, type(err).__name__)
        with _series_lock:
            _series[sid]["error"] = type(err).__name__
    return _series[sid]["points"]


def reset() -> None:
    with _series_lock:
        _series.clear()
    oracle_job.reset()
    evidence_job.reset()


def due_hard(book: "LiveBook", month: str | None = None) -> list[dict[str, Any]]:
    """Open hard markets whose resolve month has ended (so its FRED value may now exist)."""
    month = month or this_month()
    with book.lock:
        return [dict(q) for qid, q in book.meta.items()
                if qid in book.markets and book.markets[qid]["status"] == "open" and q.get("kind") == "hard"
                and q.get("series") and q.get("rule") and q.get("resolve_month") and str(q["resolve_month"]) < month]


def oracle_once(book: "LiveBook", *, fetch: Fetch = fetch_series, month: str | None = None, now: float | None = None) -> list[str]:
    """Resolves every due hard market whose resolve-month value is out. Returns the resolved qids."""
    done = []
    for q in due_hard(book, month):
        pts = series_points(q["series"], fetch=fetch, now=now)
        if not pts:
            continue
        res = questions.resolve(q, {"series": {q["series"]: {"points": pts}}})  # type: ignore[arg-type,typeddict-item]
        if res["outcome"] is None:
            continue
        try:
            apply_resolution(book, q["qid"], int(res["outcome"]), by="oracle:fred", extra={
                "value": res["resolved_value"], "resolve_month": res["resolve_month"], "series": q["series"], "target": q.get("target"),
                "source": prices.FRED_CSV.format(q["series"])})
            done.append(q["qid"])
        except MarketError as err:
            logger.info("harvest oracle: %s not resolved: %s", q["qid"], err)
    return done


# ---------------------------------------------------------------- soft evidence agent
def _prow(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "qid": r["qid"], "verdict": r["verdict"], "confidence": r["confidence"], "rationale": r["rationale"],
            "evidence": json.loads(r["evidence"]), "status": r["status"], "at": r["at"], "decided_at": r["decided_at"], "decided_by": r["decided_by"]}


def proposals(*, qid: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM resolution_proposals", []
    if qid:
        sql += " WHERE qid = ?"
        args.append(qid)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        conn.executescript(SCHEMA)
        return [_prow(r) for r in conn.execute(sql, args)]


def add_proposal(qid: str, verdict: str, confidence: float, rationale: str, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Stores a proposal (YES/NO pending admin, NOT_YET informational); it supersedes the market's older open ones."""
    if verdict not in VERDICTS:
        raise ValueError(f"bad verdict {verdict!r}")
    status = "info" if verdict == "NOT_YET" else "pending"
    with _conn() as conn:
        conn.executescript(SCHEMA)
        conn.execute("UPDATE resolution_proposals SET status = 'superseded' WHERE qid = ? AND status IN ('pending', 'info')", (qid,))
        cur = conn.execute("INSERT INTO resolution_proposals (qid, verdict, confidence, rationale, evidence, status, at) VALUES (?,?,?,?,?,?,?)",
                           (qid, verdict, round(min(max(float(confidence), 0.0), 1.0), 3), " ".join(rationale.split())[:500],
                            json.dumps(evidence), status, ledger.now_iso()))
        row = conn.execute("SELECT * FROM resolution_proposals WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _prow(row)


def _soft_open(book: "LiveBook") -> list[dict[str, Any]]:
    with book.lock:
        return [dict(q) for qid, q in book.meta.items()
                if qid in book.markets and book.markets[qid]["status"] == "open" and q.get("kind") == "soft"]


def build_evidence_prompt(q: dict[str, Any], items: list[dict[str, Any]], today: str) -> str:
    return "\n".join([
        "You are the resolution evidence agent of The Crophecy, a play-credit prediction market on European agriculture.",
        f"QUESTION: {q.get('title', q['qid'])}",
        f"Deadline: {q.get('resolve_month') or 'none given'}. Today: {today}.",
        "Recent tagged headlines (id, date, source: title):",
        *[f"{it['id']}: {(it.get('published_at') or it.get('fetched_at') or '')[:10]}, {it['source']}: {it['title']}" for it in items],
        "Has the question ALREADY resolved? YES only if the headlines report that the event has clearly happened;",
        "NO only if the deadline has passed without it or it has become impossible; otherwise NOT_YET.",
        "Risk, forecasts, speculation and warnings are NOT_YET. Be conservative. Cite the ids of the headlines you rely on.",
        'Answer only with JSON: {"verdict": "YES|NO|NOT_YET", "confidence": 0.0-1.0, "cited": [123], "rationale": "<= 2 sentences"}',
    ])


def evidence_once(book: "LiveBook", ask: Ask | None = None, *, qid: str | None = None) -> dict[str, Any] | None:
    """Assesses one open soft market (the one assessed longest ago, or `qid`) and stores the proposal. Never resolves."""
    soft = _soft_open(book)
    if qid:
        soft = [q for q in soft if q["qid"] == qid]
    if not soft:
        return None
    last = {p["qid"]: p["at"] for p in proposals(limit=1000)[::-1]}
    soft.sort(key=lambda q: last.get(q["qid"], ""))
    for q in soft:
        items, _ = news.query(spec=str(q.get("spec_id") or q["qid"]), limit=EVIDENCE_NEWS)
        if items:
            break
    else:
        return None
    if not evidence_job.spend():
        return None
    try:
        data = llm_jobs.ask_json(build_evidence_prompt(q, items, datetime.now(UTC).strftime("%Y-%m-%d")), ask)
    except Exception as err:  # noqa: BLE001 - fallback: no proposal, the market keeps its current status
        logger.info("harvest evidence: LLM failed for %s: %s", q["qid"], type(err).__name__)
        return None
    verdict = str(data.get("verdict", "")).upper().replace("-", "_").replace(" ", "_")
    if verdict not in VERDICTS:
        return None
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    by_id = {it["id"]: it for it in items}
    cited = []
    for c in data.get("cited") or []:
        try:
            cid = int(c)
        except (TypeError, ValueError):
            continue
        if cid in by_id:
            cited.append({k: by_id[cid].get(k) for k in ("id", "title", "url", "source", "published_at")})
    if verdict != "NOT_YET" and not cited:
        verdict, conf = "NOT_YET", min(conf, 0.5)  # a YES/NO with no cited headline is not evidence
    return add_proposal(q["qid"], verdict, conf, str(data.get("rationale") or ""), cited)


def decide(book: "LiveBook", qid: str, action: str, *, proposal_id: int | None = None, by: str = "admin") -> dict[str, Any]:
    """Admin: apply (resolve the market with the proposal's verdict) or reject a pending proposal."""
    if action not in ("apply", "reject"):
        raise ValueError("action must be apply or reject")
    pending = [p for p in proposals(qid=qid) if p["status"] == "pending" and (proposal_id is None or p["id"] == proposal_id)]
    if not pending:
        raise LookupError(f"no pending proposal for {qid!r}")
    p = pending[0]
    result: dict[str, Any] = {"qid": qid, "proposal_id": p["id"], "action": action}
    if action == "apply":
        result |= apply_resolution(book, qid, 1 if p["verdict"] == "YES" else 0, by=by, extra={
            "proposal_id": p["id"], "confidence": p["confidence"], "evidence": [e.get("id") for e in p["evidence"]], "rationale": p["rationale"]})
    with _conn() as conn:
        conn.execute("UPDATE resolution_proposals SET status = ?, decided_at = ?, decided_by = ? WHERE id = ?",
                     ("applied" if action == "apply" else "rejected", ledger.now_iso(), by, p["id"]))
    return result


# ---------------------------------------------------------------- statuses
def _expected(resolve_month: str) -> str:
    return f"{prices.add_months(resolve_month, 1)}-15"


def status_for(book: "LiveBook", qid: str, *, resolved: dict[str, dict[str, Any]], latest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    with book.lock:
        q, m = book.meta.get(qid) or {}, book.markets[qid]
    out: dict[str, Any] = {"qid": qid, "title": q.get("title", qid), "kind": q.get("kind", "hard")}
    if m["status"] == "resolved":
        r = resolved.get(qid, {})
        word = "YES" if m["outcome"] == 1 else "NO"
        how = f"FRED {r.get('series')} {r.get('resolve_month')} = {r.get('value')}" if r.get("by") == "oracle:fred" else "admin, on the evidence agent's proposal"
        return out | {"state": "resolved", "outcome": word, "by": r.get("by"), "at": r.get("at"), "value": r.get("value"),
                      "text": f"Resolved {word} ({how})."}
    if q.get("kind") == "hard" and q.get("series") and q.get("resolve_month"):
        month, sid = str(q["resolve_month"]), str(q["series"])
        cache = _series.get(sid) or {}
        expected = _expected(month)
        text = (f"Auto-resolves from FRED {sid}: waiting for the {month} value, expected around {expected}."
                if month >= this_month() else f"Auto-resolves from FRED {sid}: the {month} value is due now; checked hourly.")
        return out | {"state": "waiting", "mode": "auto", "series": sid, "resolve_month": month, "expected_by": expected,
                      "last_checked": cache.get("checked_at"), "latest_data_month": cache.get("last_month"), "text": text}
    p = latest.get(qid)
    base = out | {"mode": "evidence", "deadline": q.get("resolve_month")}
    if p and p["status"] == "pending":
        return base | {"state": "proposed", "proposal": p,
                       "text": f"Evidence agent proposes {p['verdict']} ({p['confidence']:.0%} confident); waiting for admin confirmation."}
    if p and p["status"] == "info":
        return base | {"state": "open", "proposal": p, "text": f"Evidence agent: not yet resolved (checked {p['at'][:10]}, {len(p['evidence'])} headlines cited)."}
    return base | {"state": "open", "text": f"Resolved by an admin from news evidence; deadline {q.get('resolve_month') or 'open'}."}


def view(book: "LiveBook") -> dict[str, Any]:
    resolved = resolved_entries()
    recent = proposals(limit=200)
    latest: dict[str, dict[str, Any]] = {}
    for p in recent:
        latest.setdefault(p["qid"], p)
    with book.lock:
        qids = list(book.markets)
    markets = [status_for(book, q, resolved=resolved, latest=latest) for q in qids]
    counts: dict[str, int] = {}
    for s in markets:
        counts[s["state"]] = counts.get(s["state"], 0) + 1
    return {"markets": markets, "counts": counts, "proposals": recent[:50], "scores": live_scores(resolved),
            "jobs": {"oracle": oracle_job.status(), "evidence": evidence_job.status()}}


# ---------------------------------------------------------------- background
def _oracle_step() -> None:
    from . import market

    oracle_once(market.live_book())


def _evidence_step() -> None:
    from . import market

    evidence_once(market.live_book())


def ensure_loops() -> bool:
    a = oracle_job.ensure(_oracle_step)
    b = evidence_job.ensure(_evidence_step)
    return a or b
