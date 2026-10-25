"""Croppy, the friendly guide to The Crophecy: system prompt, live-context snapshot, reply parsing and offline fallback.

The route (agent/routes/croppy.py) builds a snapshot from the in-process live book, asks Gemini for
{"reply", "actions"} JSON and falls back to a canned, keyword-matched answer when Gemini is unavailable
or HARVEST_OFFLINE=1. Actions are deep links limited to LINKS, so the model cannot send users off-site.
"""

import json
import re
from typing import Any

MAX_MESSAGE = 500
MAX_HISTORY = 8
MAX_TURN_TEXT = 600
MAX_ACTIONS = 3
TOP_MARKETS = 6
TOP_MODELS = 3
NEWS_ITEMS = 4

LINKS: dict[str, str] = {
    "/harvest": "Home: live overview",
    "/harvest#markets": "Live markets",
    "/harvest#models": "The eight world models",
    "/harvest#holdout": "Sealed holdout: Verify and Tamper",
    "/harvest#map-sec": "Europe risk map",
    "/harvest#fund": "Resilience Fund",
    "/harvest/play": "Play: join and bet",
    "/harvest/play#mk": "Live bets tab",
    "/harvest/play#tm": "Time machine tab",
    "/harvest/play#th": "Write your theory",
    "/harvest/play#rank": "Your rank",
    "/harvest/board": "Big-screen board",
    "/harvest/terms": "Terms of play",
    "/harvest/api/agents": "Outside-agent API",
}

SYSTEM_PROMPT = """You are Croppy, a cheerful little wheat sprout who guides visitors through The Crophecy.

WHAT THE CROPHECY IS
- An agentic prediction market for European agriculture under heat and drought. Tagline: "Every harvest has its prophets. We score them."
- Eight AI "world model" agents each hold an explicit theory of how climate, politics and money move food prices
  (e.g. a Climate Pessimist, an Adaptation Optimist). They forecast with Gemini and bet against each other and humans.
- Markets are binary YES/NO questions (commodity price moves, drought emergencies, export bans, Gulf farmland deals).
  Prices come from an LMSR automated market maker: the price is the market-implied probability of YES and moves on every trade.
  A 2% fee in play credits goes to the Resilience Fund illustration.
- The sealed backtest: past quarters are a sealed holdout. The agents committed their forecasts to a hash-chained ledger
  anchored on Solana devnet before the outcomes were revealed. On the home page (Sealed holdout section) "Verify commitments"
  recomputes every hash, and "Tamper one forecast" shows that changing a single forecast breaks verification.
  Models are scored by Brier score (lower is better) against the market and a coin flip.
- Write your theory: anyone can write their own world model in a paragraph (max 1200 characters) on the Play page's Theory tab.
  It is committed to the ledger, run through the same Gemini forecaster on the same sealed holdout (names and dates masked)
  and scored against the eight agents and the market.
- PLAY CREDITS ONLY. Everyone gets 1,000 free play credits. Credits have no monetary value and cannot be bought, sold,
  transferred or withdrawn. No deposits, no cash-out. It is a forecasting tournament, not a betting or investment service.
- The Resilience Fund (earmarked for European farmers' heat and drought adaptation) shows how money would be allocated by
  market-implied risk. Real funding comes from sponsors and subscriptions, never from player losses.

PAGES
- /harvest (home): live stats, the holdout verdict, watch agents bet (bets flow, live feed), live markets, the world models,
  the sealed holdout with Reveal, Verify and Tamper, commodity prices, the Europe risk map, the Resilience Fund, how it works.
- /harvest/play: pick a nickname (no signup) and get 1,000 credits. Tabs: "Live bets" (buy YES or NO with a 10/25/50/100 stake),
  "Time machine" (forecast the sealed holdout questions yourself, lock in, then see real outcomes and your Brier score),
  "Theory" (write your own world model and backtest it), "Rank" (your credits PnL and Brier vs the world models and everyone).
- /harvest/board: the big-screen live board (bets flow, risk map, live bets, leaderboard, humans vs agents).
- /harvest/terms: terms of play (free entry, credits have no value, 18+ for sponsor prizes, not advice, devnet test tokens).

OUTSIDE-AGENT API (for developers who want their own bot to trade)
- GET /harvest/api/agents describes the API. POST /harvest/api/agents/register {name, world_model, owner?} returns an agent_id
  and a secret token (the world model is committed to the ledger first). GET /harvest/api/agents/markets lists open markets with
  masked price history. POST /harvest/api/agents/bet {token, qid, side: "YES"|"NO", spend} places a bet. The agent then appears
  on the leaderboard as an outside agent.

RULES
- Reply in 2 to 4 short, friendly sentences. Plain text, no markdown, at most one emoji.
- Never give financial or investment advice and never tell anyone what to bet on. You may explain what a price means.
  If asked about money, real betting or cashing out, say clearly it is play credits only.
- Only answer about The Crophecy and its topics (forecasting, world models, European agriculture and climate). Politely steer back otherwise.
- Use the LIVE CONTEXT below for current numbers; do not invent numbers that are not there.
- Suggest up to 3 actions, each a deep link chosen ONLY from this list (href: what it is):
{links}

Answer ONLY with JSON: {"reply": "<text>", "actions": [{"label": "<2-4 words>", "href": "<one href from the list>"}]}
"""

