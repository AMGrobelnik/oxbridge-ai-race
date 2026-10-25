"""Accounts: usernames, the human/agent type tag, the verified badge and leaderboard categories.

Every account has a unique username (lowercase letters, digits, - and _, 3-24 characters; the
default is generated, e.g. pilot-7f3a), a type (human or agent) and, for sourced narrative
personas of real organisations and people, a verified badge that links to the sources.

Usernames live in the join / agent ledger payloads, so a restart that replays the ledger
reassigns the same names. Entries from before usernames existed get a deterministic pilot-xxxx.

The one filterable leaderboard puts every account in one category:
  verified  sourced narrative personas (origin "narrative")
  house     the house world models
  baseline  simple quant models (baseline_models.py), no LLM
  swarm     the agent swarm (swarm.py): persona x model bots, owner SWARM_OWNER, shown by their persona · model name
  agents    other agents registered through the API or "write your theory"
  users     pilots: real humans who joined, and nobody else

Swarm accounts that registered before usernames existed get a deterministic bot-<name>-xxxx instead of pilot-xxxx.
"""

import hashlib
import re
import secrets
from collections.abc import Callable
from typing import Any

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,23}$")
USERNAME_RULE = "3-24 characters: lowercase letters, digits, - and _, starting with a letter or digit"
"""Also no '--', which house agent ids use."""
RESERVED = frozenset({"admin", "administrator", "root", "system", "market", "the-market", "house", "croppy", "crophecy",
                      "moderator", "mod", "support", "staff", "official", "null", "undefined", "anonymous", "you"})
CATEGORIES = ("verified", "house", "baseline", "swarm", "agents", "users")
CATEGORY_LABELS = {"verified": "Verified", "house": "House", "baseline": "Baseline models", "swarm": "Swarm", "agents": "Pilot agents",
                   "users": "Pilots"}
SWARM_OWNER = "swarm (AI agents)"
"""The owner every agent-swarm account registers with (swarm.py); it marks the account as a bot."""
VERIFIED_LABEL = "based on public statements; not endorsed"
NARRATIVE = "narrative"


class UsernameError(ValueError):
    """A username that breaks the charset or length rule, is reserved, or is taken."""


class UsernameTakenError(UsernameError):
    """The username belongs to another account."""


def normalise_username(raw: Any) -> str:
    """Lowercased and trimmed (a leading @ is dropped); raises UsernameError unless it fits USERNAME_RULE."""
    if not isinstance(raw, str):
        raise UsernameError("username must be text")
    name = raw.strip().lstrip("@").lower()
    if not USERNAME_RE.fullmatch(name):
        raise UsernameError(f"pick a username of {USERNAME_RULE}")
    if "--" in name:  # '<persona>--<model>' is how house agents are named
        raise UsernameError("a username cannot contain '--'; pick another")
    if name in RESERVED:
        raise UsernameError(f"@{name} is reserved; pick another username")
    return name


def generate(taken: Callable[[str], bool]) -> str:
    """A fresh pilot-xxxx (4 hex digits), widened to 6 if the short space is crowded."""
    for i in range(64):
        name = f"pilot-{secrets.token_hex(2 if i < 32 else 3)}"
        if not taken(name):
            return name
    raise UsernameError("could not generate a free username; pick one")


def legacy_username(trader_id: str, taken: Callable[[str], bool]) -> str:
    """Deterministic pilot-xxxx for an account recorded before usernames existed."""
    digest = hashlib.sha256(f"crophecy-legacy-username:v1:{trader_id}".encode()).hexdigest()
    for width in range(4, len(digest) + 1):
        name = f"pilot-{digest[:width]}"
        if not taken(name):
            return name
    return f"pilot-{digest}"


def swarm_username(trader_id: str, taken: Callable[[str], bool], *, name: str = "") -> str:
    """Deterministic bot-<persona-model>-xxxx for a swarm account recorded without a username."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    digest = hashlib.sha256(f"crophecy-swarm-username:v1:{trader_id}".encode()).hexdigest()
    for width in range(4, len(digest) + 1):
        head = ("bot-" + slug)[: 24 - 1 - width].rstrip("-") if slug else "bot"
        cand = f"{head}-{digest[:width]}"
        if not taken(cand):
            return cand
    return f"bot-{digest}"


def is_swarm(info: dict[str, Any] | None) -> bool:
    """True for an agent-swarm account (registered with owner SWARM_OWNER)."""
    return (info or {}).get("owner") == SWARM_OWNER


def user_id_for(username: str) -> str:
    """The human trader id: a hash of the username, in its own namespace so it never matches an older nickname id."""
    return "user:" + hashlib.sha256(f"crophecy-username:v1:{username}".encode()).hexdigest()[:12]


def claim(usernames: dict[str, str], trader: str, wanted: Any, *, reserved: Callable[[str], bool] = lambda _n: False,
          legacy: Callable[[str, Callable[[str], bool]], str] = legacy_username) -> str:
    """Assigns `trader` a username in `usernames` (username -> trader): `wanted` when valid and free, else `legacy`'s."""
    try:
        name = normalise_username(wanted) if wanted else ""
    except UsernameError:
        name = ""
    if not name or usernames.get(name, trader) != trader or reserved(name):
        name = legacy(trader, lambda n: n in usernames or reserved(n))
    usernames[name] = trader
    return name


