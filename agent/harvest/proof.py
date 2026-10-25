"""Proof that the scoring is real: tournament replay timeline, calibration and real-world payoff.

Everything here is a pure function of the frozen sealed backtest (the `ReplayResult` that
`agent.routes.harvest.replay_result()` loads, so a re-frozen backtest shows up automatically).
Only dev and holdout quarters count: their questions resolved against real prices. Intervals use
the same seeded percentile bootstrap as `scoring` (90%), so they are reproducible.
"""

import random
from typing import Any

from . import scoring

START = 1000.0
"""Every agent starts each quarter's fresh book with this many play credits (market.STARTING_CREDITS)."""
SCORED = ("dev", "holdout")
ECE_RESAMPLES = 500
COMMODITY = {"wheat": "wheat", "maize": "maize", "olive": "olive oil", "coffee": "coffee", "cocoa": "cocoa",
             "sugar": "sugar", "sunoil": "sunflower oil", "rice": "rice"}
PAYERS = [
    {"who": "Food importers and millers", "why": "A wheat or sunflower-oil spike flagged a quarter early lets them buy forward or hedge with futures before the price moves, instead of after."},
    {"who": "Crop and parametric insurers", "why": "Calibrated odds of a price or harvest shock are an input to pricing cover and to deciding when to reinsure."},
    {"who": "Farm co-operatives", "why": "Knowing the odds that olive oil or maize prices fall helps decide when to sell forward and how much to store."},
    {"who": "EU DG AGRI market observatories", "why": "An early, auditable signal of stress in a commodity supports crisis-reserve and market-measure decisions."},
    {"who": "WFP and humanitarian buyers", "why": "Buying staples before a spike stretches the same budget over more people; pre-positioning needs months of warning."},
]


def _pid(trader: str) -> str:
    return trader.split(":", 1)[1] if trader.startswith("agent:") else trader


def _outcomes(c: dict[str, Any]) -> dict[str, int]:
    return {r["qid"]: int(r["outcome"]) for r in c.get("resolutions", []) if r.get("outcome") is not None}


def _scored(result: dict[str, Any], split: str = "all") -> list[dict[str, Any]]:
    want = SCORED if split == "all" else (split,)
    return [c for c in result.get("cutoffs", []) if c.get("split") in want]


