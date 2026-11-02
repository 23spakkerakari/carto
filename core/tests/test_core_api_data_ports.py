"""The console API over the engine's stores (plan M2 wiring of packages A, C and E): a link the
review service proposed appears in ``GET /links`` with its sentence, ends and evidence; a POST
decision goes through the review service (decision row, one audit row from the route, 412 on a
stale ``If-Match``); the inventory routes show systems, sources and fields from the metadata
store; a family the builder made appears under ``GET /entities`` and ``PATCH`` confirms its
name. The in-memory stores carry the semantics; the PostgreSQL ports are served once per port
against migration 0004 in the integration part (skipped without Docker)."""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy
from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.context import ApiContext
from carto_core.api.data_ports import (
    DataPorts,
    FieldPortAdapter,
    assemble_data_ports,
    build_data_ports,
)
from carto_core.api.memory_ports import (
    InMemoryAuditLog,
    InMemoryLoginAttemptStore,
    InMemoryRoleBindingStore,
    InMemorySessionStore,
    InMemoryUserStore,
)
from carto_core.api.ports import MAX_CURSOR_LEN, ApiPorts, DecisionError
from carto_core.api.server import build_ports, data_ports
from carto_core.ingest.health import SourceHealthRecord
from carto_core.linker.families import FamilyBuilder
from carto_core.linker.model import Direction, LinkRole, LinkStatus, LinkType, PairFeatures
from carto_core.linker.repository import (
    InMemoryEntityRepository,
    InMemoryLinkRepository,
    PostgresEntityRepository,
    PostgresLinkRepository,
)
from carto_core.linker.review import ReviewService, ScoredLink, link_id_for
from carto_core.metadata import (
    FieldRecord,
    InMemoryMetadataStore,
    MetadataStore,
    PostgresMetadataStore,
    updated_at_version,
)
from carto_core.settings import SESSION_COOKIE_NAME, ApiSettings, CoreSettings
from carto_core.store.model import FieldForm
from carto_schema.api import RejectReason, Role
from carto_schema.inventory import EdgeInventory, InventorySource, InventorySystem

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TENANT = "default"
LINKS = f"{API_PREFIX}/links"
JSON = {"content-type": "application/json"}
ORDER_ID = FieldForm("sys_a/tpl_a/order_id", "raw")
ORDER_REF = FieldForm("sys_b/tpl_b/order_ref", "digits.0")
MERCHANT = FieldForm("sys_a/tpl_a/merchant_ref", "raw")
SHA = "b" * 64


class Clock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, **delta: int) -> datetime:
        self.at += timedelta(**delta)
        return self.at


def features(n_match: int = 570, c_ab: float = 0.95) -> PairFeatures:
    return PairFeatures(
        n_a=600,
        n_b=590,
        n_match=n_match,
        c_ab=c_ab,
        c_ba=0.97,
        lag_median=5400.0,
        lag_iqr=1800.0,
        lag_positive_share=0.99,
        fanout_ab_median=1.0,
        fanout_ab_p99=1.0,
        fanout_ba_median=1.0,
        fanout_ba_p99=1.0,
        concentration=0.002,
        chance_ratio=0.01,
        domain_size=10_000.0,
    )


def scored(
    a: FieldForm, b: FieldForm, *, score: float, n_match: int, **extra: object
) -> ScoredLink:
    return ScoredLink(
        a=a,
        b=b,
        link_type=LinkType.EXACT,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=score,
        features=features(n_match=n_match),
        affected_volume=n_match,
        extra={"shape_a": "9999", "shape_b": "9999", **extra},
    )


def field(ref: str, *, label: str | None = None, forms: tuple[str, ...] = ("raw",)) -> FieldRecord:
    system_id, template_id, path = ref.split("/", 2)
    return FieldRecord(
        field_ref=ref,
        system_id=system_id,
        template_id=template_id,
        path=path,
        field_class="identifier",
        policy="tokenize",
        pinned=False,
        forms=forms,
        count=600,
        shapes=(("9999", 0.98), ("99999", 0.02)),
        first_seen=NOW - timedelta(days=5),
        last_seen=NOW,
        distinct_estimate=590,
        null_rate=0.0,
        quality=0.9,
        is_candidate=True,
        label=label,
    )


