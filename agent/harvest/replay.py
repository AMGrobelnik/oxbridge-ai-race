"""Time-machine replay over quarterly cutoffs with a sealed holdout (SPEC.md section 8, addendum R2). Owner: agents-replay."""

import asyncio
import copy
import hashlib
import json
import os
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.sealed import chain
from agent.sealed.hashing import sha256_json

from . import forecaster, market, prices, questions
from .types import Commitment, CutoffResult, Forecast, Mask, Persona, PriceFixture, QuestionInstance, QuestionSpec, ReplayResult, Split

DEMO = Path(__file__).resolve().parent / "data" / "demo_replay.json"
FIRST_CUTOFF = "2022-03"
HOLDOUT_FROM = "2025-09"
MASK_SEED = int(os.environ.get("HARVEST_MASK_SEED") or 2026)
ROUNDS = 2
"""Round-robin passes over the personas per cutoff."""
SKIP_ANCHOR = "pending on-chain: anchoring not requested (run with --anchor)"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _quarter_floor(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    q = (m // 3) * 3
    return f"{y:04d}-{q:02d}" if q else f"{y - 1:04d}-12"


def cutoffs(fixture: PriceFixture) -> list[str]:
    last = _quarter_floor(prices.last_month(fixture))
    out, c = [], FIRST_CUTOFF
    while c <= last:
        out.append(c)
        c = prices.add_months(c, 3)
    return out


def _hard_specs(specs: list[QuestionSpec]) -> list[QuestionSpec]:
    return [s for s in specs if s["kind"] == "hard" and "rule" in s and "series" in s]


def split(cutoff: str, fixture: PriceFixture, specs: list[QuestionSpec]) -> Split:
    """By months only (never by outcomes), so deciding the split reveals nothing."""
    last = prices.last_month(fixture)
    resolvable = all(
        prices.add_months(cutoff, s["rule"]["horizon_months"]) <= last
        and prices.value_at(fixture, s["series"], prices.add_months(cutoff, s["rule"]["horizon_months"])) is not None
        for s in _hard_specs(specs)
        if prices.value_at(fixture, s["series"], cutoff) is not None
    )
    if not resolvable:
        return "live"
    return "holdout" if cutoff >= HOLDOUT_FROM else "dev"


def _instances(cutoff: str, fixture: PriceFixture, specs: list[QuestionSpec]) -> list[QuestionInstance]:
    return [questions.instantiate(s, cutoff, fixture) for s in _hard_specs(specs) if prices.value_at(fixture, s["series"], cutoff) is not None]


def _windows(cutoff: str, fixture: PriceFixture, qs: list[QuestionInstance]) -> dict[str, str]:
    return {sid: forecaster.window_sha256(fixture, sid, cutoff) for sid in sorted({str(q["series"]) for q in qs})}


def _order(personas: list[Persona], cutoff: str) -> list[Persona]:
    seed = int(hashlib.sha256(cutoff.encode()).hexdigest()[:8], 16)
    order = list(personas)
    random.Random(seed).shuffle(order)
    return order


def holdout_payload(result: ReplayResult | dict[str, Any]) -> dict[str, Any]:
    """What the holdout commitment hashes: personas sha, holdout forecasts, window hashes and the mask sha."""
    hold = [c for c in result["cutoffs"] if c["split"] == "holdout"]
    return {
        "personas_sha": sha256_json(result["personas"]),
        "forecasts": [f for c in hold for f in c["forecasts"]],
        "windows": [{"cutoff": c["cutoff"], "series": s, "sha256": h} for c in hold for s, h in sorted(c["windows"].items())],
        "mask_sha": sha256_json(result["mask"]),
    }


async def _commit(kind: str, sha: str, payload_ref: str, anchor: bool) -> Commitment:
    memo = f"harvest:v1:{kind}:{sha}"
    receipt = await chain.anchor(memo) if anchor else chain.pending_receipt(memo, SKIP_ANCHOR)
    return {"kind": kind, "sha256": sha, "memo": memo, "payload_ref": payload_ref, "receipt": receipt, "committed_at": _now()}  # type: ignore[typeddict-item]


def _trade(book: market.Book, personas: list[Persona], forecasts: list[Forecast], qs: list[QuestionInstance], cutoff: str) -> None:
    p_of = {(f["persona_id"], f["qid"]): f["p_yes"] for f in forecasts}
    order = _order(personas, cutoff)
    for _ in range(ROUNDS):
        for persona in order:
            trader = f"agent:{persona['id']}"
            for q in qs:
                p = p_of.get((persona["id"], q["qid"]))
                if p is None:
                    continue
                intent = market.decide(persona, p, book.price(q["qid"]), book.balance(trader))
                if intent is None:
                    continue
                try:
                    book.buy(trader, q["qid"], intent["side"], intent["spend"], cutoff=cutoff)
                except market.MarketError:
                    continue


def _settle(book: market.Book, cr: CutoffResult, fixture: PriceFixture) -> None:
    for q in cr["questions"]:
        res = questions.resolve(q, fixture)
        cr["resolutions"].append(res)
        if res["outcome"] is not None:
            cr["payouts"].extend(book.resolve(q["qid"], int(res["outcome"])))


def _local_leaderboard(result: dict[str, Any]) -> dict[str, Any]:
    """Minimal Brier fallback for when scoring.leaderboard is not implemented yet."""
    rows = []
    for p in result["personas"]:
        per: dict[str, list[float]] = {"dev": [], "holdout": []}
        for c in result["cutoffs"]:
            if c["split"] not in per:
                continue
            out = {r["qid"]: r["outcome"] for r in c["resolutions"] if r["outcome"] is not None}
            per[c["split"]] += [(f["p_yes"] - out[f["qid"]]) ** 2 for f in c["forecasts"] if f["persona_id"] == p["id"] and f["qid"] in out]
        mean = {k: (sum(v) / len(v) if v else None) for k, v in per.items()}
        rows.append({"persona_id": p["id"], "name": p["name"], "origin": "house", "brier_dev": mean["dev"], "brier_holdout": mean["holdout"], "pnl_dev": 0.0, "pnl_holdout": 0.0, "n": len(per["dev"]) + len(per["holdout"]), "rank": 0})
    rows.sort(key=lambda r: (r["brier_holdout"] is None, r["brier_holdout"] or 0.0))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return {"world_models": rows, "baselines": {"coin": 0.25, "market": None, "climatology": None}, "humans": []}


def score(result: dict[str, Any]) -> dict[str, Any]:
    try:
        from . import scoring

        return dict(scoring.leaderboard(result))
    except NotImplementedError:
        return _local_leaderboard(result)


async def run(*, fixture: PriceFixture, specs: list[QuestionSpec], personas: list[Persona], offline: bool = False, anchor: bool = False) -> ReplayResult:
    offline = offline or forecaster.offline_env()
    mask: Mask = forecaster.make_mask(list(fixture["series"]), MASK_SEED)
    personas = [dict(p) for p in personas]  # type: ignore[misc]
    # 1. Proof of conviction: the world models are committed before any cutoff.
    commitments: list[Commitment] = [await _commit("models", sha256_json(personas), "personas", anchor)]

    # 2. Questions, masked windows and every forecast (agents see only data up to each cutoff).
    cuts = cutoffs(fixture)
    plan = []
    for c in cuts:
        qs = _instances(c, fixture, specs)
        plan.append((c, split(c, fixture, specs), qs, _windows(c, fixture, qs)))
    jobs = [forecaster.forecast(p, qs, fixture, c, mask, offline=offline) for c, _, qs, _ in plan for p in personas]
    flat = await asyncio.gather(*jobs)
    per_cut = [[f for fs in flat[i * len(personas):(i + 1) * len(personas)] for f in fs] for i in range(len(plan))]

    results: list[CutoffResult] = []
    held: list[tuple[market.Book, CutoffResult]] = []
    for (c, sp, qs, windows), fcs in zip(plan, per_cut, strict=True):
        book = market.Book()
        for q in qs:
            book.open(q["qid"], prior=0.5)
        _trade(book, personas, fcs, qs, c)
        cr: CutoffResult = {
            "cutoff": c, "split": sp, "questions": qs, "windows": windows, "forecasts": fcs,
            "trades": list(book.trades), "final_prices": {q["qid"]: round(book.price(q["qid"]), 6) for q in qs},
            "resolutions": [], "payouts": [],
        }
        results.append(cr)
        if sp == "dev":
            _settle(book, cr, fixture)  # 3. dev cutoffs resolve immediately
        elif sp == "holdout":
            held.append((book, cr))
        # 5. live cutoffs stay open; their final prices seed market.live_book()

    result: ReplayResult = {
        "v": 1, "generated_at": _now(),
        "model": "offline-fallback" if offline else (os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"),
        "mask": mask, "personas": personas, "cutoffs": results, "commitments": commitments,
        "scores": {"world_models": [], "baselines": {}, "humans": []},
    }
    # 4. Seal the holdout forecasts and window hashes, and only then resolve the holdout.
    if held:
        commitments.append(await _commit("holdout", sha256_json(holdout_payload(result)), "cutoffs[split=holdout]", anchor))
        for book, cr in held:
            _settle(book, cr, fixture)
    result["scores"] = score(result)  # type: ignore[typeddict-item]
    return result


def freeze(result: ReplayResult, path: Path = DEMO) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1) + "\n")
    return path


def load_demo(path: Path = DEMO) -> ReplayResult:
    result: ReplayResult = json.loads(path.read_text())
    return result


async def verify_commitments(result: ReplayResult, fixture: PriceFixture, *, tamper: bool = False) -> dict[str, Any]:
    """{"commitments_ok", "windows_ok", "chain_ok", "ok", "details": [...]}."""
    res: dict[str, Any] = copy.deepcopy(result)
    details: list[dict[str, Any]] = []
    if tamper:
        for c in res["cutoffs"]:
            if c["split"] == "holdout" and c["forecasts"]:
                f = c["forecasts"][0]
                f["p_yes"] = 0.9 if abs(f["p_yes"] - 0.5) < 1e-9 else round(1.0 - f["p_yes"], 4)
                details.append({"check": "tamper", "note": f"flipped {f['persona_id']} on {f['qid']} to {f['p_yes']}"})
                break
    expected = {"models": sha256_json(res["personas"])}
    if any(c["split"] == "holdout" for c in res["cutoffs"]):
        expected["holdout"] = sha256_json(holdout_payload(res))
    commitments_ok, chain_ok = True, True
    for cm in res.get("commitments", []):
        want = expected.get(cm["kind"])
        ok = want is not None and cm["sha256"] == want and cm["memo"] == f"harvest:v1:{cm['kind']}:{want}"
        onchain: str | None = None
        rec = cm.get("receipt") or {}
        if rec.get("status") == "confirmed" and rec.get("signature"):
            memo = await chain.fetch_memo(rec["signature"])
            onchain = "match" if memo == cm["memo"] else "mismatch"
            chain_ok = chain_ok and onchain == "match"
        commitments_ok = commitments_ok and ok
        details.append({"check": "commitment", "kind": cm["kind"], "ok": ok, "stored": cm["sha256"], "recomputed": want, "onchain": onchain or rec.get("status", "pending")})
    missing = set(expected) - {cm["kind"] for cm in res.get("commitments", [])}
    if missing:
        commitments_ok = False
        details.append({"check": "commitment", "missing": sorted(missing), "ok": False})
    windows_ok = True
    for c in res["cutoffs"]:
        for sid, stored in c["windows"].items():
            again = forecaster.window_sha256(fixture, sid, c["cutoff"]) if sid in fixture["series"] else None
            if again != stored:
                windows_ok = False
                details.append({"check": "window", "cutoff": c["cutoff"], "series": sid, "ok": False, "stored": stored, "recomputed": again})
    details.append({"check": "windows", "ok": windows_ok, "n": sum(len(c["windows"]) for c in res["cutoffs"])})
    return {"commitments_ok": commitments_ok, "windows_ok": windows_ok, "chain_ok": chain_ok, "ok": commitments_ok and windows_ok and chain_ok, "tampered": tamper, "details": details}
