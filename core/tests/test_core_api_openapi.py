"""The committed OpenAPI document (plan M2 "Interfaces fixed"): it matches what the application
renders byte for byte, every operation under ``/api/v1`` names its permission
(``x-carto-permission``), only the auth routes are public or merely authenticated (deny by
default, spec 14.2), ``reveal`` is required by nothing, and the root routes are the three
health routes."""

from __future__ import annotations

import json
from typing import Any

from carto_core.api.app import API_PREFIX
from carto_core.api.openapi import OPENAPI_FILE, build_document, render_openapi
from carto_core.api.sessions import AUTHENTICATED, PERMISSION_EXTENSION, PUBLIC
from carto_schema.api import API_VERSION, Permission

AUTH_PATHS = {
    f"{API_PREFIX}/healthz": PUBLIC,
    f"{API_PREFIX}/auth/login": PUBLIC,
    f"{API_PREFIX}/auth/callback": PUBLIC,
    f"{API_PREFIX}/auth/logout": AUTHENTICATED,
    f"{API_PREFIX}/me": AUTHENTICATED,
}
ROOT_PATHS = {"/healthz", "/readyz", "/metrics"}
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def _operations(document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (path, method, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    ]


def test_committed_document_matches_the_application() -> None:
    assert OPENAPI_FILE.is_file(), "run: carto-core openapi --out core/openapi/openapi.v1.json"
    committed = OPENAPI_FILE.read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    rendered = render_openapi()
    assert rendered == committed, "regenerate core/openapi/openapi.v1.json"
    assert rendered.endswith("\n") and "  " in rendered
    assert json.loads(rendered) == build_document()


def test_rendering_is_deterministic() -> None:
    assert render_openapi() == render_openapi()


def test_document_metadata() -> None:
    document = json.loads(render_openapi())
    assert document["openapi"].startswith("3.1")
    assert document["info"]["version"] == API_VERSION
    assert document["servers"] == [{"url": "/"}]


def test_every_operation_declares_its_permission_and_denies_by_default() -> None:
    document = json.loads(render_openapi())
    permissions = {permission.value for permission in Permission}
    seen_paths: set[str] = set()
    for path, method, operation in _operations(document):
        label = operation.get(PERMISSION_EXTENSION)
        assert label, f"{method.upper()} {path} declares no permission"
        seen_paths.add(path)
        if path in ROOT_PATHS:
            assert label == PUBLIC, (method, path)
            continue
        assert path.startswith(API_PREFIX), path
        if path in AUTH_PATHS:
            assert label == AUTH_PATHS[path], (method, path)
            continue
        assert label in permissions, f"{method.upper()} {path} uses {label!r}"
        assert label != Permission.REVEAL.value, "nothing in M2 is gated on reveal"
        if method in ("post", "patch", "put", "delete"):
            assert label == Permission.REVIEW_LINKS.value, (method, path)
    assert seen_paths >= ROOT_PATHS
    assert set(AUTH_PATHS) <= seen_paths


def test_the_document_covers_the_spec_12_m2_endpoints() -> None:
    document = json.loads(render_openapi())
    operations = {(method.upper(), path) for path, method, _ in _operations(document)}
    expected = {
        ("GET", "/auth/login"),
        ("GET", "/auth/callback"),
        ("POST", "/auth/logout"),
        ("GET", "/me"),
        ("GET", "/links"),
        ("GET", "/links/{link_id}"),
        ("POST", "/links/{link_id}/accept"),
        ("POST", "/links/{link_id}/reject"),
        ("POST", "/links/{link_id}/association"),
        ("GET", "/entities"),
        ("GET", "/entities/{entity_id}"),
        ("PATCH", "/entities/{entity_id}"),
        ("GET", "/systems"),
        ("GET", "/systems/{system_id}"),
        ("PATCH", "/systems/{system_id}"),
        ("GET", "/sources"),
        ("GET", "/sources/{source_id}"),
        ("GET", "/sources/{source_id}/health"),
        ("GET", "/fields"),
        ("GET", "/fields/{field_id}"),
        ("GET", "/audit"),
    }
    assert {(method, f"{API_PREFIX}{path}") for method, path in expected} <= operations
    assert "reveal" not in json.dumps(document["paths"]).lower()
