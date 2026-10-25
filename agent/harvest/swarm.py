"""Agent swarm: many AI agent accounts trading the live markets through the public agent API.

Agents are persona x model (every house and narrative persona on every working free-tier model)
plus cheap non-LLM baseline bots (momentum, mean reversion, climatology, contrarian). Each is
its own external account, registered through POST /harvest/api/agents/register with a name that
starts with the robot tag and shows persona and model, and the owner SWARM_OWNER, so it can never
pass for a human. The swarm runs as its own process and only talks HTTP to the live server:

- polls /harvest/api/agents/markets (prices) and /harvest/api/news (headlines);
- refreshes one LLM agent's beliefs per call with the house live prompt (forecaster.build_live_prompt:
  world model, price windows, NOAA climate context, market prices, news), paced per provider and
  per model inside the documented free-tier limits, with backoff on 429 and on errors;
- bets through POST /harvest/api/agents/bet, sized by edge and capped by bankroll, at most
  SWARM_BETS_PER_MIN (default 110, under the server's 120/min per IP agent limit).

Stops at SWARM_UNTIL (local HH:MM), when SWARM_STOP_FILE exists, or on SIGTERM.
Run: python -m agent.harvest.swarm [probe]. Account tokens are kept in SWARM_STATE (mode 600).
"""

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import signal
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import climate, forecaster, prices
from .accounts import SWARM_OWNER
ROBOT = "\U0001f916"
NAME_MAX = 40
WM_MAX = 1200
LOGS = Path(os.environ.get("SWARM_LOGS") or "/home/adrian/projects/ai-inventor/scratchpad/oxbridge-hackathon/logs")
NARRATIVE = Path(os.environ.get("SWARM_NARRATIVE") or "/home/adrian/projects/ai-inventor/scratchpad/oxbridge-hackathon/narrative_personas.json")

logger = logging.getLogger("swarm")


# ---------------------------------------------------------------- providers and pacing

@dataclass
class Provider:
    name: str
    base_url: str
    key_env: str
    rpm: float
    """Documented free-tier requests per minute shared by every model of this provider (0: none)."""
    budget: int
    """Calls we allow ourselves until the stop time (a share of the documented daily cap)."""
    json_mode: bool = True


@dataclass
class Model:
    id: str
    provider: str
    label: str
    family: str
    rpm: float
    budget: int
    json_mode: bool = True
    spread: bool = True
    """Spread `budget` evenly until the stop time (False: go at `rpm` until the budget is spent)."""
    price_in: float = 0.0
    """USD per million prompt tokens (0: free tier)."""
    price_out: float = 0.0

    @property
    def paid(self) -> bool:
        return self.price_in > 0 or self.price_out > 0


