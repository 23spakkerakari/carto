"""The console API application, ``carto-core api`` (spec 12, 13, 14.1, 14.2, 14.7, 16; ADR
0052).

Everything user-facing lives under ``/api/v1`` behind one router whose dependency is
:func:`carto_core.api.sessions.gate` (JSON on mutations, session to principal, per-user rate
limit, CSRF); each route then names its permission through a :class:`~carto_core.api.sessions.Guard`
(deny by default, spec 14.2). ``/healthz``, ``/readyz`` and ``/metrics`` sit at the root, as
on ``ingest-api``: the container probes them directly and the web nginx proxies ``/api/`` only,
so ``/metrics`` never reaches the ingress (spec 12 "internal network only").

Middleware, outermost first: :class:`~carto_core.ingest.app.RequestIdMiddleware` (reused), a
body cap (``api.max_body_bytes``; 413 from the declared length or while streaming, spec 2.3
invariant 8), and the response headers of spec 14.7 on every answer: HSTS when the listener
is TLS, ``X-Content-Type-Options: nosniff``, ``Referrer-Policy: no-referrer``, a restrictive
``Permissions-Policy``, the strict CSP (harmless on JSON, decisive if a browser ever renders a
problem) and ``Cache-Control: no-store``. No ``Server`` header (uvicorn is told not to add one),
no interactive documentation on the listener: the OpenAPI document is a committed file
(:mod:`carto_core.api.openapi`).

Every error is an RFC 7807 problem that never echoes input (:mod:`carto_core.api.problems`).
Request and response bodies are never logged (spec 14.12); handlers log ids and counts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from fastapi import APIRouter, Depends, FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from carto_common.logging import get_logger
from carto_common.mtls import peer_role
from carto_core.api import (
    routes_admin,
    routes_auth,
    routes_entities,
    routes_inventory,
    routes_links,
)
from carto_core.api.context import ApiContext, ApiMetrics
from carto_core.api.oidc import OidcClient
from carto_core.api.ports import ApiPorts, RoleBinding
from carto_core.api.problems import PROBLEM_CONTENT_TYPE, Problem, install_problem_handlers
from carto_core.api.ratelimit import TokenBucketLimiter
from carto_core.api.sessions import SessionManager, gate, public_route
from carto_core.ingest.app import RequestIdMiddleware
from carto_schema.api import API_VERSION, Role

if TYPE_CHECKING:
    from carto_core.settings import CoreSettings

__all__ = [
    "API_PREFIX",
    "CONTENT_SECURITY_POLICY",
    "HEALTH_PATHS",
    "LISTENER_ROLES",
    "SECURITY_HEADERS",
    "BodyCapMiddleware",
    "PeerRoleMiddleware",
    "ResponseHeadersMiddleware",
    "bootstrap_bindings",
    "create_app",
]

log = get_logger(component="api")

API_PREFIX: Final = f"/api/{API_VERSION}"
CONTENT_SECURITY_POLICY: Final = (
    "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=()"),
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("cache-control", "no-store"),
)
HSTS_HEADER: Final = ("strict-transport-security", "max-age=31536000; includeSubDomains")
LISTENER_ROLES: Final[frozenset[str]] = frozenset({"web", "api"})
"""Certificate roles that may reach the listener under mutual TLS (ADR 0052): the web nginx
and the api itself (healthchecks, carto-core tooling). Any certificate of the install CA may
probe the health paths."""
HEALTH_PATHS: Final[frozenset[str]] = frozenset(
    {"/healthz", "/readyz", "/metrics", f"{API_PREFIX}/healthz"}
)


class ResponseHeadersMiddleware:
    """Spec 14.7 headers on every response, and the request counter."""

    def __init__(self, app: ASGIApp, *, hsts: bool, metrics: ApiMetrics) -> None:
        self.app = app
        self.headers: tuple[tuple[str, str], ...] = SECURITY_HEADERS + (
            (HSTS_HEADER,) if hsts else ()
        )
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "").startswith(API_PREFIX):
            self.metrics.inc("carto_api_requests_total")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self.headers:
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


class PeerRoleMiddleware:
    """Under mutual TLS (``api.require_client_cert``): the connection's certificate role must be
    one of :data:`LISTENER_ROLES`, except on :data:`HEALTH_PATHS`, which any certificate the
    install CA signed may call. The TLS handshake already refused certificates from any other
    CA; this is the role check of ADR 0031 for the console listener."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in HEALTH_PATHS:
            await self.app(scope, receive, send)
            return
        role = peer_role(scope)
        if role not in LISTENER_ROLES:
            log.warning("request.role_refused", path=scope.get("path", ""), role=role or "none")
            await _refuse(scope, send, 403, "forbidden", "this certificate may not call the API")
            return
        await self.app(scope, receive, send)


