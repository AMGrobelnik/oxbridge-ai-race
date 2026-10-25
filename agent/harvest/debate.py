"""Agent debate threads: house world models argue about a live market and reply to each other.

Every DEBATE_S seconds (default 180) a background job picks the open market where the house agents
disagree most (spread of their p_yes, weighted a little by traded volume, skipping the markets debated
in the last few rounds), takes the two agents furthest apart, and asks Gemini for two posts: the first
agent states its case from its world model and the latest news, the second replies to it directly.
Posts are stored in the `debate_posts` table of the harvest DB as {id, qid, author, reply_to, text, p_yes, at}.

Two LLM calls per round, capped by DEBATE_MAX_CALLS (default 240 per process). A failed call falls back
to a post built from the agent's own forecast rationale; HARVEST_OFFLINE=1 disables the job.
"""

import logging
import sqlite3
from typing import TYPE_CHECKING, Any

from . import ledger, llm_jobs, news
from .llm_jobs import Ask
from .types import Persona

if TYPE_CHECKING:
    from .market import LiveBook

logger = logging.getLogger("agent")

MAX_TEXT = 420
RECENT_SKIP = 4
NEWS_LINES = 4
job = llm_jobs.Job("debate", interval_s=180.0, max_calls=240, first_delay_s=45.0)

SCHEMA = """
CREATE TABLE IF NOT EXISTS debate_posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  qid TEXT NOT NULL,
  author TEXT NOT NULL,
  reply_to INTEGER,
  text TEXT NOT NULL,
  p_yes REAL,
  at TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'llm'
);
CREATE INDEX IF NOT EXISTS debate_posts_qid ON debate_posts(qid, id);
"""


# ---------------------------------------------------------------- store
def _conn() -> Any:
    return ledger.connect()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "qid": r["qid"], "author": r["author"], "reply_to": r["reply_to"], "text": r["text"],
            "p_yes": r["p_yes"], "at": r["at"], "source": r["source"]}