PROVIDERS = {
    # OpenRouter :free variants: 20 requests/min, 1000/day for keys with purchased credits (ours).
    "openrouter": Provider("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", rpm=18, budget=900),
    # Gemini API free tier: per-model limits (below); no shared cap.
    "gemini": Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", rpm=0, budget=10**6),
    # Paid models, cents per million tokens, under the swarm's own USD cap (SWARM_PAID_CAP_USD).
    "openrouter-paid": Provider("openrouter-paid", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", rpm=0, budget=10**6),
    "openai": Provider("openai", "https://api.openai.com/v1", "OPENAI_API_KEY", rpm=0, budget=10**6),
}

MODELS = [
    # Gemini free tier (ai.google.dev/gemini-api/docs/rate-limits): 2.5 Flash-Lite 15 RPM / 1000 RPD,
    # 2.5 Flash 10 RPM / 250 RPD. (Gemma 3 via the OpenAI endpoint 404s; inkling is agent-harness only; nemotron-3-super 404s.)
    # Run at the documented RPM until the daily cap (less what the house and earlier runs used today) is spent.
    Model("gemini-2.5-flash-lite", "gemini", "gemini-2.5-lite", "Gemini", rpm=15, budget=900, spread=False),
    Model("gemini-2.5-flash", "gemini", "gemini-2.5-flash", "Gemini", rpm=10, budget=220, spread=False),
    # OpenRouter :free models (catalog of 2026-10-25), shared 20 RPM.
    Model("google/gemma-4-31b-it:free", "openrouter", "gemma-4-31b", "Gemma", rpm=4, budget=900),
    Model("nvidia/nemotron-3.5-lightning:free", "openrouter", "nemotron-3.5", "Nemotron", rpm=4, budget=900),
    Model("poolside/laguna-s-2.1:free", "openrouter", "laguna-s-2.1", "Laguna", rpm=4, budget=900),
    Model("dots-studio/dots-3-note-preview:free", "openrouter", "dots-3-note", "dots", rpm=4, budget=900),
    Model("xingchen-agi/xing4.0-29b-a4b:free", "openrouter", "xing-4.0", "Xing", rpm=4, budget=900),
    Model("recursive/flint-v1.0:free", "openrouter", "flint-1.0", "Flint", rpm=4, budget=900),
    Model("liquid/lfm-2.5-2.6b:free", "openrouter", "lfm-2.5", "LFM", rpm=4, budget=900),
    # Leakage-clean cheap models (agent/harvest/data/models.json) and three more via OpenRouter; prices per M tokens (in, out).
    Model("meta-llama/llama-3.3-70b-instruct", "openrouter-paid", "llama-3.3-70b", "Llama", rpm=8, budget=10**6, price_in=0.10, price_out=0.32),
    Model("microsoft/phi-4", "openrouter-paid", "phi-4", "Phi", rpm=8, budget=10**6, price_in=0.07, price_out=0.14),
    Model("google/gemma-3-27b-it", "openrouter-paid", "gemma-3-27b", "Gemma", rpm=8, budget=10**6, price_in=0.08, price_out=0.35),
    Model("gpt-4o-mini", "openai", "gpt-4o-mini", "GPT", rpm=8, budget=10**6, price_in=0.15, price_out=0.60),
    Model("deepseek/deepseek-chat-v3-0324", "openrouter-paid", "deepseek-v3", "DeepSeek", rpm=8, budget=10**6, price_in=0.29, price_out=1.14),
    Model("qwen/qwen3-235b-a22b-2507", "openrouter-paid", "qwen3-235b", "Qwen", rpm=8, budget=10**6, price_in=0.09, price_out=0.55),
    Model("mistralai/mistral-small-3.2-24b-instruct", "openrouter-paid", "mistral-small", "Mistral", rpm=8, budget=10**6, price_in=0.075, price_out=0.20),
]


class Pacer:
    """Spaces calls to at most `rpm` a minute and spreads `budget` calls evenly until `until` (epoch s).
    `backoff(s)` pauses it (429 or errors)."""

    def __init__(self, rpm: float, budget: int, until: float, *, spread: bool = True) -> None:
        self.rpm, self.budget, self.until, self.spread = rpm, budget, until, spread
        self.used = 0
        self.next_at = 0.0
        self.paused_until = 0.0
        self._lock = asyncio.Lock()

    def gap(self, now: float) -> float:
        left = max(self.budget - self.used, 0)
        if left == 0:
            return math.inf
        by_rpm = 60.0 / self.rpm if self.rpm > 0 else 0.0
        return max(by_rpm, self.by_budget(now, left)) if self.spread else by_rpm

    def by_budget(self, now: float, left: float) -> float:
        return max(self.until - now, 1.0) / left

    def ready_in(self, now: float) -> float:
        return max(self.next_at, self.paused_until) - now if self.used < self.budget else math.inf

    def take(self, now: float, urgent: bool = False) -> None:
        """`urgent` (an agent still without a belief) skips the even spreading; the rpm and budget still hold."""
        self.used += 1
        gap = self.gap(now)
        self.next_at = now + (min(gap, 60.0 / self.rpm) if urgent and self.rpm > 0 and gap != math.inf else gap)

    def backoff(self, seconds: float, now: float) -> None:
        self.paused_until = max(self.paused_until, now + seconds)

    async def acquire(self, stop: asyncio.Event, urgent: bool = False) -> bool:
        while not stop.is_set():
            async with self._lock:
                wait = self.ready_in(time.time())
                if wait == math.inf:
                    return False
                if wait <= 0:
                    self.take(time.time(), urgent)
                    return True
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(max(wait, 0.05), 30.0))
            except TimeoutError:
                pass
        return False


class SpendPacer(Pacer):
    """Paid calls under a hard USD cap, kept in `path` across restarts: spread so the cap lasts until the
    stop time, except urgent calls (first beliefs), which go at `rpm`. Stops once the next call could pass the cap."""

    def __init__(self, cap_usd: float, until: float, path: Path, *, rpm: float = 40) -> None:
        super().__init__(rpm, 10**9, until)
        self.cap, self.path = cap_usd, path
        self.spent, self.est = 0.0, 0.002
        try:
            data = json.loads(path.read_text())
            self.spent, self.est = float(data["spent_usd"]), float(data.get("est_usd") or self.est)
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def ready_in(self, now: float) -> float:
        return math.inf if self.spent + self.est > self.cap else super().ready_in(now)

    def by_budget(self, now: float, left: float) -> float:
        room = self.cap - self.spent
        return math.inf if room <= 0 else max(self.until - now, 1.0) * self.est / room

    def record(self, model: Model, usage: dict[str, Any]) -> float:
        cost = float(usage.get("cost") or 0.0)
        if cost <= 0:
            cost = (float(usage.get("prompt_tokens") or 0) * model.price_in + float(usage.get("completion_tokens") or 0) * model.price_out) / 1e6
        self.spent += cost
        self.est = 0.8 * self.est + 0.2 * cost if cost > 0 else self.est
        try:
            self.path.write_text(json.dumps({"spent_usd": round(self.spent, 6), "est_usd": round(self.est, 6), "cap_usd": self.cap}))
        except OSError as err:
            logger.warning("could not save spend: %s", type(err).__name__)
        return cost


