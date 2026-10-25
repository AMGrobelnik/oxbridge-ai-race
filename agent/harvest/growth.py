"""Growth and openness: share-card data, the humans-vs-AI season table and the open-data export rows.

Pure functions over a `market.LiveBook` (or the frozen replay); the HTTP side is agent/routes/harvest_growth.py and
the PNG drawing is ogcard.py. Nothing here reads tokens, token hashes or contact details: the export columns are an
explicit allow-list.
"""

import csv
import io
import re
from collections import defaultdict
from typing import Any

from . import accounts, ledger
from .market import STARTING_CREDITS, LiveBook

NAME = "The Crophecy"
TAGLINE = "Every harvest has its prophets. We score them."
LICENSE = {"name": "CC-BY-4.0", "url": "https://creativecommons.org/licenses/by/4.0/",
           "notice": "Open data from The Crophecy, licensed CC-BY-4.0: share and adapt it, with credit to The Crophecy (play credits only, no real money)."}
SEASON_HEADLINE = "Can you beat the FAO bot?"
FAO_PREFIX = "narr-fao"

_SUBJECT = re.compile(r"^Will (.+?) \(now", re.I)
_DOWN = re.compile(r"\b(below|lower|under|less|fall)\b", re.I)
_UP = re.compile(r"\b(above|higher|more|over|rise)\b", re.I)


def pct(p: float) -> int:
    return round(float(p) * 100)


def take(title: str, p_yes: float) -> str:
    """A short phrase for a view on a question: '72% wheat up' for price questions, '72% YES' otherwise."""
    m = _SUBJECT.match(title)
    if not m:
        return f"{pct(p_yes)}% YES"
    word = "down" if _DOWN.search(title) and not _UP.search(title) else "up" if _UP.search(title) else "YES"
    return f"{pct(p_yes)}% {m.group(1)} {word}"


def market_card(view: dict[str, Any], *, base_url: str) -> dict[str, Any]:
    """The share card of one market (a `LiveBook.market_view`)."""
    qid, n_agents = view["qid"], len({f["persona_id"] for f in view.get("agent_forecasts") or []})
    page = f"{base_url}/app/market/{qid}"
    stats = [f"{round(view.get('volume') or 0):,} credits traded"]
    if n_agents:
        stats.append(f"{n_agents} AI agents weigh in")
    if view.get("status") == "resolved":
        stats.insert(0, "Resolved " + ("YES" if view.get("outcome") == 1 else "NO"))
    return {"kind": "market", "id": qid, "name": NAME, "tagline": TAGLINE, "headline": f"The market says {take(view['title'], view['p_yes'])}",
            "subtitle": view["title"], "p_yes": view["p_yes"], "stats": stats, "url": page, "image_url": f"{base_url}/harvest/og/market/{qid}.png",
            "description": f"{view['title']} The market says {pct(view['p_yes'])}% YES. {TAGLINE}"}


