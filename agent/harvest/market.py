"""Credits, LMSR order book and settlement (SPEC.md sections 5-6). Owner: market-engine.

`Book` is the pure in-memory book the replay builds per cutoff. `LiveBook` (from `live_book()`)
adds market metadata, registered traders and the SQLite ledger: every live write goes through
`LiveBook.record` / `LiveBook.place_bet`, which append to the hash chain, and a restart replays
the chain's entries so the live state is a pure function of the ledger.
"""

import logging
import math
import os
import random
import threading
from datetime import UTC, datetime
from typing import Any

from agent.sealed.hashing import sha256_json

from . import accounts as acct
from . import ledger, lmsr
from .types import BetIntent, LedgerEntry, MarketState, Payout, Persona, QuestionInstance, Side, Trade

DEFAULT_B = 100.0
"""Replay liquidity (frozen): one 25-credit bet moves a 50% price about 11 points."""
LIVE_B_DEFAULT = 400.0
"""Live liquidity: one 25-credit bet moves a 50% price about 3 points, a 30% price about 4."""
FEE_RATE = 0.02
FUND_SHARE = 1.0
STARTING_CREDITS = 1000.0
FUND_TRADER = "fund:resilience"
MAX_BET_SHARE = 0.2
EPS = 1e-9

logger = logging.getLogger("agent")


class MarketError(ValueError):
    """Insufficient credits, unknown or closed market, bad side or spend."""


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def live_b() -> float:
    """LMSR b for the live book (LIVE_B env, default 400). The replay keeps DEFAULT_B."""
    try:
        b = float(os.environ.get("LIVE_B") or LIVE_B_DEFAULT)
    except ValueError:
        return LIVE_B_DEFAULT
    return b if math.isfinite(b) and b > 0 else LIVE_B_DEFAULT


def decide(persona: Persona, p_yes: float, price: float, bankroll: float) -> BetIntent | None:
    """YES if p_yes - price > edge, NO if price - p_yes > edge; spend = min(risk*bankroll*|gap|, 0.2*bankroll)."""
    gap = p_yes - price
    edge = float(persona.get("edge", 0.03))
    if abs(gap) <= edge or bankroll <= 0:
        return None
    spend = min(float(persona.get("risk", 0.25)) * bankroll * abs(gap), MAX_BET_SHARE * bankroll)
    spend = round(spend, 2)
    if spend < 0.01:
        return None
    return {"side": "YES" if gap > 0 else "NO", "spend": spend}


