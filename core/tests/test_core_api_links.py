"""The review queue API (spec 9.6, 12): queue order and cursors, ``ETag``/``If-Match`` on
decisions (428, 412), reject needs a reason (422), the state machine (409), ``review_links``
on decisions and ``view`` on reads, one audit row per decision with metadata only, and evidence
rendered from the record with unknown keys dropped and bad evidence degraded."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import (
    InMemoryAuditLog,
    InMemoryLinkPort,
    example_link,
    seeded_ports,
)
from carto_core.api.ports import ApiPorts, DecisionError
from carto_core.api.routes_links import DECISION_STATUS, decision_problem
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_schema.api import EvidenceView, LinkStatus, RejectReason, Role

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
LINKS = f"{API_PREFIX}/links"
JSON = {"content-type": "application/json"}


@dataclass
class World:
    client: TestClient
    app: FastAPI
    ports: ApiPorts

    @property
    def context(self) -> ApiContext:
        context = self.app.state.context
        assert isinstance(context, ApiContext)
        return context

    @property
    def links(self) -> InMemoryLinkPort:
        port = self.ports.links
        assert isinstance(port, InMemoryLinkPort)
        return port

    @property
    def audit(self) -> InMemoryAuditLog:
        log = self.ports.audit
        assert isinstance(log, InMemoryAuditLog)
        return log

    def login(self, *roles: Role, subject: str = "alice") -> dict[str, str]:
        issued = self.context.sessions.issue(
            tenant_id="default",
            subject=subject,
            display_name="Alice",
            roles=frozenset(roles),
            authenticated_at=NOW,
            ip="127.0.0.1",
            user_agent="pytest",
        )
        self.client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
        return {"X-CSRF-Token": issued.record.csrf_token, **JSON}


def _world() -> World:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    ports = seeded_ports()
    app = create_app(settings, ports, clock=lambda: NOW)
    return World(TestClient(app, base_url="https://testserver"), app, ports)


# --- reads -------------------------------------------------------------------------------------


def test_queue_lists_proposed_links_by_score_times_volume() -> None:
    world = _world()
    world.login(Role.BUSINESS_VIEWER)
    page = world.client.get(LINKS).json()
    assert [item["id"] for item in page["items"]] == ["lnk_01", "lnk_02"]
    assert page["next_cursor"] is None
    first = page["items"][0]
    assert first["status"] == "proposed" and first["etag"] == 'W/"1"'
    assert first["sentence"].startswith("98% of order ids")
    assert first["a"]["path"] == "order.id" and first["b"]["system_id"] == "wms"
    assert "ignored_by_the_contract" not in first["evidence"]
    assert first["evidence"]["n_match"] == 12_400
    assert world.client.get(LINKS, params={"status": "accepted"}).json()["items"] == []


def test_cursor_pagination_walks_the_queue() -> None:
    world = _world()
    world.login(Role.AUDITOR)
    first = world.client.get(LINKS, params={"limit": 1}).json()
    assert [item["id"] for item in first["items"]] == ["lnk_01"]
    assert first["next_cursor"] == "lnk_01"
    second = world.client.get(LINKS, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [item["id"] for item in second["items"]] == ["lnk_02"]
    assert second["next_cursor"] is None
    assert world.client.get(LINKS, params={"cursor": "unknown"}).json()["items"] == []
    assert world.client.get(LINKS, params={"limit": 0}).status_code == 422
    assert world.client.get(LINKS, params={"limit": 501}).status_code == 422
    assert world.client.get(LINKS, params={"status": "bogus"}).status_code == 422


def test_get_link_carries_the_etag() -> None:
    world = _world()
    world.login(Role.BUSINESS_VIEWER)
    response = world.client.get(f"{LINKS}/lnk_01")
    assert response.status_code == 200
    assert response.headers["etag"] == 'W/"1"'
    assert response.json()["etag"] == 'W/"1"'
    assert world.client.get(f"{LINKS}/lnk_99").status_code == 404
    assert world.client.get(f"{LINKS}/not%20an%20id").status_code == 422


def test_reads_need_a_session_and_view() -> None:
    world = _world()
    assert world.client.get(LINKS).status_code == 401
    world.login()
    assert world.client.get(LINKS).status_code == 403


def test_bad_evidence_degrades_to_zero_counts() -> None:
    world = _world()
    world.links.put("default", replace(example_link("lnk_03"), evidence={"n_a": -1}))
    world.login(Role.BUSINESS_VIEWER)
    response = world.client.get(f"{LINKS}/lnk_03")
    assert response.status_code == 200
    assert response.json()["evidence"] == EvidenceView(
        n_a=0, n_b=0, n_match=0, c_ab=0.0, c_ba=0.0
    ).model_dump(mode="json")


# --- decisions ---------------------------------------------------------------------------------


def test_accept_needs_if_match_and_the_current_version() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    missing = world.client.post(f"{LINKS}/lnk_01/accept", json={}, headers=headers)
    assert missing.status_code == 428
    stale = world.client.post(
        f"{LINKS}/lnk_01/accept", json={}, headers={**headers, "If-Match": 'W/"7"'}
    )
    assert stale.status_code == 412
    garbage = world.client.post(
        f"{LINKS}/lnk_01/accept", json={}, headers={**headers, "If-Match": "seven"}
    )
    assert garbage.status_code == 428
    ok = world.client.post(
        f"{LINKS}/lnk_01/accept", json={}, headers={**headers, "If-Match": 'W/"1"'}
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "accepted"
    assert body["version"] == 2 and body["etag"] == 'W/"2"'
    assert ok.headers["etag"] == 'W/"2"'
    assert body["decided_by"] == "alice"
    assert body["decided_at"] == "2026-10-18T12:00:00.000Z"
    assert world.links.decisions == [("lnk_01", "accept", "alice", 2)]
    star = world.client.post(
        f"{LINKS}/lnk_02/accept", json={"note": "fine"}, headers={**headers, "If-Match": "*"}
    )
    assert star.status_code == 200


def test_a_decision_writes_one_audit_row_with_metadata_only() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    response = world.client.post(
        f"{LINKS}/lnk_01/accept",
        json={"note": "looks right"},
        headers={**headers, "If-Match": 'W/"1"', "X-Request-Id": "req-accept-1"},
    )
    assert response.status_code == 200
    [entry] = world.audit.entries()
    assert entry.actor == "alice"
    assert entry.action == "link.accept"
    assert entry.target == "link:lnk_01"
    assert entry.before == {
        "status": "proposed",
        "role": "transaction",
        "score": 0.93,
        "version": 1,
    }
    assert entry.after == {"status": "accepted", "role": "transaction", "score": 0.93, "version": 2}
    assert entry.reason == "looks right"
    assert entry.request_id == "req-accept-1"
    assert entry.ip == "testclient"
    assert entry.user_agent is not None
    assert entry.at == NOW
    assert world.context.metrics.get("carto_api_audit_rows_total") == 1


def test_reject_needs_a_reason_and_is_final() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    no_reason = world.client.post(
        f"{LINKS}/lnk_01/reject", json={}, headers={**headers, "If-Match": 'W/"1"'}
    )
    assert no_reason.status_code == 422
    assert no_reason.json()["errors"] == [
        {"loc": ["body", "reason"], "msg": "a reason is required", "type": "missing"}
    ]
    assert world.audit.entries() == []
    rejected = world.client.post(
        f"{LINKS}/lnk_01/reject",
        json={"reason": "coincidence", "note": "same shape, different meaning"},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["reject_reason"] == "coincidence"
    assert world.audit.entries()[-1].reason == "coincidence"
    again = world.client.post(
        f"{LINKS}/lnk_01/accept", json={}, headers={**headers, "If-Match": 'W/"2"'}
    )
    assert again.status_code == 409
    assert again.json()["title"] == "conflict"
    assert world.client.get(LINKS, params={"status": "rejected"}).json()["items"][0]["id"] == (
        "lnk_01"
    )


def test_association_keeps_the_link_and_changes_its_role() -> None:
    world = _world()
    headers = world.login(Role.ADMIN)
    response = world.client.post(
        f"{LINKS}/lnk_01/association", json={}, headers={**headers, "If-Match": 'W/"1"'}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    assert response.json()["role"] == "association"
    [entry] = world.audit.entries()
    assert entry.action == "link.association"
    assert entry.after is not None and entry.after["role"] == "association"


def test_decisions_need_review_links() -> None:
    world = _world()
    for role in (Role.BUSINESS_VIEWER, Role.AUDITOR):
        headers = world.login(role)
        response = world.client.post(
            f"{LINKS}/lnk_01/accept", json={}, headers={**headers, "If-Match": 'W/"1"'}
        )
        assert response.status_code == 403, role
    assert world.audit.entries() == []
    assert world.links.get("default", "lnk_01").status is LinkStatus.PROPOSED  # type: ignore[union-attr]


def test_unknown_link_and_bad_bodies() -> None:
    world = _world()
    headers = world.login(Role.INTEGRATION_ENGINEER)
    assert (
        world.client.post(
            f"{LINKS}/lnk_99/accept", json={}, headers={**headers, "If-Match": 'W/"1"'}
        ).status_code
        == 404
    )
    too_long = world.client.post(
        f"{LINKS}/lnk_01/accept",
        json={"note": "x" * 600},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert too_long.status_code == 422


def test_the_port_catches_the_race_the_header_cannot() -> None:
    world = _world()
    port = world.links
    try:
        port.accept(
            "default",
            "lnk_01",
            actor="bob",
            expected_version=3,
            reason=None,
            note="",
            at=NOW,
        )
    except DecisionError as exc:
        assert exc.kind == "stale_version"
        assert decision_problem(exc).status == 412
    else:  # pragma: no cover
        raise AssertionError("a stale version must be refused")
    for kind, status in DECISION_STATUS.items():
        assert decision_problem(DecisionError(kind)).status == status  # type: ignore[arg-type]
    try:
        port.reject(
            "default", "lnk_01", actor="bob", expected_version=1, reason=None, note="", at=NOW
        )
    except DecisionError as error:
        assert error.kind == "reason_required"
    assert (
        port.reject(
            "default",
            "lnk_01",
            actor="bob",
            expected_version=1,
            reason=RejectReason.WRONG_DIRECTION,
            note="",
            at=NOW,
        ).status
        is LinkStatus.REJECTED
    )
