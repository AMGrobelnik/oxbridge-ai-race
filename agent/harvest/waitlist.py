"""Early-access waitlist: the Supabase table "waitlist" is the one source, shared with the /waitlist page.

POST /harvest/api/waitlist inserts {kind: human, email, role, signup_source: "app"} with the publishable key
(row-level security lets it insert and nothing else; a duplicate email answers 409), and the public count is
Supabase's waitlist_count() RPC. Only when Supabase is not configured (or HARVEST_OFFLINE is set) or cannot be
reached does a sign-up land in the SQLite fallback table (same database file as the ledger, never hash-chained,
since it holds contacts), and the count then comes from there. Only the count is ever public.
"""

import logging
import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from . import ledger

NOTE_MAX = 60
"""The note is the Supabase "role" column (at most 60 characters)."""
SOURCE = "app"
TIMEOUT_S = 5.0
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[a-z]{2,24}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS waitlist (
  id INTEGER PRIMARY KEY,
  contact TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  note TEXT,
  at TEXT NOT NULL
);
"""

logger = logging.getLogger("agent")


class WaitlistError(ValueError):
    """A contact that is not an email address, or a note that is too long."""


class _Unreachable(Exception):
    """Supabase is not configured, offline, or did not answer usably: use the SQLite fallback."""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = ledger.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.executescript(SCHEMA)
        yield conn
    finally:
        conn.close()


def _config() -> tuple[str, str] | None:
    """(project URL, publishable key), or None when unset or HARVEST_OFFLINE is on."""
    if os.environ.get("HARVEST_OFFLINE", "") not in ("", "0", "false"):
        return None
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    return (url, key) if url and key else None


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S)


def _headers(key: str) -> dict[str, str]:
    """A legacy anon key (a JWT) also goes in Authorization; a publishable key goes in apikey only."""
    return {"apikey": key, "Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key.startswith("eyJ") else {})}


def _supabase_post(path: str, body: dict[str, object], *, prefer: str | None = None) -> httpx.Response:
    cfg = _config()
    if cfg is None:
        raise _Unreachable("not configured")
    url, key = cfg
    headers = _headers(key) | ({"Prefer": prefer} if prefer else {})
    try:
        with _client() as client:
            return client.post(f"{url}{path}", json=body, headers=headers)
    except httpx.HTTPError as err:
        raise _Unreachable(type(err).__name__) from err


def parse_contact(raw: object) -> str:
    """The normalised email address; raises WaitlistError."""
    text = " ".join(str(raw or "").split()).lower()
    if EMAIL_RE.fullmatch(text):
        return text
    raise WaitlistError("give an email address, e.g. maria@example.org")


def _add_supabase(email: str, role: str) -> bool:
    """True if already on the list; raises _Unreachable to fall back, WaitlistError if Supabase rejects the row."""
    row = {"kind": "human", "email": email, "role": role or None, "signup_source": SOURCE}
    r = _supabase_post("/rest/v1/waitlist", row, prefer="return=minimal")
    if r.status_code == 409:
        return True
    if r.status_code in (400, 422):
        raise WaitlistError("the waitlist did not accept that address")
    if not r.is_success:
        raise _Unreachable(f"HTTP {r.status_code}")
    return False


def _add_local(email: str, role: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO waitlist (contact, kind, note, at) VALUES (?, ?, ?, ?)",
                           (email, "email", role or None, ledger.now_iso()))
    return cur.rowcount == 0


def add(contact: object, note: object = None) -> dict[str, object]:
    """Adds a sign-up (Supabase, else the SQLite fallback); returns {already, count}."""
    email = parse_contact(contact)
    role = " ".join(str(note or "").split())
    if len(role) > NOTE_MAX:
        raise WaitlistError(f"the note is at most {NOTE_MAX} characters")
    try:
        already = _add_supabase(email, role)
    except _Unreachable as err:
        if _config() is not None:
            logger.warning("waitlist: Supabase unreachable (%s); kept the sign-up in SQLite", err)
        already = _add_local(email, role)
    return {"already": already, "count": count()}


def local_count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0])


def count() -> int:
    """Supabase's waitlist_count() total; the SQLite fallback's count only when Supabase cannot answer."""
    try:
        r = _supabase_post("/rest/v1/rpc/waitlist_count", {})
        if r.is_success:
            return int(r.json()["total"])
        raise _Unreachable(f"HTTP {r.status_code}")
    except (_Unreachable, ValueError, KeyError, TypeError) as err:
        if _config() is not None:
            logger.warning("waitlist: Supabase count unavailable (%s); counting SQLite", err)
        return local_count()