def inventory() -> EdgeInventory:
    return EdgeInventory(
        schema_version="1",
        tenant_id=TENANT,
        edge_id="edge-gateway",
        sent_at=NOW - timedelta(hours=1),
        sources_file_sha256=SHA,
        systems=[
            InventorySystem(id="sys_a", name="Order system", owner_group="shop"),
            InventorySystem(id="sys_b", name="Warehouse", criticality="high"),
        ],
        sources=[
            InventorySource(
                id="src_a",
                system_id="sys_a",
                type="file",
                config={"path": "/var/log/shop/*.log"},
                backfill_days=7,
            ),
            InventorySource(
                id="src_a_db",
                system_id="sys_a",
                type="sql",
                config={"host": "db.internal"},
                secret_ref="vault://db/shop",  # noqa: S106 - a reference name, not a secret
            ),
            InventorySource(id="src_b", system_id="sys_b", type="otlp", enabled=False),
        ],
    )


def seed_metadata(
    metadata: MetadataStore, received_at: datetime = NOW - timedelta(hours=1)
) -> None:
    metadata.replace_inventory(inventory(), received_at)
    metadata.upsert_fields(
        TENANT,
        [
            field(ORDER_ID.field_ref, label="Order number"),
            field(ORDER_REF.field_ref, forms=("raw", "digits.0")),
            field(MERCHANT.field_ref),
        ],
    )


@dataclass
class World:
    client: TestClient
    app: FastAPI
    clock: Clock
    links: InMemoryLinkRepository
    entities: InMemoryEntityRepository
    metadata: InMemoryMetadataStore
    audit: InMemoryAuditLog
    review: ReviewService
    data: DataPorts

    @property
    def context(self) -> ApiContext:
        context = self.app.state.context
        assert isinstance(context, ApiContext)
        return context

    def login(self, *roles: Role, subject: str = "alice") -> dict[str, str]:
        issued = self.context.sessions.issue(
            tenant_id=TENANT,
            subject=subject,
            display_name="Alice",
            roles=frozenset(roles),
            authenticated_at=self.clock(),
            ip="127.0.0.1",
            user_agent="pytest",
        )
        self.client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
        return {"X-CSRF-Token": issued.record.csrf_token, **JSON}


def world() -> World:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    clock = Clock()
    links = InMemoryLinkRepository()
    entities = InMemoryEntityRepository()
    metadata = InMemoryMetadataStore()
    audit = InMemoryAuditLog()
    seed_metadata(metadata)
    # The linker's side: the review service of the worker proposes the pairs of a pass.
    review = ReviewService(links, audit, settings.linker, clock=clock)
    review.propose(
        TENANT,
        [
            scored(
                ORDER_ID,
                ORDER_REF,
                score=0.93,
                n_match=570,
                sentence="95% of Order system orders (order_id, 9999) match an order in Warehouse.",
            ),
            scored(MERCHANT, ORDER_ID, score=0.99, n_match=60),
        ],
        now=NOW - timedelta(minutes=30),
    )
    data = assemble_data_ports(
        links=links, entities=entities, metadata=metadata, settings=settings.linker, clock=clock
    )
    ports = ApiPorts(
        links=data.links,
        entities=data.entities,
        systems=data.systems,
        sources=data.sources,
        fields=data.fields,
        audit=audit,
        audit_recorder=audit,
        sessions=InMemorySessionStore(),
        login_attempts=InMemoryLoginAttemptStore(),
        users=InMemoryUserStore(),
        role_bindings=InMemoryRoleBindingStore(),
    )
    app = create_app(settings, ports, clock=clock)
    return World(
        TestClient(app, base_url="https://testserver"),
        app,
        clock,
        links,
        entities,
        metadata,
        audit,
        review,
        data,
    )


EXACT_ID = link_id_for(TENANT, ORDER_ID, ORDER_REF)
BRIDGE_ID = link_id_for(TENANT, MERCHANT, ORDER_ID)


