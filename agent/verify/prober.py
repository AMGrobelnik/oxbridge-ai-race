"""Sealed Verify's prober: sealed probe rounds, the hash-chained ledger, anchoring and the background loop.

A scheduled round: the agent plans one probe per merchant, draws secret probe times, commits
sha256(schedule + salt) on devnet (memo cmap:v1:sched:<round>:<commitment>), runs each probe at
its sealed time, appends the results to the ledger, reveals schedule and salt, and anchors the
new chain head (memo cmap:v1:probe:<n>:<head>). Anchoring never blocks the demo: an unfunded
wallet leaves "pending on-chain" receipts.

State lives in one JSON file (SEALED_VERIFY_DIR, default <repo>/.sealed_runs/verify/state.json).
"""

import asyncio
import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent.config import PROJECT_ROOT

from . import ledger, merchants, probes, shopper

logger = logging.getLogger("agent")

STATE_VERSION = 1
FEED_SIZE = 30
ANCHORS_KEPT = 60
ANCHOR_USDC = 0.000001  # one micro-USDC self-transfer just carries the memo


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def data_dir() -> Path:
    return Path(os.environ.get("SEALED_VERIFY_DIR") or PROJECT_ROOT / ".sealed_runs" / "verify")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(at: str) -> datetime:
    return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


