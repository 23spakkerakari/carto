"""Server-side sessions and the per-request gate of the console API (spec 14.1, 14.2, 14.7).

A session is a random 32-byte id in the ``__Host-carto_session`` cookie (``HttpOnly``,
``Secure``, ``SameSite=Lax``, path ``/``); the store holds only its SHA-256, the subject, the
roles the ID token's groups mapped to at login, the CSRF token and three times: when the
identity provider authenticated the user (``auth_time``, for step-up), when the session was
last seen (idle timeout, 30 minutes) and when it ends whatever happens (absolute timeout, 12
hours). Roles are fixed at login; a binding change takes effect at the next login (M6 adds
re-evaluation). Login always issues a new id, never adopts one from the request (fixation).

:func:`gate` is the one dependency every route under ``/api/v1`` runs before anything else,
in this order: a mutation must send JSON (415); the cookie resolves to a principal or to
:data:`~carto_core.api.auth.ANONYMOUS` on ``request.state.principal`` (an expired session is
deleted and treated as absent); an authenticated caller is rate-limited per subject (429); and
a mutation by an authenticated caller must carry the session's CSRF token (403). Authorization
itself is :func:`carto_core.api.auth.require` through :class:`Guard`, which also writes the
permission into the OpenAPI document (``x-carto-permission``) for the matrix test.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from carto_common.logging import get_logger
from carto_core.api.auth import ANONYMOUS, AuthorizationError, Principal, require
from carto_core.api.context import context_of
from carto_core.api.csrf import CSRF_HEADER, UNSAFE_METHODS, csrf_matches, new_csrf_token
from carto_core.api.ports import SessionRecord, SessionStore
from carto_core.api.problems import Problem
from carto_core.settings import SESSION_COOKIE_NAME

if TYPE_CHECKING:
    from starlette.responses import Response

    from carto_core.settings import ApiSettings
    from carto_schema.api import Permission, Role

__all__ = [
    "AUTHENTICATED",
    "PERMISSION_EXTENSION",
    "PUBLIC",
    "Guard",
    "SessionManager",
    "gate",
    "hash_session_id",
    "principal_of",
    "public_route",
]

log = get_logger(component="api")

PERMISSION_EXTENSION: Final = "x-carto-permission"
"""OpenAPI operation extension: a :class:`~carto_schema.api.Permission` value, ``authenticated``
(any signed-in principal) or ``public`` (login, callback, health)."""
AUTHENTICATED: Final = "authenticated"
PUBLIC: Final = "public"
_SESSION_ID_BYTES: Final = 32
_TOUCH_EVERY: Final = timedelta(seconds=30)
"""Idle-window slides closer together than this are not written (one write per half minute
per session at most, not one per request)."""
_JSON_MEDIA_TYPE: Final = "application/json"


def hash_session_id(cookie_value: str) -> str:
    return hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IssuedSession:
    cookie_value: str
    record: SessionRecord


class SessionManager:
    """Issue, resolve and revoke sessions over a :class:`SessionStore`."""

    def __init__(
        self, store: SessionStore, settings: ApiSettings, clock: Callable[[], datetime]
    ) -> None:
        self._store = store
        self._settings = settings
        self._clock = clock
        self.idle = timedelta(minutes=settings.session_idle_minutes)
        self.absolute = timedelta(hours=settings.session_absolute_hours)

    @property
    def store(self) -> SessionStore:
        return self._store

    def issue(
        self,
        *,
        tenant_id: str,
        subject: str,
        display_name: str,
        roles: frozenset[Role],
        authenticated_at: datetime,
        ip: str | None,
        user_agent: str | None,
        reveal_granted: bool = False,
    ) -> IssuedSession:
        now = self._clock()
        cookie_value = secrets.token_urlsafe(_SESSION_ID_BYTES)
        record = SessionRecord(
            id=hash_session_id(cookie_value),
            tenant_id=tenant_id,
            subject=subject,
            csrf_token=new_csrf_token(),
            authenticated_at=authenticated_at,
            last_seen_at=now,
            expires_at=now + self.absolute,
            roles=tuple(sorted(roles, key=lambda role: role.value)),
            display_name=display_name,
            ip=ip,
            user_agent=user_agent,
            reveal_granted=reveal_granted,
        )
        self._store.create(record)
        return IssuedSession(cookie_value, record)

    def resolve(self, cookie_value: str | None) -> SessionRecord | None:
        """The live session for a cookie, with its idle window slid; ``None`` when there is no
        such session or it timed out (an expired row is deleted)."""
        if not cookie_value or len(cookie_value) > 128:
            return None
        session_id = hash_session_id(cookie_value)
        record = self._store.get(session_id)
        if record is None or not record.subject:
            return None
        now = self._clock()
        if now >= record.expires_at or now - record.last_seen_at > self.idle:
            self._store.delete(session_id)
            return None
        if now - record.last_seen_at >= _TOUCH_EVERY:
            self._store.touch(session_id, now)
        return record

    def revoke(self, cookie_value: str | None) -> None:
        if cookie_value:
            self._store.delete(hash_session_id(cookie_value))

    def expires_at(self, record: SessionRecord, now: datetime) -> datetime:
        """When the session ends if it stays idle from ``now``: the earlier of both deadlines."""
        return min(record.expires_at, max(record.last_seen_at, now) + self.idle)

    def set_cookie(self, response: Response, cookie_value: str) -> None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            cookie_value,
            max_age=int(self.absolute.total_seconds()),
            path="/",
            secure=self._settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )

    def clear_cookie(self, response: Response) -> None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            "",
            max_age=0,
            expires=0,
            path="/",
            secure=self._settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )


def principal_of(record: SessionRecord) -> Principal:
    return Principal(
        subject=record.subject,
        display_name=record.display_name,
        roles=frozenset(record.roles),
        reveal_granted=record.reveal_granted,
        session_id=record.id,
        authenticated_at=record.authenticated_at,
    )


def _require_json(request: Request) -> None:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    has_body = request.headers.get("content-length", "0") != "0" or (
        "transfer-encoding" in request.headers
    )
    if media_type == _JSON_MEDIA_TYPE:
        return
    if media_type or has_body:
        raise Problem(415, "unsupported media type", f"send {_JSON_MEDIA_TYPE}")


async def gate(request: Request) -> None:
    """The router-level dependency described in the module docstring."""
    context = context_of(request)
    if request.method in UNSAFE_METHODS:
        _require_json(request)
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    record = await run_in_threadpool(context.sessions.resolve, cookie_value)
    if record is None:
        request.state.principal = ANONYMOUS
        request.state.session = None
        return
    request.state.principal = principal_of(record)
    request.state.session = record
    wait = context.user_limiter.check(record.subject)
    if wait is not None:
        context.metrics.inc("carto_api_rate_limited_total")
        raise Problem(429, "too many requests", "slow down", headers={"Retry-After": str(wait)})
    if request.method in UNSAFE_METHODS and not csrf_matches(
        record.csrf_token, request.headers.get(CSRF_HEADER)
    ):
        context.metrics.inc("carto_api_csrf_refused_total")
        log.warning("csrf token missing or wrong", subject=record.subject, path=request.url.path)
        raise Problem(403, "forbidden", f"send the session's CSRF token in {CSRF_HEADER}")


async def _authenticated(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal) or not principal.subject:
        raise AuthorizationError(401, "sign in to use the console API")
    return principal


class Guard:
    """One permission per route: the dependency handlers take their principal from and the
    OpenAPI extension the matrix test reads. ``Guard(None)`` means any signed-in principal
    (``GET /me``, logout)."""

    def __init__(self, permission: Permission | None) -> None:
        self.permission = permission
        self.dependency: Callable[[Request], Coroutine[Any, Any, Principal]] = (
            require(permission) if permission is not None else _authenticated
        )
        self.principal: Any = Depends(self.dependency)

    @property
    def label(self) -> str:
        return self.permission.value if self.permission is not None else AUTHENTICATED

    def route(self) -> dict[str, Any]:
        """Keyword arguments for ``router.get(...)`` and friends."""
        return {"openapi_extra": {PERMISSION_EXTENSION: self.label}}


def public_route() -> dict[str, Any]:
    return {"openapi_extra": {PERMISSION_EXTENSION: PUBLIC}}