class Book:
    """In-memory LMSR book with credit accounts. Replay builds one per cutoff."""

    def __init__(self, *, starting_credits: float = STARTING_CREDITS) -> None:
        self.starting_credits = starting_credits
        self.markets: dict[str, MarketState] = {}
        self.accounts: dict[str, float] = {}
        self.holdings: dict[tuple[str, str], dict[Side, float]] = {}
        self.spent: dict[tuple[str, str], float] = {}
        self.trades: list[Trade] = []
        self.fund: float = 0.0

    def open(self, qid: str, prior: float = 0.5, b: float = DEFAULT_B) -> MarketState:
        if qid in self.markets:
            return self.markets[qid]
        p = clamp(float(prior), 0.01, 0.99)
        q_yes, q_no = lmsr.initial_q(p, b)
        state: MarketState = {
            "qid": qid, "b": b, "q_yes": q_yes, "q_no": q_no, "volume": 0.0, "fees": 0.0,
            "status": "open", "outcome": None, "history": [(len(self.trades), lmsr.price_yes(q_yes, q_no, b))],
        }
        self.markets[qid] = state
        return state

    def _market(self, qid: str) -> MarketState:
        m = self.markets.get(qid)
        if m is None:
            raise MarketError(f"unknown market {qid!r}")
        return m

    def price(self, qid: str) -> float:
        m = self._market(qid)
        return lmsr.price_yes(m["q_yes"], m["q_no"], m["b"])

    def balance(self, trader: str) -> float:
        if trader not in self.accounts:
            self.accounts[trader] = self.starting_credits
        return self.accounts[trader]

    def buy(self, trader: str, qid: str, side: Side, spend: float, *, cutoff: str | None = None, at: str | None = None) -> Trade:
        if side not in ("YES", "NO"):
            raise MarketError("side must be YES or NO")
        try:
            spend = float(spend)
        except (TypeError, ValueError):
            raise MarketError("spend must be a number") from None
        if not math.isfinite(spend) or spend <= 0:
            raise MarketError("spend must be positive")
        m = self._market(qid)
        if m["status"] != "open":
            raise MarketError(f"market {qid!r} is closed")
        bal = self.balance(trader)
        if spend > bal + EPS:
            raise MarketError(f"insufficient credits: {bal:.2f} available, {spend:.2f} asked")
        fee = FEE_RATE * spend
        before = self.price(qid)
        shares = lmsr.shares_for_spend(m["q_yes"], m["q_no"], m["b"], side, spend - fee)
        if side == "YES":
            m["q_yes"] += shares
        else:
            m["q_no"] += shares
        after = self.price(qid)
        self.accounts[trader] = bal - spend
        hold = self.holdings.setdefault((trader, qid), {"YES": 0.0, "NO": 0.0})
        hold[side] += shares
        self.spent[(trader, qid)] = self.spent.get((trader, qid), 0.0) + spend
        m["volume"] += spend
        m["fees"] += fee
        self.fund += fee * FUND_SHARE
        trade: Trade = {
            "seq": len(self.trades) + 1, "trader": trader, "qid": qid, "side": side, "spend": spend, "fee": fee,
            "shares": shares, "price_before": before, "price_after": after, "at": at or _now(), "cutoff": cutoff,
        }
        self.trades.append(trade)
        m["history"].append((trade["seq"], after))
        return trade

    def resolve(self, qid: str, outcome: int) -> list[Payout]:
        m = self._market(qid)
        if m["status"] == "resolved":
            raise MarketError(f"market {qid!r} is already resolved")
        if outcome not in (0, 1):
            raise MarketError("outcome must be 0 or 1")
        m["status"], m["outcome"] = "resolved", outcome
        win: Side = "YES" if outcome == 1 else "NO"
        payouts: list[Payout] = []
        for (trader, q), hold in self.holdings.items():
            if q != qid or hold[win] <= EPS:
                continue
            amount = hold[win]
            self.accounts[trader] = self.balance(trader) + amount
            payouts.append({"trader": trader, "qid": qid, "shares": hold[win], "amount": amount})
        return payouts

    def positions(self, trader: str) -> list[dict[str, Any]]:
        out = []
        for (t, qid), hold in self.holdings.items():
            if t != trader:
                continue
            m = self.markets[qid]
            p = self.price(qid)
            if m["status"] == "resolved":
                value = hold["YES"] if m["outcome"] == 1 else hold["NO"]
            else:
                value = hold["YES"] * p + hold["NO"] * (1 - p)
            spent = self.spent.get((t, qid), 0.0)
            out.append({"qid": qid, "yes": hold["YES"], "no": hold["NO"], "spent": spent, "value": value,
                        "pnl": value - spent, "p_yes": p, "status": m["status"], "outcome": m["outcome"]})
        return out

    def equity(self, trader: str) -> float:
        """Cash plus mark-to-market value of open positions (resolved payouts are already cash)."""
        open_value = sum(p["value"] for p in self.positions(trader) if p["status"] == "open")
        return self.balance(trader) + open_value

    def snapshot(self) -> dict[str, Any]:
        return {
            "markets": {qid: {**m, "history": [list(h) for h in m["history"]], "p_yes": self.price(qid)} for qid, m in self.markets.items()},
            "accounts": dict(self.accounts),
            "fund": self.fund,
            "trades": len(self.trades),
            "volume": sum(m["volume"] for m in self.markets.values()),
        }


# ---------------------------------------------------------------- live book

def token_sha(token: str) -> str:
    return sha256_json({"harvest-token": token})


def _soft_instance(spec: dict[str, Any]) -> QuestionInstance:
    return {
        "qid": spec["id"], "spec_id": spec["id"], "kind": "soft", "title": spec["title"], "region": spec.get("region", ""),
        "crop": spec.get("crop", ""), "adverse_if": spec.get("adverse_if", "yes"), "cutoff": None, "series": None,
        "base_month": None, "base_value": None, "resolve_month": spec.get("deadline"), "target": None, "rule": None,
        "prior": float(spec.get("prior", 0.5)),
    }


