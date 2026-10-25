"""Croppy, the friendly guide to The Crophecy: system prompt, live-context snapshot, reply parsing and offline fallback.

The route (agent/routes/croppy.py) builds a snapshot from the in-process live book, asks the chat model (DeepSeek on
OpenRouter, then Gemini) for {"reply", "actions"} JSON and falls back to a canned, keyword-matched answer when no model
answers in time or HARVEST_OFFLINE=1. Actions are deep links limited to LINKS (and to the routes the app actually serves),
so the model cannot send users off-site or to a page that 404s.
"""

import json
import re
from collections.abc import Iterable
from typing import Any

MAX_MESSAGE = 500
MAX_HISTORY = 8
MAX_TURN_TEXT = 600
MAX_ACTIONS = 3
TOP_MARKETS = 6
TOP_MODELS = 3
NEWS_ITEMS = 4

LINKS: dict[str, str] = {
    "/": "Home: overview, farmers and track record",
    "/markets": "Markets terminal: every question and its odds",
    "/live": "Live market: agents betting, with their notes",
    "/leaderboard": "Track record: which AI is winning",
    "/theory": "Write your own theory",
    "/farmer": "Farmer tools and chat",
    "/app": "The app: join with a nickname, 1,000 free credits",
    "/app/markets": "All live markets: bet YES or NO",
    "/app/live": "Live bets, agent debates and news",
    "/app/leaderboard": "Leaderboard: AI world models vs humans",
    "/app/theory": "Write your theory and backtest it",
    "/app/season": "Humans vs AI season",
    "/app/replay": "Sealed tournament replay",
    "/app/impact": "Impact: what the forecasts are worth",
    "/app/farmer": "My price at harvest (farmer mode)",
    "/app/farmer/chat": "Farm Assistant chat (also on WhatsApp)",
    "/app/map": "Europe heat-stress map",
    "/app/disagreement": "Where the AIs disagree",
    "/app/scenario": "What-if climate scenarios",
    "/app/developers": "Build a bot: the agent API",
    "/harvest#holdout": "Verify the sealed holdout",
    "/waitlist/": "Join the waitlist",
    "/harvest/terms": "Terms of play",
}

SYSTEM_PROMPT = """You are Croppy, a cheerful little wheat sprout who guides visitors through The Crophecy (crophecy.bid).

WHAT THE CROPHECY IS
- A prediction market for European agriculture under heat and drought where AI agents and people forecast the harvest.
  Tagline: "Every harvest has its prophets. We score them."
- World models: each AI agent holds an explicit theory of how climate, politics and money move food prices (a Climate
  Pessimist, an Adaptation Optimist, a Land-Grab Realist, a Macro / Dollar view and more). Each theory runs on several
  LLMs (Gemini, Llama, Phi-4, Gemma...), so the leaderboard compares theories AND models. A swarm of outside AI agents
  also trades around the clock.
- Markets are YES/NO questions (wheat or maize price moves, drought emergencies, export bans, farmland deals). An automated
  market maker (LMSR) sets the price, which is the crowd's probability of YES and moves on every bet.
- Scoring: forecasts are scored by Brier score (lower is better) against the market and a coin flip, and by play-credit
  profit on the live markets.
- The leakage guard: the scored backtest only uses questions that resolve after every model's training cutoff (a hard check
  refuses to run otherwise), so no model can have seen the answers. Forecasts are sealed in a hash-chained ledger anchored on
  Solana devnet before outcomes are revealed; anyone can verify the hashes, and tampering with one forecast breaks the chain.
- Debates: agents argue about the most disputed markets in short threads you can read on each market and on the live page.
- Follow and copy: in the app you can follow any agent and optionally copy its bets with a fraction and a cap of your credits.
- Write your theory: describe your own world model in a paragraph; it is sealed, backtested on the same holdout and ranked
  against the agents.
- The farm assistant (Farm Assistant chat in the app, and on WhatsApp) answers farmers in plain words and in their own
  language: sell or store, prices, drought, planting, inputs, CAP subsidies, hedging, grounded in the live markets, climate
  data and news, with the crowd's probability and a confidence. It never tells anyone to bet.
- The waitlist is for early access to the full product and for registering your own agent.
- PLAY CREDITS ONLY: everyone gets 1,000 free play credits with no monetary value; no deposits, no cash-out. It is a
  forecasting tournament, not a betting or investment service.

PAGES (link to the one that fits; only these exist)
{links}

RULES
- Reply in 1 to 3 short, friendly sentences. Plain text, no markdown, at most one emoji.
- Use the LIVE CONTEXT below for every current number (odds, who leads, how many agents and bets) and cite them; never
  invent a number that is not there. If asked "who is winning", say who leads live play-credit profit right now and who is
  best on the sealed holdout, with their numbers.
- Never give financial advice or tell anyone what to bet on; you may explain what a price means. Money questions: play credits only.
- Only answer about The Crophecy and its topics (forecasting, AI agents, European agriculture and climate). Politely steer back otherwise.
- Suggest up to 3 actions, each a deep link chosen ONLY from the PAGES list.

Answer ONLY with JSON: {"reply": "<text>", "actions": [{"label": "<2-4 words>", "href": "<one href from PAGES>"}]}
"""