# ---------------------------------------------------------------- agents

def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def load_all_personas() -> list[dict[str, Any]]:
    from . import personas

    out = [dict(p) for p in personas.load_personas()]
    try:
        out += [dict(p) for p in json.loads(NARRATIVE.read_text())]
    except (OSError, ValueError) as err:
        logger.warning("narrative personas unavailable: %s", type(err).__name__)
    return list({str(p["id"]): p for p in reversed(out)}.values())[::-1]  # first wins (personas.json may already hold them)


SHORT_NAMES = {
    "efficient-market-skeptic": "Market Skeptic", "narr-fao": "FAO-style", "narr-imf": "IMF-style", "narr-eu-dgagri": "DG AGRI-style",
    "narr-usda-wasde": "WASDE-style", "narr-copa-cogeca": "Copa-Cogeca-style", "narr-env-ngos": "Green NGOs-style",
    "narr-ifpri-glauber": "Glauber-style", "narr-irwin-farmdoc": "farmdoc-style",
}
"""Display names that fit NAME_MAX next to the longest model label; narrative personas say '-style' (not the real organisation)."""


def short_persona(p: dict[str, Any]) -> str:
    pid, name = str(p["id"]), str(p["name"])
    if pid in SHORT_NAMES:
        return SHORT_NAMES[pid]
    return name.split(" (")[0] + "-style" if pid.startswith("narr-") else name


def agent_name(persona_name: str, label: str) -> str:
    """'<robot> <persona> · <model>', the persona shortened so the whole name fits NAME_MAX."""
    tail = f" · {label}"
    room = NAME_MAX - len(ROBOT) - 1 - len(tail)
    short = persona_name if len(persona_name) <= room else persona_name[: max(room - 1, 1)].rstrip() + "…"
    return f"{ROBOT} {short}{tail}"


def username(*parts: str) -> str:
    """A 3-24 character lowercase handle (for the accounts model once it lands; ignored before)."""
    raw = "-".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return ("bot-" + slug)[:19].rstrip("-") + "-" + hashlib.sha256(raw.encode()).hexdigest()[:4]


@dataclass
class Agent:
    key: str
    name: str
    kind: str
    """'llm' or a baseline kind: momentum, meanrev, climatology, contrarian."""
    world_model: str
    persona: dict[str, Any] = field(default_factory=dict)
    model: Model | None = None
    params: dict[str, float] = field(default_factory=dict)
    agent_id: str | None = None
    token: str | None = None
    bankroll: float = 1000.0
    beliefs: dict[str, tuple[float, str]] = field(default_factory=dict)
    believed_at: float = 0.0
    last_bet: dict[str, float] = field(default_factory=dict)
    bets: int = 0
    edge: float = 0.03
    risk: float = 0.3

    @property
    def username(self) -> str:
        return username(self.key)


def build_agents(models: list[Model], personas: list[dict[str, Any]]) -> list[Agent]:
    out: list[Agent] = []
    for p in personas:
        for m in models:
            note = (f"AI agent (not a human), run by The Crophecy agent swarm. Persona: {p['name']}. Model: {m.id} via {m.provider}."
                    + (" Narrative persona based on public statements; not endorsed." if str(p["id"]).startswith("narr-") else ""))
            out.append(Agent(key=f"{p['id']}|{m.id}", name=agent_name(short_persona(p), m.label), kind="llm", persona=p, model=m,
                             world_model=(note + "\n\n" + str(p.get("world_model", "")))[:WM_MAX],
                             edge=float(p.get("edge", 0.03)), risk=float(p.get("risk", 0.3))))
    return out


BASELINES: list[tuple[str, str, dict[str, float], str]] = [
    ("momentum", "momentum fast", {"lookback": 120, "k": 1.5}, "Buys what the market just bought: belief = price + 1.5 x the move of the last 2 minutes."),
    ("momentum", "momentum slow", {"lookback": 600, "k": 1.0}, "Trend follower: belief = price + the move of the last 10 minutes."),
    ("meanrev", "mean-reversion fast", {"halflife": 180, "k": 0.8}, "Fades short moves: belief = price pulled 80% back to its 3-minute average."),
    ("meanrev", "mean-reversion slow", {"halflife": 900, "k": 0.6}, "Fades drifts: belief = price pulled 60% back to its 15-minute average."),
    ("climatology", "climatology", {"shrink": 0.0}, "Historical base rate: price moves follow last year's monthly volatility; soft events at a 30% base rate."),
    ("climatology", "climatology humble", {"shrink": 0.5}, "Half base rate, half market price."),
    ("contrarian", "contrarian", {"k": 0.6}, "Fades extremes: belief = 0.5 + 0.6 x (price - 0.5)."),
    ("contrarian", "contrarian flow", {"lookback": 60, "k": 1.2}, "Bets against the last minute's flow: belief = price - 1.2 x the 1-minute move."),
]


