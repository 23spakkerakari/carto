"""Container-backed checks of package A on a real PostgreSQL (spec 18.2; plan M2 package A):
migration 0004 creates the map discovery tables, the audit log refuses UPDATE and DELETE (spec
14.9), the migration downgrades and upgrades again, ``PostgresJobQueue`` leases with
``SKIP LOCKED`` under concurrency, the ledger's ``after`` hook commits with the done mark or not
at all (ADR 0056), and ``PostgresMetadataStore`` answers the same cases as the in-memory
reference (ADR 0050, 0056). The ClickHouse store is covered by ``test_core_store_contract.py``.
Skipped when Docker is unreachable."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import docker  # type: ignore[import-untyped]
import pytest
import sqlalchemy
from alembic import command
from sqlalchemy.exc import DBAPIError
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from carto_core.db.postgres import create_engine_from_settings
from carto_core.ingest.health import PostgresSourceHealthStore, SourceHealthRecord
from carto_core.ingest.ledger import ClaimState, LedgerEntry, LedgerError, PostgresBatchLedger
from carto_core.jobs import JobKind
from carto_core.jobs_postgres import PostgresJobQueue
from carto_core.metadata import (
    FieldProfileUpdate,
    FieldRecord,
    InventoryResult,
    MetadataError,
    PostgresMetadataStore,
    TemplateRecord,
)
from carto_core.migrations import alembic_config, apply_postgres_migrations
from carto_core.settings import PostgresSettings
from carto_schema.inventory import (
    EdgeInventory,
    InventoryFieldPolicy,
    InventorySource,
    InventorySystem,
)

POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"
NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
KINDS = frozenset({JobKind.PROFILE, JobKind.LINK})


def _docker_available() -> bool:
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


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


@pytest.fixture(scope="module", autouse=True)
def _no_ryuk_on_windows() -> None:
    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True


@pytest.fixture(scope="module")
def postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PostgresSettings]:
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with PostgresContainer(
        POSTGRES_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, driver=None
    ) as container:
        yield PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            sslmode="disable",
        )


@pytest.fixture(scope="module")
def engine(postgres: PostgresSettings) -> Iterator[sqlalchemy.Engine]:
    engine = create_engine_from_settings(postgres)
    apply_postgres_migrations(engine)
    yield engine
    engine.dispose()


# --- migration 0004 ----------------------------------------------------------------------------


def test_migration_0004_creates_the_map_discovery_tables(engine: sqlalchemy.Engine) -> None:
    inspector = sqlalchemy.inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {
        "templates",
        "fields",
        "field_sketches",
        "key_links",
        "key_link_decisions",
        "composite_rules",
        "entities",
        "entity_fields",
        "jobs",
        "audit_log",
        "users",
        "groups",
        "role_bindings",
        "sessions",
        "settings",
        "llm_calls",
        "edge_inventories",
    }
    assert expected <= tables
    for table in expected - {"audit_log"}:
        columns = {column["name"] for column in inspector.get_columns(table)}
        assert {"tenant_id", "created_at", "updated_at"} <= columns, table
    audit_columns = {column["name"] for column in inspector.get_columns("audit_log")}
    assert "updated_at" not in audit_columns and {"prev_hash", "row_hash"} <= audit_columns
    llm_id = next(c for c in inspector.get_columns("llm_calls") if c["name"] == "id")
    assert isinstance(llm_id["type"], sqlalchemy.String) and llm_id["type"].length == 26
    source_columns = {column["name"] for column in inspector.get_columns("sources")}
    assert {"enabled", "backfill_days", "schedule", "last_test", "last_test_at", "edge_id"} <= (
        source_columns
    )
    indexes = {index["name"] for index in inspector.get_indexes("jobs")}
    assert "ix_jobs_tenant_kind_run_after" in indexes
    with engine.connect() as connection:
        version = connection.execute(
            sqlalchemy.text("SELECT version_num FROM alembic_version")
        ).scalar_one()
    assert version == "0004"


def test_the_audit_log_refuses_update_and_delete(engine: sqlalchemy.Engine) -> None:
    """Spec 14.9: append-only by trigger, whatever the role's grants say."""
    insert = sqlalchemy.text(
        "INSERT INTO audit_log (tenant_id, at, actor, action, target, prev_hash, row_hash) "
        "VALUES ('default', :at, 'tester', 'link.accept', 'kl_1', :prev, :row) RETURNING id"
    )
    with engine.begin() as connection:
        row_id = connection.execute(
            insert, {"at": NOW, "prev": "0" * 64, "row": "1" * 64}
        ).scalar_one()
    for statement in (
        "UPDATE audit_log SET actor = 'x' WHERE id = :id",
        "DELETE FROM audit_log WHERE id = :id",
    ):
        with pytest.raises(DBAPIError) as info, engine.begin() as connection:
            connection.execute(sqlalchemy.text(statement), {"id": row_id})
        assert "append-only" in str(info.value)
    with pytest.raises(DBAPIError), engine.begin() as connection:
        connection.execute(sqlalchemy.text("TRUNCATE audit_log"))
    with engine.connect() as connection:
        kept = connection.execute(
            sqlalchemy.text("SELECT actor FROM audit_log WHERE id = :id"), {"id": row_id}
        ).scalar_one()
    assert kept == "tester"