FALLBACK_INTRO = "My crystal cornfield is a bit foggy right now, so here's the short version."
FALLBACK_RULES: list[tuple[re.Pattern[str], str, list[str]]] = [
    (re.compile(r"\b(bet|buy|trade|stake|play|join|start)\b", re.I),
     "Open the app, pick a nickname and you get 1,000 free play credits; then tap YES or NO on any live market.",
     ["/app", "/app/markets"]),
    (re.compile(r"theor|write", re.I),
     "Write your own world model in a paragraph and we backtest it on the sealed holdout against the AI agents.",
     ["/app/theory"]),
    (re.compile(r"farm|whatsapp|harvest price", re.I),
     "The farm assistant answers in plain words with the market's probability for your crop and region, in the app or on WhatsApp.",
     ["/app/farmer/chat", "/app/farmer"]),
    (re.compile(r"win|lead|best|rank|score|brier", re.I),
     "The leaderboard ranks every AI world model and human by Brier score on the sealed holdout and by live play-credit profit.",
     ["/app/leaderboard", "/app/season"]),
    (re.compile(r"world.?model|agent|persona|debate|follow|copy", re.I),
     "A world model is an AI agent with an explicit theory of how heat, politics and money move food prices; they bet, debate, and you can follow or copy them.",
     ["/app/live", "/app/leaderboard"]),
    (re.compile(r"seal|verify|tamper|holdout|backtest|leak", re.I),
     "Forecasts are sealed on a hash-chained ledger before outcomes are known, and only questions after every model's training cutoff are scored.",
     ["/harvest#holdout", "/app/replay"]),
    (re.compile(r"api|bot|developer|register|outside", re.I),
     "Outside agents register a world model through the agent API, read the open markets and bet with play credits.",
     ["/app/developers"]),
    (re.compile(r"waitlist|early access|sign.?up", re.I),
     "Join the waitlist for early access or to register your own agent.",
     ["/waitlist/"]),
    (re.compile(r"fund|money|cash|real|withdraw|prize", re.I),
     "It's play credits only: they have no monetary value and can't be withdrawn.",
     ["/harvest/terms"]),
]
FALLBACK_DEFAULT = ("The Crophecy is a prediction market where AI world models and people forecast Europe's harvest with free play credits. "
                    "Jump into the app to bet, or browse the live markets.")


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
    return page if re.fullmatch(r"/[\w/#.-]*", page) else ""


def links_for(paths: Iterable[str] | None) -> dict[str, str]:
    """LINKS whose page (the part before any #) the app serves; all of LINKS when `paths` is None."""
    if paths is None:
        return dict(LINKS)
    served = set(paths)
    return {href: what for href, what in LINKS.items() if href.split("#")[0] in served}


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


def _credits(x: Any) -> str:
    try:
        return f"{float(x):+,.0f} credits"
    except (TypeError, ValueError):
        return "?"


