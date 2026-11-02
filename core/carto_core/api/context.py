"""What every route handler of the console API shares, built once by
:func:`carto_core.api.app.create_app` and read through :func:`context_of`.

Also the one place a mutation writes its audit row (:func:`record_audit`, spec 2.3 invariant
6, 14.9): actor, action, target, ``before``/``after`` as metadata views only, request id, the
socket address and the user agent. ``before``/``after`` pass through
:func:`carto_core.audit.scrub` in the recorder as a backstop.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from carto_core.audit import AuditEntry

if TYPE_CHECKING:
    from carto_core.api.auth import Principal
    from carto_core.api.oidc import OidcClient
    from carto_core.api.ports import ApiPorts
    from carto_core.api.ratelimit import TokenBucketLimiter
    from carto_core.api.sessions import SessionManager
    from carto_core.settings import CoreSettings

__all__ = [
    "MAX_USER_AGENT_LEN",
    "METRIC_NAMES",
    "ApiContext",
    "ApiMetrics",
    "client_ip",
    "context_of",
    "record_audit",
    "user_agent_of",
]

MAX_USER_AGENT_LEN: Final = 512

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


def client_ip(request: Request) -> str | None:
    """The socket peer. ``X-Forwarded-For`` is not trusted in M2: the api listens behind the
    install's own nginx only (ADR 0052) and trusting a proxy header needs the proxy allowlist
    that arrives with the ingress work in M6."""
    return request.client.host if request.client else None


def context_of(request: Request) -> ApiContext:
    context = getattr(request.app.state, "context", None)
    if not isinstance(context, ApiContext):
        msg = "the application has no ApiContext"
        raise RuntimeError(msg)
    return context


async def record_audit(
    context: ApiContext,
    request: Request,
    principal: Principal,
    *,
    action: str,
    target: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    reason: str = "",
) -> str:
    """Append one audit row for a mutation the handler just made; returns its ``row_hash``."""
    entry = AuditEntry(
        at=context.clock(),
        actor=principal.subject,
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
    row_hash = await run_in_threadpool(context.ports.audit_recorder.record, entry)
    context.metrics.inc("carto_api_audit_rows_total")
    return row_hash