def test_migration_0004_downgrades_and_upgrades_again(postgres: PostgresSettings) -> None:
    other = create_engine_from_settings(postgres)
    try:
        config = alembic_config()
        with other.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "0003")
        tables = set(sqlalchemy.inspect(other).get_table_names())
        assert "key_links" not in tables and "audit_log" not in tables
        source_columns = {c["name"] for c in sqlalchemy.inspect(other).get_columns("sources")}
        assert "edge_id" not in source_columns
        apply_postgres_migrations(other)
        assert "key_links" in set(sqlalchemy.inspect(other).get_table_names())
    finally:
        other.dispose()


# --- jobs -----------------------------------------------------------------------------------------


def test_postgres_job_queue_contract(engine: sqlalchemy.Engine) -> None:
    queue = PostgresJobQueue(engine)
    assert queue.ping() is True
    tenant = "jobs_contract"
    first = queue.enqueue(tenant, JobKind.PROFILE, {}, NOW)
    assert first is not None and first.attempts == 0 and first.run_after == NOW
    assert queue.enqueue(tenant, JobKind.PROFILE, {}, NOW + timedelta(hours=1)) is None
    full = queue.enqueue(tenant, JobKind.PROFILE, {"full": True}, NOW + timedelta(days=1))
    link = queue.enqueue(tenant, JobKind.LINK, {}, NOW - timedelta(minutes=5))
    assert full is not None and link is not None
    assert [job.id for job in queue.pending(tenant)] == [link.id, first.id, full.id]

    leased = queue.lease(KINDS, NOW, 600)
    assert leased is not None and leased.id == link.id
    assert leased.attempts == 1 and leased.leased_until == NOW + timedelta(seconds=600)
    second = queue.lease(KINDS, NOW, 600)
    assert second is not None and second.id == first.id
    assert queue.lease(KINDS, NOW, 600) is None, "the full pass is tomorrow"
    assert queue.lease(frozenset({JobKind.FAMILIES}), NOW, 600) is None
    # A leased job does not block the next run from being queued.
    following = queue.enqueue(tenant, JobKind.PROFILE, {}, NOW + timedelta(minutes=5))
    assert following is not None
    assert queue.enqueue(tenant, JobKind.PROFILE, {}, NOW + timedelta(minutes=9)) is None
    # Expired leases are retaken, attempts keep counting.
    retaken = queue.lease(KINDS, NOW + timedelta(seconds=600), 600)
    assert retaken is not None and retaken.id == link.id and retaken.attempts == 2
    queue.complete(second.id)
    queue.fail(retaken.id, "ValueError", NOW + timedelta(minutes=1))
    released = next(job for job in queue.pending(tenant) if job.id == link.id)
    assert released.leased_until is None and released.last_error == "ValueError"
    assert released.run_after == NOW + timedelta(minutes=1)
    assert queue.lease(frozenset({JobKind.LINK}), NOW, 600) is None
    again = queue.lease(frozenset({JobKind.LINK}), NOW + timedelta(minutes=1), 600)
    assert again is not None and again.attempts == 3
    queue.fail(again.id, "X" * 1000, None)
    assert {job.id for job in queue.pending(tenant)} == {full.id, following.id}
    queue.complete(full.id)
    queue.complete(following.id)
    assert queue.pending(tenant) == []