def add_post(qid: str, author: str, text: str, *, p_yes: float | None = None, reply_to: int | None = None, source: str = "llm") -> dict[str, Any]:
    text = " ".join(str(text).split())[:MAX_TEXT]
    if not text:
        raise ValueError("empty post")
    with _conn() as conn:
        conn.executescript(SCHEMA)
        if reply_to is not None and not conn.execute("SELECT 1 FROM debate_posts WHERE id = ? AND qid = ?", (reply_to, qid)).fetchone():
            raise ValueError(f"reply_to {reply_to} is not a post on {qid}")
        cur = conn.execute("INSERT INTO debate_posts (qid, author, reply_to, text, p_yes, at, source) VALUES (?,?,?,?,?,?,?)",
                           (qid, author, reply_to, text, None if p_yes is None else round(float(p_yes), 4), ledger.now_iso(), source))
        row = conn.execute("SELECT * FROM debate_posts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row(row)


def posts(*, qid: str | None = None, since: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Posts with id > since (optionally on one market), oldest first, the newest `limit` of them."""
    sql, args = "SELECT * FROM debate_posts WHERE id > ?", [int(since or 0)]
    if qid:
        sql += " AND qid = ?"
        args.append(qid)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with _conn() as conn:
        conn.executescript(SCHEMA)
        return [_row(r) for r in conn.execute(sql, args)][::-1]


def threads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Groups posts into threads under their root post (replies whose root is outside `rows` start their own)."""
    by_id = {r["id"]: r for r in rows}
    roots: dict[int, dict[str, Any]] = {}
    for r in rows:
        root = r
        while root["reply_to"] is not None and root["reply_to"] in by_id:
            root = by_id[root["reply_to"]]
        t = roots.setdefault(root["id"], {"id": root["id"], "qid": root["qid"], "posts": []})
        t["posts"].append(r)
    out = list(roots.values())
    for t in out:
        t["at"] = t["posts"][-1]["at"]
    return sorted(out, key=lambda t: -t["posts"][-1]["id"])


def recent_qids(n: int = RECENT_SKIP) -> list[str]:
    with _conn() as conn:
        conn.executescript(SCHEMA)
        rows = conn.execute("SELECT qid FROM debate_posts WHERE reply_to IS NULL ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [r["qid"] for r in rows]


# ---------------------------------------------------------------- picking
def pick(book: "LiveBook", skip: list[str] | None = None) -> tuple[str, dict[str, Any], dict[str, Any]] | None:
    """(qid, first, second): the open market with the widest agent disagreement (volume as a tie-breaker), and its
    two most opposed agents; `first` is the one further from the market price."""
    skip = skip or []
    with book.lock:
        cands = []
        vmax = max((m["volume"] for m in book.markets.values()), default=0.0) or 1.0
        for qid, m in book.markets.items():
            fs = [f for f in book.agent_forecasts.get(qid, []) if f.get("p_yes") is not None]
            if m["status"] != "open" or len(fs) < 2:
                continue
            lo, hi = min(fs, key=lambda f: f["p_yes"]), max(fs, key=lambda f: f["p_yes"])
            spread = hi["p_yes"] - lo["p_yes"]
            if spread <= 0:
                continue
            score = spread + 0.15 * m["volume"] / vmax - (1.0 if qid in skip else 0.0)
            cands.append((score, qid, lo, hi, book.price(qid)))
    if not cands:
        return None
    _, qid, lo, hi, price = max(cands, key=lambda c: c[0])
    first, second = (hi, lo) if abs(hi["p_yes"] - price) >= abs(lo["p_yes"] - price) else (lo, hi)
    return qid, dict(first), dict(second)


# ---------------------------------------------------------------- prompts
def _persona_block(p: Persona | None, f: dict[str, Any]) -> list[str]:
    if p is None:
        return [f"You are {f.get('name', f['persona_id'])}, a forecasting agent."]
    lines = [f"You are {p['name']}, a world-model agent in The Crophecy, a play-credit prediction market on European agriculture.",
             f"Your world model: {p.get('world_model', '')}"]
    if p.get("beliefs"):
        lines.append("Your beliefs: " + "; ".join(p["beliefs"]))
    return lines


def _context(book: "LiveBook", qid: str) -> tuple[str, float, list[str]]:
    with book.lock:
        q = book.meta.get(qid) or {}
        title, price = str(q.get("title", qid)), book.price(qid)
        spec = str(q.get("spec_id") or qid.split("@")[0])
    lines = news.headlines_for([spec], per_spec=NEWS_LINES).get(spec, [])
    return title, price, lines


def build_case(persona: Persona | None, me: dict[str, Any], other: dict[str, Any], title: str, price: float, headlines: list[str]) -> str:
    return "\n".join([
        *_persona_block(persona, me),
        f"MARKET: {title}",
        f"Market price: {price:.0%} YES. Your forecast: {me['p_yes']:.0%} YES. {other.get('name', other['persona_id'])} forecasts {other['p_yes']:.0%} YES.",
        f"Your earlier rationale: {me.get('rationale') or '-'}",
        "Latest news on this market:" if headlines else "No fresh headlines on this market.",
        *[f"- {h}" for h in headlines],
        "Write a short public argument (2-3 sentences, under 380 characters) for your forecast, grounded in your world model;",
        "cite at most one headline. Plain text, no markdown, no investment advice, play credits only.",
        'Answer only with JSON: {"text": "..."}',
    ])


def build_reply(persona: Persona | None, me: dict[str, Any], other: dict[str, Any], opening: str, title: str, price: float, headlines: list[str]) -> str:
    oname = other.get("name", other["persona_id"])
    return "\n".join([
        *_persona_block(persona, me),
        f"MARKET: {title}",
        f"Market price: {price:.0%} YES. Your forecast: {me['p_yes']:.0%} YES. {oname} forecasts {other['p_yes']:.0%} YES.",
        f'{oname} just argued: "{opening}"',
        "Latest news on this market:" if headlines else "No fresh headlines on this market.",
        *[f"- {h}" for h in headlines],
        f"Reply directly to {oname} in 2-3 sentences (under 380 characters): rebut their argument from your world model",
        "and say why you stay at your forecast. Address them by name. Plain text, no markdown, no investment advice.",
        'Answer only with JSON: {"text": "..."}',
    ])


def fallback_case(me: dict[str, Any]) -> str:
    return f"I'm at {me['p_yes']:.0%} YES. {me.get('rationale') or 'My world model points this way.'}"


def fallback_reply(me: dict[str, Any], other: dict[str, Any]) -> str:
    return f"I don't buy it, {other.get('name', other['persona_id'])}: I stay at {me['p_yes']:.0%} YES. {me.get('rationale') or 'My world model reads it the other way.'}"


def _say(prompt: str, fallback: str, ask: Ask | None) -> tuple[str, str]:
    try:
        text = " ".join(str(llm_jobs.ask_json(prompt, ask).get("text") or "").split())
        if len(text) >= 20:
            return text[:MAX_TEXT], "llm"
    except Exception as err:  # noqa: BLE001 - any LLM failure falls back to the agent's own rationale
        logger.info("harvest debate: LLM failed, using fallback: %s", type(err).__name__)
    return fallback, "fallback"


# ---------------------------------------------------------------- one round
def round_once(book: "LiveBook", ask: Ask | None = None) -> list[dict[str, Any]]:
    """One exchange (case + reply) on the most disputed market. [] when nothing to debate or the cap is spent."""
    picked = pick(book, recent_qids())
    if picked is None or not job.spend():
        return []
    qid, a, b = picked
    personas = {p["id"]: p for p in book.personas}
    title, price, headlines = _context(book, qid)
    text, src = _say(build_case(personas.get(a["persona_id"]), a, b, title, price, headlines), fallback_case(a), ask)
    first = add_post(qid, f"agent:{a['persona_id']}", text, p_yes=a["p_yes"], source=src)
    if job.spend():
        text, src = _say(build_reply(personas.get(b["persona_id"]), b, a, first["text"], title, price, headlines), fallback_reply(b, a), ask)
    else:
        text, src = fallback_reply(b, a), "fallback"
    second = add_post(qid, f"agent:{b['persona_id']}", text, p_yes=b["p_yes"], reply_to=first["id"], source=src)
    logger.info("harvest debate: %s vs %s on %s", a["persona_id"], b["persona_id"], qid)
    return [first, second]


def _step() -> None:
    from . import market

    round_once(market.live_book())


def ensure_loop() -> bool:
    return job.ensure(_step)


# ---------------------------------------------------------------- view
def view(book: "LiveBook", *, qid: str | None = None, since: int | None = None, limit: int = 200) -> dict[str, Any]:
    rows = posts(qid=qid, since=since, limit=limit)
    with book.lock:
        personas = {f"agent:{p['id']}": p for p in book.personas}
        titles = {q: str((book.meta.get(q) or {}).get("title", q)) for q in {r["qid"] for r in rows}}
    for r in rows:
        p = personas.get(r["author"])
        r["name"] = p["name"] if p else r["author"].split(":", 1)[-1]
        r["color"] = p["color"] if p else "#888888"
        r["title"] = titles.get(r["qid"], r["qid"])
    return {"threads": threads(rows), "latest_id": rows[-1]["id"] if rows else int(since or 0), "job": job.status()}