def load_replay() -> dict[str, Any] | None:
    """The frozen demo replay, or None while the replay module or its file is missing."""
    try:
        from . import replay

        return dict(replay.load_demo())
    except (ImportError, NotImplementedError, FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return None


def load_personas(result: dict[str, Any] | None) -> list[Persona]:
    if result and result.get("personas"):
        return list(result["personas"])
    try:
        from . import personas

        return list(personas.load_personas())
    except (ImportError, NotImplementedError, FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return []


def live_roster(personas: list[Persona], result: dict[str, Any] | None) -> list[Persona]:
    """Agents that trade the live markets. A persona x model replay would put every persona on every model into
    each market, so live trading uses one model only: the one that scored best on the sealed holdout."""
    keys = [str(p["model"]) for p in personas if p.get("model")]
    if not keys:
        return personas
    try:
        from . import scoring

        ranked = scoring.group_table(result or {}, "model")
    except (ImportError, KeyError, TypeError, ValueError):
        ranked = []
    best = str(ranked[0]["key"]) if ranked else keys[0]
    return [p for p in personas if p.get("model") == best]


def load_soft_specs() -> list[dict[str, Any]]:
    try:
        from . import questions

        return [dict(s) for s in questions.soft_specs(questions.load_bank())]
    except (ImportError, NotImplementedError, FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return []


def live_cutoff(result: dict[str, Any] | None) -> dict[str, Any] | None:
    lives = [c for c in (result or {}).get("cutoffs", []) if c.get("split") == "live"]
    return lives[-1] if lives else None


class LiveBook(Book):
    """The live server's book: markets for the replay's live cutoff plus the soft questions,
    traders registered through the ledger, and the ledger replayed on start."""

    def __init__(self, *, starting_credits: float = STARTING_CREDITS, b: float | None = None) -> None:
        super().__init__(starting_credits=starting_credits)
        self.b = live_b() if b is None else b
        self.lock = threading.RLock()
        self.meta: dict[str, QuestionInstance] = {}
        self.agent_forecasts: dict[str, list[dict[str, Any]]] = {}
        self.personas: list[Persona] = []
        self.traders: dict[str, dict[str, Any]] = {}
        self.usernames: dict[str, str] = {}
        """username -> trader id, unique across every account (accounts.py)."""
        self.tokens: dict[str, str] = {}
        self.forecasts: dict[str, dict[str, Any]] = {}
        self.ledger_head: tuple[int, str] = (0, ledger.GENESIS)
        self.version = 0
        self.replay_errors: list[str] = []
        self.live_reforecast_at: dict[str, str] = {}

    def open(self, qid: str, prior: float = 0.5, b: float | None = None) -> MarketState:
        """Live markets open with the deeper live liquidity; the seeded price is the prior either way."""
        return super().open(qid, prior=prior, b=self.b if b is None else b)

    # ---- setup
    def seed(self, result: dict[str, Any] | None, soft_specs: list[dict[str, Any]]) -> None:
        self.personas = live_roster(load_personas(result), result)
        roster = {p["id"] for p in self.personas}
        cut = live_cutoff(result)
        if cut:
            prices = cut.get("final_prices") or {}
            for q in cut.get("questions", []):
                self.meta[q["qid"]] = q
                self.open(q["qid"], prior=prices.get(q["qid"], q.get("prior", 0.5)))
            for f in cut.get("forecasts", []):
                if f["persona_id"] not in roster:
                    continue
                self.agent_forecasts.setdefault(f["qid"], []).append(self._forecast_view(f))
        for spec in soft_specs:
            q = _soft_instance(spec)
            if q["qid"] in self.meta:
                continue
            self.meta[q["qid"]] = q
            self.open(q["qid"], prior=q["prior"])
            if q["qid"] not in self.agent_forecasts:
                # World-model lean on soft questions (SPEC section 4): prior + soft_lean when adverse.
                for p in self.personas:
                    lean = float(p.get("soft_lean", 0.0))
                    p_yes = clamp(q["prior"] + (lean if q["adverse_if"] == "yes" else -lean), 0.02, 0.98)
                    self.agent_forecasts.setdefault(q["qid"], []).append(self._forecast_view(
                        {"persona_id": p["id"], "qid": q["qid"], "p_yes": p_yes, "rationale": "world-model lean on the prior", "source": "fallback"}))

    def _forecast_view(self, f: dict[str, Any]) -> dict[str, Any]:
        persona = next((p for p in self.personas if p["id"] == f["persona_id"]), None)
        return {"persona_id": f["persona_id"], "name": persona["name"] if persona else f["persona_id"],
                "color": persona["color"] if persona else "#888888", "p_yes": f["p_yes"],
                "rationale": f.get("rationale", ""), "source": f.get("source", ""), "at": f.get("at")}

    def set_live_forecasts(self, persona_id: str, rows: list[tuple[str, float, str]], *, source: str = "live") -> int:
        """Replace `persona_id`'s forecast on each open qid with a live re-forecast (p_yes, rationale), stamped now."""
        at = _now()
        n = 0
        with self.lock:
            for qid, p_yes, rationale in rows:
                if qid not in self.markets or self.markets[qid]["status"] != "open":
                    continue
                view = self._forecast_view({"persona_id": persona_id, "qid": qid, "p_yes": round(clamp(float(p_yes), 0.01, 0.99), 4),
                                            "rationale": str(rationale)[:200], "source": source, "at": at})
                fs = [f for f in self.agent_forecasts.get(qid, []) if f["persona_id"] != persona_id]
                self.agent_forecasts[qid] = [*fs, view]
                n += 1
            if n:
                self.live_reforecast_at[persona_id] = at
                self.version += 1
        return n

    def replay_ledger(self) -> None:
        for e in ledger.all_entries():
            try:
                self._apply(e, replaying=True)
            except MarketError as err:
                self.replay_errors.append(f"entry {e['seq']}: {err}")
            self.ledger_head = (e["seq"], e["hash"])

    # ---- applying chain entries
    def _apply(self, e: LedgerEntry, *, replaying: bool = False) -> Trade | None:
        p, trader = e["payload"], e["trader"]
        self.version += 1
        if e["kind"] in ("join", "agent"):
            info = self.traders.setdefault(trader, {"id": trader, "kind": "human" if e["kind"] == "join" else "external", "joined_at": e["at"]})
            for k in ("nickname", "name", "owner", "world_model", "world_model_sha256"):
                if k in p:
                    info[k] = p[k]
            if "username" not in info:
                info["username"] = acct.claim(self.usernames, trader, p.get("username"), reserved=self.username_reserved)
            if p.get("token_sha256"):
                self.tokens[p["token_sha256"]] = trader
            self.balance(trader)
        elif e["kind"] == "forecast":
            self.forecasts[trader] = {**p, "at": e["at"]}
        elif e["kind"] == "bet" and replaying:
            trade = self.buy(trader, p["qid"], p["side"], p["spend"], cutoff=p.get("cutoff"), at=e["at"])
            if p.get("rationale"):
                trade["rationale"] = p["rationale"]  # type: ignore[typeddict-unknown-key]
            return trade
        return None

    def record(self, *, kind: str, trader: str, payload: dict[str, Any]) -> LedgerEntry:
        with self.lock:
            entry = ledger.append(kind=kind, trader=trader, payload=payload)
            self._apply(entry)
            self.ledger_head = (entry["seq"], entry["hash"])
            return entry

    def place_bet(self, trader: str, qid: str, side: Side, spend: float, *, p_yes: float | None = None, rationale: str | None = None) -> tuple[Trade, LedgerEntry]:
        """Buys in memory first (validates), then appends the bet to the chain, under one lock so
        the chain order is the book order and a restart replays to the same state."""
        with self.lock:
            trade = self.buy(trader, qid, side, spend)
            payload: dict[str, Any] = {"qid": qid, "side": side, "spend": trade["spend"], "shares": trade["shares"], "price_after": trade["price_after"]}
            if p_yes is not None:
                payload["p_yes"] = p_yes
            if rationale:
                payload["rationale"] = rationale
            try:
                entry = ledger.append(kind="bet", trader=trader, payload=payload)
            except BaseException:
                self._undo_last(trade)
                raise
            trade["at"] = entry["at"]
            if rationale:
                trade["rationale"] = rationale  # type: ignore[typeddict-unknown-key]
            self.ledger_head = (entry["seq"], entry["hash"])
            self.version += 1
            return trade, entry

    def _undo_last(self, trade: Trade) -> None:
        m = self.markets[trade["qid"]]
        if trade["side"] == "YES":
            m["q_yes"] -= trade["shares"]
        else:
            m["q_no"] -= trade["shares"]
        self.accounts[trade["trader"]] += trade["spend"]
        self.holdings[(trade["trader"], trade["qid"])][trade["side"]] -= trade["shares"]
        self.spent[(trade["trader"], trade["qid"])] -= trade["spend"]
        m["volume"] -= trade["spend"]
        m["fees"] -= trade["fee"]
        self.fund -= trade["fee"] * FUND_SHARE
        m["history"].pop()
        self.trades.pop()

    # ---- house agents trade live markets on their replay forecasts
    def house_step(self, rng: random.Random | None = None) -> Trade | None:
        """One house trade toward a persona's current belief. Also starts a due live re-forecast (background thread)."""
        try:
            from . import live_forecast

            live_forecast.maybe_reforecast(self)
        except Exception:  # noqa: BLE001 - re-forecasting is best effort; trading goes on with the old beliefs
            logger.exception("harvest live re-forecast could not start")
        rng = rng or random.Random()
        options = [(qid, f) for qid, fs in self.agent_forecasts.items() if qid in self.markets and self.markets[qid]["status"] == "open" for f in fs]
        rng.shuffle(options)
        by_id = {p["id"]: p for p in self.personas}
        for qid, f in options[:24]:
            persona = by_id.get(f["persona_id"])
            if persona is None:
                continue
            trader = f"agent:{persona['id']}"
            with self.lock:
                intent = decide(persona, f["p_yes"], self.price(qid), self.balance(trader))
                if intent is None:
                    continue
                trade, _ = self.place_bet(trader, qid, intent["side"], intent["spend"], p_yes=f["p_yes"], rationale=f.get("rationale"))
                return trade
        return None

    # ---- accounts
    def username_reserved(self, name: str) -> bool:
        """True for names users may not take: reserved words and the house, verified and baseline accounts."""
        from . import baseline_models

        return name in acct.RESERVED or name in baseline_models.USERNAMES or any(acct.system_username(p["id"]) == name for p in self.personas)

    def username_taken(self, name: str) -> bool:
        return name in self.usernames or self.username_reserved(name)

    # ---- views
    def trader_for(self, token: str | None) -> str | None:
        return self.tokens.get(token_sha(token)) if token else None

    def users(self) -> int:
        return sum(1 for t in self.traders.values() if t["kind"] == "human")

    def market_view(self, qid: str, *, full: bool = False) -> dict[str, Any]:
        m, q = self.markets[qid], self.meta.get(qid) or {}
        view = {
            "qid": qid, "title": q.get("title", qid), "kind": q.get("kind", "hard"), "region": q.get("region", ""),
            "crop": q.get("crop", ""), "adverse_if": q.get("adverse_if", "yes"), "series": q.get("series"),
            "target": q.get("target"), "base_value": q.get("base_value"), "base_month": q.get("base_month"),
            "resolve_month": q.get("resolve_month"), "cutoff": q.get("cutoff"), "p_yes": self.price(qid), "b": m["b"],
            "volume": m["volume"], "fees": m["fees"], "status": m["status"], "outcome": m["outcome"],
            "history": [list(h) for h in (m["history"] if full else m["history"][-120:])],
            "agent_forecasts": self.agent_forecasts.get(qid, []),
        }
        if full:
            view["trades"] = [t for t in self.trades if t["qid"] == qid][-200:]
        return view


_LIVE: dict[str, LiveBook] = {}
_LIVE_LOCK = threading.Lock()


def live_book() -> LiveBook:
    """Process-wide live book: seeded from the replay's live cutoff + soft questions, then the SQLite ledger replayed."""
    key = str(ledger.db_path())
    with _LIVE_LOCK:
        book = _LIVE.get(key)
        if book is None:
            book = LiveBook()
            book.seed(load_replay(), load_soft_specs())
            book.replay_ledger()
            _LIVE.clear()
            _LIVE[key] = book
        return book


def reset_live_book() -> None:
    with _LIVE_LOCK:
        _LIVE.clear()


def house_tick_s() -> float:
    try:
        return float(os.environ.get("HARVEST_HOUSE_TICK_S") or 5)
    except ValueError:
        return 5.0