def test_two_workers_leasing_at_once_never_share_a_job_on_postgres(
    engine: sqlalchemy.Engine,
) -> None:
    queue = PostgresJobQueue(engine)
    tenant = "jobs_concurrent"
    for index in range(40):
        queue.enqueue(tenant, JobKind.LINK, {"n": index}, NOW)

    def take(_: int) -> list[int]:
        taken: list[int] = []
        while (job := queue.lease(KINDS, NOW, 600)) is not None:
            if job.tenant_id == tenant:
                taken.append(job.id)
        return taken

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(take, range(4)))
    ids = [job_id for taken in results for job_id in taken]
    assert len(ids) == 40 and len(set(ids)) == 40
    for job_id in ids:
        queue.complete(job_id)


def test_concurrent_enqueues_of_one_recurring_job_leave_one_row(engine: sqlalchemy.Engine) -> None:
    queue = PostgresJobQueue(engine)
    tenant = "jobs_dedupe"

    def add(_: int) -> bool:
        return queue.enqueue(tenant, JobKind.PROFILE, {"full": True}, NOW) is not None

    with ThreadPoolExecutor(max_workers=8) as pool:
        added = list(pool.map(add, range(8)))
    assert added.count(True) == 1
    (job,) = queue.pending(tenant)
    queue.complete(job.id)


# --- the ledger hook (ADR 0056) -------------------------------------------------------------------


def test_the_ledger_hook_commits_with_the_done_mark_or_not_at_all(
    engine: sqlalchemy.Engine,
) -> None:
    ledger = PostgresBatchLedger(engine)
    store = PostgresMetadataStore(engine)
    tenant = "ledger_hook"
    entry = LedgerEntry(tenant, "01K7HOOK0000000000000000AA", "src_hook", 2, NOW)
    assert ledger.claim(entry, NOW - timedelta(minutes=2)).state is ClaimState.CLAIMED
    record = TemplateRecord("sys_a", "tpl_a", "INSERT a", "row_change", NOW, NOW, 2)

    def failing(connection: object) -> None:
        store.upsert_templates(tenant, [record], connection=connection)  # type: ignore[arg-type]
        raise sqlalchemy.exc.OperationalError("boom", None, Exception("boom"))

    with pytest.raises(LedgerError):
        ledger.complete(entry, NOW, after=failing)
    assert store.list_templates(tenant) == [], "rolled back with the mark"
    assert ledger.claim(entry, NOW - timedelta(minutes=2)).state is ClaimState.BUSY, "pending"

    def recording(connection: object) -> None:
        store.upsert_templates(tenant, [record], connection=connection)  # type: ignore[arg-type]

    ledger.complete(entry, NOW, after=recording)
    assert ledger.claim(entry, NOW - timedelta(minutes=2)).state is ClaimState.DONE
    (template,) = store.list_templates(tenant)
    assert template.count == 2


# --- metadata -------------------------------------------------------------------------------------


def _field(tenant: str, path: str, **overrides: object) -> FieldRecord:
    data: dict[str, object] = {
        "field_ref": f"sys_orders/tpl_create/{path}",
        "system_id": "sys_orders",
        "template_id": "tpl_create",
        "path": path,
        "field_class": "identifier",
        "policy": "tokenize",
        "pinned": False,
        "forms": ("raw", "digits.0"),
        "count": 120,
        "shapes": (("9999", 0.9), ("99999", 0.1)),
        "first_seen": NOW,
        "last_seen": NOW + timedelta(hours=1),
    }
    del tenant
    return FieldRecord(**(data | overrides))  # type: ignore[arg-type]