class VerifyService:
    def __init__(self, root: Path | None = None, *, anchor_enabled: bool | None = None) -> None:
        self.root = root or data_dir()
        self.path = self.root / "state.json"
        self.lock = asyncio.Lock()
        self.anchor_enabled = (os.environ.get("VERIFY_ANCHOR", "1") != "0") if anchor_enabled is None else anchor_enabled
        self.head_anchor_busy = False
        self.next_round_at: str | None = None
        self.state = self._load()

    # ---------- persistence ----------

    def _load(self) -> dict[str, Any]:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("v") == STATE_VERSION:
                self._refresh_seeds(state)
                for r in state["rounds"]:  # a restart cut these rounds short: reveal them as abandoned
                    if not r.get("revealed_at"):
                        r.update(revealed_at=_iso(datetime.now(UTC)), abandoned=True)
                return state
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            logger.warning("sealed-verify: state unreadable (%s), starting fresh", type(err).__name__)
        state = {"v": STATE_VERSION, "merchants": merchants.seed_merchants(), "entries": [], "rounds": [], "anchors": [], "micropay_paid": {}}
        self._save(state)
        return state

    def _refresh_seeds(self, state: dict[str, Any]) -> None:
        """Seed targets follow the current port and code; onboarded merchants are kept."""
        fresh = {m["id"]: m for m in merchants.seed_merchants()}
        kept = [m for m in state["merchants"] if not m.get("seed")]
        old = {m["id"]: m for m in state["merchants"] if m.get("seed")}
        seeds = [{**m, "created_at": old.get(mid, m)["created_at"]} for mid, m in fresh.items()]
        state["merchants"] = seeds + kept

    def _save(self, state: dict[str, Any] | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state or self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    # ---------- queries ----------

    def merchant(self, merchant_id: str) -> dict[str, Any] | None:
        return next((m for m in self.state["merchants"] if m["id"] == merchant_id), None)

    def last_entry(self, merchant_id: str) -> dict[str, Any] | None:
        return next((e for e in reversed(self.state["entries"]) if e["merchant_id"] == merchant_id), None)

    def _micropay_ok(self, merchant: dict[str, Any], now: datetime) -> bool:
        paid = self.state["micropay_paid"].get(merchant["id"])
        return bool(merchant.get("micropay")) and (paid is None or (now - _parse(paid)).total_seconds() >= shopper.MICROPAY_EVERY_S)

    def receipt_for(self, seq: int) -> dict[str, Any]:
        """The newest probe-chain anchor that covers entry `seq`."""
        covering = [a for a in self.state["anchors"] if a["kind"] == "probe" and a["count"] >= seq]
        confirmed = [a for a in covering if a["receipt"]["status"] == "confirmed"]
        if confirmed:
            a = min(confirmed, key=lambda a: a["count"])
            return {"status": "confirmed", "memo": a["memo"], "explorer_url": a["receipt"]["explorer_url"], "signature": a["receipt"]["signature"]}
        if covering:
            a = covering[-1]
            return {"status": "pending", "memo": a["memo"], "error": a["receipt"].get("error")}
        return {"status": "pending", "memo": None, "error": "pending on-chain: waiting for the next chain-head anchor"}

    def public_state(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        entries = self.state["entries"]
        pins = []
        for m in self.state["merchants"]:
            last = self.last_entry(m["id"])
            pin = {k: m.get(k) for k in ("id", "name", "lat", "lng", "kind", "target", "micropay", "seed", "note", "place", "created_at")}
            pin["status"] = merchants.pin_status(m, last, now)
            pin["probes"] = sum(1 for e in entries if e["merchant_id"] == m["id"])
            if last:
                pin["last"] = {k: last.get(k) for k in ("seq", "at", "probe", "status", "reason", "verdict", "verdict_by", "hash", "round")}
                pin["last"]["explorer_url"] = (last.get("detail") or {}).get("explorer_url")
                pin["last"]["receipt"] = self.receipt_for(last["seq"])
            pins.append(pin)
        today = now.strftime("%Y-%m-%d")
        rounds = self.state["rounds"]
        current = next((r for r in reversed(rounds) if r["kind"] == "scheduled"), None)
        feed = [{k: e.get(k) for k in ("seq", "at", "merchant_id", "merchant_name", "probe", "status", "verdict", "hash", "round")} for e in entries[-FEED_SIZE:]][::-1]
        return {
            "now": _iso(now),
            "merchants": pins,
            "feed": feed,
            "counters": {
                "merchants": len(self.state["merchants"]),
                "probes": len(entries),
                "onboarded_today": sum(1 for m in self.state["merchants"] if not m.get("seed") and m["created_at"].startswith(today)),
                "rounds": len(rounds),
                "passing": sum(1 for p in pins if p["status"] == "green"),
                "failing": sum(1 for p in pins if p["status"] == "red"),
            },
            "chain": {"count": len(entries), "head": ledger.chain_head(entries), "memo": ledger.probe_memo(len(entries), ledger.chain_head(entries))},
            "round": self._public_round(current) if current else None,
            "next_round_at": self.next_round_at,
            "anchors": self.state["anchors"][-5:][::-1],
        }

    @staticmethod
    def _public_round(r: dict[str, Any]) -> dict[str, Any]:
        out = {k: r.get(k) for k in ("round", "kind", "created_at", "commitment", "memo", "receipt", "revealed_at", "probe_count", "planner")}
        if r.get("revealed_at"):
            out["schedule"], out["salt"] = r.get("schedule"), r.get("salt")
        return out

    def verify(self) -> dict[str, Any]:
        entries = self.state["entries"]
        chain = ledger.verify_chain(entries)
        rounds = []
        for r in self.state["rounds"]:
            check = ledger.verify_round(r)
            check["probes_follow_schedule"] = ledger.probes_follow_schedule(r, entries)
            check["receipt"] = r.get("receipt")
            rounds.append(check)
        bad_rounds = [r["round"] for r in rounds if r["ok"] is False or r["probes_follow_schedule"] is False]
        anchors = [{"memo": a["memo"], "status": a["receipt"]["status"], "explorer_url": a["receipt"].get("explorer_url")} for a in self.state["anchors"] if a["kind"] == "probe"]
        return {
            "ok": chain["ok"] and not bad_rounds,
            "chain": chain,
            "expected_memo": ledger.probe_memo(chain["count"], chain["head"]),
            "rounds_checked": sum(1 for r in rounds if r["ok"] is not None),
            "rounds_sealed": sum(1 for r in rounds if r["ok"] is None),
            "bad_rounds": bad_rounds,
            "rounds": rounds[-20:],
            "probe_anchors": anchors[-10:],
        }

    # ---------- mutations ----------

    async def add_merchant(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self.lock:
            merchant = merchants.build_onboarded(payload, self.state["merchants"])
            self.state["merchants"].append(merchant)
            self._save()
        return merchant

    async def _run_probe(self, merchant: dict[str, Any], probe: str, round_no: int) -> probes.ProbeResult:
        target = merchant.get("target") or ""
        if probe == "x402":
            return await probes.check_x402(target, allow_private=bool(merchant.get("seed")))
        if probe == "micropay":
            recipient = probes.parse_solana_pay_url(target)["recipient"]
            return await probes.micropay(recipient, memo=f"cmap:v1:shop:{round_no}:{merchant['id']}")
        return await probes.check_solana_pay(target)

    async def run_round(self, *, kind: str = "scheduled", merchant_ids: list[str] | None = None, window_s: tuple[float, float] | None = None) -> dict[str, Any]:
        """Seal, run and reveal one probe round. Returns the round record (public view)."""
        now = datetime.now(UTC)
        pool = [m for m in self.state["merchants"] if m.get("kind") != "physical" and (merchant_ids is None or m["id"] in merchant_ids)]
        micro_ok = {m["id"]: self._micropay_ok(m, now) for m in pool}
        if kind == "scheduled":
            plan, planner = await shopper.plan(pool, micro_ok)
        else:  # on-demand probes answer fast: rules decide
            plan, planner = {m["id"]: p for m in pool if (p := shopper.rule_plan(m, micro_ok[m["id"]]))}, "rules"
        lo, hi = window_s or ((_env_float("VERIFY_WINDOW_MIN_S", 20), _env_float("VERIFY_WINDOW_MAX_S", 75)) if kind == "scheduled" else (0, 0))
        start = datetime.now(UTC)
        planned = [{"merchant_id": mid, "type": p, "at": _iso(start + timedelta(seconds=lo + secrets.randbelow(int(max(hi - lo, 0)) + 1)))} for mid, p in plan.items()]
        async with self.lock:
            round_no = len(self.state["rounds"]) + 1
            schedule = ledger.build_schedule(round_no, [m for m in pool if m["id"] in plan], planned)
            salt = ledger.new_salt()
            commitment = ledger.schedule_commitment(schedule, salt)
            record: dict[str, Any] = {
                "round": round_no, "kind": kind, "created_at": _iso(start), "commitment": commitment,
                "memo": ledger.sched_memo(round_no, commitment), "schedule": schedule, "salt": salt,
                "revealed_at": None, "probe_count": len(planned), "planner": planner,
                "receipt": {"status": "pending", "error": "pending on-chain: anchoring" if kind == "scheduled" and self.anchor_enabled else "not anchored (on-demand probe; covered by the next chain-head anchor)"},
            }
            self.state["rounds"].append(record)
            self._save()
        if kind == "scheduled" and self.anchor_enabled:
            receipt = await self._anchor(record["memo"])
            async with self.lock:
                record["receipt"] = receipt
                self._add_anchor({"kind": "sched", "round": round_no, "memo": record["memo"], "receipt": receipt})
                self._save()

        async def timed(p: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], probes.ProbeResult]:
            delay = (_parse(p["at"]) - datetime.now(UTC)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            m = next(x for x in pool if x["id"] == p["merchant_id"])
            try:
                result = await self._run_probe(m, p["type"], round_no)
            except Exception as err:  # a probe bug must not kill the round
                logger.warning("sealed-verify probe %s failed: %s", m["id"], type(err).__name__)
                result = probes.ProbeResult(p["type"], "UNREACHABLE", f"probe error: {type(err).__name__}")
            return p, m, result

        done = await asyncio.gather(*(timed(p) for p in sorted(planned, key=lambda p: p["at"])))
        lines, verdict_by = await shopper.verdicts([(m["name"], r.to_dict()) for _, m, r in done])
        async with self.lock:
            for (_p, m, result), line in zip(done, lines):
                ledger.append_entry(self.state["entries"], {
                    "at": _iso(datetime.now(UTC)), "round": round_no, "merchant_id": m["id"], "merchant_name": m["name"],
                    "probe": result.probe, "status": result.status, "reason": result.reason, "detail": result.detail,
                    "verdict": line, "verdict_by": verdict_by,
                })
                if result.probe == "micropay" and result.status == "PASS":
                    self.state["micropay_paid"][m["id"]] = _iso(datetime.now(UTC))
            record["revealed_at"] = _iso(datetime.now(UTC))
            self._save()
        if done and self.anchor_enabled:
            asyncio.get_running_loop().create_task(self.anchor_head())
        return self._public_round(record)

    async def anchor_head(self) -> dict[str, Any] | None:
        """Anchors cmap:v1:probe:<n>:<head>; one at a time (a later head covers every earlier entry)."""
        if self.head_anchor_busy or not self.state["entries"]:
            return None
        self.head_anchor_busy = True
        try:
            count = len(self.state["entries"])
            memo = ledger.probe_memo(count, ledger.chain_head(self.state["entries"]))
            receipt = await self._anchor(memo)
            async with self.lock:
                anchor = {"kind": "probe", "count": count, "memo": memo, "receipt": receipt}
                self._add_anchor(anchor)
                self._save()
            return anchor
        finally:
            self.head_anchor_busy = False

    def _add_anchor(self, anchor: dict[str, Any]) -> None:
        self.state["anchors"].append(anchor)
        del self.state["anchors"][:-ANCHORS_KEPT]

    async def _anchor(self, memo: str) -> dict[str, Any]:
        from agent.sealed import chain

        return dict(await chain.anchor(memo, amount_usdc=ANCHOR_USDC, timeout_s=30))

    # ---------- background loop ----------

    async def loop(self) -> None:
        interval = max(_env_float("VERIFY_INTERVAL_S", 150), 60)
        first = datetime.now(UTC) + timedelta(seconds=_env_float("VERIFY_FIRST_DELAY_S", 3))
        self.next_round_at = _iso(first)
        await asyncio.sleep(max((first - datetime.now(UTC)).total_seconds(), 0))
        while True:
            started = datetime.now(UTC)
            self.next_round_at = _iso(started + timedelta(seconds=interval))
            try:
                await self.run_round(kind="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:  # keep probing even if one round breaks
                logger.exception("sealed-verify: round failed")
            await asyncio.sleep(max(interval - (datetime.now(UTC) - started).total_seconds(), 5))


_service: VerifyService | None = None


def get_service() -> VerifyService:
    global _service
    if _service is None:
        _service = VerifyService()
    return _service