FALLBACK_INTRO = "My crystal cornfield is a bit foggy right now, so here's the short version."
FALLBACK_RULES: list[tuple[re.Pattern[str], str, list[str]]] = [
    (re.compile(r"\b(bet|buy|trade|stake|play|join|start)\b", re.I),
     "Head to Play, pick a nickname and you get 1,000 free play credits; then tap YES or NO on any live market.",
     ["/harvest/play", "/harvest/play#mk"]),
    (re.compile(r"theor|write", re.I),
     "On the Play page's Theory tab you write your own world model in a paragraph, and we backtest it on the sealed holdout against the eight agents.",
     ["/harvest/play#th"]),
    (re.compile(r"world.?model|agent|persona", re.I),
     "A world model is an AI agent with an explicit theory of how heat, politics and money move food prices; eight of them forecast and bet here.",
     ["/harvest#models", "/harvest/board"]),
    (re.compile(r"seal|verify|tamper|holdout|backtest|brier", re.I),
     "The agents sealed their forecasts on a hash-chained ledger before the outcomes were known; Verify recomputes every hash and Tamper shows one edit breaks it.",
     ["/harvest#holdout", "/harvest/play#tm"]),
    (re.compile(r"api|bot|developer|register|outside", re.I),
     "Outside agents register a world model via POST /harvest/api/agents/register, read /harvest/api/agents/markets and bet via /harvest/api/agents/bet.",
     ["/harvest/api/agents"]),
    (re.compile(r"fund|resilien|money|cash|real|withdraw|prize", re.I),
     "It's play credits only: they have no monetary value and can't be withdrawn. The Resilience Fund is a sponsor-funded illustration for farmers' heat adaptation.",
     ["/harvest#fund", "/harvest/terms"]),
]
FALLBACK_DEFAULT = ("The Crophecy is a prediction market where AI world models and humans forecast Europe's harvest with free play credits. "
                    "Jump into Play to bet, or browse the live markets.")


def clean_message(raw: Any) -> str:
    return " ".join(str(raw or "").split())[:MAX_MESSAGE]


def clean_history(raw: Any) -> list[dict[str, str]]:
    """The last MAX_HISTORY {role, text} turns; role is 'user' or 'croppy'."""
    if not isinstance(raw, list):
        return []
    out = []
    for turn in raw[-MAX_HISTORY:]:
        if not isinstance(turn, dict):
            continue
        text = " ".join(str(turn.get("text") or "").split())[:MAX_TURN_TEXT]
        if text:
            out.append({"role": "user" if turn.get("role") == "user" else "croppy", "text": text})
    return out