def account_type(kind: str | None) -> str:
    """'human' for accounts that joined to play, 'agent' for everything else."""
    return "human" if kind == "human" else "agent"


def system_username(account_id: str) -> str:
    """Username of a house, verified or baseline account: its id, cleaned to the username charset (no length cap)."""
    base = account_id.split(":", 1)[-1].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", base).strip("-") or "agent"


def sources(persona: dict[str, Any] | None) -> list[dict[str, str]]:
    """A persona's sources as [{title, url}], from strings or dicts with url/link, title/label/name/note and an optional date."""
    out = []
    for s in (persona or {}).get("sources") or []:
        if isinstance(s, str):
            out.append({"title": s, "url": s if s.startswith(("http://", "https://")) else ""})
        elif isinstance(s, dict):
            url = str(s.get("url") or s.get("link") or s.get("href") or "")
            title = str(s.get("title") or s.get("label") or s.get("name") or s.get("note") or s.get("publisher") or url)
            if s.get("date"):
                title += f" ({s['date']})"
            out.append({"title": title, "url": url if url.startswith(("http://", "https://")) else ""})
    return out


def persona_index(personas: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(p.get("id")): p for p in personas if p.get("id")}


def _persona_for(pid: str, index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The persona behind a row id; multi-model agent ids are '<persona>--<model>'."""
    return index.get(pid) or index.get(pid.split("--", 1)[0])


def is_verified(row: dict[str, Any], persona: dict[str, Any] | None) -> bool:
    return row.get("origin") == NARRATIVE or (persona or {}).get("origin") == NARRATIVE


def _score_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("brier_holdout", "brier_holdout_lo", "brier_holdout_hi", "n", "beats_market_ci")}


def board(world_models: list[dict[str, Any]], *, personas: list[dict[str, Any]], traders: dict[str, dict[str, Any]],
          baselines: list[dict[str, Any]], humans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every account on one leaderboard, best holdout Brier first, unscored last; each row has a category and type."""
    index = persona_index(personas)
    rows: list[dict[str, Any]] = []
    for m in world_models:
        pid = str(m["persona_id"])
        info = traders.get(pid)
        base = {"id": pid, "name": m.get("name", pid), "color": m.get("color"), "origin": m.get("origin"), **_score_fields(m)}
        if info is not None:  # registered through the agent API or "write your theory"
            rows.append({**base, "username": info.get("username") or system_username(pid), "type": "agent",
                         "category": "swarm" if is_swarm(info) else "agents", "verified": False, "owner": info.get("owner")})
            continue
        persona = _persona_for(pid, index)
        row = {**base, "username": system_username(pid), "type": "agent", "category": "house", "verified": False}
        if is_verified(m, persona):
            row.update(category="verified", verified=True, verified_label=VERIFIED_LABEL, sources=sources(m if m.get("sources") else persona))
        rows.append(row)
    for b in baselines:
        rows.append({"id": b["id"], "name": b["name"], "color": b.get("color"), "origin": "baseline", **_score_fields(b),
                     "username": b["username"], "type": "agent", "category": "baseline", "verified": False, "about": b.get("about")})
    for h in humans:
        tid = str(h["user_id"])
        info = traders.get(tid) or {}
        rows.append({"id": tid, "name": h.get("nickname") or info.get("nickname") or tid, "color": None, "origin": "human",
                     "brier_holdout": h.get("brier"), "brier_holdout_lo": None, "brier_holdout_hi": None, "n": h.get("n", 0),
                     "beats_market_ci": False, "username": info.get("username") or system_username(tid), "type": "human",
                     "category": "users", "verified": False})
    rows.sort(key=lambda r: (r["brier_holdout"] is None, r["brier_holdout"] if r["brier_holdout"] is not None else 0.0, r["username"]))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def category_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {c: sum(1 for r in rows if r["category"] == c) for c in CATEGORIES}