async def _refuse(scope: Scope, send: Send, status: int, title: str, detail: str) -> None:
    """A problem answered from a middleware, before the application runs."""
    request_id = str(scope.get("state", {}).get("request_id", ""))
    body = json.dumps(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": f"urn:carto:request:{request_id}" if request_id else None,
        }
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", PROBLEM_CONTENT_TYPE.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodyCapMiddleware:
    """413 when ``Content-Length`` exceeds the cap, or as soon as a streamed body does."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            await _refuse(scope, send, 413, "payload too large", "body exceeds the cap")
            return
        received = 0

        async def capped_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise Problem(413, "payload too large", "body exceeds the cap")
            return message

        await self.app(scope, capped_receive, send)


def bootstrap_bindings(ports: ApiPorts, tenant_id: str, group_roles: dict[str, str]) -> bool:
    """Seed ``role_bindings`` from ``api.bootstrap_group_roles`` when the tenant has none."""
    if not group_roles:
        return False
    bindings = [
        RoleBinding(
            id=f"bootstrap:{tenant_id}:{group}:{role}",
            tenant_id=tenant_id,
            group_name=group,
            role=Role(role),
        )
        for group, role in sorted(group_roles.items())
    ]
    seeded = ports.role_bindings.seed(bindings)
    if seeded:
        log.info("role bindings seeded from settings", bindings=len(bindings))
    return seeded


def create_app(
    settings: CoreSettings,
    ports: ApiPorts,
    *,
    clock: Callable[[], datetime] | None = None,
    oidc: OidcClient | None = None,
) -> FastAPI:
    """Build the application with its ports injected (tests pass the in-memory ones)."""
    now = clock if clock is not None else lambda: datetime.now(UTC)
    api_settings = settings.api
    metrics = ApiMetrics()
    bootstrap_bindings(ports, settings.tenant_id, api_settings.bootstrap_group_roles)
    context = ApiContext(
        settings=settings,
        ports=ports,
        clock=now,
        metrics=metrics,
        sessions=SessionManager(ports.sessions, api_settings, now),
        oidc=oidc
        if oidc is not None
        else OidcClient(api_settings.oidc, api_settings.redirect_url, clock=now),
        login_limiter=TokenBucketLimiter(api_settings.login_rate_per_minute_per_ip, now),
        user_limiter=TokenBucketLimiter(api_settings.request_rate_per_minute_per_user, now),
    )

    app = FastAPI(
        title="carto console API",
        version=API_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.context = context
    app.state.metrics = metrics
    install_problem_handlers(
        app, on_problem=lambda _status: metrics.inc("carto_api_problems_total")
    )

    api = APIRouter(prefix=API_PREFIX, dependencies=[Depends(gate)])
    api.include_router(routes_auth.router)
    api.include_router(routes_links.router)
    api.include_router(routes_entities.router)
    api.include_router(routes_inventory.router)
    api.include_router(routes_admin.router)

    @api.get("/healthz", **public_route())
    async def api_healthz() -> Response:
        """The container healthcheck's probe (``carto-core healthcheck --listener api``)."""
        return JSONResponse({"status": "ok"})

    app.include_router(api)

    @app.get("/healthz", **public_route())
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", **public_route())
    async def readyz() -> Response:
        ready = await run_in_threadpool(ports.ping_all)
        if ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/metrics", **public_route())
    async def metrics_text() -> Response:
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    # Innermost first: add_middleware wraps the stack, so the request id is outermost and
    # every response, the 403 of the role check and the 413 of the body cap included, carries
    # it and the headers.
    if api_settings.require_client_cert:
        app.add_middleware(PeerRoleMiddleware)
    app.add_middleware(BodyCapMiddleware, limit=api_settings.max_body_bytes)
    app.add_middleware(
        ResponseHeadersMiddleware, hsts=not api_settings.insecure_http, metrics=metrics
    )
    app.add_middleware(RequestIdMiddleware)
    return app