def build_baselines() -> list[Agent]:
    out = []
    for kind, label, params, rule in BASELINES:
        out.append(Agent(key=f"baseline|{label}", name=agent_name("baseline", label), kind=kind, params=params, edge=0.025, risk=0.25,
                         world_model=f"AI agent (not a human), a non-LLM baseline bot run by The Crophecy agent swarm. Rule: {rule}"))
    return out


# ---------------------------------------------------------------- baseline beliefs

def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _month(m: str) -> int:
    y, mo = m[:7].split("-")
    return int(y) * 12 + int(mo) - 1


def climatology_p(market: dict[str, Any]) -> float | None:
    """Lognormal base rate from the visible window's monthly volatility (hard), 30% (soft)."""
    if market.get("kind") == "soft":
        return 0.3
    hist = [float(v) for _, v in (market.get("visible_history") or []) if v]
    target, closes, qid = market.get("target_index"), market.get("closes"), str(market.get("qid", ""))
    if len(hist) < 4 or not target or not closes or "@" not in qid:
        return None
    rets = [math.log(b / a) for a, b in zip(hist, hist[1:], strict=False) if a > 0 and b > 0]
    if len(rets) < 3:
        return None
    try:
        horizon = max(_month(str(closes)) - _month(qid.split("@")[1]), 1)
    except (ValueError, IndexError):
        return None
    sigma = max(statistics.pstdev(rets), 0.01) * math.sqrt(horizon)
    z = math.log(float(target) / hist[-1]) / sigma
    below = "below" in str(market.get("title", "")).lower()
    return _clamp(_phi(z) if below else 1.0 - _phi(z), 0.03, 0.97)


def baseline_belief(agent: Agent, market: dict[str, Any], history: deque[tuple[float, float]], now: float) -> tuple[float, str] | None:
    p = float(market["p_yes"])
    k = agent.params.get("k", 1.0)

    def ago(seconds: float) -> float | None:
        old = [v for t, v in history if t <= now - seconds]
        return old[-1] if old else None

    if agent.kind == "momentum" or (agent.kind == "contrarian" and "lookback" in agent.params):
        then = ago(agent.params["lookback"])
        if then is None:
            return None
        move = p - then
        if agent.kind == "contrarian":
            return _clamp(p - k * move, 0.02, 0.98), f"fading the last minute's {move * 100:+.1f} pt flow"
        return _clamp(p + k * move, 0.02, 0.98), f"riding a {move * 100:+.1f} pt move"
    if agent.kind == "meanrev":
        hl = agent.params["halflife"]
        pts = [(t, v) for t, v in history if t >= now - 4 * hl]
        if len(pts) < 3:
            return None
        w = [math.exp(-(now - t) / hl) for t, _ in pts]
        avg = sum(wi * v for wi, (_, v) in zip(w, pts, strict=True)) / sum(w)
        return _clamp(p + k * (avg - p), 0.02, 0.98), f"reverting toward the recent average {avg:.2f}"
    if agent.kind == "climatology":
        base = climatology_p(market)
        if base is None:
            return None
        shrink = agent.params.get("shrink", 0.0)
        return _clamp((1 - shrink) * base + shrink * p, 0.02, 0.98), f"historical base rate {base:.2f}"
    if agent.kind == "contrarian":
        return _clamp(0.5 + k * (p - 0.5), 0.02, 0.98), "fading an extreme price"
    return None


def size_bet(agent: Agent, belief: float, price: float) -> tuple[str, float] | None:
    """YES/NO and spend, scaled by the edge, capped at 4% of the bankroll (and 60 credits), at least 1."""
    gap = belief - price
    if abs(gap) <= agent.edge or agent.bankroll < 1.0:
        return None
    spend = min(agent.risk * agent.bankroll * abs(gap), 0.04 * agent.bankroll, 60.0)
    spend = round(max(spend, 1.0), 2)
    return ("YES" if gap > 0 else "NO"), spend


# ---------------------------------------------------------------- LLM calls

