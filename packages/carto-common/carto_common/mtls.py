"""Service roles on internal mutual TLS (spec 14.4; ADR 0031).

A certificate from the install CA names its role in one URI subject alternative name,
``urn:carto:role:<role>`` (:func:`role_uri`). The listeners read the role of the connection's
verified certificate (:func:`peer_role_h11_protocol` puts it into the request scope's ``state``)
and every route accepts only the roles it lists; a certificate with no role name, several, or
an unknown one has no role. The routes' lists live in each app (``carto_edge.gateway.app``,
``carto_core.ingest.app``); this module holds what both share. :class:`ActAsRole` is for
in-process tests, which have no TLS connection.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    import asyncio

    from uvicorn.protocols.http.h11_impl import H11Protocol

__all__ = [
    "ANY_ROLE",
    "PEER_ROLE_STATE_KEY",
    "ROLES",
    "ROLE_URI_PREFIX",
    "ActAsRole",
    "peer_role",
    "peer_role_h11_protocol",
    "role_allowed",
    "role_from_peercert",
    "role_uri",
]

ROLE_URI_PREFIX: Final = "urn:carto:role:"
ROLES: Final = frozenset(
    {"edge-gateway", "ingest-api", "otel-collector", "collector", "proxy", "api", "web"}
)
"""Every role a certificate can carry (ADR 0031)."""
ANY_ROLE: Final = "*"
"""A route marked with it accepts any certificate the install CA signed (health, metrics)."""
PEER_ROLE_STATE_KEY: Final = "carto_peer_role"


def role_uri(role: str) -> str:
    """The URI SAN that names ``role``; raises ``ValueError`` for an unknown role."""
    if role not in ROLES:
        msg = f"unknown role {role!r}"
        raise ValueError(msg)
    return ROLE_URI_PREFIX + role


def role_from_peercert(certificate: Mapping[str, Any] | None) -> str | None:
    """The role of a verified peer certificate as ``ssl.SSLSocket.getpeercert()`` returns it:
    exactly one known ``urn:carto:role:`` URI, else ``None``."""
    if not certificate:
        return None
    names = certificate.get("subjectAltName", ())
    roles = [
        value[len(ROLE_URI_PREFIX) :]
        for kind, value in names
        if kind == "URI" and isinstance(value, str) and value.startswith(ROLE_URI_PREFIX)
    ]
    if len(roles) != 1 or roles[0] not in ROLES:
        return None
    return roles[0]


def peer_role(scope: Mapping[str, Any]) -> str | None:
    """The role the listener recorded for the request's connection, if any."""
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    role = state.get(PEER_ROLE_STATE_KEY)
    return role if isinstance(role, str) and role in ROLES else None


def role_allowed(
    table: Mapping[tuple[str, str], frozenset[str]],
    method: str,
    route: str | None,
    role: str | None,
) -> bool:
    """Whether ``role`` may call ``method route`` under ``table`` (``(method, route template)``
    to the roles it accepts, :data:`ANY_ROLE` for any certificate of the install CA). A route
    missing from the table is refused."""
    if route is None:
        return False
    allowed = table.get((method, route))
    if allowed is None:
        return False
    return ANY_ROLE in allowed or (role is not None and role in allowed)


def peer_role_h11_protocol() -> type[H11Protocol]:
    """uvicorn's h11 protocol, recording the verified client certificate's role in every
    request's scope ``state`` (uvicorn does not hand the certificate to the app). Pass it as
    ``uvicorn.Config(http=...)``; it relies on ``H11Protocol.app_state``, which the listeners'
    real-server tests pin."""
    from uvicorn.protocols.http.h11_impl import H11Protocol  # noqa: PLC0415

    class PeerRoleH11Protocol(H11Protocol):
        def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
            super().connection_made(transport)
            ssl_object = transport.get_extra_info("ssl_object")
            certificate = ssl_object.getpeercert() if ssl_object is not None else None
            # Per connection: this protocol instance serves one connection only.
            self.app_state = {
                **self.app_state,
                PEER_ROLE_STATE_KEY: role_from_peercert(certificate),
            }

    return PeerRoleH11Protocol


class ActAsRole:
    """Tests only: an ASGI wrapper that records ``role`` for every request, as the listener
    does for a real connection. ``role`` may be changed between requests."""

    def __init__(self, app: Any, role: str | None) -> None:
        self.app = app
        self.role = role

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in {"http", "websocket"}:
            scope = dict(scope)
            scope["state"] = {**scope.get("state", {}), PEER_ROLE_STATE_KEY: self.role}
        await self.app(scope, receive, send)
