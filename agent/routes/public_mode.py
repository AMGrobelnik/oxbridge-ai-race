"""Public-exposure guard, active when PUBLIC_MODE=1 (for serving the app through a public tunnel).

Without an admin token a request may only reach:
  - GET/HEAD pages: /, /race, /favicon.ico, /challenge and everything under it, read-only
    GETs of /sealed pages, demos, static files and run records, and the Sealed Verify map
    (/map, /map/static/*, /map/api/state|ledger, /map/verify);
  - the Beat the Seal API: POST /challenge/api/score|parse|submit (rate-limited per IP there);
  - the Sealed Verify map: POST /map/api/onboard|reverify (rate-limited per IP there);
  - POST /sealed/api/verify (read-only bundle check; capped here at PUBLIC_VERIFY_PER_MIN, default 12);
  - the AI Race verifier routes /agent-studio/v1/* (they check their own bearer token).
  - Harvest: GET /harvest and everything under it; POST /harvest/api/join|bet|forecast|verify|byo|
    agents/register|agents/bet (each rate-limited per IP there).
Everything else (starting Sealed runs, anchoring, co-sign approve/reject and
listings, payments and buys, paid routes, API docs) needs `X-Admin-Token: <ADMIN_TOKEN>`:
401 without the header, 403 with a wrong one or when ADMIN_TOKEN is unset. Every public
client also gets a coarse per-IP request cap (PUBLIC_REQUESTS_PER_MIN, default 300).
"""

import hmac
import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from agent.challenge.ratelimit import RateLimiter, client_ip

ADMIN_HEADER = "x-admin-token"
PUBLIC_GET = re.compile(r"^/(?:|race|favicon\.ico|challenge(?:/.*)?|sealed(?:/.*)?|harvest(?:/.*)?|map(?:/static/[\w.-]+|/api/(?:state|ledger)|/verify)?)$")
PUBLIC_POST = re.compile(r"^/(?:challenge/api/(?:score|parse|submit)|map/api/(?:onboard|reverify)|sealed/api/verify|harvest/api/(?:join|bet|forecast|verify|byo|agents/register|agents/bet))$")
SEALED_VERIFY = "/sealed/api/verify"
VERIFIER = re.compile(r"^/agent-studio/v1/[A-Za-z0-9_/-]*$")
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}

request_limiter = RateLimiter(int(os.environ.get("PUBLIC_REQUESTS_PER_MIN") or 300))
verify_limiter = RateLimiter(int(os.environ.get("PUBLIC_VERIFY_PER_MIN") or 12))


def public_mode() -> bool:
    return os.environ.get("PUBLIC_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def is_public(method: str, path: str) -> bool:
    """True if an anonymous visitor may make this request in public mode."""
    if VERIFIER.match(path):
        return True
    if method in ("GET", "HEAD"):
        return bool(PUBLIC_GET.match(path))
    if method == "POST":
        return bool(PUBLIC_POST.match(path))
    return False


def admin_denial(request: Request) -> JSONResponse | None:
    """None when the request carries the right admin token, else the 401/403 to return."""
    token = os.environ.get("ADMIN_TOKEN", "")
    given = request.headers.get(ADMIN_HEADER)
    if given is None:
        return JSONResponse({"error": "Admin route: send the X-Admin-Token header."}, status_code=401)
    if not token or not hmac.compare_digest(given.encode(), token.encode()):
        return JSONResponse({"error": "Admin token rejected."}, status_code=403)
    return None


async def guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
    if not public_mode():
        return await call_next(request)
    path, method = request.url.path, request.method.upper()
    denied = admin_denial(request)
    if denied is not None:  # only a valid admin token skips the per-IP cap
        wait = request_limiter.hit(client_ip(request))
        if wait:
            return JSONResponse({"error": "Too many requests."}, status_code=429, headers={"Retry-After": str(int(wait) + 1)})
        if not is_public(method, path):
            return denied
        if method == "POST" and path == SEALED_VERIFY:  # it fetches devnet memos: keep it cheap to serve
            wait = verify_limiter.hit(client_ip(request))
            if wait:
                return JSONResponse({"error": "Too many verify requests."}, status_code=429, headers={"Retry-After": str(int(wait) + 1)})
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    return response


def register(app: FastAPI) -> None:
    app.middleware("http")(guard)