def extract_json(text: str) -> str:
    """The outermost JSON object in a model reply (strips code fences and thinking)."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return text[start:end + 1]


_ROW = re.compile(r'"qid"\s*:\s*"(Q\d+)"[^{}]*?"p_yes"\s*:\s*"?([0-9]*\.?[0-9]+)(?:[^{}]*?"rationale"\s*:\s*"((?:[^"\\]|\\.)*))?', re.S)


def parse_reply(text: str, n: int) -> dict[str, tuple[float, str]]:
    """Forecasts from a reply: strict JSON first, else row by row (small models often break the JSON)."""
    try:
        parsed = forecaster._parse(extract_json(text), n)
        if parsed:
            return parsed
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    out: dict[str, tuple[float, str]] = {}
    for qid, p, why in _ROW.findall(text or ""):
        if 1 <= int(qid[1:]) <= n and qid not in out:
            out[qid] = (_clamp(float(p), 0.01, 0.99), (why or "")[:200])
    if not out:
        raise ValueError("no forecasts in the reply")
    return out


class RateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"429, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


async def chat(client: httpx.AsyncClient, model: Model, prompt: str, *, timeout_s: float = 90.0) -> tuple[str, dict[str, Any]]:
    """The reply text and the usage block (tokens, and OpenRouter's cost)."""
    prov = PROVIDERS[model.provider]
    key = os.environ.get(prov.key_env)
    if not key:
        raise RuntimeError(f"{prov.key_env} not set")
    body: dict[str, Any] = {"model": model.id, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7, "max_tokens": 8000}
    # Keep thinking short so it cannot eat the answer (2.5 Flash and the OpenRouter reasoning models truncated at 4000 tokens).
    if model.provider == "gemini" and "lite" not in model.id:
        body["reasoning_effort"] = "low"
    elif model.provider == "openrouter":
        body["reasoning"] = {"effort": "low"}
    if model.json_mode and prov.json_mode:
        body["response_format"] = {"type": "json_object"}
    r = await client.post(f"{prov.base_url}/chat/completions", json=body, timeout=timeout_s,
                          headers={"Authorization": f"Bearer {key}", "X-Title": "The Crophecy agent swarm"})
    if r.status_code == 429:
        try:
            retry = float(r.headers.get("retry-after") or 30)
        except ValueError:
            retry = 30.0
        raise RateLimited(retry)
    if r.status_code == 402:
        raise RateLimited(600.0)  # out of credits on that key: back off for long
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
    data = r.json()
    if data.get("error"):
        code = (data["error"] or {}).get("code")
        if code == 429:
            raise RateLimited(30.0)
        raise RuntimeError(f"provider error {code}")
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    # Some reasoning models leave the answer in the reasoning text when content comes back empty.
    return str(msg.get("content") or msg.get("reasoning") or ""), dict(data.get("usage") or {})


# ---------------------------------------------------------------- the swarm

def _until_epoch(hhmm: str) -> float:
    now = datetime.now().astimezone()
    h, m = (int(x) for x in hhmm.split(":"))
    end = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return end.timestamp()


class Swarm:
    def __init__(self, *, base: str, until: float, stop_file: Path, state_file: Path, models: list[Model], bets_per_min: float,
                 paid_cap: float = 2.0) -> None:
        self.base = base.rstrip("/")
        self.until, self.stop_file, self.state_file = until, stop_file, state_file
        self.models = models
        self.bet_pacer = Pacer(bets_per_min, 10**9, until)
        self.provider_pacers = {n: Pacer(p.rpm, p.budget, until) for n, p in PROVIDERS.items() if p.rpm > 0}
        self.model_pacers = {m.id: Pacer(m.rpm, m.budget, until, spread=m.spread) for m in models}
        self.spend = SpendPacer(paid_cap, until, state_file.with_name("swarm_spend.json"))
        self.calls_file = state_file.with_name("swarm_calls.json")
        self._load_calls()
        self.agents = build_agents(models, load_all_personas()) + build_baselines()
        self.markets: dict[str, dict[str, Any]] = {}
        self.history: dict[str, deque[tuple[float, float]]] = {}
        self.news: dict[str, list[str]] = {}
        self.stop = asyncio.Event()
        self.stats: dict[str, Any] = {"bets": 0, "bet_errors": 0, "bet_429": 0, "calls": {}, "fail": {}, "llm_429": {}, "started": time.time()}
        self.bet_times: deque[float] = deque()
        self.fixture = prices.load_fixture()
        self.climate = climate.context_lines(climate.load_climate())

    # ---- daily call counts (so a restart cannot reset a provider's daily cap)
    def _pacers(self) -> dict[str, Pacer]:
        return {**self.provider_pacers, **self.model_pacers}

    def _load_calls(self) -> None:
        try:
            data = json.loads(self.calls_file.read_text())
        except (OSError, ValueError):
            return
        if data.get("day") != datetime.now(UTC).strftime("%Y-%m-%d"):
            return
        for k, p in self._pacers().items():
            p.used = int((data.get("used") or {}).get(k, 0))

    def _save_calls(self) -> None:
        data = {"day": datetime.now(UTC).strftime("%Y-%m-%d"), "used": {k: p.used for k, p in self._pacers().items()}}
        try:
            self.calls_file.write_text(json.dumps(data, indent=1))
        except OSError as err:
            logger.warning("could not save call counts: %s", type(err).__name__)

    # ---- accounts
    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        data = {a.key: {"agent_id": a.agent_id, "token": a.token} for a in self.agents if a.token}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.state_file)

    def _beliefs_file(self) -> Path:
        return self.state_file.with_name("swarm_beliefs.json")

    def _save_beliefs(self) -> None:
        data = {a.key: {"at": a.believed_at, "beliefs": a.beliefs} for a in self.agents if a.beliefs}
        try:
            tmp = self._beliefs_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self._beliefs_file())
        except OSError as err:
            logger.warning("could not save beliefs: %s", type(err).__name__)

    def load_beliefs(self) -> int:
        """Beliefs from an earlier run (so a restart keeps trading at once); kept only for markets still open."""
        try:
            data = json.loads(self._beliefs_file().read_text())
        except (OSError, ValueError):
            return 0
        n = 0
        for a in self.agents:
            row = data.get(a.key)
            if a.kind == "llm" and row:
                a.beliefs = {q: (float(v[0]), str(v[1])) for q, v in row["beliefs"].items()}
                a.believed_at = float(row["at"])
                n += 1
        return n

    async def register_all(self, http: httpx.AsyncClient) -> None:
        saved = self._load_state()
        for a in self.agents:
            if a.key in saved:
                a.agent_id, a.token = saved[a.key]["agent_id"], saved[a.key]["token"]
        # Baselines first (they bet at once), then LLM agents round-robin across models.
        todo = [a for a in self.agents if not a.token]
        todo.sort(key=lambda a: (a.kind == "llm", a.persona.get("id", ""), a.model.id if a.model else ""))
        llm = [a for a in todo if a.kind == "llm"]
        by_model: dict[str, list[Agent]] = {}
        for a in llm:
            by_model.setdefault(a.model.id if a.model else "", []).append(a)
        interleaved = [x for group in zip(*by_model.values(), strict=False) for x in group] if by_model else []
        rest = [a for a in llm if a not in interleaved]
        for a in [x for x in todo if x.kind != "llm"] + interleaved + rest:
            while not self.stop.is_set():
                try:
                    r = await http.post(f"{self.base}/harvest/api/agents/register", timeout=20, json={
                        "name": a.name, "world_model": a.world_model, "owner": SWARM_OWNER, "username": a.username, "type": "agent"})
                except httpx.HTTPError as err:
                    logger.warning("register %s failed: %s", a.key, type(err).__name__)
                    await self._sleep(5)
                    continue
                if r.status_code == 429:
                    await self._sleep(float(r.headers.get("retry-after") or 5))
                    continue
                if r.status_code != 200:
                    logger.warning("register %s: HTTP %s %s", a.key, r.status_code, r.text[:120])
                    break
                d = r.json()
                a.agent_id, a.token = d["agent_id"], d["token"]
                self._save_state()
                logger.info("registered %s as %s", a.name, a.agent_id)
                await self._sleep(2.1)  # 30/min per IP
                break
            if self.stop.is_set():
                return

    async def _sleep(self, s: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=s)
        except TimeoutError:
            pass

    # ---- market data
    async def poll_markets(self, http: httpx.AsyncClient) -> None:
        while not self.stop.is_set():
            try:
                r = await http.get(f"{self.base}/harvest/api/agents/markets", timeout=20)
                if r.status_code == 200:
                    now = time.time()
                    self.markets = {m["qid"]: m for m in r.json()["markets"]}
                    for qid, m in self.markets.items():
                        h = self.history.setdefault(qid, deque(maxlen=2000))
                        h.append((now, float(m["p_yes"])))
            except (httpx.HTTPError, ValueError, KeyError) as err:
                logger.warning("market poll failed: %s", type(err).__name__)
            await self._sleep(4)

    async def poll_news(self, http: httpx.AsyncClient) -> None:
        while not self.stop.is_set():
            try:
                r = await http.get(f"{self.base}/harvest/api/news", params={"limit": 200}, timeout=20)
                if r.status_code == 200:
                    by: dict[str, list[str]] = {}
                    for it in r.json().get("items", []):
                        line = f"{str(it.get('published_at') or '')[:10]} {it.get('title', '')} ({it.get('source', '')})"
                        for qid in it.get("qids") or []:
                            if len(by.setdefault(qid, [])) < 6:
                                by[qid].append(line)
                    self.news = by
            except (httpx.HTTPError, ValueError, KeyError) as err:
                logger.warning("news poll failed: %s", type(err).__name__)
            await self._sleep(90)

    def build_prompt(self, persona: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        qs: list[dict[str, Any]] = []
        windows: dict[str, list[tuple[str, float]]] = {}
        for m in self.markets.values():
            qs.append({"qid": m["qid"], "title": m["title"], "region": m.get("region") or "", "crop": m.get("crop") or "", "resolve_month": m.get("closes")})
            sid = m.get("series")
            if sid and sid in self.fixture["series"] and sid not in windows:
                pts = self.fixture["series"][sid]["points"]
                windows[sid] = prices.window(self.fixture, sid, pts[-1][0], 13) if pts else []
        prompt = forecaster.build_live_prompt(persona, qs, windows=windows, prices_now={q["qid"]: float(self.markets[q["qid"]]["p_yes"]) for q in qs},  # type: ignore[arg-type]
                                              climate_lines=self.climate, today=datetime.now(UTC).strftime("%Y-%m-%d"), news=self.news)
        return prompt, qs

    # ---- LLM workers
    async def llm_worker(self, http: httpx.AsyncClient, model: Model) -> None:
        pacer = self.model_pacers[model.id]
        prov = self.spend if model.paid else self.provider_pacers.get(model.provider)
        fails = limited = 0
        while not self.stop.is_set():
            mine = [a for a in self.agents if a.model is model and a.token]
            if not mine or not self.markets:
                await self._sleep(3)
                continue
            urgent = any(not a.beliefs for a in mine)
            if not await pacer.acquire(self.stop, urgent):
                return
            if prov is not None and not await prov.acquire(self.stop, urgent):
                logger.info("%s stopped: %s", model.label, "paid cap reached" if model.paid else "budget spent")
                return
            agent = min(mine, key=lambda a: (bool(a.beliefs), a.believed_at))
            prompt, qs = self.build_prompt(agent.persona)
            now = time.time()
            try:
                text, usage = await chat(http, model, prompt)
                if model.paid:
                    self.spend.record(model, usage)
                parsed = parse_reply(text, len(qs))
            except RateLimited as err:
                self.stats["llm_429"][model.label] = self.stats["llm_429"].get(model.label, 0) + 1
                limited += 1
                wait = min(max(err.retry_after, 30.0 * 2 ** min(limited - 1, 4)), 600.0)
                pacer.backoff(wait, time.time())  # OpenRouter :free 429s are per upstream model; the shared pacer keeps the account limit
                logger.warning("%s rate-limited, backing off %.0fs", model.label, wait)
                continue
            except Exception as err:  # noqa: BLE001 - any model failure backs that model off; the others keep going
                fails += 1
                self.stats["fail"][model.label] = self.stats["fail"].get(model.label, 0) + 1
                wait = min(30.0 * 2 ** min(fails - 1, 4), 600.0)
                pacer.backoff(wait, time.time())
                logger.warning("%s call failed (%s: %s), backing off %.0fs", model.label, type(err).__name__, str(err)[:120], wait)
                continue
            fails = limited = 0
            self.stats["calls"][model.label] = self.stats["calls"].get(model.label, 0) + 1
            agent.beliefs = {q["qid"]: parsed[f"Q{i}"] for i, q in enumerate(qs, start=1) if f"Q{i}" in parsed}
            agent.believed_at = now
            self._save_beliefs()
            logger.info("%s refreshed %d beliefs in %.1fs", agent.name, len(agent.beliefs), time.time() - now)

    # ---- betting
    def candidates(self, now: float) -> list[tuple[float, Agent, str, float, str]]:
        out = []
        for a in self.agents:
            if not a.token or a.bankroll < 1.0:
                continue
            cooldown = 60.0 if a.kind == "llm" else 25.0
            for qid, m in self.markets.items():
                if now - a.last_bet.get(qid, 0.0) < cooldown:
                    continue
                if a.kind == "llm":
                    b = a.beliefs.get(qid)
                else:
                    b = baseline_belief(a, m, self.history.get(qid, deque()), now)
                if b is None:
                    continue
                gap = abs(b[0] - float(m["p_yes"]))
                if gap > a.edge:
                    out.append(((gap - a.edge) * (2.0 if a.kind == "llm" else 1.0), a, qid, b[0], b[1]))
        return out

    async def bettor(self, http: httpx.AsyncClient) -> None:
        while not self.stop.is_set():
            if not await self.bet_pacer.acquire(self.stop):
                return
            now = time.time()
            cands = self.candidates(now)
            if not cands:
                self.bet_pacer.used -= 1
                self.bet_pacer.next_at = now + 1.0
                continue
            _, a, qid, belief, why = random.choices(cands, weights=[c[0] for c in cands], k=1)[0]
            price = float(self.markets[qid]["p_yes"])
            sized = size_bet(a, belief, price)
            if sized is None:
                continue
            side, spend = sized
            a.last_bet[qid] = now
            why = (f"{a.model.label}: " if a.model else "baseline: ") + why
            try:
                r = await http.post(f"{self.base}/harvest/api/agents/bet", timeout=20, json={
                    "token": a.token, "qid": qid, "side": side, "spend": spend, "p_yes": round(belief, 4), "rationale": why[:200]})
            except httpx.HTTPError as err:
                self.stats["bet_errors"] += 1
                logger.warning("bet failed: %s", type(err).__name__)
                continue
            if r.status_code == 429:
                self.stats["bet_429"] += 1
                self.bet_pacer.backoff(float(r.headers.get("retry-after") or 5), time.time())
                continue
            if r.status_code != 200:
                self.stats["bet_errors"] += 1
                msg = r.text[:160]
                if "insufficient credits" in msg:
                    found = re.search(r"([\d.]+) available", msg)
                    a.bankroll = float(found.group(1)) if found else 0.0
                logger.warning("bet by %s on %s: HTTP %s %s", a.key, qid, r.status_code, msg)
                continue
            d = r.json()
            a.bankroll = float(d.get("balance", a.bankroll))
            a.bets += 1
            self.stats["bets"] += 1
            self.bet_times.append(time.time())
            mk = d.get("market") or {}
            if qid in self.markets and "p_yes" in mk:
                self.markets[qid]["p_yes"] = mk["p_yes"]
                self.history.setdefault(qid, deque(maxlen=2000)).append((time.time(), float(mk["p_yes"])))

    # ---- control
    async def watchdog(self) -> None:
        last = 0.0
        while not self.stop.is_set():
            now = time.time()
            if now >= self.until or self.stop_file.exists():
                logger.info("stopping: %s", "stop file" if self.stop_file.exists() else "deadline reached")
                self.stop.set()
                break
            if now - last >= 60:
                last = now
                self.report()
            await self._sleep(2)

    def report(self) -> None:
        now = time.time()
        while self.bet_times and self.bet_times[0] < now - 60:
            self.bet_times.popleft()
        reg = sum(1 for a in self.agents if a.token)
        by_model: dict[str, int] = {}
        for a in self.agents:
            if a.token:
                k = a.model.label if a.model else "baseline"
                by_model[k] = by_model.get(k, 0) + 1
        status = {**self.stats, "at": datetime.now().astimezone().isoformat(timespec="seconds"), "registered": reg, "agents": len(self.agents),
                  "bets_last_min": len(self.bet_times), "registered_by_model": by_model,
                  "believing": sum(1 for a in self.agents if a.kind == "llm" and a.beliefs),
                  "believing_by_model": {m.label: sum(1 for a in self.agents if a.model is m and a.beliefs) for m in self.models},
                  "paid_spent_usd": round(self.spend.spent, 4), "paid_cap_usd": self.spend.cap,
                  "pacers": {k: {"used": p.used, "budget": p.budget} for k, p in {**self.provider_pacers, **self.model_pacers}.items()}}
        logger.info("STATUS %s", json.dumps(status))
        self._save_calls()
        try:
            (LOGS / "swarm_status.json").write_text(json.dumps(status, indent=1))
        except OSError:
            pass

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stop.set)
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32)
        async with httpx.AsyncClient(limits=limits) as http, httpx.AsyncClient(limits=limits) as llm_http:
            tasks = [asyncio.create_task(c) for c in (self.watchdog(), self.poll_markets(http), self.poll_news(http), self.register_all(http), self.bettor(http))]
            tasks += [asyncio.create_task(self.llm_worker(llm_http, m)) for m in self.models]
            await self.stop.wait()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.report()
        logger.info("swarm stopped")