def _personas(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": p["id"], "name": p.get("name", p["id"]), "color": p.get("color")} for p in result.get("personas", [])]


def _interval(values: list[float]) -> tuple[float | None, float | None]:
    return scoring._bootstrap(tuple(values)) if values else (None, None)


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------- s. tournament replay timeline

def timeline(result: dict[str, Any] | None) -> dict[str, Any]:
    """Per scored quarter, per agent: bankroll (start plus cumulative PnL) and cumulative Brier."""
    if not result:
        return {"has_replay": False, "agents": [], "steps": [], "final": []}
    agents = _personas(result)
    ids = [a["id"] for a in agents]
    pnl = dict.fromkeys(ids, 0.0)
    sq = dict.fromkeys(ids, 0.0)
    n = dict.fromkeys(ids, 0)
    m_sq, m_n = 0.0, 0
    steps = []
    for c in _scored(result):
        out = _outcomes(c)
        step_pnl = dict.fromkeys(ids, 0.0)
        for t in c.get("trades", []):
            if _pid(t["trader"]) in step_pnl:
                step_pnl[_pid(t["trader"])] -= float(t["spend"])
        for p in c.get("payouts", []):
            if _pid(p["trader"]) in step_pnl:
                step_pnl[_pid(p["trader"])] += float(p["amount"])
        step_sq = dict.fromkeys(ids, 0.0)
        step_n = dict.fromkeys(ids, 0)
        for f in c.get("forecasts", []):
            if f["qid"] in out and f["persona_id"] in step_sq:
                step_sq[f["persona_id"]] += scoring.brier(float(f["p_yes"]), out[f["qid"]])
                step_n[f["persona_id"]] += 1
        prices = c.get("final_prices", {})
        mk = [scoring.brier(float(prices[q]), o) for q, o in out.items() if q in prices]
        m_sq, m_n = m_sq + sum(mk), m_n + len(mk)
        rows = {}
        for pid in ids:
            pnl[pid] += step_pnl[pid]
            sq[pid] += step_sq[pid]
            n[pid] += step_n[pid]
            rows[pid] = {"bankroll": round(START + pnl[pid], 2), "pnl": round(step_pnl[pid], 2),
                         "brier": _r(step_sq[pid] / step_n[pid]) if step_n[pid] else None,
                         "brier_cum": _r(sq[pid] / n[pid]) if n[pid] else None}
        steps.append({"cutoff": c["cutoff"], "split": c["split"], "n_questions": len(out),
                      "yes_rate": _r(sum(out.values()) / len(out)) if out else None,
                      "market_brier_cum": _r(m_sq / m_n) if m_n else None, "agents": rows})
    last = steps[-1]["agents"] if steps else {}
    final = sorted(({"id": a["id"], "name": a["name"], "color": a["color"], **last.get(a["id"], {})} for a in agents),
                   key=lambda r: -(r.get("bankroll") or 0.0))
    for i, r in enumerate(final, start=1):
        r["rank_bankroll"] = i
    by_brier = sorted(final, key=lambda r: (r.get("brier_cum") is None, r.get("brier_cum") or 0.0))
    for i, r in enumerate(by_brier, start=1):
        r["rank_brier"] = i
    return {"has_replay": True, "model": result.get("model"), "generated_at": result.get("generated_at"), "start_bankroll": START,
            "note": ("Each quarter the agents trade a fresh LMSR book with 1,000 play credits; bankroll is 1,000 plus their cumulative "
                     "profit across quarters. Brier is cumulative over every resolved forecast so far (lower is better, 0.25 is a coin flip)."),
            "agents": agents, "steps": steps, "final": final, "baselines": {"coin": scoring.COIN_BRIER}}


# ---------------------------------------------------------------- n. calibration

def _forecast_pairs(result: dict[str, Any], split: str) -> dict[str, list[tuple[float, int]]]:
    """Resolved (p_yes, outcome) per agent, plus "market" (final LMSR price) and "consensus" (mean of the agents)."""
    pairs: dict[str, list[tuple[float, int]]] = {}
    for c in _scored(result, split):
        out = _outcomes(c)
        by_q: dict[str, list[float]] = {}
        for f in c.get("forecasts", []):
            if f["qid"] in out:
                pairs.setdefault(f["persona_id"], []).append((float(f["p_yes"]), out[f["qid"]]))
                by_q.setdefault(f["qid"], []).append(float(f["p_yes"]))
        prices = c.get("final_prices", {})
        for q, o in out.items():
            if q in prices:
                pairs.setdefault("market", []).append((float(prices[q]), o))
            if by_q.get(q):
                pairs.setdefault("consensus", []).append((sum(by_q[q]) / len(by_q[q]), o))
    return pairs


def _ece(pairs: list[tuple[float, int]], bins: int) -> float:
    groups: dict[int, list[tuple[float, int]]] = {}
    for p, o in pairs:
        groups.setdefault(min(int(p * bins), bins - 1), []).append((p, o))
    return sum(len(g) * abs(sum(p for p, _ in g) / len(g) - sum(o for _, o in g) / len(g)) for g in groups.values()) / len(pairs)


def _ece_interval(pairs: list[tuple[float, int]], bins: int) -> tuple[float, float]:
    rng = random.Random(scoring.CI_SEED)
    vals = sorted(_ece(rng.choices(pairs, k=len(pairs)), bins) for _ in range(ECE_RESAMPLES))
    tail = (1 - scoring.CI_LEVEL) / 2
    return vals[int(tail * ECE_RESAMPLES)], vals[min(ECE_RESAMPLES - 1, int((1 - tail) * ECE_RESAMPLES))]


def calibration_agents(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The agents a calibration can be asked for: the world models plus the market and the agent consensus."""
    if not result:
        return []
    return _personas(result) + [{"id": "market", "name": "The market (final price)", "color": "#1a1a1a"},
                                {"id": "consensus", "name": "Agent consensus (mean)", "color": "#868e96"}]


def calibration(result: dict[str, Any] | None, agent: str, *, split: str = "all", bins: int = 10) -> dict[str, Any] | None:
    """Reliability bins (count, mean forecast, observed frequency and its bootstrap interval) and ECE; None if `agent` is unknown."""
    who = {a["id"]: a for a in calibration_agents(result)}.get(agent)
    if who is None or result is None:
        return None
    pairs = _forecast_pairs(result, split).get(agent, [])
    rows = []
    for b in range(bins):
        g = [(p, o) for p, o in pairs if min(int(p * bins), bins - 1) == b]
        lo, hi = _interval([float(o) for _, o in g])
        rows.append({"lo": round(b / bins, 4), "hi": round((b + 1) / bins, 4), "n": len(g),
                     "mean_p": _r(sum(p for p, _ in g) / len(g)) if g else None,
                     "freq": _r(sum(o for _, o in g) / len(g)) if g else None, "freq_lo": _r(lo), "freq_hi": _r(hi)})
    ece = _ece(pairs, bins) if pairs else None
    elo, ehi = _ece_interval(pairs, bins) if pairs else (None, None)
    return {"agent": who, "split": split, "n": len(pairs), "bins": rows, "ece": _r(ece), "ece_lo": _r(elo), "ece_hi": _r(ehi),
            "brier": _r(scoring.mean_brier(pairs)), "interval_level": scoring.CI_LEVEL,
            "note": ("A well calibrated forecaster's 70% calls come true about 70% of the time, so its bins sit on the diagonal. "
                     "ECE is the count-weighted gap between forecast and observed frequency (0 is perfect). "
                     f"Intervals are {round(scoring.CI_LEVEL * 100)}% bootstrap intervals; small bins are wide.")}


def calibration_summary(result: dict[str, Any] | None, *, split: str = "all", bins: int = 10) -> dict[str, Any]:
    rows = []
    all_pairs = _forecast_pairs(result, split) if result else {}
    for a in calibration_agents(result):
        pairs = all_pairs.get(a["id"], [])
        lo, hi = _ece_interval(pairs, bins) if pairs else (None, None)
        rows.append({**a, "n": len(pairs), "ece": _r(_ece(pairs, bins)) if pairs else None, "ece_lo": _r(lo), "ece_hi": _r(hi),
                     "brier": _r(scoring.mean_brier(pairs))})
    rows.sort(key=lambda r: (r["ece"] is None, r["ece"] or 0.0))
    return {"split": split, "bins": bins, "agents": rows, "interval_level": scoring.CI_LEVEL}


# ---------------------------------------------------------------- o. real-world payoff

def _rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per resolved scored question: market price, agent consensus, the honest historical base rate and the outcome.

    The base rate for a question is the share of earlier questions of the same kind (spec) that
    resolved YES no later than this question's cutoff, so it uses only what was known at the time.
    """
    rows = []
    history: list[tuple[str, str, int]] = []  # (spec_id, resolve_month, outcome)
    for c in _scored(result):
        out = _outcomes(c)
        res = {r["qid"]: r for r in c.get("resolutions", [])}
        by_q: dict[str, list[float]] = {}
        for f in c.get("forecasts", []):
            by_q.setdefault(f["qid"], []).append(float(f["p_yes"]))
        prices = c.get("final_prices", {})
        for q in c.get("questions", []):
            qid = q["qid"]
            if qid not in out or qid not in prices or not by_q.get(qid):
                continue
            seen = [o for s, m, o in history if s == q["spec_id"] and m <= c["cutoff"]]
            rows.append({"qid": qid, "spec_id": q["spec_id"], "commodity": COMMODITY.get(q["spec_id"].split("-")[0], q.get("crop")),
                         "cutoff": c["cutoff"], "split": c["split"], "resolve_month": q.get("resolve_month"),
                         "horizon": int(q.get("rule", {}).get("horizon_months") or 0), "adverse_if": q.get("adverse_if", "yes"),
                         "title": q.get("title"), "outcome": out[qid], "market": float(prices[qid]),
                         "consensus": sum(by_q[qid]) / len(by_q[qid]), "base_rate": sum(seen) / len(seen) if seen else 0.5})
        history += [(q["spec_id"], str(res[q["qid"]].get("resolve_month") or q.get("resolve_month")), out[q["qid"]])
                    for q in c.get("questions", []) if q["qid"] in out]
    return rows


def _score_block(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows)}
    base = [scoring.brier(r["base_rate"], r["outcome"]) for r in rows]
    for key in ("market", "consensus", "base_rate"):
        b = [scoring.brier(r[key], r["outcome"]) for r in rows]
        lo, hi = _interval(b)
        out[key] = {"brier": _r(scoring.mean_brier([(r[key], r["outcome"]) for r in rows])), "lo": _r(lo), "hi": _r(hi)}
        if key != "base_rate" and rows:
            d = [x - y for x, y in zip(b, base, strict=True)]
            dlo, dhi = _interval(d)
            out[key] |= {"diff_vs_base": _r(sum(d) / len(d)), "diff_lo": _r(dlo), "diff_hi": _r(dhi), "beats_base_ci": dhi is not None and dhi < 0}
    out["coin"] = {"brier": scoring.COIN_BRIER}
    return out


def _warning(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """How often `key` gave an adverse move 50%+ odds when the question opened, months before it resolved."""
    def adverse_p(r: dict[str, Any]) -> float:
        return r[key] if r["adverse_if"] == "yes" else 1 - r[key]

    def happened(r: dict[str, Any]) -> bool:
        return r["outcome"] == (1 if r["adverse_if"] == "yes" else 0)

    events = [r for r in rows if happened(r)]
    quiet = [r for r in rows if not happened(r)]
    hits = [r for r in events if adverse_p(r) >= 0.5]
    false = [r for r in quiet if adverse_p(r) >= 0.5]
    return {"events": len(events), "flagged": len(hits), "hit_rate": _r(len(hits) / len(events)) if events else None,
            "false_alarms": len(false), "false_alarm_rate": _r(len(false) / len(quiet)) if quiet else None,
            "lead_months_avg": _r(sum(r["horizon"] for r in hits) / len(hits), 1) if hits else None}


def impact(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {"has_replay": False}
    rows = _rows(result)
    hold = [r for r in rows if r["split"] == "holdout"]
    by_com: dict[str, list[dict[str, Any]]] = {}
    for r in hold:
        by_com.setdefault(r["commodity"], []).append(r)
    commodities = sorted(({"commodity": k, **_score_block(v)} for k, v in by_com.items()), key=lambda x: x["commodity"])
    warn = {k: _warning(hold, k) for k in ("market", "consensus", "base_rate")}
    warn_all = {k: _warning(rows, k) for k in ("market", "consensus", "base_rate")}
    headline = _score_block(hold)
    m, base = headline["market"], headline["base_rate"]
    summary = None
    if m["brier"] is not None and base["brier"] is not None:
        verdict = ("better than the historical base rate, and the 90% interval on the gap stays below zero"
                   if m["beats_base_ci"] else "better than the historical base rate, but the 90% interval on the gap crosses zero, so it could be noise"
                   if m["diff_vs_base"] < 0 else "no better than the historical base rate")
        summary = (f"On {headline['n']} sealed questions the agents' market scored {m['brier']:.3f} Brier against {base['brier']:.3f} "
                   f"for the historical base rate and 0.250 for a coin flip: {verdict}. It flagged {warn['market']['flagged']} of {warn['market']['events']} adverse "
                   f"price moves at 50%+ odds when each question opened, {warn['market']['lead_months_avg'] or 0:g} months ahead on average; "
                   f"the base rate flagged {warn['base_rate']['flagged']}.")
    return {
        "has_replay": True, "model": result.get("model"), "generated_at": result.get("generated_at"),
        "interval_level": scoring.CI_LEVEL, "summary": summary,
        "forecasters": {"market": "The Crophecy market: the final LMSR price after the AI agents traded (the crowd of agents)",
                        "consensus": "Agent consensus: the plain mean of every world model's forecast",
                        "base_rate": "Historical base rate: how often this kind of question resolved YES before the cutoff (a desk with a spreadsheet)",
                        "coin": "Coin flip: 50% on everything (Brier 0.25)"},
        "holdout": headline, "all_scored": _score_block(rows), "by_commodity": commodities,
        "early_warning": {"holdout": warn, "all_scored": warn_all,
                          "note": ("An adverse move is the outcome marked bad for European buyers or farmers. Flagged means the forecast "
                                   "gave it 50% or more when the question opened, so the lead is the question's horizon (3 or 6 months).")},
        "payers": PAYERS,
        "caveats": [f"Holdout has {headline['n']} questions over {len({r['cutoff'] for r in hold})} quarters; intervals are wide and per-commodity cells are small.",
                    "No futures-implied probabilities are available, so the comparison is against a historical base rate, not the real commodity market.",
                    "Play credits only: these are backtest scores, not trading returns."],
    }
