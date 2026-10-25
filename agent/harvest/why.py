"""'Why did this move?': Croppy explains a market's latest price move from the ledger bets and tagged news.

`move` takes the market's last `n` trades (the latest move), names who bet what and how far each bet moved the
YES price, and picks the news headlines tagged to the market's question that were published in (or shortly before)
that window. `explain_fallback` writes the explanation deterministically; `build_prompt`/`parse` let Croppy (a fast
LLM) write it instead, citing the same bets and headlines.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any

NEWS_LEAD_H = 72
TOP_BETS = 5
MAX_NEWS = 5


def trader_name(trader: str, traders: dict[str, dict[str, Any]], personas: dict[str, str]) -> str:
    if trader.startswith("agent:"):
        return personas.get(trader[6:], trader[6:])
    info = traders.get(trader) or {}
    if trader.startswith("user:"):
        return str(info.get("nickname") or info.get("name") or "a player")
    return str(info.get("name") or info.get("nickname") or "an outside agent")


def _iso(at: str) -> datetime | None:
    try:
        return datetime.fromisoformat(at.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def move(trades: list[dict[str, Any]], traders: dict[str, dict[str, Any]], personas: dict[str, str], *, n: int, price_now: float) -> dict[str, Any]:
    """The latest move: the last `n` trades on the market, price before/after and the bets that moved it most."""
    window = trades[-n:]
    if not window:
        return {"n_bets": 0, "from": price_now, "to": price_now, "delta": 0.0, "start": None, "end": None, "bets": [], "top": [],
                "yes_spend": 0.0, "no_spend": 0.0}
    bets = []
    for t in window:
        delta = float(t["price_after"]) - float(t["price_before"])
        bets.append({"seq": t["seq"], "trader": t["trader"], "name": trader_name(t["trader"], traders, personas), "side": t["side"],
                     "spend": round(float(t["spend"]), 2), "price_before": round(float(t["price_before"]), 4),
                     "price_after": round(float(t["price_after"]), 4), "delta": round(delta, 4), "at": t.get("at"),
                     "rationale": str(t.get("rationale") or "")[:240]})
    start, end = float(window[0]["price_before"]), float(window[-1]["price_after"])
    top = sorted(bets, key=lambda b: -abs(b["delta"]))[:TOP_BETS]
    return {"n_bets": len(bets), "from": round(start, 4), "to": round(end, 4), "delta": round(end - start, 4),
            "start": window[0].get("at"), "end": window[-1].get("at"), "bets": bets, "top": top,
            "yes_spend": round(sum(b["spend"] for b in bets if b["side"] == "YES"), 2),
            "no_spend": round(sum(b["spend"] for b in bets if b["side"] == "NO"), 2)}


def news_in_window(items: list[dict[str, Any]], start: str | None, *, lead_h: int = NEWS_LEAD_H, limit: int = MAX_NEWS) -> list[dict[str, Any]]:
    """Tagged headlines published from `lead_h` hours before the window start onwards (newest first);
    when none are that recent, the latest few, marked in_window=False."""
    begin = _iso(start) if start else None
    cutoff = begin - timedelta(hours=lead_h) if begin else None
    out = []
    for r in items:
        when = _iso(str(r.get("published_at") or r.get("fetched_at") or ""))
        inside = bool(cutoff and when and when >= cutoff)
        out.append({"title": r["title"], "source": r.get("source"), "url": r.get("url"), "published_at": r.get("published_at") or r.get("fetched_at"),
                    "direction": r.get("direction"), "summary": r.get("summary"), "in_window": inside})
    recent = [x for x in out if x["in_window"]]
    return (recent or out)[:limit]


def _pts(x: float) -> str:
    return f"{x * 100:+.1f} pts"


def explain_fallback(title: str, mv: dict[str, Any], headlines: list[dict[str, Any]]) -> str:
    if not mv["n_bets"]:
        return f"Nobody has bet on \"{title}\" yet, so the price has not moved from {mv['to'] * 100:.0f}%."
    verb = "rose" if mv["delta"] > 0.0005 else "fell" if mv["delta"] < -0.0005 else "held"
    parts = [f"Over the last {mv['n_bets']} bets the YES price {verb} from {mv['from'] * 100:.0f}% to {mv['to'] * 100:.0f}% "
             f"({mv['yes_spend']:.0f} credits on YES, {mv['no_spend']:.0f} on NO)."]
    tops = [f"{b['name']} bet {b['spend']:.0f} credits on {b['side']} ({_pts(b['delta'])})" for b in mv["top"][:3]]
    if tops:
        parts.append("The biggest pushes: " + "; ".join(tops) + ".")
    fresh = [h for h in headlines if h["in_window"]]
    if fresh:
        parts.append(f"In the news around then: \"{fresh[0]['title']}\"" + (f" ({fresh[0]['source']})" if fresh[0].get("source") else "") + ".")
    else:
        parts.append("No tagged headline landed in this window, so the move came from the traders' own world models.")
    return " ".join(parts)


def build_prompt(title: str, mv: dict[str, Any], headlines: list[dict[str, Any]]) -> str:
    lines = ["You are Croppy, the cheerful wheat-sprout guide of The Crophecy, a play-credit prediction market on European agriculture.",
             f"Market: {title}",
             f"Over the last {mv['n_bets']} bets the YES price went from {mv['from']:.3f} to {mv['to']:.3f}.", "Bets (oldest first):"]
    for b in mv["bets"]:
        why = f" because: {b['rationale'][:140]}" if b["rationale"] else ""
        lines.append(f"- #{b['seq']} {b['name']} bet {b['spend']:.0f} credits on {b['side']}, price {b['price_before']:.3f} -> {b['price_after']:.3f}{why}")
    lines.append("Tagged news headlines:" if headlines else "No tagged news headlines.")
    lines += [f"- {h['published_at'] or ''} {h.get('source') or ''}: {h['title']}" + ("" if h["in_window"] else " (older)") for h in headlines]
    lines += ["", "In 2-4 plain sentences (under 90 words, no jargon), explain why the price moved. Cite the specific bets "
              "(who, side, credits, how much they moved it) and any headline that plausibly drove them. Do not invent facts.",
              'Answer only with JSON: {"explanation": "..."}']
    return "\n".join(lines)


def parse(text: str) -> str | None:
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(match.group(0) if match else text)
    except (json.JSONDecodeError, TypeError):
        return None
    out = data.get("explanation") if isinstance(data, dict) else None
    return str(out).strip()[:900] if out and str(out).strip() else None
