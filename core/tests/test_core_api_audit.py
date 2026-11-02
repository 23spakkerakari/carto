"""``GET /audit`` (spec 14.2 "View audit log", 14.9) and the in-memory audit log: admin and
auditor read it newest first with cursors, nobody else does; rows chain by
:func:`carto_core.audit.chain_hash` and verify; ``before``/``after`` are scrubbed of keys that
name secrets; ``at`` is hashed in UTC."""

from __future__ import annotations

import warnings
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import InMemoryAuditLog, seeded_ports
from carto_core.api.ports import ApiPorts
from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash, verify_chain
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_schema.api import Role

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
AUDIT = f"{API_PREFIX}/audit"
JSON = {"content-type": "application/json"}


def _world() -> tuple[TestClient, ApiPorts, ApiContext]:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    ports = seeded_ports()
    app = create_app(settings, ports, clock=lambda: NOW)
    context = app.state.context
    assert isinstance(context, ApiContext)
    return TestClient(app, base_url="https://testserver"), ports, context


def _login(
    client: TestClient, context: ApiContext, *roles: Role, subject: str = "alice"
) -> dict[str, str]:
    issued = context.sessions.issue(
        tenant_id="default",
        subject=subject,
        display_name="Alice",
        roles=frozenset(roles),
        authenticated_at=NOW,
        ip="127.0.0.1",
        user_agent="pytest",
    )
    client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
    return {"X-CSRF-Token": issued.record.csrf_token, **JSON}


def _mutate_twice(client: TestClient, context: ApiContext) -> None:
    headers = _login(client, context, Role.INTEGRATION_ENGINEER, subject="eng")
    assert (
        client.post(
            f"{API_PREFIX}/links/lnk_01/accept", json={}, headers={**headers, "If-Match": 'W/"1"'}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"{API_PREFIX}/entities/ent_01",
            json={"name": "Order"},
            headers={**headers, "If-Match": 'W/"1"'},
        ).status_code
        == 200
    )


def test_audit_lists_newest_first_for_admin_and_auditor() -> None:
    client, ports, context = _world()
    _mutate_twice(client, context)
    for role in (Role.ADMIN, Role.AUDITOR):
        _login(client, context, role, subject="reader")
        page = client.get(AUDIT).json()
        assert [row["action"] for row in page["items"]] == ["entity.patch", "link.accept"], role
    row = page["items"][1]
    assert row["actor"] == "eng" and row["target"] == "link:lnk_01"
    assert row["before"] == {
        "status": "proposed",
        "role": "transaction",
        "score": 0.93,
        "version": 1,
    }
    assert row["request_id"]
    assert len(row["row_hash"]) == 64
    log = ports.audit
    assert isinstance(log, InMemoryAuditLog)
    assert row["row_hash"] == log.rows[0][3]
    assert log.latest_hash("default") == page["items"][0]["row_hash"]


def test_audit_pages_by_cursor() -> None:
    client, _ports, context = _world()
    _mutate_twice(client, context)
    _login(client, context, Role.AUDITOR, subject="reader")
    first = client.get(AUDIT, params={"limit": 1}).json()
    assert first["items"][0]["action"] == "entity.patch"
    assert first["next_cursor"] == "2"
    second = client.get(AUDIT, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert second["items"][0]["action"] == "link.accept"
    assert second["next_cursor"] is None


def test_audit_needs_view_audit() -> None:
    client, _ports, context = _world()
    assert client.get(AUDIT).status_code == 401
    for role in (Role.INTEGRATION_ENGINEER, Role.BUSINESS_VIEWER):
        _login(client, context, role)
        assert client.get(AUDIT).status_code == 403, role


def test_in_memory_log_chains_like_the_table_and_verifies() -> None:
    log = InMemoryAuditLog()
    assert log.latest_hash("default") == GENESIS_HASH
    first = AuditEntry(
        at=NOW,
        actor="a",
        action="link.accept",
        target="link:1",
        before={"status": "proposed"},
        after={"status": "accepted", "api_key": "drop me", "Secret_Thing": 1},
        reason="",
        request_id="r1",
        ip="127.0.0.1",
        user_agent="t",
    )
    hash_one = log.record(first)
    recorded = log.entries()[0]
    assert recorded.after == {"status": "accepted"}, "secret-named keys are scrubbed"
    assert hash_one == chain_hash(GENESIS_HASH, recorded.content())
    second = AuditEntry(
        at=NOW.astimezone(timezone(timedelta(hours=2))),
        actor="a",
        action="link.reject",
        target="link:2",
        before=None,
        after=None,
        reason="coincidence",
        request_id=None,
        ip=None,
        user_agent=None,
    )
    hash_two = log.record(second)
    assert log.entries()[1].at == NOW and log.entries()[1].at.tzinfo is UTC
    assert hash_two == chain_hash(hash_one, log.entries()[1].content())
    assert log.latest_hash("default") == hash_two
    assert verify_chain(log.chain()) is None
    other = replace(first, tenant_id="other")
    assert log.record(other) == chain_hash(GENESIS_HASH, log.entries("other")[0].content())
    assert verify_chain(log.chain("other")) is None
    tampered = [
        (rid, prev, row_hash, {**content, "actor": "mallory"})
        for rid, prev, row_hash, content in log.chain()
    ]
    broken = verify_chain(tampered)
    assert broken is not None and broken.row_id == 1
