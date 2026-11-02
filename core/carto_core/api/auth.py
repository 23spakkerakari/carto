"""Authorization: roles, permissions and the one dependency handlers use (spec 14.2).

Deny by default. :data:`ROLE_PERMISSIONS` is the spec 14.2 table; ``reveal`` is in no role and
is granted separately (``Principal.reveal_granted``), never implied. Handlers declare exactly
one :class:`~carto_schema.api.Permission` through :func:`require`; nothing else in the API
checks roles. The authorization matrix test calls every endpoint as every role and compares with
:data:`ROLE_PERMISSIONS`, so a route that forgets ``require`` fails the test (it answers a role
the table denies).

Session lookup, OIDC and CSRF live in :mod:`carto_core.api.sessions` (package E); this module
is the seam they populate: ``request.state.principal``.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from fastapi import Request

from carto_schema.api import Permission, Role

__all__ = [
    "ANONYMOUS",
    "ROLE_PERMISSIONS",
    "AuthorizationError",
    "Principal",
    "allowed",
    "permissions_of",
    "require",
]

ROLE_PERMISSIONS: Final[dict[Role, frozenset[Permission]]] = {
    Role.ADMIN: frozenset(
        {
            Permission.VIEW,
            Permission.REVIEW_LINKS,
            Permission.MANAGE_EXPECTATIONS,
            Permission.ACK_ALERTS,
            Permission.MANAGE_SOURCES,
            Permission.MANAGE_ADMIN,
            Permission.VIEW_AUDIT,
        }
    ),
    Role.INTEGRATION_ENGINEER: frozenset(
        {
            Permission.VIEW,
            Permission.REVIEW_LINKS,
            Permission.MANAGE_EXPECTATIONS,
            Permission.ACK_ALERTS,
        }
    ),
    Role.BUSINESS_VIEWER: frozenset({Permission.VIEW, Permission.ACK_ALERTS}),
    Role.AUDITOR: frozenset({Permission.VIEW, Permission.VIEW_AUDIT}),
}
"""Spec 14.2, row by row. ``business_viewer`` rows are "scoped to their entities"; scoped
bindings arrive in M6 (plan M2 "Deferred"), so in M2 the role sees what every viewer sees.
``reveal`` appears in no set: it is a separate grant."""


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling: the subject of the session (or API key, M6) and what they may do."""

    subject: str
    display_name: str
    roles: frozenset[Role]
    reveal_granted: bool = False
    session_id: str | None = None
    authenticated_at: datetime | None = None
    """When the identity provider authenticated them; step-up compares it (spec 14.1)."""
    scopes: frozenset[str] = field(default_factory=frozenset)
    """Entity or system scopes of the bindings; empty means unscoped (M6 enforces)."""

    @property
    def permissions(self) -> frozenset[Permission]:
        return permissions_of(self.roles, reveal_granted=self.reveal_granted)

    def may(self, permission: Permission) -> bool:
        return permission in self.permissions


ANONYMOUS: Final = Principal(subject="", display_name="", roles=frozenset())


def permissions_of(
    roles: frozenset[Role], *, reveal_granted: bool = False
) -> frozenset[Permission]:
    granted: set[Permission] = set()
    for role in roles:
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    if reveal_granted:
        granted.add(Permission.REVEAL)
    return frozenset(granted)


def allowed(
    roles: frozenset[Role], permission: Permission, *, reveal_granted: bool = False
) -> bool:
    return permission in permissions_of(roles, reveal_granted=reveal_granted)


class AuthorizationError(Exception):
    """Raised by :func:`require`; the API layer renders it as a 401 or 403 problem."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def require(permission: Permission) -> Callable[[Request], Coroutine[Any, Any, Principal]]:
    """FastAPI dependency: the request's principal must hold ``permission``.

    An unauthenticated request (no principal on ``request.state``) is a 401; a principal without
    the permission is a 403. Returns the principal so the handler can audit it.
    """

    async def dependency(request: Request) -> Principal:
        principal = getattr(request.state, "principal", None)
        if not isinstance(principal, Principal) or not principal.subject:
            raise AuthorizationError(401, "sign in to use the console API")
        if not principal.may(permission):
            raise AuthorizationError(403, f"this account may not {permission.value}")
        return principal

    dependency.__name__ = f"require_{permission.value}"
    return dependency