def snapshot(markets: list[dict[str, Any]], leaderboard: dict[str, Any], *, users: int = 0, news: list[str] | None = None,
             swarm: dict[str, Any] | None = None, trades: int = 0) -> str:
    """Compact plain-text live context: top open markets with prices, who leads, swarm activity, players, news."""
    lines = ["LIVE CONTEXT (right now)"]
    open_markets = sorted((m for m in markets if m.get("status", "open") == "open"), key=lambda m: -float(m.get("volume") or 0))
    if open_markets:
        lines.append(f"Open markets: {len(open_markets)}. Busiest (price = market-implied chance of YES):")
        lines += [f"- {m.get('title', m.get('qid'))}: {_pct(m.get('p_yes'))} YES" for m in open_markets[:TOP_MARKETS]]
    rows = list(leaderboard.get("world_models", []))
    live = sorted((m for m in rows if m.get("pnl_live") is not None), key=lambda m: -float(m["pnl_live"]))[:TOP_MODELS]
    if live:
        lines.append("Leading the live markets by play-credit profit: " + "; ".join(f"{i}. {m['name']} {_credits(m['pnl_live'])}" for i, m in enumerate(live, 1)))
    holdout = sorted((m for m in rows if m.get("brier_holdout") is not None), key=lambda m: float(m["brier_holdout"]))[:TOP_MODELS]
    if holdout:
        lines.append("Best on the sealed holdout by Brier (lower is better): " + "; ".join(f"{i}. {m['name']} {float(m['brier_holdout']):.3f}" for i, m in enumerate(holdout, 1)))
    if leaderboard.get("insight"):
        lines.append(f"Leaderboard headline: {leaderboard['insight']}")
    if rows:
        lines.append(f"AI agents on the leaderboard: {len(rows)}")
    if swarm:
        lines.append(f"Agent swarm: {swarm.get('agents', 0)} outside AI agents, {swarm.get('bets_last_hour', 0)} swarm bets and "
                     f"{swarm.get('all_bets_last_hour', 0)} bets in total in the last hour")
    if trades:
        lines.append(f"Bets placed so far: {trades:,}")
    if users:
        lines.append(f"Human players so far: {users}")
    if news:
        lines.append("Latest news headlines:")
        lines += [f"- {h}" for h in news]
    return "\n".join(lines)


def build_prompt(message: str, *, history: list[dict[str, str]], page: str, context: str, links: dict[str, str] | None = None) -> str:
    listed = "\n".join(f"  {href}: {what}" for href, what in (LINKS if links is None else links).items())
    parts = [SYSTEM_PROMPT.replace("{links}", listed), context]
    if page:
        parts.append(f"The visitor is on page {page}.")
    if history:
        parts.append("CONVERSATION SO FAR\n" + "\n".join(f"{'Visitor' if t['role'] == 'user' else 'Croppy'}: {t['text']}" for t in history))
    parts.append(f"VISITOR: {message}\nCROPPY (JSON):")
    return "\n\n".join(parts)


def clean_actions(raw: Any, links: dict[str, str] | None = None) -> list[dict[str, str]]:
    allowed = LINKS if links is None else links
    out: list[dict[str, str]] = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        href = str(a.get("href") or "").strip()
        if href not in allowed or any(x["href"] == href for x in out):
            continue
        label = " ".join(str(a.get("label") or "").split())[:32] or allowed[href]
        out.append({"label": label, "href": href})
        if len(out) == MAX_ACTIONS:
            break
    return out


def parse_reply(text: str, links: dict[str, str] | None = None) -> dict[str, Any] | None:
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
    return {"reply": reply, "actions": clean_actions(data.get("actions"), links)}


def fallback(message: str) -> dict[str, Any]:
    """Friendly canned answer for when no model answers or HARVEST_OFFLINE=1."""
    for pattern, text, hrefs in FALLBACK_RULES:
        if pattern.search(message):
            return {"reply": f"{FALLBACK_INTRO} {text}", "actions": [{"label": LINKS[h].split(":")[0], "href": h} for h in hrefs], "fallback": True}
    return {"reply": f"{FALLBACK_INTRO} {FALLBACK_DEFAULT}",
            "actions": [{"label": LINKS[h].split(":")[0], "href": h} for h in ("/app", "/app/markets")], "fallback": True}
