"""Container-backed checks (spec 18.2): migrations apply idempotently to a real ClickHouse and
PostgreSQL, a retention change rewrites the TTLs, a batch posted to the app lands as rows in
``events`` and ``event_identifiers`` with its ledger row, a duplicate is skipped, the ledger's
claims work on PostgreSQL and the replacing tables merge a second copy (ADR 0034), a batch over
many days is inserted in day groups (ADR 0036), a heartbeat upserts, a bundle loads
idempotently, and a bundle the offline analyzer builds from scenario A loads into core with no
marker in the stored rows (spec 21, M1 acceptance; spec 18.3). Skipped when Docker is
unreachable."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import warnings
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import docker  # type: ignore[import-untyped]
import pytest
import sqlalchemy
import zstandard
from clickhouse_connect.driver.client import Client
from testcontainers.community.clickhouse import ClickHouseContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from carto_common.crypto import SigningKey, b64url_encode
from carto_common.ids import derive_ulid, new_ulid
from carto_common.mtls import ActAsRole
from carto_common.settings import RetentionSettings
from carto_core.bundle import LoadResult, iter_events, load_bundle, verify_bundle
from carto_core.db.clickhouse import ClickHouseWriter, create_client
from carto_core.db.postgres import create_engine_from_settings
from carto_core.ingest.app import create_app
from carto_core.ingest.health import PostgresSourceHealthStore, SourceHealthRecord
from carto_core.ingest.ledger import ClaimState, LedgerEntry, PostgresBatchLedger
from carto_core.migrations import (
    MIGRATION_LOCK_KEY,
    apply_clickhouse_migrations,
    apply_postgres_migrations,
    apply_retention,
    migration_lock,
)
from carto_core.settings import ClickHouseSettings, CoreSettings, PostgresSettings
from carto_edge.cli.analyze import analyze
from carto_schema.bundle import (
    DATA_FILES,
    EVENTS_FILE,
    FIELDS_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
    SIGNATURE_FILE,
    TEMPLATES_FILE,
    BundleCounts,
    BundleManifest,
    BundleSignature,
    BundleSourceSummary,
    FileDigest,
)
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch, SourceHeartbeat, SourceStatus
from carto_simulator.api import GenerationRequest, generate, run_as_of

with warnings.catch_warnings():
    # starlette 1.7 deprecates httpx (vs httpx2) under its TestClient at import time.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

# Pinned defaults; CARTO_TEST_*_IMAGE lets a developer or CI point at an image already present.
CLICKHOUSE_IMAGE = os.environ.get(
    "CARTO_TEST_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:24.8"
)
POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"
JSON = {"content-type": "application/json"}
BUNDLE_ID = "01K71Y5B2XQ0M4N8P3R6S9T1VX"
BASE_MS = 1_790_000_000_000
RECENT = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
"""Events are observed an hour ago, inside the plausible range of ADR 0036 whenever this runs."""
ROOT = Path(__file__).resolve().parents[2]


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
    # Docker Desktop's named-pipe transport stalls on starting the Ryuk reaper; the ``with``
    # blocks below stop and remove the containers themselves. Linux CI keeps Ryuk.
    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True


@pytest.fixture(scope="module")
def clickhouse(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ClickHouseSettings]:
    password_file = tmp_path_factory.mktemp("secrets") / "clickhouse"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with ClickHouseContainer(
        CLICKHOUSE_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME
    ) as container:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(8123))
        yield ClickHouseSettings(
            url=f"http://{host}:{port}",
            secure=False,
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            timeout_seconds=60,
        )


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
def migrated(
    clickhouse: ClickHouseSettings, postgres: PostgresSettings
) -> Iterator[tuple[Client, sqlalchemy.Engine]]:
    client = create_client(clickhouse)
    apply_clickhouse_migrations(client, RetentionSettings())
    engine = create_engine_from_settings(postgres)
    apply_postgres_migrations(engine)
    yield client, engine
    engine.dispose()
    client.close()


def _event(tag: str, index: int, observed_at: datetime | None = None) -> CanonicalEvent:
    ms = BASE_MS + index
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(ms, tag, str(index)),
        "observed_at": observed_at or RECENT + timedelta(milliseconds=index),
    }
    return CanonicalEvent.model_validate(data)


def _count(client: Client, table: str, event_ids: Sequence[str]) -> int:
    result = client.query(
        "SELECT count() FROM {table:Identifier} WHERE event_id IN {ids:Array(String)}",
        parameters={"table": table, "ids": list(event_ids)},
    )
    return int(result.result_rows[0][0])


def _write_bundle(path: Path, events: Sequence[CanonicalEvent]) -> None:
    path.mkdir()
    lines = b"".join(event.model_dump_json().encode("utf-8") + b"\n" for event in events)
    (path / EVENTS_FILE).write_bytes(zstandard.ZstdCompressor(level=3).compress(lines))
    (path / FIELDS_FILE).write_bytes(b"[]\n")
    (path / TEMPLATES_FILE).write_bytes(b"[]\n")
    (path / MANIFEST_MD_FILE).write_bytes(b"# integration bundle\n")
    files = {
        name: FileDigest(
            sha256=hashlib.sha256((path / name).read_bytes()).hexdigest(),
            bytes=(path / name).stat().st_size,
        )
        for name in DATA_FILES
    }
    count = len(events)
    manifest = BundleManifest(
        bundle_version="1",
        schema_version="1",
        bundle_id=BUNDLE_ID,
        tenant_id="default",
        created_at=datetime(2026, 10, 8, 10, 0, tzinfo=UTC),
        producer="integration test",
        key_versions=[1],
        policy_version="3",
        sources=[
            BundleSourceSummary(
                source_id="src_wms_db",
                system_id="sys_warehouse",
                connector_type="upload",
                records_read=count,
                events_written=count,
                records_dropped=0,
                parse_errors=0,
            )
        ],
        counts=BundleCounts(
            records_read=count,
            events=count,
            records_dropped=0,
            parse_errors=0,
            identifiers=4 * count,
            fields_kept=2,
            fields_tokenized=2,
            fields_dropped=2,
        ),
        files=files,
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    (path / MANIFEST_FILE).write_bytes(manifest_bytes)
    key = SigningKey.generate()
    signature = BundleSignature(
        algorithm="ed25519",
        key_id=key.verify_key.key_id,
        public_key=key.verify_key.to_text(),
        signature=b64url_encode(key.sign(manifest_bytes)),
        signed_at=manifest.created_at,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    (path / SIGNATURE_FILE).write_bytes(signature.model_dump_json(indent=2).encode("utf-8"))


def test_clickhouse_migrations_apply_once_and_retention_rewrites_ttls(
    clickhouse: ClickHouseSettings,
) -> None:
    client = create_client(clickhouse)
    try:
        names = [
            "0001_events.sql",
            "0002_event_identifiers.sql",
            "0003_drop_events.sql",
            "0004_events_replacing.sql",
            "0005_drop_event_identifiers.sql",
            "0006_event_identifiers_replacing.sql",
        ]
        first = apply_clickhouse_migrations(client, RetentionSettings())
        assert first in (names, [])
        assert apply_clickhouse_migrations(client, RetentionSettings()) == []
        ledger = client.query("SELECT name FROM schema_migrations ORDER BY name").result_rows
        assert [row[0] for row in ledger] == names
        tables = client.query(
            "SELECT name FROM system.tables WHERE database = currentDatabase()"
        ).result_rows
        assert {"events", "event_identifiers", "schema_migrations"} <= {row[0] for row in tables}
        assert apply_retention(client, RetentionSettings()) == []
        assert apply_retention(client, RetentionSettings(events_days=45)) == [
            "events",
            "event_identifiers",
        ]
        for table in ("events", "event_identifiers"):
            engine_full = client.query(
                "SELECT engine_full FROM system.tables "
                "WHERE database = currentDatabase() AND name = {name:String}",
                parameters={"name": table},
            ).result_rows[0][0]
            assert "toIntervalDay(45)" in engine_full, engine_full
            assert engine_full.startswith("ReplacingMergeTree"), engine_full
        assert apply_retention(client, RetentionSettings(events_days=45)) == []
    finally:
        client.close()


def test_postgres_migrations_apply_idempotently(postgres: PostgresSettings) -> None:
    engine = create_engine_from_settings(postgres)
    try:
        apply_postgres_migrations(engine)
        apply_postgres_migrations(engine)
        inspector = sqlalchemy.inspect(engine)
        tables = set(inspector.get_table_names())
        assert {
            "systems",
            "sources",
            "source_health",
            "ingest_batches",
            "alembic_version",
        } <= tables
        for table in ("systems", "sources", "source_health", "ingest_batches"):
            columns = {column["name"] for column in inspector.get_columns(table)}
            assert {"tenant_id", "created_at", "updated_at"} <= columns, table
        ledger_columns = {column["name"] for column in inspector.get_columns("ingest_batches")}
        assert ledger_columns == {
            "tenant_id",
            "batch_id",
            "source_id",
            "received_at",
            "event_count",
            "state",
            "claimed_at",
            "created_at",
            "updated_at",
        }
        with engine.connect() as connection:
            version = connection.execute(
                sqlalchemy.text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == "0003"
        health_columns = {column["name"] for column in inspector.get_columns("source_health")}
        assert "sent_at" in health_columns
    finally:
        engine.dispose()


def test_the_migration_lock_excludes_a_second_migrate_run(postgres: PostgresSettings) -> None:
    engine = create_engine_from_settings(postgres)
    other = create_engine_from_settings(postgres)
    probe = sqlalchemy.text("SELECT pg_try_advisory_lock(:key)")
    release = sqlalchemy.text("SELECT pg_advisory_unlock(:key)")
    try:
        with migration_lock(engine), other.connect() as connection:
            assert connection.execute(probe, {"key": MIGRATION_LOCK_KEY}).scalar_one() is False
        with other.connect() as connection:
            assert connection.execute(probe, {"key": MIGRATION_LOCK_KEY}).scalar_one() is True
            connection.execute(release, {"key": MIGRATION_LOCK_KEY})
    finally:
        engine.dispose()
        other.dispose()


def test_batch_through_the_app_lands_in_clickhouse_and_the_ledger(
    migrated: tuple[Client, sqlalchemy.Engine],
    clickhouse: ClickHouseSettings,
    postgres: PostgresSettings,
) -> None:
    client, engine = migrated
    settings = CoreSettings(clickhouse=clickhouse, postgres=postgres)
    app = create_app(
        settings,
        ClickHouseWriter(client),
        PostgresBatchLedger(engine),
        PostgresSourceHealthStore(engine),
    )
    events = [_event("app", 0), _event("app", 1)]
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=datetime.now(UTC),
        events=events,
    )
    heartbeat = SourceHeartbeat(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        sent_at=datetime.now(UTC),
        status=SourceStatus.DEGRADED,
        last_success_at=datetime.now(UTC),
        lag_seconds=12.5,
        error_count=2,
        buffer_depth=40,
        oldest_buffered_at=None,
        message="splunk export slow",
    )
    body = batch.model_dump_json().encode("utf-8")
    with TestClient(ActAsRole(app, "edge-gateway")) as test_client:  # ADR 0031
        assert test_client.get("/readyz").status_code == 200
        first = test_client.post("/internal/ingest", content=body, headers=JSON)
        assert first.status_code == 200, first.text
        assert first.json() == {"accepted": 2, "duplicate": False}
        second = test_client.post("/internal/ingest", content=body, headers=JSON)
        assert second.json() == {"accepted": 2, "duplicate": True}
        beat = test_client.post(
            "/internal/heartbeat", content=heartbeat.model_dump_json().encode(), headers=JSON
        )
        assert beat.status_code == 200, beat.text
    ids = [event.event_id for event in events]
    assert _count(client, "events", ids) == 2
    assert _count(client, "event_identifiers", ids) == 8
    identifier_rows = client.query(
        "SELECT field_ref, form, shape FROM event_identifiers WHERE event_id = {id:String} "
        "ORDER BY field_ref, form",
        parameters={"id": ids[0]},
    ).result_rows
    assert [tuple(row) for row in identifier_rows] == [
        ("sys_warehouse/tpl_4f1c9a/order_ref", "digits.0", "9999"),
        ("sys_warehouse/tpl_4f1c9a/order_ref", "raw", "AA-9999999"),
        ("sys_warehouse/tpl_4f1c9a/po_num", "alnum", "99999"),
        ("sys_warehouse/tpl_4f1c9a/po_num", "raw", "99-999"),
    ]
    event_row = client.query(
        "SELECT kind, attributes, actor_token, actor_kind, severity, system_id FROM events "
        "WHERE event_id = {id:String}",
        parameters={"id": ids[0]},
    ).result_rows[0]
    assert tuple(event_row) == (
        "row_change",
        {"status": "CREATED", "warehouse_code": "DC-03"},
        "t1.Hh3Vq6Zt1Nm4Rk8Pw2Ls7D",
        "human",
        None,
        "sys_warehouse",
    )
    with engine.connect() as connection:
        event_count = connection.execute(
            sqlalchemy.text(
                "SELECT event_count FROM ingest_batches "
                "WHERE tenant_id = :tenant_id AND batch_id = :batch_id"
            ),
            {"tenant_id": "default", "batch_id": batch.batch_id},
        ).scalar_one()
    assert event_count == 2
    record = PostgresSourceHealthStore(engine).get("default", "src_wms_db")
    assert record is not None
    assert record.status == "degraded"
    assert record.lag_seconds == 12.5
    assert record.error_count == 2
    assert record.message == "splunk export slow"


def test_postgres_heartbeat_upsert_never_goes_back_in_time(
    migrated: tuple[Client, sqlalchemy.Engine],
) -> None:
    _client, engine = migrated
    store = PostgresSourceHealthStore(engine)
    base = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)

    def record(status: str, sent_minute: int) -> SourceHealthRecord:
        sent = base.replace(minute=sent_minute)
        return SourceHealthRecord(
            tenant_id="default",
            source_id="src_order_svc",
            status=status,
            last_success_at=None,
            lag_seconds=0.0,
            error_count=0,
            buffer_depth=0,
            oldest_buffered_at=None,
            message="",
            received_at=base.replace(minute=59),
            sent_at=sent,
        )

    store.upsert(record("ok", 30))
    store.upsert(record("failing", 10))
    stored = store.get("default", "src_order_svc")
    assert stored is not None
    assert (stored.status, stored.sent_at) == ("ok", base.replace(minute=30))
    store.upsert(record("degraded", 40))
    stored = store.get("default", "src_order_svc")
    assert stored is not None and stored.status == "degraded"


def test_bundle_loads_into_clickhouse_idempotently(
    migrated: tuple[Client, sqlalchemy.Engine], tmp_path: Path
) -> None:
    client, engine = migrated
    events = [_event("bundle", index) for index in range(7)]
    path = tmp_path / "scenario.carto"
    _write_bundle(path, events)
    verified = verify_bundle(path)
    writer, ledger = ClickHouseWriter(client), PostgresBatchLedger(engine)
    assert load_bundle(verified, writer, ledger, chunk_size=3) == LoadResult(7, 3, 0)
    ids = [event.event_id for event in events]
    assert _count(client, "events", ids) == 7
    assert _count(client, "event_identifiers", ids) == 28
    assert load_bundle(verified, writer, ledger, chunk_size=3) == LoadResult(0, 3, 3)
    assert _count(client, "events", ids) == 7
    with engine.connect() as connection:
        chunks = connection.execute(
            sqlalchemy.text("SELECT count(*) FROM ingest_batches WHERE source_id = :source_id"),
            {"source_id": f"bundle_{BUNDLE_ID.lower()}"},
        ).scalar_one()
    assert chunks == 3


def test_postgres_ledger_claims_take_over_release_complete_and_prune(
    migrated: tuple[Client, sqlalchemy.Engine],
) -> None:
    """ADR 0034 on the real table: the same cases as test_core_ledger.py."""
    _client, engine = migrated
    ledger = PostgresBatchLedger(engine)
    t0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    timeout = timedelta(seconds=120)
    batch_id = new_ulid()

    def entry(at: datetime) -> LedgerEntry:
        return LedgerEntry("default", batch_id, "src_ledger", 3, at)

    first = ledger.claim(entry(t0), t0 - timeout)
    assert (first.state, first.took_over) == (ClaimState.CLAIMED, False)
    soon = t0 + timedelta(seconds=60)
    assert ledger.claim(entry(soon), soon - timeout).state is ClaimState.BUSY
    later = t0 + timedelta(seconds=180)
    taken = ledger.claim(entry(later), later - timeout)
    assert (taken.state, taken.took_over) == (ClaimState.CLAIMED, True)
    ledger.release("default", batch_id, first.claimed_at)
    assert ledger.claim(entry(later), later - timeout).state is ClaimState.BUSY, "kept"
    ledger.release("default", batch_id, taken.claimed_at)
    again = ledger.claim(entry(later), later - timeout)
    assert (again.state, again.took_over) == (ClaimState.CLAIMED, False), "released"
    ledger.complete(entry(t0), later)
    much_later = t0 + timedelta(days=2)
    assert ledger.claim(entry(much_later), much_later - timeout).state is ClaimState.DONE
    with engine.connect() as connection:
        state = connection.execute(
            sqlalchemy.text(
                "SELECT state FROM ingest_batches WHERE tenant_id = 'default' "
                "AND batch_id = :batch_id"
            ),
            {"batch_id": batch_id},
        ).scalar_one()
    assert state == "done"
    assert ledger.prune(datetime(2000, 1, 1, tzinfo=UTC)) == 0
    # released and claimed again at `later`, so that is the receipt time the prune sees
    assert ledger.prune(later + timedelta(seconds=1)) >= 1
    assert ledger.claim(entry(much_later), much_later - timeout).state is ClaimState.CLAIMED


def test_concurrent_takeovers_of_a_stale_claim_admit_one(
    migrated: tuple[Client, sqlalchemy.Engine],
) -> None:
    _client, engine = migrated
    ledger = PostgresBatchLedger(engine)
    t0 = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    batch_id = new_ulid()
    stale = LedgerEntry("default", batch_id, "src_ledger", 1, t0)
    assert ledger.claim(stale, t0 - timedelta(seconds=120)).state is ClaimState.CLAIMED
    later = t0 + timedelta(minutes=10)

    def take(offset: int) -> ClaimState:
        at = later + timedelta(microseconds=offset)
        entry = LedgerEntry("default", batch_id, "src_ledger", 1, at)
        return ledger.claim(entry, later - timedelta(seconds=120)).state

    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(take, range(8)))
    assert states.count(ClaimState.CLAIMED) == 1
    assert states.count(ClaimState.BUSY) == 7


def test_a_second_copy_of_a_batch_merges_away(migrated: tuple[Client, sqlalchemy.Engine]) -> None:
    """ADR 0034 item 2: a replayed batch is one row per event and identifier form under FINAL."""
    client, _engine = migrated
    writer = ClickHouseWriter(client)
    events = [_event("replayed", index) for index in range(3)]
    writer.write_events(events)
    writer.write_events(events)
    ids = [event.event_id for event in events]
    for table, expected in (("events", 3), ("event_identifiers", 12)):
        final = client.query(
            "SELECT count() FROM {table:Identifier} FINAL WHERE event_id IN {ids:Array(String)}",
            parameters={"table": table, "ids": ids},
        )
        assert int(final.result_rows[0][0]) == expected, table


def test_a_batch_over_more_than_a_hundred_days_is_written(
    migrated: tuple[Client, sqlalchemy.Engine],
) -> None:
    """ADR 0036 item 2: one insert of 150 partition days would hit TOO_MANY_PARTS."""
    client, _engine = migrated
    events = [
        _event("spread", day, RECENT - timedelta(days=day, milliseconds=day)) for day in range(150)
    ]
    assert ClickHouseWriter(client).write_events(events).events == 150


def test_one_client_takes_concurrent_inserts(migrated: tuple[Client, sqlalchemy.Engine]) -> None:
    """ingest-api shares one client across its threadpool: concurrent inserts must not trip
    over a ClickHouse session (SESSION_IS_LOCKED)."""
    client, _engine = migrated
    writer = ClickHouseWriter(client)
    groups = [[_event(f"concurrent{group}", index) for index in range(20)] for group in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(writer.write_events, groups))
    assert [result.events for result in results] == [20] * 8
    ids = [event.event_id for group in groups for event in group]
    assert _count(client, "events", ids) == len(ids)


def _leak_scan_module() -> Any:
    """``tools/ci/leak_scan.py``, the scanner CI runs over the Compose stack's rows."""
    spec = importlib.util.spec_from_file_location(
        "leak_scan", ROOT / "tools" / "ci" / "leak_scan.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scenario_a_bundle_from_the_analyzer_loads_into_core(
    migrated: tuple[Client, sqlalchemy.Engine], tmp_path: Path
) -> None:
    client, engine = migrated
    sim = tmp_path / "shop"
    generate(GenerationRequest(days=2, daily_volume=20, seed=11), sim)
    out = tmp_path / "shop.carto"
    # The scenario has fixed dates: analyze and load it as of its end (ADR 0036).
    as_of = run_as_of(sim)
    result = analyze(
        ROOT / "simulator" / "analyze.shop.yaml",
        sim,
        out,
        state_dir=tmp_path / "state",
        as_of=as_of,
    )
    expected = result.manifest.counts.events
    assert expected > 0
    verified = verify_bundle(out)
    writer, ledger = ClickHouseWriter(client), PostgresBatchLedger(engine)
    loaded = load_bundle(verified, writer, ledger, clock=lambda: as_of)
    assert loaded.events_written == expected
    assert loaded.duplicates == 0
    again = load_bundle(verified, writer, ledger, clock=lambda: as_of)
    assert again == LoadResult(0, loaded.chunks, loaded.chunks)

    event_ids = [event.event_id for event in iter_events(verified)]
    assert len(event_ids) == expected
    # Thousands of ids exceed ClickHouse's HTTP parameter limit: query in chunks.
    chunks = [event_ids[i : i + 500] for i in range(0, len(event_ids), 500)]
    assert sum(_count(client, "events", chunk) for chunk in chunks) == expected
    rows = tmp_path / "rows.ndjson"
    with rows.open("wb") as handle:
        for table in ("events", "event_identifiers"):
            for chunk in chunks:
                handle.write(
                    client.raw_query(
                        "SELECT * FROM {table:Identifier} WHERE event_id IN {ids:Array(String)} "
                        "FORMAT JSONEachRow",
                        parameters={"table": table, "ids": chunk},
                    )
                )

    leak_scan = _leak_scan_module()
    markers = leak_scan.Markers(
        json.loads((sim / "ground_truth" / "markers.json").read_text(encoding="utf-8"))
    )
    assert len(markers) > 0
    hits = leak_scan.scan_rows(rows, markers)
    assert not hits, f"markers in the ClickHouse rows: {dict(hits)}"
