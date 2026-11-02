"""``/auth/login``, ``/auth/callback``, ``/auth/logout`` and ``/me`` (spec 12, 14.1, 14.2).

Login starts the OIDC flow (:mod:`carto_core.api.oidc`): the state, nonce and PKCE verifier
are kept server-side in a :class:`~carto_core.api.ports.LoginAttemptStore` keyed by the
SHA-256 of ``state`` for ten minutes and consumed once by the callback. The callback exchanges
the code, verifies the ID token, maps the groups claim to roles through ``role_bindings``
(exact group names; a group with no binding grants nothing, so such a user signs in and sees
``403`` everywhere, which ``GET /me`` makes visible), upserts the user, issues a new session
(never adopting an id from the request) and redirects to ``/``. Both login routes are
rate-limited per socket address (spec 14.7 "stricter on login"). Logout deletes the session
and is CSRF-checked like every mutation. Login and logout are audited (spec 14.9).

Failures answer a problem that names the step (``state``, ``exchange``, ``token``), never the
code, the state or a claim; the log line carries the same.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import timedelta
from typing import Annotated, Final

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

__all__ = ["LOGIN_ATTEMPT_TTL", "roles_for_groups", "router"]

log = get_logger(component="api")
router = APIRouter(tags=["auth"])

LOGIN_ATTEMPT_TTL: Final = timedelta(minutes=10)
_NO_STORE: Final = {"Cache-Control": "no-store"}
_AUTHENTICATED = Guard(None)


def roles_for_groups(bindings: Sequence[RoleBinding], groups: Iterable[str]) -> frozenset[Role]:
    wanted = set(groups)
    return frozenset(binding.role for binding in bindings if binding.group_name in wanted)


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _login_limited(request: Request) -> None:
    context = context_of(request)
    wait = context.login_limiter.check(client_ip(request) or "unknown")
    if wait is not None:
        context.metrics.inc("carto_api_rate_limited_total")
        raise Problem(
            429, "too many requests", "too many login attempts", headers={"Retry-After": str(wait)}
        )


def _login_failed(request: Request, step: str, detail: str) -> Problem:
    context = context_of(request)
    context.metrics.inc("carto_api_login_failures_total")
    log.warning("login refused", step=step, detail=detail)
    return Problem(400, "login failed", f"the login could not be completed ({step})")


@router.get("/auth/login", status_code=302, response_class=RedirectResponse, **public_route())
async def login(request: Request) -> Response:
    """Send the browser to the identity provider."""
    context = context_of(request)
    _login_limited(request)
    if not context.oidc.configured:
        raise Problem(503, "login unavailable", "no identity provider is configured")
    try:
        start = await run_in_threadpool(context.oidc.begin)
    except OidcError as exc:
        log.warning("login cannot start", step=exc.kind, detail=exc.detail)
        raise Problem(503, "login unavailable", "the identity provider is unreachable") from exc
    now = context.clock()
    attempt = LoginAttempt(
        state_hash=_state_hash(start.state),
        tenant_id=context.tenant_id,
        code_verifier=start.code_verifier,
        nonce=start.nonce,
        created_at=now,
        expires_at=now + LOGIN_ATTEMPT_TTL,
    )
    await run_in_threadpool(context.ports.login_attempts.put, attempt)
    return RedirectResponse(start.authorization_url, status_code=302, headers=_NO_STORE)


@router.get("/auth/callback", status_code=302, response_class=RedirectResponse, **public_route())
async def callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    """Finish the flow: code to ID token to session."""
    context = context_of(request)
    _login_limited(request)
    if error is not None or not code or not state:
        raise _login_failed(request, "state", "provider returned an error or no code")
    attempt = await run_in_threadpool(context.ports.login_attempts.pop, _state_hash(state))
    now = context.clock()
    if attempt is None or attempt.expires_at <= now or attempt.tenant_id != context.tenant_id:
        raise _login_failed(request, "state", "unknown or expired state")
    try:
        identity = await run_in_threadpool(
            context.oidc.finish, code, attempt.code_verifier, attempt.nonce
        )
    except OidcError as exc:
        if exc.kind == "not_configured":
            raise Problem(
                503, "login unavailable", "the identity provider is not configured"
            ) from exc
        raise _login_failed(request, exc.kind, exc.detail) from exc
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
