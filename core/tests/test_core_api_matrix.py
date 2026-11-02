"""The authorization matrix (spec 14.2, 18.3; ADR 0052): every operation the committed OpenAPI
document lists under ``/api/v1`` is called unauthenticated and as each of the four roles, with
a session written through the production session manager into the in-memory store (no test
backdoor in the application). Unauthenticated is 401 (public routes aside); a role the
spec table denies is 403; a role it allows is neither; every allowed mutation writes exactly one
audit row and no read writes any; ``reveal`` appears nowhere."""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.auth import ROLE_PERMISSIONS
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import InMemoryAuditLog, seeded_ports
from carto_core.api.openapi import OPENAPI_FILE
from carto_core.api.sessions import AUTHENTICATED, PERMISSION_EXTENSION, PUBLIC
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_schema.api import Permission, Role

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MUTATING = {"post", "patch", "put", "delete"}
PATH_VALUES = {
    "link_id": "lnk_01",
    "entity_id": "ent_01",
    "system_id": "shop",
    "source_id": "shop_logs",
    "field_id": "shop/t-order-created/order.id",
}
BODIES: dict[str, dict[str, Any]] = {
    "/links/{link_id}/reject": {"reason": "coincidence"},
}
ROLES = [None, *list(Role)]


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    label: str

    @property
    def url(self) -> str:
        url = self.path
        for name, value in PATH_VALUES.items():
            url = url.replace("{" + name + "}", value)
        return url

    def __str__(self) -> str:
        return f"{self.method.upper()} {self.path}"


def _operations() -> list[Operation]:
    document = json.loads(OPENAPI_FILE.read_bytes().replace(b"\r\n", b"\n").decode("utf-8"))
    return sorted(
        (
            Operation(method, path, operation[PERMISSION_EXTENSION])
            for path, item in document["paths"].items()
            for method, operation in item.items()
            if path.startswith(API_PREFIX)
        ),
        key=lambda op: (op.path, op.method),
    )


OPERATIONS = _operations()


def _call(role: Role | None, operation: Operation) -> tuple[int, str, list[Any]]:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    ports = seeded_ports()
    app = create_app(settings, ports, clock=lambda: NOW)
    context = app.state.context
    assert isinstance(context, ApiContext)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    headers = {"If-Match": 'W/"1"', "content-type": "application/json"}
    if role is not None:
        issued = context.sessions.issue(
            tenant_id="default",
            subject=f"user-{role.value}",
            display_name=role.value,
            roles=frozenset({role}),
            authenticated_at=NOW,
            ip="127.0.0.1",
            user_agent="matrix",
        )
        client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
        headers["X-CSRF-Token"] = issued.record.csrf_token
    body = BODIES.get(operation.path.removeprefix(API_PREFIX), {})
    content = json.dumps(body).encode() if operation.method in MUTATING else None
    response = client.request(
        operation.method.upper(), operation.url, content=content, headers=headers
    )
    audit = ports.audit
    assert isinstance(audit, InMemoryAuditLog)
    return response.status_code, response.text, audit.entries()


def _expected(role: Role | None, label: str) -> str:
    """``public``, ``allowed`` or ``denied`` for this role and route label."""
    if label == PUBLIC:
        return "public"
    if role is None:
        return "unauthenticated"
    if label == AUTHENTICATED:
        return "allowed"
    return "allowed" if Permission(label) in ROLE_PERMISSIONS[role] else "denied"


@pytest.mark.parametrize("operation", OPERATIONS, ids=str)
@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.value if role else "anonymous")
def test_every_operation_as_every_role(role: Role | None, operation: Operation) -> None:
    status, text, entries = _call(role, operation)
    verdict = _expected(role, operation.label)
    if verdict == "unauthenticated":
        assert status == 401, (operation, role, status)
        assert entries == []
    elif verdict == "denied":
        assert status == 403, (operation, role, status)
        assert entries == []
    elif verdict == "public":
        assert status not in (401, 403), (operation, role, status)
    else:
        assert status not in (401, 403), (operation, role, status, text[:200])
        if operation.method in MUTATING:
            assert status < 400, (operation, role, status, text[:200])
            assert len(entries) == 1, (operation, role, [entry.action for entry in entries])
        else:
            assert entries == []
    assert "reveal" not in text.lower(), (operation, role)


def test_the_matrix_covers_every_role_permission_pair() -> None:
    labels = {operation.label for operation in OPERATIONS}
    assert Permission.VIEW.value in labels
    assert Permission.REVIEW_LINKS.value in labels
    assert Permission.VIEW_AUDIT.value in labels
    assert Permission.REVEAL.value not in labels
    assert all(Permission.REVEAL not in granted for granted in ROLE_PERMISSIONS.values())
    assert len(OPERATIONS) >= 21


def test_every_mutating_operation_is_review_links_or_logout() -> None:
    for operation in OPERATIONS:
        if operation.method in MUTATING:
            assert operation.label in (Permission.REVIEW_LINKS.value, AUTHENTICATED), operation
