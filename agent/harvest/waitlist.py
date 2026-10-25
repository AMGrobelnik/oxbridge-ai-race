"""Early-access waitlist in SQLite (same database file as the ledger, own table; never hash-chained, since it holds contacts).

A sign-up is one contact (an email address or a handle such as @maria) and an optional note.
Contacts are unique case-insensitively; signing up twice is harmless. Only the count is public.
"""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import ledger

NOTE_MAX = 280
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[a-z]{2,24}$")
HANDLE_RE = re.compile(r"^@?[a-z0-9_.-]{2,32}$")
SCHEMA = """
CREATE TABLE IF NOT EXISTS waitlist (
  id INTEGER PRIMARY KEY,
  contact TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL,
  note TEXT,
  at TEXT NOT NULL
);
"""


class WaitlistError(ValueError):
    """A contact that is neither an email address nor a handle, or a note that is too long."""


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


def parse_contact(raw: object) -> tuple[str, str]:
    """(normalised contact, 'email' | 'handle'); raises WaitlistError."""
    text = " ".join(str(raw or "").split()).lower()
    if EMAIL_RE.fullmatch(text):
        return text, "email"
    if HANDLE_RE.fullmatch(text):
        return "@" + text.lstrip("@"), "handle"
    raise WaitlistError("give an email address or a handle such as @maria")


def add(contact: object, note: object = None) -> dict[str, object]:
    """Adds a sign-up; returns {already, count}."""
    value, kind = parse_contact(contact)
    text = " ".join(str(note or "").split())
    if len(text) > NOTE_MAX:
        raise WaitlistError(f"the note is at most {NOTE_MAX} characters")
    with _connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO waitlist (contact, kind, note, at) VALUES (?, ?, ?, ?)",
                           (value, kind, text or None, ledger.now_iso()))
        total = conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0]
    return {"already": cur.rowcount == 0, "count": int(total)}


def count() -> int:
    with _connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM waitlist").fetchone()[0])
