"""Entities (spec 9.6, 12): list and get with ``ETag``; ``PATCH`` confirms the name under
``review_links`` with ``If-Match`` and one audit row; a description-only patch leaves the
suggestion unconfirmed."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import InMemoryAuditLog, seeded_ports
from carto_core.api.ports import ApiPorts
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_schema.api import Role

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
ENTITIES = f"{API_PREFIX}/entities"
JSON = {"content-type": "application/json"}


def _world() -> tuple[TestClient, ApiPorts, ApiContext]:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    ports = seeded_ports()
    app = create_app(settings, ports, clock=lambda: NOW)
    context = app.state.context
    assert isinstance(context, ApiContext)
    return TestClient(app, base_url="https://testserver"), ports, context


def _login(client: TestClient, context: ApiContext, *roles: Role) -> dict[str, str]:
    issued = context.sessions.issue(
        tenant_id="default",
        subject="alice",
        display_name="Alice",
        roles=frozenset(roles),
        authenticated_at=NOW,
        ip="127.0.0.1",
        user_agent="pytest",
    )
    client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
    return {"X-CSRF-Token": issued.record.csrf_token, **JSON}


def _audit(ports: ApiPorts) -> InMemoryAuditLog:
    log = ports.audit
    assert isinstance(log, InMemoryAuditLog)
    return log


def test_list_and_get_entities() -> None:
    client, _ports, context = _world()
    _login(client, context, Role.BUSINESS_VIEWER)
    page = client.get(ENTITIES).json()
    assert [item["id"] for item in page["items"]] == ["ent_01"]
    entity = page["items"][0]
    assert entity["name"] == "Order" and entity["name_confirmed"] is False
    assert entity["suggested_name"] == "Order"
    assert [row["system_id"] for row in entity["fields"]] == ["shop", "wms"]
    assert entity["link_ids"] == ["lnk_01"] and entity["system_count"] == 2
    one = client.get(f"{ENTITIES}/ent_01")
    assert one.headers["etag"] == 'W/"1"'
    assert client.get(f"{ENTITIES}/ent_99").status_code == 404
    assert client.get(ENTITIES, params={"limit": 1}).json()["next_cursor"] is None
    assert client.get(ENTITIES).status_code == 200
    client.cookies.clear()
    assert client.get(ENTITIES).status_code == 401


def test_patch_confirms_the_name_and_is_audited() -> None:
    client, ports, context = _world()
    headers = _login(client, context, Role.INTEGRATION_ENGINEER)
    assert (
        client.patch(
            f"{ENTITIES}/ent_01", json={"name": "Customer order"}, headers=headers
        ).status_code
        == 428
    )
    response = client.patch(
        f"{ENTITIES}/ent_01",
        json={"name": "Customer order"},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "Customer order"
    assert body["name_confirmed"] is True
    assert body["etag"] == 'W/"2"' and response.headers["etag"] == 'W/"2"'
    assert body["updated_at"] == "2026-10-18T12:00:00.000Z"
    [entry] = _audit(ports).entries()
    assert entry.action == "entity.patch" and entry.target == "entity:ent_01"
    assert entry.before == {
        "name": "Order",
        "name_confirmed": False,
        "description": "",
        "version": 1,
    }
    assert entry.after == {
        "name": "Customer order",
        "name_confirmed": True,
        "description": "",
        "version": 2,
    }
    stale = client.patch(
        f"{ENTITIES}/ent_01", json={"name": "Again"}, headers={**headers, "If-Match": 'W/"1"'}
    )
    assert stale.status_code == 412


def test_description_only_patch_leaves_the_name_unconfirmed() -> None:
    client, _ports, context = _world()
    headers = _login(client, context, Role.ADMIN)
    response = client.patch(
        f"{ENTITIES}/ent_01",
        json={"description": "One purchase from the shop to delivery"},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert response.status_code == 200
    assert response.json()["name_confirmed"] is False
    assert response.json()["description"].startswith("One purchase")


def test_patch_needs_review_links_and_a_valid_body() -> None:
    client, ports, context = _world()
    viewer = _login(client, context, Role.BUSINESS_VIEWER)
    forbidden = client.patch(
        f"{ENTITIES}/ent_01", json={"name": "x"}, headers={**viewer, "If-Match": 'W/"1"'}
    )
    assert forbidden.status_code == 403
    engineer = _login(client, context, Role.INTEGRATION_ENGINEER)
    empty_name = client.patch(
        f"{ENTITIES}/ent_01", json={"name": ""}, headers={**engineer, "If-Match": 'W/"1"'}
    )
    assert empty_name.status_code == 422
    unknown = client.patch(
        f"{ENTITIES}/ent_01", json={"colour": "red"}, headers={**engineer, "If-Match": 'W/"1"'}
    )
    assert unknown.status_code == 422
    assert "colour" not in unknown.text
    missing = client.patch(
        f"{ENTITIES}/ent_99", json={"name": "x"}, headers={**engineer, "If-Match": 'W/"1"'}
    )
    assert missing.status_code == 404
    assert _audit(ports).entries() == []
