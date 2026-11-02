"""The console API application (spec 12, 14.7, 16): health and metrics at the root, no
interactive documentation, RFC 7807 problems with the request id on every error path, the
body cap, JSON-only mutations, request ids echoed or generated, and readiness that pings every
port."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

with warnings.catch_warnings():
    # starlette 1.7 deprecates httpx (vs httpx2) under its TestClient at import time.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_common.ids import ULID_PATTERN
from carto_common.mtls import ActAsRole
from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import InMemoryLinkPort, seeded_ports
from carto_core.api.ports import ApiPorts
from carto_core.api.problems import PROBLEM_CONTENT_TYPE
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_schema.api import Role

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
KIB = 1024
JSON = {"content-type": "application/json"}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    client: TestClient
    app: FastAPI
    ports: ApiPorts
    clock: Clock

    @property
    def context(self) -> ApiContext:
        context = self.app.state.context
        assert isinstance(context, ApiContext)
        return context

    def login(self, *roles: Role, subject: str = "alice") -> dict[str, str]:
        issued = self.context.sessions.issue(
            tenant_id="default",
            subject=subject,
            display_name="Alice",
            roles=frozenset(roles),
            authenticated_at=self.clock.now,
            ip="127.0.0.1",
            user_agent="pytest",
        )
        self.client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
        return {"X-CSRF-Token": issued.record.csrf_token, **JSON}


def _world(**api: object) -> World:
    clock = Clock()
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True, **api))  # type: ignore[arg-type]
    ports = seeded_ports()
    app = create_app(settings, ports, clock=clock)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    return World(client, app, ports, clock)


def _assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["title"]
    assert str(body["instance"]).startswith("urn:carto:request:")
    return body


# --- health, readiness, metrics ---------------------------------------------------------------


def test_health_routes_sit_at_the_root_without_auth() -> None:
    world = _world()
    assert world.client.get("/healthz").json() == {"status": "ok"}
    assert world.client.get("/readyz").json() == {"status": "ready"}
    metrics = world.client.get("/metrics")
    assert metrics.status_code == 200
    assert "carto_api_requests_total" in metrics.text
    assert world.client.get(f"{API_PREFIX}/healthz").json() == {"status": "ok"}


def test_readyz_fails_when_a_port_does_not_answer() -> None:
    world = _world()
    links = world.ports.links
    assert isinstance(links, InMemoryLinkPort)
    links.ready = False
    assert world.client.get("/readyz").status_code == 503
    assert world.client.get("/readyz").json() == {"status": "unavailable"}


def test_no_interactive_docs_on_the_listener() -> None:
    world = _world()
    for path in ("/docs", "/redoc", "/openapi.json", f"{API_PREFIX}/openapi.json"):
        assert world.client.get(path).status_code == 404, path


def test_metrics_count_requests_and_problems() -> None:
    world = _world()
    world.client.get(f"{API_PREFIX}/links")  # 401: a request and a problem
    world.client.get("/healthz")  # not under the prefix
    metrics = world.context.metrics
    assert metrics.get("carto_api_requests_total") == 1
    assert metrics.get("carto_api_problems_total") == 1


# --- problems ----------------------------------------------------------------------------------


def test_request_id_is_echoed_when_well_formed_and_generated_otherwise() -> None:
    world = _world()
    echoed = world.client.get("/healthz", headers={"X-Request-Id": "req-abc.1"})
    assert echoed.headers["x-request-id"] == "req-abc.1"
    generated = world.client.get("/healthz", headers={"X-Request-Id": "bad id with spaces"})
    assert ULID_PATTERN.fullmatch(generated.headers["x-request-id"])


def test_errors_are_problem_documents_with_the_request_id() -> None:
    world = _world()
    response = world.client.get(f"{API_PREFIX}/nope", headers={"X-Request-Id": "req-404"})
    body = _assert_problem(response, 404)
    assert body["instance"] == "urn:carto:request:req-404"
    assert response.headers["x-request-id"] == "req-404"
    _assert_problem(world.client.delete(f"{API_PREFIX}/links"), 405)
    _assert_problem(world.client.get(f"{API_PREFIX}/links"), 401)


def test_validation_problems_carry_locations_but_never_the_input() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    response = world.client.post(
        f"{API_PREFIX}/links/lnk_01/accept",
        content=b'{"reason": "PLANTED-MARKER-7731", "PLANTEDKEY": 1}',
        headers={**headers, "If-Match": 'W/"1"'},
    )
    body = _assert_problem(response, 422)
    text = response.text
    assert "PLANTED-MARKER-7731" not in text
    assert "PLANTEDKEY" not in text
    locations = [tuple(error["loc"]) for error in body["errors"]]
    assert ("body", "reason") in locations
    assert ("body", "<unknown key>") in locations


def test_invalid_json_is_a_400_without_the_body() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    response = world.client.post(
        f"{API_PREFIX}/links/lnk_01/accept",
        content=b"{not json MARKER",
        headers={**headers, "If-Match": 'W/"1"'},
    )
    _assert_problem(response, 400)
    assert "MARKER" not in response.text


def test_unhandled_errors_are_a_500_problem_without_detail() -> None:
    world = _world()
    world.login(Role.BUSINESS_VIEWER)
    links = world.ports.links
    assert isinstance(links, InMemoryLinkPort)

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "secret-ish detail MARKER"
        raise RuntimeError(msg)

    links.list = boom  # type: ignore[method-assign, assignment]
    client = TestClient(world.app, base_url="https://testserver", raise_server_exceptions=False)
    client.cookies = world.client.cookies
    response = client.get(f"{API_PREFIX}/links")
    body = _assert_problem(response, 500)
    assert body["detail"] == ""
    assert "MARKER" not in response.text


# --- body cap and content type ----------------------------------------------------------------


def test_body_over_the_cap_is_refused_with_413() -> None:
    world = _world(max_body_bytes=16 * KIB)
    headers = world.login(Role.INTEGRATION_ENGINEER)
    big = b'{"note": "' + b"x" * (17 * KIB) + b'"}'
    response = world.client.post(
        f"{API_PREFIX}/links/lnk_01/accept", content=big, headers={**headers, "If-Match": 'W/"1"'}
    )
    body = _assert_problem(response, 413)
    assert response.headers["x-request-id"]
    assert body["detail"] == "body exceeds the cap"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_mutations_must_be_json() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    response = world.client.post(
        f"{API_PREFIX}/links/lnk_01/accept",
        content=b"reason=coincidence",
        headers={**headers, "content-type": "application/x-www-form-urlencoded"},
    )
    _assert_problem(response, 415)
    no_type = world.client.post(
        f"{API_PREFIX}/links/lnk_01/accept",
        content=b"{}",
        headers={k: v for k, v in headers.items() if k != "content-type"},
    )
    _assert_problem(no_type, 415)


def test_logout_without_a_body_needs_no_content_type() -> None:
    world = _world()
    headers = world.login(Role.BUSINESS_VIEWER)
    response = world.client.post(
        f"{API_PREFIX}/auth/logout",
        headers={k: v for k, v in headers.items() if k != "content-type"},
    )
    assert response.status_code == 204


def test_problem_detail_never_contains_a_path_value() -> None:
    world = _world()
    world.login(Role.BUSINESS_VIEWER)
    response = world.client.get(f"{API_PREFIX}/links/MARKER%20VALUE")
    assert response.status_code in (404, 422)
    assert not re.search(r"MARKER", response.text)


# --- mutual TLS from the web proxy (ADR 0052) -------------------------------------------------


def _mtls_app(tmp_path: Path) -> FastAPI:
    files = {}
    for name in ("api.crt", "api.key", "ca.crt"):
        (tmp_path / name).write_text("not read by create_app\n", encoding="utf-8")
        files[name] = tmp_path / name
    settings = CoreSettings(
        _env_file=None,
        api=ApiSettings(
            tls_cert_file=files["api.crt"],
            tls_key_file=files["api.key"],
            client_ca_file=files["ca.crt"],
            require_client_cert=True,
        ),
    )
    return create_app(settings, seeded_ports(), clock=lambda: NOW)


def test_under_mutual_tls_only_the_web_and_api_roles_reach_the_console_api(
    tmp_path: Path,
) -> None:
    app = _mtls_app(tmp_path)
    for role in ("web", "api"):
        client = TestClient(ActAsRole(app, role), base_url="https://testserver")
        assert client.get(f"{API_PREFIX}/healthz").status_code == 200, role
        assert client.get(f"{API_PREFIX}/links").status_code == 401, role
    stranger = TestClient(ActAsRole(app, "edge-gateway"), base_url="https://testserver")
    refused = stranger.get(f"{API_PREFIX}/links", headers={"X-Request-Id": "req-role"})
    body = _assert_problem(refused, 403)
    assert body["instance"] == "urn:carto:request:req-role"
    assert refused.headers["x-content-type-options"] == "nosniff"
    assert stranger.get(f"{API_PREFIX}/me").status_code == 403
    nobody = TestClient(ActAsRole(app, None), base_url="https://testserver")
    assert nobody.get(f"{API_PREFIX}/links").status_code == 403


def test_health_paths_take_any_certificate_of_the_install_ca(tmp_path: Path) -> None:
    app = _mtls_app(tmp_path)
    client = TestClient(ActAsRole(app, "edge-gateway"), base_url="https://testserver")
    for path in ("/healthz", "/readyz", "/metrics", f"{API_PREFIX}/healthz"):
        assert client.get(path).status_code == 200, path


def test_without_require_client_cert_no_role_is_checked() -> None:
    world = _world()
    client = TestClient(ActAsRole(world.app, None), base_url="https://testserver")
    assert client.get(f"{API_PREFIX}/links").status_code == 401
