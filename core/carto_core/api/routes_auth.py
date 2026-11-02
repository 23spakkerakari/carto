"""``/auth/login``, ``/auth/callback``, ``/auth/logout`` and ``/me`` (spec 12, 14.1, 14.2).

Login starts the OIDC flow (:mod:`carto_core.api.oidc`): the state, nonce and PKCE verifier
are kept server-side in a :class:`~carto_core.api.ports.LoginAttemptStore` keyed by the
SHA-256 of ``state`` for ten minutes and consumed once by the callback. The same response sets
the ``__Host-carto_login`` cookie (:data:`LOGIN_COOKIE_NAME`: 32 random bytes, ``Secure``,
``HttpOnly``, ``SameSite=Lax``, path ``/``, the attempt's lifetime) and the attempt stores its
SHA-256, so the callback completes only in the browser that started the login: a callback URL
an attacker started and handed to a victim is refused as a ``state`` failure (login CSRF,
session swapping; ADR 0070). The callback requires that cookie, compares its hash in constant
time, exchanges the code, verifies the ID token, maps the groups claim to roles through
``role_bindings`` (exact group names; a group with no binding grants nothing, so such a user
signs in and sees ``403`` everywhere, which ``GET /me`` makes visible), upserts the user,
issues a new session (never adopting an id from the request), clears the login cookie and
redirects to ``/``.

Rate limits (spec 14.7 "stricter on login"; :class:`carto_core.api.ratelimit.LoginLimiter`;
ADR 0070 section 5): a login start is limited per client address (the browser's, behind the
web proxy; :func:`carto_core.api.context.client_ip`) with a budget sized for a whole office
behind one NAT or VPN egress address, and under a ceiling across every address that bounds
the pending attempts in the store. The callback is limited per login attempt, not per
address: the attempt is single-use, so a callback that carries its attempt's cookie needs no
address budget and one address's junk cannot lock the office out. Only a callback without a
valid cookie (no state or code, an unknown or expired state, a missing or foreign cookie) takes
from a small per-address budget, checked before the refusal is audited (so junk bounds the
audit log too) and, when the request carries no cookie at all, before the store is read. The
attempt a state names is popped in every case, so a state stays single-use (ADR 0070 section
3). Every ``429`` carries ``Retry-After``.

Logout deletes the session and is CSRF-checked like every mutation. Login, logout and every
refused login are audited (spec 14.9); the refused one as ``auth.login_failed`` by the
anonymous actor with the step as target and the detail as reason.

Failures answer a problem that names the step (``state``, ``exchange``, ``token``), never the
code, the state, the cookie or a claim; the log line and the audit row carry the same, and
every failure clears the login cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, Annotated, Final

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from carto_common.logging import get_logger
from carto_core.api.auth import Principal
from carto_core.api.context import client_ip, context_of, record_audit, user_agent_of
from carto_core.api.oidc import OidcError
from carto_core.api.ports import LoginAttempt, RoleBinding, UserRecord
from carto_core.api.problems import Problem
from carto_core.api.sessions import Guard, principal_of, public_route
from carto_core.settings import SESSION_COOKIE_NAME
from carto_schema.api import MeView, Role

if TYPE_CHECKING:
    from carto_core.api.context import ApiContext
    from carto_core.settings import ApiSettings

__all__ = ["LOGIN_ATTEMPT_TTL", "LOGIN_COOKIE_NAME", "roles_for_groups", "router"]

log = get_logger(component="api")
router = APIRouter(tags=["auth"])

LOGIN_ATTEMPT_TTL: Final = timedelta(minutes=10)
LOGIN_COOKIE_NAME: Final = "__Host-carto_login"
"""Binds a pending login to the browser that started it (module docstring, ADR 0070)."""
_LOGIN_COOKIE_BYTES: Final = 32
_MAX_LOGIN_COOKIE_LEN: Final = 128
_NO_STORE: Final = {"Cache-Control": "no-store"}
_AUTHENTICATED = Guard(None)


def roles_for_groups(bindings: Sequence[RoleBinding], groups: Iterable[str]) -> frozenset[Role]:
    wanted = set(groups)
    return frozenset(binding.role for binding in bindings if binding.group_name in wanted)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state_hash(state: str) -> str:
    return _sha256(state)


def _binding_matches(cookie_value: str | None, binding_hash: str) -> bool:
    """Whether the login cookie the browser sent is the one the attempt was bound to; a missing
    or oversized cookie, or an attempt without a binding, never matches."""
    if not cookie_value or len(cookie_value) > _MAX_LOGIN_COOKIE_LEN or not binding_hash:
        return False
    return hmac.compare_digest(_sha256(cookie_value), binding_hash)


def _set_login_cookie(response: Response, settings: ApiSettings, value: str) -> None:
    response.set_cookie(
        LOGIN_COOKIE_NAME,
        value,
        max_age=int(LOGIN_ATTEMPT_TTL.total_seconds()),
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clear_login_cookie(response: Response, settings: ApiSettings) -> None:
    response.set_cookie(
        LOGIN_COOKIE_NAME,
        "",
        max_age=0,
        expires=0,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _clearing_header(settings: ApiSettings) -> dict[str, str]:
    """The ``Set-Cookie`` that clears the login cookie, for a problem response."""
    scratch = Response()
    _clear_login_cookie(scratch, settings)
    return {"Set-Cookie": scratch.headers["set-cookie"]}


def _too_many(context: ApiContext, wait: int, counter: str) -> Problem:
    context.metrics.inc("carto_api_rate_limited_total")
    context.metrics.inc(counter)
    return Problem(
        429, "too many requests", "too many login attempts", headers={"Retry-After": str(wait)}
    )


def _start_limited(request: Request) -> None:
    """The per-address budget and the ceiling of a login start (module docstring)."""
    context = context_of(request)
    wait = context.login_limiter.check_start(client_ip(request) or "unknown")
    if wait is not None:
        raise _too_many(context, wait, "carto_api_login_starts_rate_limited_total")


def _junk_callback_limited(request: Request) -> None:
    """The per-address budget of a callback that carries no valid login cookie (module
    docstring); runs before the refusal is audited, so junk bounds the audit log too."""
    context = context_of(request)
    wait = context.login_limiter.check_callback(client_ip(request) or "unknown")
    if wait is not None:
        raise _too_many(context, wait, "carto_api_login_callbacks_rate_limited_total")


async def _junk_callback(request: Request, *, limited: bool, detail: str) -> Problem:
    """Refuse a callback without a valid login cookie as a ``state`` failure, taking from the
    junk budget first unless ``limited`` says it already was."""
    if not limited:
        _junk_callback_limited(request)
    return await _login_failed(request, "state", detail)


async def _login_failed(request: Request, step: str, detail: str) -> Problem:
    """Count, log and audit a refused login (``auth.login_failed`` by the anonymous actor,
    spec 14.9); the problem names the step only and clears the login cookie."""
    context = context_of(request)
    context.metrics.inc("carto_api_login_failures_total")
    log.warning("login refused", step=step, detail=detail)
    await record_audit(
        context,
        request,
        None,
        action="auth.login_failed",
        target=step,
        before=None,
        after=None,
        reason=detail,
    )
    return Problem(
        400,
        "login failed",
        f"the login could not be completed ({step})",
        headers=_clearing_header(context.settings.api),
    )


@router.get("/auth/login", status_code=302, response_class=RedirectResponse, **public_route())
async def login(request: Request) -> Response:
    """Send the browser to the identity provider, bound to it by the login cookie."""
    context = context_of(request)
    _start_limited(request)
    if not context.oidc.configured:
        raise Problem(503, "login unavailable", "no identity provider is configured")
    try:
        start = await run_in_threadpool(context.oidc.begin)
    except OidcError as exc:
        log.warning("login cannot start", step=exc.kind, detail=exc.detail)
        raise Problem(503, "login unavailable", "the identity provider is unreachable") from exc
    now = context.clock()
    binding = secrets.token_urlsafe(_LOGIN_COOKIE_BYTES)
    attempt = LoginAttempt(
        state_hash=_state_hash(start.state),
        tenant_id=context.tenant_id,
        code_verifier=start.code_verifier,
        nonce=start.nonce,
        created_at=now,
        expires_at=now + LOGIN_ATTEMPT_TTL,
        binding_hash=_sha256(binding),
    )
    await run_in_threadpool(context.ports.login_attempts.put, attempt)
    response = RedirectResponse(start.authorization_url, status_code=302, headers=_NO_STORE)
    _set_login_cookie(response, context.settings.api, binding)
    return response


@router.get("/auth/callback", status_code=302, response_class=RedirectResponse, **public_route())
async def callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    """Finish the flow: cookie to attempt, code to ID token to session."""
    context = context_of(request)
    if error is not None or not code or not state:
        raise await _junk_callback(
            request, limited=False, detail="provider returned an error or no code"
        )
    cookie = request.cookies.get(LOGIN_COOKIE_NAME)
    unbound = not cookie or len(cookie) > _MAX_LOGIN_COOKIE_LEN
    if unbound:
        # Nothing this callback could be bound to: it takes from the junk budget before the
        # store is read; the state it names is still consumed below.
        _junk_callback_limited(request)
    attempt = await run_in_threadpool(context.ports.login_attempts.pop, _state_hash(state))
    now = context.clock()
    if attempt is None or attempt.expires_at <= now or attempt.tenant_id != context.tenant_id:
        raise await _junk_callback(request, limited=unbound, detail="unknown or expired state")
    if not _binding_matches(cookie, attempt.binding_hash):
        raise await _junk_callback(
            request,
            limited=unbound,
            detail="login cookie missing or from another login attempt",
        )
    # From here the callback is the single-use attempt's own: no address budget applies.
    try:
        identity = await run_in_threadpool(
            context.oidc.finish, code, attempt.code_verifier, attempt.nonce
        )
    except OidcError as exc:
        if exc.kind == "not_configured":
            raise Problem(
                503, "login unavailable", "the identity provider is not configured"
            ) from exc
        raise await _login_failed(request, exc.kind, exc.detail) from exc
    bindings = await run_in_threadpool(context.ports.role_bindings.list, context.tenant_id)
    roles = roles_for_groups(bindings, identity.groups)
    user = await run_in_threadpool(
        context.ports.users.upsert,
        UserRecord(
            tenant_id=context.tenant_id,
            subject=identity.subject,
            display_name=identity.display_name,
            email=identity.email,
            last_login_at=now,
        ),
    )
    issued = await run_in_threadpool(
        lambda: context.sessions.issue(
            tenant_id=context.tenant_id,
            subject=identity.subject,
            display_name=identity.display_name,
            roles=roles,
            authenticated_at=identity.auth_time or now,
            ip=client_ip(request),
            user_agent=user_agent_of(request),
            reveal_granted=user.reveal_granted,
        )
    )
    principal = principal_of(issued.record)
    await record_audit(
        context,
        request,
        principal,
        action="auth.login",
        target=identity.subject,
        before=None,
        after={"roles": sorted(role.value for role in roles)},
    )
    context.metrics.inc("carto_api_logins_total")
    log.info("login", subject=identity.subject, roles=len(roles))
    response = RedirectResponse("/", status_code=302, headers=_NO_STORE)
    context.sessions.set_cookie(response, issued.cookie_value)
    _clear_login_cookie(response, context.settings.api)
    return response


@router.post("/auth/logout", status_code=204, **_AUTHENTICATED.route())
async def logout(request: Request, principal: Principal = _AUTHENTICATED.principal) -> Response:
    context = context_of(request)
    await run_in_threadpool(context.sessions.revoke, request.cookies.get(SESSION_COOKIE_NAME))
    await record_audit(
        context,
        request,
        principal,
        action="auth.logout",
        target=principal.subject,
        before=None,
        after=None,
    )
    response = Response(status_code=204, headers=_NO_STORE)
    context.sessions.clear_cookie(response)
    return response


@router.get("/me", response_model=MeView, **_AUTHENTICATED.route())
async def me(request: Request, principal: Principal = _AUTHENTICATED.principal) -> MeView:
    context = context_of(request)
    record = request.state.session
    return MeView(
        subject=principal.subject,
        display_name=principal.display_name,
        roles=sorted(principal.roles, key=lambda role: role.value),
        permissions=sorted(principal.permissions, key=lambda permission: permission.value),
        csrf_token=record.csrf_token,
        session_expires_at=context.sessions.expires_at(record, context.clock()),
    )
