"""Read-only inventory (ADR 0050; spec 12): systems with a ``review_links`` rename, sources
with their latest heartbeat and configuration without secrets, fields addressed by their
``field_ref`` with shapes and never values."""

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


# --- systems -----------------------------------------------------------------------------------


def test_systems_are_listed_and_fetched_with_etags() -> None:
    client, _ports, context = _world()
    _login(client, context, Role.BUSINESS_VIEWER)
    page = client.get(f"{API_PREFIX}/systems").json()
    assert [item["id"] for item in page["items"]] == ["shop", "wms"]
    assert page["items"][0]["name"] == "Shop" and page["items"][0]["source_count"] == 1
    one = client.get(f"{API_PREFIX}/systems/shop")
    assert one.status_code == 200 and one.headers["etag"] == 'W/"1"'
    assert client.get(f"{API_PREFIX}/systems/nope").status_code == 404
    assert client.get(f"{API_PREFIX}/systems/Not-Valid").status_code == 422


def test_renaming_a_system_is_review_links_and_audited() -> None:
    client, ports, context = _world()
    viewer = _login(client, context, Role.BUSINESS_VIEWER)
    assert (
        client.patch(
            f"{API_PREFIX}/systems/shop",
            json={"name": "x"},
            headers={**viewer, "If-Match": 'W/"1"'},
        ).status_code
        == 403
    )
    engineer = _login(client, context, Role.INTEGRATION_ENGINEER)
    assert (
        client.patch(f"{API_PREFIX}/systems/shop", json={"name": "x"}, headers=engineer).status_code
        == 428
    )
    response = client.patch(
        f"{API_PREFIX}/systems/shop",
        json={"name": "Web shop", "description": "Customer-facing shop"},
        headers={**engineer, "If-Match": 'W/"1"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Web shop"
    assert response.json()["etag"] == 'W/"2"'
    log = ports.audit
    assert isinstance(log, InMemoryAuditLog)
    [entry] = log.entries()
    assert entry.action == "system.patch" and entry.target == "system:shop"
    assert entry.after == {"name": "Web shop", "description": "Customer-facing shop", "version": 2}


# --- sources -----------------------------------------------------------------------------------


def test_sources_carry_configuration_without_secrets_and_their_health() -> None:
    client, _ports, context = _world()
    _login(client, context, Role.AUDITOR)
    page = client.get(f"{API_PREFIX}/sources").json()
    assert [item["id"] for item in page["items"]] == ["shop_logs", "wms_db"]
    shop_logs, wms_db = page["items"]
    assert shop_logs["config"] == {"path": "/var/log/shop/*.log", "format": "json"}
    assert shop_logs["secret_ref"] is None
    assert shop_logs["health"]["status"] == "ok"
    assert shop_logs["health"]["lag_seconds"] == 12.0
    assert shop_logs["last_test"] == "read-only verified"
    assert wms_db["health"] is None
    one = client.get(f"{API_PREFIX}/sources/shop_logs")
    assert one.status_code == 200 and one.json()["type"] == "file"
    assert client.get(f"{API_PREFIX}/sources/nope").status_code == 404


def test_source_health_is_404_before_the_first_heartbeat() -> None:
    client, _ports, context = _world()
    _login(client, context, Role.BUSINESS_VIEWER)
    health = client.get(f"{API_PREFIX}/sources/shop_logs/health")
    assert health.status_code == 200
    assert health.json()["received_at"] == "2026-10-17T09:00:00.000Z"
    assert client.get(f"{API_PREFIX}/sources/wms_db/health").status_code == 404
    assert client.get(f"{API_PREFIX}/sources/nope/health").status_code == 404


# --- fields ------------------------------------------------------------------------------------


def test_fields_list_filters_by_system_and_show_shapes_not_values() -> None:
    client, _ports, context = _world()
    _login(client, context, Role.BUSINESS_VIEWER)
    page = client.get(f"{API_PREFIX}/fields").json()
    assert [item["id"] for item in page["items"]] == [
        "shop/t-order-created/order.id",
        "wms/t-pick/orderRef",
    ]
    field = page["items"][0]
    assert field["field_class"] == "identifier" and field["policy"] == "tokenize"
    assert field["shapes"] == [
        {"shape": "A-999999", "share": 0.97},
        {"shape": "A-99999", "share": 0.03},
    ]
    assert field["forms"] == ["exact", "normalized"]
    assert field["quality"] == 0.91 and field["suggested_label"] == "Order number"
    filtered = client.get(f"{API_PREFIX}/fields", params={"system": "wms"}).json()
    assert [item["id"] for item in filtered["items"]] == ["wms/t-pick/orderRef"]
    assert client.get(f"{API_PREFIX}/fields", params={"system": "Bad System"}).status_code == 422


def test_a_field_is_fetched_by_its_field_ref_with_slashes() -> None:
    client, _ports, context = _world()
    _login(client, context, Role.BUSINESS_VIEWER)
    response = client.get(f"{API_PREFIX}/fields/shop/t-order-created/order.id")
    assert response.status_code == 200
    assert response.json()["path"] == "order.id"
    assert client.get(f"{API_PREFIX}/fields/shop/t-order-created/nope").status_code == 404
    assert client.get(f"{API_PREFIX}/fields/shop").status_code == 404


def test_inventory_needs_a_session_and_view() -> None:
    client, _ports, context = _world()
    for path in ("/systems", "/sources", "/fields", "/systems/shop", "/sources/shop_logs/health"):
        assert client.get(f"{API_PREFIX}{path}").status_code == 401, path
    _login(client, context)
    for path in ("/systems", "/sources", "/fields"):
        assert client.get(f"{API_PREFIX}{path}").status_code == 403, path
