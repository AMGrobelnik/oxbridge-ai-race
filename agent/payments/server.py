"""Seller side: makes callers pay before they get the response.

    from agent.payments.server import PaidRouteOptions, paid_route

    @paid_route(app, "GET", "/my-data", lambda: PaidRouteOptions(price="$0.02", description="My data"))
    async def my_data() -> dict[str, str]:
        return {"secret": "only for paying callers"}

`paid_route` registers the endpoint *and* its price in one place, so they can't drift apart.
The facilitator checks each payment and submits it to Solana devnet. The paywall is built on the
first paid request, so the service starts without any env vars and without calling the facilitator.
Payment is only settled if the handler responds with a status below 400.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from x402 import x402ResourceServer
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.svm.exact import ExactSvmServerScheme

from .config import PaymentsConfigError, get_facilitator_url, get_network, get_pay_to_address


@dataclass
class PaidRouteOptions:
    price: str
    """Price in US dollars, e.g. "$0.01". Paid in devnet USDC."""
    description: str


class FacilitatorUnavailableError(Exception):
    """The x402 facilitator could not be reached or does not support Solana devnet."""


Handler = TypeVar("Handler", bound=Callable[..., Any])
CallNext = Callable[[Request], Awaitable[Response]]


class _Paywall:
    """One per app: the paid routes, plus the x402 middleware built for them on first use."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[], PaidRouteOptions]] = {}
        self.matchers: list[tuple[str, re.Pattern[str]]] = []
        self.middleware: Callable[[Request, CallNext], Awaitable[Response]] | None = None
        self.lock = asyncio.Lock()

    def add(self, method: str, path: str, options: Callable[[], PaidRouteOptions]) -> None:
        # FastAPI writes path parameters as {id}; x402 route patterns use [id].
        x402_path = re.sub(r"{([^}]+)}", r"[\1]", path)
        self.routes[f"{method} {x402_path}"] = options
        regex = "^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(path)) + "$"
        self.matchers.append((method, re.compile(regex)))

    def protects(self, request: Request) -> bool:
        return any(request.method == m and rx.match(request.url.path) for m, rx in self.matchers)

    async def build(self) -> Callable[[Request, CallNext], Awaitable[Response]]:
        async with self.lock:
            if self.middleware is None:
                network = get_network()
                pay_to = get_pay_to_address()
                routes: dict[str, RouteConfig] = {}
                for route, options in self.routes.items():
                    o = options()
                    routes[route] = RouteConfig(
                        accepts=PaymentOption(scheme="exact", price=o.price, network=network, pay_to=pay_to),
                        description=o.description,
                    )
                url = get_facilitator_url()
                server = x402ResourceServer(HTTPFacilitatorClient(FacilitatorConfig(url=url)))
                # x402 2.25's own type hints for ExactSvmServerScheme don't match its protocol; it works at runtime.
                server.register(network, ExactSvmServerScheme())  # pyright: ignore[reportArgumentType]
                # Loads the facilitator's /supported list (fee payer etc.). It is a blocking HTTP call,
                # so it runs in a thread. On failure nothing is cached and the next request retries.
                try:
                    await asyncio.to_thread(server.initialize)
                except Exception as err:
                    raise FacilitatorUnavailableError(f"Could not reach the x402 facilitator at {url}: {err}") from err
                if server.get_supported_kind(2, network, "exact") is None:
                    raise FacilitatorUnavailableError(f"The x402 facilitator at {url} does not support {network}.")
                # Already initialized above; initializing again inside the middleware would block the event
                # loop and exit the process on a configuration error.
                self.middleware = payment_middleware(routes, server, sync_facilitator_on_start=False)
            return self.middleware

    async def __call__(self, request: Request, call_next: CallNext) -> Response:
        if not self.protects(request):
            return await call_next(request)
        try:
            middleware = await self.build()
        except PaymentsConfigError as err:
            return JSONResponse({"error": str(err)}, status_code=500)
        except FacilitatorUnavailableError as err:
            return JSONResponse({"error": str(err)}, status_code=502)
        return await middleware(request, call_next)


def paid_route(
    app: FastAPI, method: str, path: str, options: Callable[[], PaidRouteOptions]
) -> Callable[[Handler], Handler]:
    """Decorator: adds `method path` to `app` and puts an x402 paywall in front of it."""
    method = method.upper()
    paywall: _Paywall | None = getattr(app.state, "x402_paywall", None)
    if paywall is None:
        paywall = _Paywall()
        app.state.x402_paywall = paywall
        app.middleware("http")(paywall)
    paywall.add(method, path, options)

    def decorator(handler: Handler) -> Handler:
        app.add_api_route(path, handler, methods=[method])
        return handler

    return decorator
