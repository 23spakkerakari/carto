"""What every route handler of the console API shares, built once by
:func:`carto_core.api.app.create_app` and read through :func:`context_of`.

Also the one place an audit row is shaped (spec 2.3 invariant 6, 14.9): :func:`audit_entry`
builds it from the request (actor, action, target, ``before``/``after`` as metadata views
only, request id, client address and user agent) and :func:`record_audit` appends it for a
mutation that has no port transaction of its own (login, logout, a refused login). The
decision routes hand the entry builder to their port instead, which appends the row inside
the transaction of the state change (ADR 0070). ``before``/``after`` pass through
:func:`carto_core.audit.scrub` in the recorder as a backstop.

The client address (:func:`client_ip`) is the socket peer unless the connection presented the
install's ``web`` certificate, the role of the console's own nginx (ADR 0052, 0070): then it
is the last hop of ``X-Forwarded-For``, the address nginx itself appended, validated as an IP
literal. Any other caller's header is ignored, so nobody can pick the bucket of the login
limiter or the address the audit log records.
"""

from __future__ import annotations

import ipaddress
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from carto_common.mtls import peer_role
from carto_core.audit import ANONYMOUS_ACTOR, AuditEntry

if TYPE_CHECKING:
    from carto_core.api.auth import Principal
    from carto_core.api.oidc import OidcClient
    from carto_core.api.ports import ApiPorts
    from carto_core.api.ratelimit import TokenBucketLimiter
    from carto_core.api.sessions import SessionManager
    from carto_core.settings import CoreSettings

__all__ = [
    "MAX_FORWARDED_FOR_LEN",
    "MAX_USER_AGENT_LEN",
    "METRIC_NAMES",
    "PROXY_ROLE",
    "ApiContext",
    "ApiMetrics",
    "audit_entry",
    "client_ip",
    "context_of",
    "forwarded_client",
    "record_audit",
    "user_agent_of",
]

MAX_USER_AGENT_LEN: Final = 512
MAX_FORWARDED_FOR_LEN: Final = 256
"""An ``X-Forwarded-For`` longer than this is ignored (a handful of hops at most)."""
PROXY_ROLE: Final = "web"
"""The certificate role whose ``X-Forwarded-For`` is trusted: the install's own nginx."""

METRIC_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("carto_api_requests_total", "Requests answered under /api/v1."),
    ("carto_api_problems_total", "Requests answered with a problem (4xx or 5xx)."),
    ("carto_api_logins_total", "Successful OIDC logins."),
    ("carto_api_login_failures_total", "OIDC callbacks refused (state, token or claims)."),
    ("carto_api_rate_limited_total", "Requests answered 429."),
    ("carto_api_csrf_refused_total", "Mutations refused for a missing or wrong CSRF token."),
    ("carto_api_decisions_total", "Link decisions and entity or system patches written."),
    ("carto_api_audit_rows_total", "Audit rows appended by the API."),
)


class ApiMetrics:
    """Counters in the Prometheus text format (spec 16), the shape of
    :class:`carto_core.ingest.app.IngestMetrics` with the API's names. ``/metrics`` is served
    on the listener itself and the web nginx never proxies it (spec 12 "internal network only").
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {name: 0 for name, _help in METRIC_NAMES}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
        lines: list[str] = []
        for name, help_text in METRIC_NAMES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {values[name]}")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class ApiContext:
    settings: CoreSettings
    ports: ApiPorts
    clock: Callable[[], datetime]
    metrics: ApiMetrics
    sessions: SessionManager
    oidc: OidcClient
    login_limiter: TokenBucketLimiter
    user_limiter: TokenBucketLimiter

    @property
    def tenant_id(self) -> str:
        return self.settings.tenant_id

    def page_limit(self, limit: int | None) -> int:
        return limit if limit is not None else self.settings.api.page_size_default


def user_agent_of(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:MAX_USER_AGENT_LEN] if value else None


def forwarded_client(header: str | None) -> str | None:
    """The last hop of an ``X-Forwarded-For`` value as a normalised IP literal, or ``None``
    when the header is missing, over :data:`MAX_FORWARDED_FOR_LEN` or its last element is not
    an address. The last element is the one the trusted proxy appended (nginx's
    ``$proxy_add_x_forwarded_for``); anything before it came from the client."""
    if not header or len(header) > MAX_FORWARDED_FOR_LEN:
        return None
    last = header.rsplit(",", 1)[-1].strip()
    if not last:
        return None
    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        return None


def client_ip(request: Request) -> str | None:
    """The socket peer, or, when the connection presented the :data:`PROXY_ROLE` certificate
    (the web nginx under mutual TLS, ADR 0052), the browser address that proxy appended to
    ``X-Forwarded-For`` (module docstring). A malformed header falls back to the peer."""
    peer = request.client.host if request.client else None
    if peer_role(request.scope) != PROXY_ROLE:
        return peer
    forwarded = forwarded_client(request.headers.get("x-forwarded-for"))
    return forwarded if forwarded is not None else peer


def context_of(request: Request) -> ApiContext:
    context = getattr(request.app.state, "context", None)
    if not isinstance(context, ApiContext):
        msg = "the application has no ApiContext"
        raise RuntimeError(msg)
    return context


def audit_entry(
    context: ApiContext,
    request: Request,
    principal: Principal | None,
    *,
    action: str,
    target: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    reason: str = "",
) -> AuditEntry:
    """One audit row's content for a request: the actor is the principal's subject, or
    :data:`~carto_core.audit.ANONYMOUS_ACTOR` when nobody is signed in (a refused login)."""
    subject = principal.subject if principal is not None else ""
    return AuditEntry(
        at=context.clock(),
        actor=subject or ANONYMOUS_ACTOR,
        action=action,
        target=target,
        before=before,
        after=after,
        reason=reason,
        request_id=str(getattr(request.state, "request_id", "")) or None,
        ip=client_ip(request),
        user_agent=user_agent_of(request),
        tenant_id=context.tenant_id,
    )


async def record_audit(
    context: ApiContext,
    request: Request,
    principal: Principal | None,
    *,
    action: str,
    target: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    reason: str = "",
) -> str:
    """Append one audit row for a mutation that has no port transaction (module docstring);
    returns its ``row_hash``."""
    entry = audit_entry(
        context,
        request,
        principal,
        action=action,
        target=target,
        before=before,
        after=after,
        reason=reason,
    )
    row_hash = await run_in_threadpool(context.ports.audit_recorder.record, entry)
    context.metrics.inc("carto_api_audit_rows_total")
    return row_hash