# --- links ---------------------------------------------------------------------------------------


def test_the_queue_shows_the_proposed_links_with_their_sentence_and_ends() -> None:
    w = world()
    w.login(Role.BUSINESS_VIEWER)
    page = w.client.get(LINKS).json()
    assert [item["id"] for item in page["items"]] == [EXACT_ID, BRIDGE_ID], "score x volume"
    exact = page["items"][0]
    assert exact["sentence"].startswith("95% of Order system orders")
    assert exact["status"] == "proposed" and exact["etag"] == 'W/"1"'
    assert exact["a"] == {
        "field_id": ORDER_ID.field_ref,
        "system_id": "sys_a",
        "template_id": "tpl_a",
        "path": "order_id",
        "form": "raw",
        "label": "Order number",
        "top_shape": "9999",
    }
    assert exact["b"]["form"] == "digits.0" and exact["b"]["label"] is None
    assert exact["evidence"]["n_match"] == 570
    assert exact["evidence"]["c_ab"] == pytest.approx(0.95)
    assert exact["evidence"]["components"] == []
    assert exact["affected_volume"] == 570
    bridge = page["items"][1]
    assert bridge["sentence"], "a stored link without a sentence gets one from its evidence"
    assert "t1." not in w.client.get(LINKS).text


def test_the_queue_pages_with_the_id_cursor() -> None:
    w = world()
    w.login(Role.AUDITOR)
    first = w.client.get(LINKS, params={"limit": 1}).json()
    assert [i["id"] for i in first["items"]] == [EXACT_ID] and first["next_cursor"] == EXACT_ID
    second = w.client.get(LINKS, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [i["id"] for i in second["items"]] == [BRIDGE_ID] and second["next_cursor"] is None
    assert w.client.get(LINKS, params={"cursor": "nope"}).json()["items"] == []
    assert w.client.get(LINKS, params={"status": "accepted"}).json()["items"] == []
    assert w.client.get(f"{LINKS}/lnk_missing").status_code == 404


def test_accept_writes_the_decision_row_and_one_audit_row() -> None:
    w = world()
    headers = w.login(Role.INTEGRATION_ENGINEER)
    at = w.clock.tick(minutes=1)
    response = w.client.post(
        f"{LINKS}/{EXACT_ID}/accept",
        json={"note": "confirmed on the call"},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "accepted" and body["version"] == 2
    assert body["decided_by"] == "alice" and body["etag"] == 'W/"2"'
    assert response.headers["etag"] == 'W/"2"'

    stored = w.links.get(TENANT, EXACT_ID)
    assert stored is not None and stored.status == LinkStatus.ACCEPTED
    assert stored.decided_at == at, "the route's clock is the decision time"
    assert stored.baseline_match_rate == pytest.approx(0.95), "ADR 0053 baseline"
    decisions = w.links.decisions(TENANT, EXACT_ID)
    assert [(d.version, d.status, d.decided_by, d.note) for d in decisions] == [
        (2, LinkStatus.ACCEPTED, "alice", "confirmed on the call")
    ]
    entries = w.audit.entries(TENANT)
    assert [(e.action, e.actor, e.target) for e in entries] == [
        ("link.accept", "alice", f"link:{EXACT_ID}")
    ], "one row per decision, written by the route"
    assert entries[0].after == {
        "status": "accepted",
        "role": "transaction",
        "score": 0.93,
        "version": 2,
    }
    assert entries[0].request_id and entries[0].ip == "testclient"

    accepted = w.client.get(LINKS, params={"status": "accepted"}).json()
    assert [item["id"] for item in accepted["items"]] == [EXACT_ID]


def test_a_stale_if_match_is_412_and_changes_nothing() -> None:
    w = world()
    headers = w.login(Role.ADMIN)
    response = w.client.post(
        f"{LINKS}/{EXACT_ID}/accept", json={}, headers={**headers, "If-Match": 'W/"7"'}
    )
    assert response.status_code == 412
    assert w.client.post(f"{LINKS}/{EXACT_ID}/accept", json={}, headers=headers).status_code == 428
    stored = w.links.get(TENANT, EXACT_ID)
    assert stored is not None and stored.status == LinkStatus.PROPOSED and stored.version == 1
    assert w.audit.entries(TENANT) == []


def test_the_port_itself_refuses_a_version_the_header_let_through() -> None:
    """The route checks the header, the review service checks again under the repository."""
    w = world()
    with pytest.raises(DecisionError) as info:
        w.data.links.accept(
            TENANT, EXACT_ID, actor="x", expected_version=5, reason=None, note="", at=NOW
        )
    assert info.value.kind == "stale_version"
    with pytest.raises(DecisionError) as missing:
        w.data.links.accept(
            TENANT, "lnk_none", actor="x", expected_version=1, reason=None, note="", at=NOW
        )
    assert missing.value.kind == "not_found"
    with pytest.raises(DecisionError) as reason:
        w.data.links.reject(
            TENANT, EXACT_ID, actor="x", expected_version=1, reason=None, note="", at=NOW
        )
    assert reason.value.kind == "reason_required"


def test_reject_needs_a_reason_and_association_changes_the_role() -> None:
    w = world()
    headers = w.login(Role.INTEGRATION_ENGINEER)
    assert (
        w.client.post(
            f"{LINKS}/{EXACT_ID}/reject", json={}, headers={**headers, "If-Match": 'W/"1"'}
        ).status_code
        == 422
    )
    rejected = w.client.post(
        f"{LINKS}/{EXACT_ID}/reject",
        json={"reason": RejectReason.COINCIDENCE.value},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert rejected.status_code == 200 and rejected.json()["reject_reason"] == "coincidence"
    association = w.client.post(
        f"{LINKS}/{BRIDGE_ID}/association", json={}, headers={**headers, "If-Match": 'W/"1"'}
    )
    assert association.status_code == 200
    assert association.json()["role"] == "association"
    assert association.json()["status"] == "accepted"
    again = w.client.post(
        f"{LINKS}/{BRIDGE_ID}/association", json={}, headers={**headers, "If-Match": 'W/"2"'}
    )
    assert again.status_code == 409, "already an association"
    assert [e.action for e in w.audit.entries(TENANT)] == ["link.reject", "link.association"]


# --- inventory -----------------------------------------------------------------------------------


def test_systems_sources_and_fields_come_from_the_metadata_store() -> None:
    w = world()
    w.login(Role.BUSINESS_VIEWER)
    systems = w.client.get(f"{API_PREFIX}/systems").json()["items"]
    assert [(s["id"], s["name"], s["source_count"], s["criticality"]) for s in systems] == [
        ("sys_a", "Order system", 2, "medium"),
        ("sys_b", "Warehouse", 1, "high"),
    ]
    received = NOW - timedelta(hours=1)
    assert systems[0]["etag"] == f'W/"{updated_at_version(received)}"'
    one = w.client.get(f"{API_PREFIX}/systems/sys_b")
    assert one.status_code == 200 and one.headers["etag"] == systems[1]["etag"]
    assert w.client.get(f"{API_PREFIX}/systems/sys_z").status_code == 404

    w.metadata.health[(TENANT, "src_a")] = SourceHealthRecord(
        tenant_id=TENANT,
        source_id="src_a",
        status="ok",
        last_success_at=NOW,
        lag_seconds=3.0,
        error_count=0,
        buffer_depth=0,
        oldest_buffered_at=None,
        message="",
        received_at=NOW,
        sent_at=NOW,
    )
    sources = w.client.get(f"{API_PREFIX}/sources").json()["items"]
    assert [s["id"] for s in sources] == ["src_a", "src_a_db", "src_b"]
    assert sources[0]["health"]["status"] == "ok" and sources[0]["config"] == {
        "path": "/var/log/shop/*.log"
    }
    assert sources[1]["secret_ref"] == "vault://db/shop" and sources[1]["health"] is None  # noqa: S105
    assert sources[2]["enabled"] is False
    assert w.client.get(f"{API_PREFIX}/sources/src_a_db").json()["type"] == "sql"
    assert w.client.get(f"{API_PREFIX}/sources/src_a/health").json()["lag_seconds"] == 3.0

    fields = w.client.get(f"{API_PREFIX}/fields", params={"system": "sys_b"}).json()
    assert [f["id"] for f in fields["items"]] == [ORDER_REF.field_ref]
    assert fields["items"][0]["forms"] == ["raw", "digits.0"]
    assert fields["items"][0]["shapes"] == [
        {"shape": "9999", "share": 0.98},
        {"shape": "99999", "share": 0.02},
    ]
    assert fields["items"][0]["quality"] == 0.9
    every = w.client.get(f"{API_PREFIX}/fields", params={"limit": 2}).json()
    assert len(every["items"]) == 2 and every["next_cursor"] == every["items"][-1]["id"]
    rest = w.client.get(f"{API_PREFIX}/fields", params={"cursor": every["next_cursor"]}).json()
    assert [f["id"] for f in rest["items"]] == [ORDER_REF.field_ref]
    assert rest["next_cursor"] is None
    by_id = w.client.get(f"{API_PREFIX}/fields/{ORDER_ID.field_ref}")
    assert by_id.status_code == 200 and by_id.json()["label"] == "Order number"
    assert "t1." not in fields and "t1." not in str(sources)


def test_renaming_a_system_bumps_its_updated_at_version() -> None:
    w = world()
    headers = w.login(Role.INTEGRATION_ENGINEER)
    before = w.client.get(f"{API_PREFIX}/systems/sys_a").json()
    w.clock.tick(minutes=2)
    stale = w.client.patch(
        f"{API_PREFIX}/systems/sys_a",
        json={"name": "Shop"},
        headers={**headers, "If-Match": 'W/"1"'},
    )
    assert stale.status_code == 412
    renamed = w.client.patch(
        f"{API_PREFIX}/systems/sys_a",
        json={"name": "Shop", "description": "The web shop"},
        headers={**headers, "If-Match": before["etag"]},
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert (body["name"], body["description"], body["source_count"]) == ("Shop", "The web shop", 2)
    assert body["etag"] == f'W/"{updated_at_version(w.clock())}"' != before["etag"]
    assert next(s.name for s in w.metadata.list_systems(TENANT)) == "Shop"
    assert [(e.action, e.target) for e in w.audit.entries(TENANT)] == [
        ("system.patch", "system:sys_a")
    ]
    missing = w.client.patch(
        f"{API_PREFIX}/systems/sys_z", json={"name": "x"}, headers={**headers, "If-Match": "*"}
    )
    assert missing.status_code == 404


def test_field_pages_end_cleanly_when_a_field_ref_is_too_long_for_a_cursor() -> None:
    metadata = InMemoryMetadataStore()
    long_path = "x" * (MAX_CURSOR_LEN + 10)
    metadata.upsert_fields(
        TENANT,
        [field("sys_a/tpl_a/a"), field(f"sys_a/tpl_a/{long_path}"), field("sys_a/tpl_a/z")],
    )
    port = FieldPortAdapter(metadata)
    first = port.list(TENANT, None, None, 2)
    assert [row.id for row in first.items] == ["sys_a/tpl_a/a"], "cut before the long ref"
    assert first.next_cursor == "sys_a/tpl_a/a"
    second = port.list(TENANT, None, first.next_cursor, 2)
    assert [row.path for row in second.items] == [long_path, "z"]
    assert second.next_cursor is None
    assert port.list(TENANT, None, None, 50).next_cursor is None


# --- entities ------------------------------------------------------------------------------------


def test_a_family_built_from_accepted_links_is_listed_and_renamed() -> None:
    w = world()
    for link_id in (EXACT_ID, BRIDGE_ID):
        w.review.accept(TENANT, link_id, actor="alice", expected_version=1)
    built = w.clock.tick(minutes=5)
    report = FamilyBuilder(w.links, w.entities, namer=lambda _members: "Order").rebuild(
        TENANT, now=built
    )
    assert report.families == 1 and report.names_suggested == 1

    headers = w.login(Role.INTEGRATION_ENGINEER)
    page = w.client.get(f"{API_PREFIX}/entities").json()
    assert len(page["items"]) == 1
    entity = page["items"][0]
    assert entity["name"] == "Order" and entity["suggested_name"] == "Order"
    assert entity["name_confirmed"] is False and entity["system_count"] == 2
    assert [(f["field_id"], f["form"]) for f in entity["fields"]] == [
        (MERCHANT.field_ref, "raw"),
        (ORDER_ID.field_ref, "raw"),
        (ORDER_REF.field_ref, "digits.0"),
    ]
    assert sorted(entity["link_ids"]) == sorted([EXACT_ID, BRIDGE_ID])
    assert entity["etag"] == f'W/"{updated_at_version(built)}"'
    one = w.client.get(f"{API_PREFIX}/entities/{entity['id']}")
    assert one.status_code == 200 and one.headers["etag"] == entity["etag"]
    assert w.client.get(f"{API_PREFIX}/entities/ent_missing").status_code == 404

    w.clock.tick(minutes=1)
    stale = w.client.patch(
        f"{API_PREFIX}/entities/{entity['id']}",
        json={"name": "Customer order"},
        headers={**headers, "If-Match": 'W/"3"'},
    )
    assert stale.status_code == 412
    renamed = w.client.patch(
        f"{API_PREFIX}/entities/{entity['id']}",
        json={"name": "Customer order"},
        headers={**headers, "If-Match": entity["etag"]},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Customer order"
    assert renamed.json()["name_confirmed"] is True
    assert renamed.json()["etag"] == f'W/"{updated_at_version(w.clock())}"' != entity["etag"]
    stored = w.entities.get(TENANT, entity["id"])
    assert stored is not None and stored.name == "Customer order" and stored.name_confirmed
    assert [(e.action, e.target) for e in w.audit.entries(TENANT)][-1] == (
        "entity.patch",
        f"entity:{entity['id']}",
    )
    # A later rebuild keeps the confirmed name (families.inherit_names).
    FamilyBuilder(w.links, w.entities, namer=lambda _m: "Order").rebuild(TENANT, now=w.clock())
    assert w.client.get(f"{API_PREFIX}/entities").json()["items"][0]["name"] == "Customer order"


def test_readiness_asks_every_data_port() -> None:
    w = world()
    assert w.client.get("/readyz").status_code == 200
    assert w.data.links.ping() and w.data.entities.ping() and w.data.fields.ping()


def test_updated_at_versions_are_whole_milliseconds_in_utc() -> None:
    assert updated_at_version(None) == 0
    at = datetime(2026, 10, 18, 12, 0, 0, 123_456, tzinfo=UTC)
    assert updated_at_version(at) == 1_792_324_800_123
    assert updated_at_version(at.replace(tzinfo=None)) == updated_at_version(at), "naive is UTC"
    assert updated_at_version(at) < 10**18, "fits the ETag grammar"


# --- PostgreSQL ----------------------------------------------------------------------------------

POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return False
    try:
        client = docker.from_env()
    except Exception:
        return False
    try:
        client.ping()
    except Exception:
        return False
    finally:
        client.close()
    return True


integration = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlalchemy.Engine]:
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    from testcontainers.core.config import testcontainers_config  # noqa: PLC0415

    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.migrations import apply_postgres_migrations  # noqa: PLC0415
    from carto_core.settings import PostgresSettings  # noqa: PLC0415

    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with PostgresContainer(
        POSTGRES_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, driver=None
    ) as container:
        settings = PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            sslmode="disable",
        )
        engine = create_engine_from_settings(settings)
        apply_postgres_migrations(engine)
        yield engine
        engine.dispose()


@pytest.mark.parametrize("_marker", [pytest.param(None, marks=integration)])
def test_the_postgres_ports_serve_one_request_per_port(
    engine: sqlalchemy.Engine, _marker: None
) -> None:
    """``carto-core api``'s wiring: :func:`data_ports` over migration 0004 behind
    :func:`build_ports`, one request per port through the real app."""
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    clock = Clock()
    metadata = PostgresMetadataStore(engine)
    seed_metadata(metadata)
    links = PostgresLinkRepository(engine)
    entities = PostgresEntityRepository(engine)
    audit = build_ports(engine, data_ports(engine, settings)).audit_recorder
    ReviewService(links, audit, settings.linker, clock=clock).propose(
        TENANT,
        [scored(ORDER_ID, ORDER_REF, score=0.93, n_match=570, sentence="95% match.")],
        now=NOW - timedelta(minutes=30),
    )
    data = build_data_ports(engine, settings, clock=clock)
    assert isinstance(data, DataPorts)
    ports = build_ports(engine, data)
    app = create_app(settings, ports, clock=clock)
    client = TestClient(app, base_url="https://testserver")
    context = app.state.context
    assert isinstance(context, ApiContext)
    issued = context.sessions.issue(
        tenant_id=TENANT,
        subject="alice",
        display_name="Alice",
        # admin: the walk ends at GET /audit, which spec 14.2 grants admins and auditors
        roles=frozenset({Role.ADMIN}),
        authenticated_at=NOW,
        ip="127.0.0.1",
        user_agent="pytest",
    )
    client.cookies.set(SESSION_COOKIE_NAME, issued.cookie_value)
    headers = {"X-CSRF-Token": issued.record.csrf_token, **JSON}

    assert client.get("/readyz").status_code == 200
    queue = client.get(LINKS).json()
    assert [item["id"] for item in queue["items"]] == [EXACT_ID]
    assert queue["items"][0]["sentence"] == "95% match."
    assert queue["items"][0]["a"]["label"] == "Order number"

    clock.tick(minutes=1)
    accepted = client.post(
        f"{LINKS}/{EXACT_ID}/accept", json={}, headers={**headers, "If-Match": 'W/"1"'}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted" and accepted.json()["version"] == 2
    assert len(links.decisions(TENANT, EXACT_ID)) == 1
    audit_rows = client.get(f"{API_PREFIX}/audit").json()
    assert audit_rows["items"][0]["action"] == "link.accept"
    assert audit_rows["items"][0]["target"] == f"link:{EXACT_ID}"
    assert len(audit_rows["items"]) == 1, "the route writes the one row"

    FamilyBuilder(links, entities, namer=lambda _m: "Order").rebuild(
        TENANT, now=clock.tick(minutes=1)
    )
    families = client.get(f"{API_PREFIX}/entities").json()["items"]
    assert len(families) == 1 and families[0]["name"] == "Order"
    entity_id = families[0]["id"]
    renamed = client.patch(
        f"{API_PREFIX}/entities/{entity_id}",
        json={"name": "Customer order"},
        headers={**headers, "If-Match": families[0]["etag"]},
    )
    clock.tick(minutes=1)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name_confirmed"] is True

    systems = client.get(f"{API_PREFIX}/systems").json()["items"]
    assert [(s["id"], s["source_count"]) for s in systems] == [("sys_a", 2), ("sys_b", 1)]
    clock.tick(minutes=1)
    patched = client.patch(
        f"{API_PREFIX}/systems/sys_a",
        json={"name": "Shop"},
        headers={**headers, "If-Match": systems[0]["etag"]},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Shop" and patched.json()["etag"] != systems[0]["etag"]
    stale = client.patch(
        f"{API_PREFIX}/systems/sys_a",
        json={"name": "Shop again"},
        headers={**headers, "If-Match": systems[0]["etag"]},
    )
    assert stale.status_code == 412, "the UPDATE itself checks the updated_at version"

    sources = client.get(f"{API_PREFIX}/sources").json()["items"]
    assert [s["id"] for s in sources] == ["src_a", "src_a_db", "src_b"]
    assert client.get(f"{API_PREFIX}/sources/src_b").json()["enabled"] is False
    fields = client.get(f"{API_PREFIX}/fields", params={"system": "sys_a"}).json()["items"]
    assert [f["id"] for f in fields] == [MERCHANT.field_ref, ORDER_ID.field_ref]
    assert client.get(f"{API_PREFIX}/fields/{ORDER_REF.field_ref}").json()["forms"] == [
        "raw",
        "digits.0",
    ]