def clean_page(raw: Any) -> str:
    page = str(raw or "")[:80]
    return page if re.fullmatch(r"/harvest[\w/#.-]*", page) else ""


def _pct(p: Any) -> str:
    try:
        return f"{float(p) * 100:.0f}%"
    except (TypeError, ValueError):
        return "?"


def news_headlines(limit: int = NEWS_ITEMS) -> list[str]:
    """Newest headlines from agent.harvest.news when that module exists; [] without it or on any error in it."""
    try:
        from agent.harvest import news  # optional: the live news feed
    except ImportError:
        return []
    try:
        items, _latest = news.query(limit=limit)
    except Exception:  # noqa: BLE001 - an optional feed must never break Croppy
        return []
    return [" ".join(str(i.get("title") or "").split())[:160] for i in items if i.get("title")][:limit]


def snapshot(markets: list[dict[str, Any]], leaderboard: dict[str, Any], *, users: int = 0, news: list[str] | None = None) -> str:
    """Compact plain-text live context: top open markets with prices, leaderboard headline, players, news."""
    lines = ["LIVE CONTEXT"]
    open_markets = sorted((m for m in markets if m.get("status", "open") == "open"), key=lambda m: -float(m.get("volume") or 0))
    if open_markets:
        lines.append("Top live markets (price = market-implied chance of YES):")
        lines += [f"- {m.get('title', m.get('qid'))}: {_pct(m.get('p_yes'))} YES" for m in open_markets[:TOP_MARKETS]]
    if leaderboard.get("insight"):
        lines.append(f"Leaderboard headline: {leaderboard['insight']}")
    models = [m for m in leaderboard.get("world_models", []) if m.get("brier_holdout") is not None][:TOP_MODELS]
    if models:
        lines.append("Top world models by holdout Brier: " + "; ".join(f"{m['rank']}. {m['name']} {float(m['brier_holdout']):.3f}" for m in models))
    if users:
        lines.append(f"Human players so far: {users}")
    if news:
        lines.append("Latest news headlines:")
        lines += [f"- {h}" for h in news]
    return "\n".join(lines)


def build_prompt(message: str, *, history: list[dict[str, str]], page: str, context: str) -> str:
    links = "\n".join(f"  {href}: {what}" for href, what in LINKS.items())
    parts = [SYSTEM_PROMPT.replace("{links}", links), context]
    if page:
        parts.append(f"The visitor is on page {page}.")
    if history:
        parts.append("CONVERSATION SO FAR\n" + "\n".join(f"{'Visitor' if t['role'] == 'user' else 'Croppy'}: {t['text']}" for t in history))
    parts.append(f"VISITOR: {message}\nCROPPY (JSON):")
    return "\n\n".join(parts)


def clean_actions(raw: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        href = str(a.get("href") or "").strip()
        if href not in LINKS or any(x["href"] == href for x in out):
            continue
        label = " ".join(str(a.get("label") or "").split())[:32] or LINKS[href]
        out.append({"label": label, "href": href})
        if len(out) == MAX_ACTIONS:
            break
    return out


def parse_reply(text: str) -> dict[str, Any] | None:
    """{reply, actions} from the model's JSON answer, or None when it is unusable."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```\w*\s*|\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    reply = " ".join(str(data.get("reply") or "").split())[:900]
    if not reply:
        return None
    return {"reply": reply, "actions": clean_actions(data.get("actions"))}


def fallback(message: str) -> dict[str, Any]:
    """Friendly canned answer for when Gemini is unavailable or HARVEST_OFFLINE=1."""
    for pattern, text, hrefs in FALLBACK_RULES:
        if pattern.search(message):
            return {"reply": f"{FALLBACK_INTRO} {text}", "actions": [{"label": LINKS[h], "href": h} for h in hrefs], "fallback": True}
    return {"reply": f"{FALLBACK_INTRO} {FALLBACK_DEFAULT}",
            "actions": [{"label": LINKS[h], "href": h} for h in ("/harvest/play", "/harvest#markets")], "fallback": True}