def find_account(accounts_rows: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    """A leaderboard account by id or username (a leading @ is ignored)."""
    key = key.lstrip("@")
    return next((a for a in accounts_rows if a["id"] == key), None) or next((a for a in accounts_rows if a.get("username") == key), None)


def trader_id(account: dict[str, Any]) -> str | None:
    """The book's trader id of a leaderboard account; None for baselines, which do not trade."""
    if account["category"] == "baseline":
        return None
    return account["id"] if account["category"] == "users" else f"agent:{account['id']}"


def hottest_take(book: LiveBook, trader: str) -> dict[str, Any] | None:
    """The open market where an agent is most sure (furthest from 50%), from its live forecast or else its latest bet."""
    best: tuple[float, dict[str, Any]] | None = None
    persona = trader.removeprefix("agent:") if trader.startswith("agent:") else None
    for qid, rows in book.agent_forecasts.items():
        if book.markets.get(qid, {}).get("status") != "open":
            continue
        for f in rows:
            if persona and f["persona_id"] == persona and (best is None or abs(f["p_yes"] - 0.5) > best[0]):
                best = (abs(f["p_yes"] - 0.5), {"qid": qid, "p_yes": f["p_yes"], "rationale": f.get("rationale")})
    if best is None:
        for t in reversed(book.trades):
            if t["trader"] == trader and book.markets.get(t["qid"], {}).get("status") == "open":
                p = t["price_after"]
                best = (0.0, {"qid": t["qid"], "p_yes": p, "rationale": t.get("rationale"), "side": t["side"]})
                break
    if best is None:
        return None
    qid = best[1]["qid"]
    return {**best[1], "title": (book.meta.get(qid) or {}).get("title", qid)}


def agent_card(account: dict[str, Any], take_: dict[str, Any] | None, *, rank_of: int, base_url: str) -> dict[str, Any]:
    """The share card of one leaderboard account. `take_` is its hottest_take (or None)."""
    full, brier = account["name"], account.get("brier_holdout")
    name, _, model = full.partition(" · ")  # "FAO (narrative) · Phi-4": the headline names the persona, the stats the model
    icon = "human" if account["type"] == "human" else "agent"
    stats = [f"Model {model}"] if model else []
    if brier is not None:
        stats.append(f"Brier {brier:.3f} (lower is better, coin flip 0.250)")
    stats.append(f"Rank {account['rank']} of {rank_of}")
    if take_ and "side" not in take_:
        headline, sub, p = f"{name} says {take(take_['title'], take_['p_yes'])}", take_["title"], take_["p_yes"]
    elif take_:
        headline, sub, p = f"{name} backs {take_['side']} at {pct(take_['p_yes'])}%", take_["title"], take_["p_yes"]
    else:
        headline = f"{name} is on the leaderboard"
        sub, p = ("A human forecaster" if icon == "human" else "An AI forecaster") + " scored in the open.", None
    ident = account.get("username") or account["id"]
    return {"kind": "agent", "id": account["id"], "name": NAME, "tagline": TAGLINE, "headline": headline, "subtitle": sub, "p_yes": p, "stats": stats,
            "type": icon, "username": ident, "qid": take_["qid"] if take_ else None, "url": f"{base_url}/app/agent/{ident}", "image_url": f"{base_url}/harvest/og/agent/{ident}.png",
            "description": f"{headline}. {sub} {TAGLINE}"}


def meta_tags(card: dict[str, Any]) -> str:
    """Open Graph and Twitter tags for a page head (escaped)."""
    from html import escape

    title = f"{card['headline']} · {NAME}"
    pairs = [("property", "og:type", "website"), ("property", "og:site_name", NAME), ("property", "og:title", title),
             ("property", "og:description", card["description"]), ("property", "og:url", card["url"]), ("property", "og:image", card["image_url"]),
             ("property", "og:image:width", "1200"), ("property", "og:image:height", "630"), ("property", "og:image:alt", card["headline"]),
             ("name", "twitter:card", "summary_large_image"), ("name", "twitter:title", title), ("name", "twitter:description", card["description"]),
             ("name", "twitter:image", card["image_url"]), ("name", "description", card["description"])]
    tags = [f'<meta {k}="{escape(a, quote=True)}" content="{escape(v, quote=True)}">' for k, a, v in pairs]
    return "\n".join(tags + [f'<link rel="canonical" href="{escape(card["url"], quote=True)}">'])


def with_meta(page: str, card: dict[str, Any]) -> str:
    """A static page with the card's tags in <head> and its title replaced."""
    from html import escape

    page = re.sub(r"<title>.*?</title>", lambda _m: f"<title>{escape(card['headline'])} · {NAME}</title>", page, count=1, flags=re.S)
    return page.replace("</head>", meta_tags(card) + "\n</head>", 1)


# ---------------------------------------------------------------- humans vs AI season

def _live_positions(book: LiveBook) -> dict[str, float]:
    """Mark-to-market value of every trader's open positions, in one pass."""
    prices = {q: book.price(q) for q, m in book.markets.items() if m["status"] == "open"}
    value: dict[str, float] = defaultdict(float)
    for (trader, qid), hold in book.holdings.items():
        if qid in prices:
            value[trader] += hold["YES"] * prices[qid] + hold["NO"] * (1 - prices[qid])
    return value


def _stated(book: LiveBook, resolved: set[str]) -> dict[tuple[str, str], float]:
    """Probabilities traders stated on resolved live markets: house live forecasts, then the p_yes on explicit bets (latest wins)."""
    out: dict[tuple[str, str], float] = {}
    for qid in resolved:
        for f in book.agent_forecasts.get(qid, []):
            out[(f"agent:{f['persona_id']}", qid)] = float(f["p_yes"])
    for e in ledger.all_entries():
        p = e["payload"]
        if e["kind"] == "bet" and p.get("qid") in resolved and p.get("p_yes") is not None:
            out[(e["trader"], p["qid"])] = float(p["p_yes"])
    return out


def season(book: LiveBook, *, personas_verified: set[str]) -> dict[str, Any]:
    """One table ranking humans against AI agents on the live markets.

    Score: mean Brier over the resolved live markets the trader stated a probability on (lower is better); a trader
    with none scores by mark-to-market P&L (cash plus the value of open positions minus the 1,000 starting credits).
    Everyone with a Brier ranks above everyone without one, then P&L descending.
    """
    resolved = {q for q, m in book.markets.items() if m["status"] == "resolved"}
    stated = _stated(book, resolved) if resolved else {}
    value = _live_positions(book)
    trades: dict[str, list[float]] = defaultdict(list)
    for t in book.trades:
        trades[t["trader"]].append(float(t["spend"]))
    forecasters = {f"agent:{f['persona_id']}" for rows in book.agent_forecasts.values() for f in rows}
    rows = []
    for tid in sorted(set(trades) | forecasters):
        info = book.traders.get(tid)
        if info is None and not tid.startswith("agent:"):
            continue
        kind = "human" if info and info["kind"] == "human" else "agent"
        pairs = [((stated[(tid, q)] - float(book.markets[q]["outcome"])) ** 2) for q in resolved if (tid, q) in stated]
        pid = tid.removeprefix("agent:")
        rows.append({
            "id": tid, "name": book.display_name(tid), "username": book.username_of(tid), "type": kind, "icon": "👤" if kind == "human" else "🤖",
            "category": "users" if info else "verified" if pid in personas_verified else "house",
            "trades": len(trades.get(tid, [])), "staked": round(sum(trades.get(tid, [])), 2),
            "pnl": round(book.balance(tid) + value.get(tid, 0.0) - STARTING_CREDITS, 2),
            "brier": round(sum(pairs) / len(pairs), 4) if pairs else None, "n_resolved": len(pairs),
        })
    rows.sort(key=lambda r: (r["brier"] is None, r["brier"] if r["brier"] is not None else 0.0, -r["pnl"], r["username"]))
    for i, r in enumerate(rows, start=1):
        r["rank"], r["metric"] = i, "brier" if r["brier"] is not None else "pnl"
        r["score"] = r["brier"] if r["brier"] is not None else r["pnl"]
    fao = next((r for r in rows if r["id"].removeprefix("agent:").startswith(FAO_PREFIX)), None)
    humans = [r for r in rows if r["type"] == "human"]
    return {
        "headline": SEASON_HEADLINE,
        "season": {"name": "Season 1", "markets_open": sum(1 for m in book.markets.values() if m["status"] == "open"), "markets_resolved": len(resolved),
                   "start_credits": STARTING_CREDITS},
        "scoring": "Brier on resolved live markets where a probability was stated (lower is better); until then, mark-to-market P&L in play credits.",
        "fao_bot": fao, "best_human": humans[0] if humans else None,
        "humans_ahead_of_fao": sum(1 for h in humans if fao and h["rank"] < fao["rank"]) if fao else None,
        "totals": {"humans": len(humans), "agents": len(rows) - len(humans), "ranked": len(rows)},
        "rows": rows,
    }


# ---------------------------------------------------------------- open data export

FORECAST_COLUMNS = ("record", "seq", "at", "split", "cutoff", "qid", "trader", "username", "account_type", "p_yes", "side", "spend", "price_after",
                    "outcome", "source", "rationale")
RESOLUTION_COLUMNS = ("source", "qid", "spec_id", "title", "cutoff", "split", "resolve_month", "outcome", "resolved_value", "target", "base_value",
                      "final_market_p")


def _outcome_index(result: dict[str, Any] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in (result or {}).get("cutoffs", []):
        for r in c.get("resolutions", []):
            if r.get("outcome") is not None:
                out[r["qid"]] = int(r["outcome"])
    return out


def forecast_rows(book: LiveBook, result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every probability forecast and bet: the frozen backtest's forecasts, the live agent forecasts and every ledger bet."""
    outcomes = _outcome_index(result)
    live_outcomes = {q: m["outcome"] for q, m in book.markets.items() if m["status"] == "resolved"}
    rows: list[dict[str, Any]] = []
    for c in (result or {}).get("cutoffs", []):
        for f in c.get("forecasts", []):
            rows.append({"record": "backtest_forecast", "split": c.get("split"), "cutoff": c.get("cutoff"), "qid": f["qid"], "trader": f"agent:{f['persona_id']}",
                         "username": accounts.system_username(f["persona_id"]), "account_type": "agent", "p_yes": f["p_yes"], "outcome": outcomes.get(f["qid"]),
                         "source": f.get("source"), "rationale": f.get("rationale")})
    for qid, fs in book.agent_forecasts.items():
        for f in fs:
            rows.append({"record": "live_forecast", "split": "live", "qid": qid, "trader": f"agent:{f['persona_id']}", "username": accounts.system_username(f["persona_id"]),
                         "account_type": "agent", "p_yes": f["p_yes"], "outcome": live_outcomes.get(qid), "at": f.get("at"), "source": f.get("source"), "rationale": f.get("rationale")})
    for e in ledger.all_entries():
        p = e["payload"]
        if e["kind"] != "bet":
            continue
        kind = (book.traders.get(e["trader"]) or {}).get("kind")
        rows.append({"record": "bet", "seq": e["seq"], "at": e["at"], "split": "live", "qid": p["qid"], "trader": e["trader"], "username": book.username_of(e["trader"]),
                     "account_type": "human" if kind == "human" else "agent", "p_yes": p.get("p_yes"), "side": p["side"], "spend": p["spend"],
                     "price_after": p.get("price_after"), "outcome": live_outcomes.get(p["qid"], outcomes.get(p["qid"])), "rationale": p.get("rationale")})
    return [{k: r.get(k) for k in FORECAST_COLUMNS} for r in rows]


def resolution_rows(book: LiveBook, result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every resolved question: the backtest's and any live market that has resolved."""
    rows: list[dict[str, Any]] = []
    for c in (result or {}).get("cutoffs", []):
        questions = {q["qid"]: q for q in c.get("questions", [])}
        for r in c.get("resolutions", []):
            if r.get("outcome") is None:
                continue
            q = questions.get(r["qid"], {})
            rows.append({"source": "backtest", "qid": r["qid"], "spec_id": q.get("spec_id"), "title": q.get("title"), "cutoff": c.get("cutoff"), "split": c.get("split"),
                         "resolve_month": r.get("resolve_month"), "outcome": r["outcome"], "resolved_value": r.get("resolved_value"), "target": q.get("target"),
                         "base_value": q.get("base_value"), "final_market_p": (c.get("final_prices") or {}).get(r["qid"])})
    for qid, m in book.markets.items():
        if m["status"] == "resolved":
            q = book.meta.get(qid) or {}
            rows.append({"source": "live", "qid": qid, "spec_id": q.get("spec_id"), "title": q.get("title"), "cutoff": q.get("cutoff"), "split": "live",
                         "resolve_month": q.get("resolve_month"), "outcome": m["outcome"], "target": q.get("target"), "base_value": q.get("base_value"),
                         "final_market_p": book.price(qid)})
    return [{k: r.get(k) for k in RESOLUTION_COLUMNS} for r in rows]


def to_csv(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()