def _inventory(tenant: str, **overrides: object) -> EdgeInventory:
    data: dict[str, object] = {
        "schema_version": "1",
        "tenant_id": tenant,
        "edge_id": "edge-gateway",
        "sent_at": NOW,
        "sources_file_sha256": "a" * 64,
        "systems": [
            InventorySystem(id="sys_orders", name="Orders", owner_group="shop"),
            InventorySystem(id="sys_pay", name="Payments", criticality="high"),
        ],
        "sources": [
            InventorySource(
                id="src_orders_db",
                system_id="sys_orders",
                type="sql",
                config={"host": "db.internal", "port": 5432, "tables": ["orders"]},
                secret_ref="vault://db/orders",  # noqa: S106 - a reference name
                backfill_days=7,
                schedule="every 60 s",
            ),
            InventorySource(id="src_pay_log", system_id="sys_pay", type="otlp", enabled=False),
        ],
        "field_policies": [
            InventoryFieldPolicy(
                field="sys_orders/*/customer_name",
                field_class="person_name",
                policy="tokenize",
                forms=["phonetic.0"],
            ),
        ],
    }
    return EdgeInventory(**(data | overrides))  # type: ignore[arg-type]


def test_postgres_metadata_templates_and_fields(engine: sqlalchemy.Engine) -> None:
    store = PostgresMetadataStore(engine)
    tenant = "meta_fields"
    assert store.ping() is True
    first = TemplateRecord("sys_orders", "tpl_create", "INSERT orders", "row_change", NOW, NOW, 3)
    later = TemplateRecord(
        "sys_orders",
        "tpl_create",
        "INSERT orders v2",
        "row_change",
        NOW - timedelta(days=1),
        NOW + timedelta(days=1),
        2,
        display_name="Order created",
    )
    assert store.upsert_templates(tenant, [first]) == 1
    assert store.upsert_templates(tenant, [later]) == 1
    (template,) = store.list_templates(tenant)
    assert (template.count, template.template_text, template.display_name) == (
        5,
        "INSERT orders v2",
        "Order created",
    )
    assert template.first_seen == NOW - timedelta(days=1)
    assert template.last_seen == NOW + timedelta(days=1)
    assert store.list_templates(tenant, "sys_pay") == []

    assert store.upsert_fields(tenant, [_field(tenant, "order_id")]) == 1
    profile = FieldProfileUpdate(
        "sys_orders/tpl_create/order_id",
        4000,
        3900,
        0.01,
        (("9999", 1.0),),
        0.87,
        True,
        NOW + timedelta(days=2),
    )
    assert store.update_field_profiles(tenant, [profile]) == 1
    store.upsert_fields(
        tenant, [_field(tenant, "order_id", count=130, forms=("raw",), pinned=True)]
    )
    field = store.get_field(tenant, "sys_orders/tpl_create/order_id")
    assert field is not None
    assert (field.count, field.forms, field.pinned) == (130, ("raw",), True)
    assert (field.distinct_estimate, field.null_rate, field.quality, field.is_candidate) == (
        3900,
        0.01,
        0.87,
        True,
    )
    assert field.shapes == (("9999", 0.9), ("99999", 0.1))
    assert field.last_seen == NOW + timedelta(days=2) and field.first_seen == NOW
    unknown = FieldProfileUpdate("sys_pay/tpl_pay/order_ref", 10, 10, 0.0, (), None, False, NOW)
    assert store.update_field_profiles(tenant, [unknown]) == 1
    created = store.get_field(tenant, "sys_pay/tpl_pay/order_ref")
    assert created is not None
    assert (created.system_id, created.template_id, created.path) == (
        "sys_pay",
        "tpl_pay",
        "order_ref",
    )
    assert (created.field_class, created.policy) == ("identifier", "tokenize")
    refs = [f.field_ref for f in store.list_fields(tenant)]
    assert refs == ["sys_orders/tpl_create/order_id", "sys_pay/tpl_pay/order_ref"]
    assert [f.field_ref for f in store.list_fields(tenant, candidates_only=True)] == refs[:1]
    assert [f.field_ref for f in store.list_fields(tenant, after=refs[0])] == refs[1:]
    assert [f.field_ref for f in store.list_fields(tenant, system_id="sys_pay")] == refs[1:]
    assert store.list_fields(tenant, limit=1)[0].field_ref == refs[0]
    assert store.list_fields("nobody") == []


