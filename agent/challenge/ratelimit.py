"""In-memory sliding-window rate limits keyed by client IP (one process, so no shared store)."""

import ipaddress
import threading
import time
from collections import deque

from starlette.requests import Request

MAX_KEYS = 50_000


def _from_proxy(host: str | None) -> bool:
    """True when the socket peer is local or private (cloudflared on this machine), so the
    forwarding headers can be believed; a direct public peer could forge them."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:  # a unix socket or the test client carries no IP
        return True
    return ip.is_loopback or ip.is_private


def client_ip(request: Request) -> str:
    """The visitor's IP: Cloudflare's CF-Connecting-IP, else the first X-Forwarded-For hop,
    else the socket peer. Behind a tunnel the peer is always 127.0.0.1, so the headers matter,
    but they are read only when the peer is the local proxy."""
    peer = request.client.host if request.client else None
    if not _from_proxy(peer):
        return peer or "unknown"
    for header in ("cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value[:64]
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    return peer or "unknown"


class RateLimiter:
    """At most `limit` hits per `window_s` seconds per key."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, now: float | None = None) -> float:
        """Records a hit; returns 0 if allowed, else the seconds until the next hit is allowed."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= MAX_KEYS:
                    self._purge(now)
                hits = self._hits[key] = deque()
            while hits and hits[0] <= now - self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(hits[0] + self.window_s - now, 0.01)
            hits.append(now)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def _purge(self, now: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or h[-1] <= now - self.window_s]
        for key in stale:
            del self._hits[key]
