"""Spec 14.7 response headers on every answer of the console API, HSTS only on a TLS listener,
no ``Server`` header, ``Cache-Control: no-store``, and spec 2.3 invariant 7: a planted marker
in a request never reaches a response body or the logs."""

from __future__ import annotations

import io
import warnings
from datetime import UTC, datetime
from pathlib import Path

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_common.logging import configure_logging
from carto_core.api.app import API_PREFIX, CONTENT_SECURITY_POLICY, SECURITY_HEADERS, create_app
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import seeded_ports
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_schema.api import Role

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
MARKER = "PLANTED-LEAK-MARKER-4418"


def _client(settings: CoreSettings) -> tuple[TestClient, ApiContext]:
    app = create_app(settings, seeded_ports(), clock=lambda: NOW)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    context = app.state.context
    assert isinstance(context, ApiContext)
    return client, context


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
    return {"X-CSRF-Token": issued.record.csrf_token, "content-type": "application/json"}


@pytest.fixture
def tls_files(tmp_path: Path) -> tuple[Path, Path]:
    cert, key = tmp_path / "api.crt", tmp_path / "api.key"
    cert.write_text("not read by create_app\n", encoding="utf-8")
    key.write_text("not read by create_app\n", encoding="utf-8")
    return cert, key


def test_every_response_carries_the_security_headers() -> None:
    client, context = _client(CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True)))
    headers = _login(client, context, Role.BUSINESS_VIEWER)
    responses = [
        client.get("/healthz"),
        client.get(f"{API_PREFIX}/links"),
        client.get(f"{API_PREFIX}/does-not-exist"),
        client.post(f"{API_PREFIX}/links/lnk_01/accept", content=b"{}", headers=headers),
    ]
    for response in responses:
        for name, value in SECURITY_HEADERS:
            assert response.headers.get(name) == value, (response.url, name)
        assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert response.headers["cache-control"] == "no-store"
        assert "server" not in response.headers
        assert "strict-transport-security" not in response.headers


def test_hsts_is_sent_by_a_tls_listener(tls_files: tuple[Path, Path]) -> None:
    cert, key = tls_files
    client, _ = _client(
        CoreSettings(_env_file=None, api=ApiSettings(tls_cert_file=cert, tls_key_file=key))
    )
    response = client.get("/healthz")
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_a_planted_marker_never_reaches_a_response_or_the_logs() -> None:
    buffer = io.StringIO()
    configure_logging("api-test", level="debug", stream=buffer)
    client, context = _client(CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True)))
    headers = _login(client, context, Role.INTEGRATION_ENGINEER)
    probes = [
        client.post(
            f"{API_PREFIX}/links/lnk_01/accept",
            content=f'{{"reason": "{MARKER}"}}'.encode(),
            headers={**headers, "If-Match": 'W/"1"'},
        ),
        client.post(
            f"{API_PREFIX}/links/lnk_01/accept",
            content=f'{{"{MARKER}": true}}'.encode(),
            headers={**headers, "If-Match": 'W/"1"'},
        ),
        client.post(
            f"{API_PREFIX}/links/lnk_01/reject",
            content=b'{"reason": "coincidence", "note": "%s"}' % MARKER.encode(),
            headers={**headers, "If-Match": 'W/"1"', "X-CSRF-Token": MARKER},
        ),
        client.get(f"{API_PREFIX}/links", params={"cursor": MARKER}),
        client.get(f"{API_PREFIX}/links", params={"status": MARKER}),
        client.get(f"{API_PREFIX}/fields/{MARKER}"),
        client.get(f"{API_PREFIX}/auth/callback", params={"code": MARKER, "state": MARKER}),
        client.get(f"{API_PREFIX}/links", headers={"User-Agent": MARKER}),
    ]
    assert [probe.status_code for probe in probes[:3]] == [422, 422, 403]
    for probe in probes:
        assert MARKER not in probe.text, probe.url
    logs = buffer.getvalue()
    assert "login refused" in logs or "csrf" in logs, "something was logged"
    assert MARKER not in logs