def test_postgres_metadata_inventory_sources_and_pins(engine: sqlalchemy.Engine) -> None:
    store = PostgresMetadataStore(engine)
    tenant = "meta_inventory"
    store.upsert_fields(
        tenant,
        [
            _field(tenant, "customer_name", field_class="unknown", policy="drop", forms=()),
            _field(
                tenant,
                "customer_name",
                template_id="tpl_ship",
                field_ref="sys_orders/tpl_ship/customer_name",
            ),
            _field(tenant, "order_id"),
        ],
    )
    result = store.replace_inventory(_inventory(tenant), NOW)
    assert result == InventoryResult(systems=2, sources=2, disabled=0, pins=1, fields_pinned=2)
    assert [(s.id, s.name, s.criticality) for s in store.list_systems(tenant)] == [
        ("sys_orders", "Orders", "medium"),
        ("sys_pay", "Payments", "high"),
    ]
    source = store.get_source(tenant, "src_orders_db")
    assert source is not None
    assert source.config == {"host": "db.internal", "port": 5432, "tables": ["orders"]}
    assert (source.enabled, source.backfill_days, source.schedule, source.edge_id) == (
        True,
        7,
        "every 60 s",
        "edge-gateway",
    )
    assert source.health is None
    name = store.get_field(tenant, "sys_orders/tpl_create/customer_name")
    ship = store.get_field(tenant, "sys_orders/tpl_ship/customer_name")
    order_id = store.get_field(tenant, "sys_orders/tpl_create/order_id")
    assert name is not None and ship is not None and order_id is not None
    assert (name.field_class, name.policy, name.pinned, name.forms) == (
        "person_name",
        "tokenize",
        True,
        ("phonetic.0",),
    )
    assert ship.pinned is True and order_id.pinned is False
    # A later field upsert sees the kept pins.
    store.upsert_fields(
        tenant,
        [
            _field(
                tenant,
                "customer_name",
                template_id="tpl_x",
                field_ref="sys_orders/tpl_x/customer_name",
                field_class="unknown",
            )
        ],
    )
    late = store.get_field(tenant, "sys_orders/tpl_x/customer_name")
    assert late is not None and (late.field_class, late.pinned) == ("person_name", True)
    # Heartbeats join in; a missing source is disabled, not deleted.
    PostgresSourceHealthStore(engine).upsert(
        SourceHealthRecord(tenant, "src_orders_db", "ok", NOW, 1.5, 0, 0, None, "", NOW, NOW)
    )
    listed = {s.id: s for s in store.list_sources(tenant)}
    assert listed["src_orders_db"].health is not None
    assert listed["src_orders_db"].health.status == "ok"
    assert listed["src_orders_db"].health.tenant_id == tenant
    assert listed["src_pay_log"].health is None and listed["src_pay_log"].enabled is False
    shrunk = _inventory(tenant, sources=[_inventory(tenant).sources[0]], field_policies=[])
    assert store.replace_inventory(shrunk, NOW + timedelta(hours=1)).disabled == 0
    empty = _inventory(tenant, sources=[], field_policies=[])
    assert store.replace_inventory(empty, NOW + timedelta(hours=2)).disabled == 1
    assert all(not s.enabled for s in store.list_sources(tenant))
    with engine.connect() as connection:
        payload = connection.execute(
            sqlalchemy.text(
                "SELECT payload ->> 'sources_file_sha256' FROM edge_inventories "
                "WHERE tenant_id = :tenant_id AND edge_id = 'edge-gateway'"
            ),
            {"tenant_id": tenant},
        ).scalar_one()
    assert payload == "a" * 64
    assert store.list_sources("nobody") == []


def test_postgres_metadata_errors_name_the_driver_class_only(postgres: PostgresSettings) -> None:
    broken = create_engine_from_settings(postgres.model_copy(update={"port": 1}))
    try:
        store = PostgresMetadataStore(broken)
        with pytest.raises(MetadataError) as info:
            store.list_systems("default")
        assert "OperationalError" in str(info.value)
        assert DB_PASSWORD not in str(info.value)
        assert store.ping() is False
    finally:
        broken.dispose()