async def probe(models: list[Model]) -> list[Model]:
    """One tiny JSON call per model; returns the ones that answer."""
    ok = []
    prompt = 'Answer only with JSON: {"forecasts": [{"qid": "Q1", "p_yes": 0.0-1.0, "rationale": "short"}]}\nQ1: Will wheat be above $250/mt by 2027-03?'
    async with httpx.AsyncClient() as http:
        async def one(m: Model) -> tuple[Model, str]:
            t = time.time()
            try:
                p = parse_reply((await chat(http, m, prompt, timeout_s=60))[0], 1)
                return m, f"ok {time.time() - t:.1f}s p={p.get('Q1', ('?',))[0]}"
            except Exception as err:  # noqa: BLE001 - probe reports every failure
                return m, f"FAIL {type(err).__name__}: {str(err)[:140]}"
        for m, res in await asyncio.gather(*(one(m) for m in models)):
            logger.info("probe %-45s %s", m.id, res)
            if res.startswith("ok") or "RateLimited" in res:  # a 429 is transient: the pacer backs off
                ok.append(m)
    return ok


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    only = [s.strip() for s in (os.environ.get("SWARM_MODELS") or "").split(",") if s.strip()]
    models = [m for m in MODELS if not only or m.id in only]
    if sys.argv[1:2] == ["probe"]:
        asyncio.run(probe(models))
        return
    if os.environ.get("SWARM_PROBE", "1") != "0":
        models = asyncio.run(probe(models))
    swarm = Swarm(base=os.environ.get("SWARM_BASE") or "http://127.0.0.1:3001", until=_until_epoch(os.environ.get("SWARM_UNTIL") or "19:45"),
                  stop_file=Path(os.environ.get("SWARM_STOP_FILE") or LOGS / "swarm.stop"),
                  state_file=Path(os.environ.get("SWARM_STATE") or LOGS / "swarm_accounts.json"), models=models,
                  bets_per_min=float(os.environ.get("SWARM_BETS_PER_MIN") or 110),
                  paid_cap=float(os.environ.get("SWARM_PAID_CAP_USD") or 2.0))
    logger.info("restored beliefs of %d LLM agents", swarm.load_beliefs())
    logger.info("swarm: %d agents (%d LLM on %d models, %d baselines), until %s", len(swarm.agents), sum(1 for a in swarm.agents if a.kind == "llm"),
                len(models), sum(1 for a in swarm.agents if a.kind != "llm"), datetime.fromtimestamp(swarm.until).strftime("%H:%M"))
    asyncio.run(swarm.run())


if __name__ == "__main__":
    main()
