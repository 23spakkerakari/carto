"""ClickHouse migrations (spec 7.2, Section 20): the DDL renders with the configured TTL, files
apply in order exactly once with a checksum guard, and retention changes rewrite the TTLs. The
Alembic scripts form a single linear history."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
import subprocess
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from carto_common.settings import RetentionSettings
from carto_core.migrations import (
    CLICKHOUSE_MIGRATIONS_DIR,
    MIGRATION_TABLE,
    POSTGRES_MIGRATIONS_DIR,
    Migration,
    MigrationError,
    alembic_config,
    apply_clickhouse_migrations,
    apply_retention,
    current_ttl_days,
    load_clickhouse_migrations,
    render,
)

ALL_MIGRATIONS = [
    "0001_events.sql",
    "0002_event_identifiers.sql",
    "0003_drop_events.sql",
    "0004_events_replacing.sql",
    "0005_drop_event_identifiers.sql",
    "0006_event_identifiers_replacing.sql",
    "0007_events_stored_at.sql",
    "0008_event_identifiers_stored_at.sql",
]


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class FakeClickHouse:
    """Just enough of clickhouse-connect for the migration runner."""

    def __init__(self, engine_full: dict[str, str] | None = None) -> None:
        self.commands: list[str] = []
        self.rows: list[tuple[str, datetime, str]] = []
        self.engine_full = dict(engine_full or {})
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def command(self, cmd: str, parameters: dict[str, Any] | None = None) -> str:
        assert parameters is None
        self.commands.append(cmd)
        match = re.match(r"ALTER TABLE (\w+) MODIFY TTL .* INTERVAL (\d+) DAY", cmd)
        if match:
            self.engine_full[match.group(1)] = (
                "MergeTree ORDER BY x TTL toDateTime(observed_at) + "
                f"toIntervalDay({match.group(2)})"
            )
        return ""

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _Result:
        self.queries.append((query, parameters))
        if f"FROM {MIGRATION_TABLE}" in query:
            return _Result([(name, checksum) for name, _at, checksum in self.rows])
        if "FROM system.tables" in query:
            assert parameters is not None
            name = parameters["name"]
            return _Result([(self.engine_full[name],)] if name in self.engine_full else [])
        msg = f"unexpected query: {query}"
        raise AssertionError(msg)

    def insert(
        self, table: str, data: Sequence[Sequence[Any]], column_names: Sequence[str]
    ) -> None:
        assert table == MIGRATION_TABLE
        assert list(column_names) == ["name", "applied_at", "checksum"]
        for row in data:
            self.rows.append((row[0], row[1], row[2]))


# --- files -----------------------------------------------------------------------------------


def test_migration_files_are_named_ordered_and_checksummed() -> None:
    migrations = load_clickhouse_migrations()
    assert [m.name for m in migrations] == ALL_MIGRATIONS
    for migration in migrations:
        assert re.fullmatch(r"[0-9a-f]{64}", migration.checksum)
        expected = hashlib.sha256(migration.sql.encode("utf-8")).hexdigest()
        assert migration.checksum == expected
        assert "\r" not in migration.sql
        assert migration.sql.count(";") == 1, "one statement per file (ClickHouse HTTP)"


def test_checksum_ignores_line_endings(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_bytes(b"CREATE TABLE a (x String) ENGINE = Memory;\r\n")
    (tmp_path / "0002_b.sql").write_bytes(b"CREATE TABLE b (x String) ENGINE = Memory;\n")
    first, second = load_clickhouse_migrations(tmp_path)
    assert "\r" not in first.sql
    assert first.checksum == hashlib.sha256(first.sql.encode()).hexdigest()
    assert second.name == "0002_b.sql"


@pytest.mark.parametrize(
    "names",
    [["0001_a.sql", "0003_c.sql"], ["0001_a.sql", "0001_b.sql"], ["0002_b.sql"], ["01_a.sql"]],
)
def test_migration_file_sequence_must_be_consecutive_from_one(
    tmp_path: Path, names: list[str]
) -> None:
    for name in names:
        (tmp_path / name).write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError):
        load_clickhouse_migrations(tmp_path)


def test_unrelated_files_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("notes\n", encoding="utf-8")
    assert [m.name for m in load_clickhouse_migrations(tmp_path)] == ["0001_a.sql"]


# --- rendering (spec 7.2 DDL) ------------------------------------------------------------------


def _rendered(name: str, days: int) -> str:
    migration = next(m for m in load_clickhouse_migrations() if m.name == name)
    return render(migration, RetentionSettings(events_days=days))


def test_events_ddl_reproduces_spec_7_2_with_the_configured_ttl() -> None:
    sql = _rendered("0004_events_replacing.sql", 45)
    assert "CREATE TABLE IF NOT EXISTS events (" in sql
    for column in (
        "tenant_id LowCardinality(String)",
        "event_id String",
        "source_id LowCardinality(String)",
        "system_id LowCardinality(String)",
        "kind LowCardinality(String)",
        "observed_at DateTime64(3, 'UTC')",
        "ingested_at DateTime64(3, 'UTC')",
        "observed_at_quality LowCardinality(String)",
        "template_id LowCardinality(String)",
        "severity LowCardinality(Nullable(String))",
        "attributes Map(LowCardinality(String), String)",
        "actor_token Nullable(String)",
        "actor_kind LowCardinality(Nullable(String))",
    ):
        assert column in sql, column
    assert ") ENGINE = ReplacingMergeTree(ingested_at)" in sql, "ADR 0034"
    assert "PARTITION BY toYYYYMMDD(observed_at)" in sql
    assert "ORDER BY (tenant_id, system_id, template_id, observed_at, event_id)" in sql
    assert "TTL toDateTime(observed_at) + INTERVAL 45 DAY;" in sql
    assert "{" not in sql and "}" not in sql


def test_identifiers_ddl_reproduces_spec_7_2_with_the_configured_ttl() -> None:
    sql = _rendered("0006_event_identifiers_replacing.sql", 7)
    assert "CREATE TABLE IF NOT EXISTS event_identifiers (" in sql
    for column in (
        "tenant_id LowCardinality(String)",
        "token String",
        "field_ref LowCardinality(String)",
        "form LowCardinality(String)",
        "shape LowCardinality(String)",
        "event_id String",
        "system_id LowCardinality(String)",
        "observed_at DateTime64(3, 'UTC')",
    ):
        assert column in sql, column
    assert ") ENGINE = ReplacingMergeTree" in sql, "ADR 0034"
    assert "PARTITION BY toYYYYMMDD(observed_at)" in sql
    # ADR 0034: the event, field and form are in the key, so only true duplicates merge.
    assert "ORDER BY (tenant_id, token, observed_at, event_id, field_ref, form)" in sql
    assert "TTL toDateTime(observed_at) + INTERVAL 7 DAY;" in sql


def test_render_rejects_unknown_placeholders() -> None:
    migration = Migration(name="0001_x.sql", sql="SELECT {bogus};", checksum="0" * 64)
    with pytest.raises(MigrationError, match="bogus"):
        render(migration, RetentionSettings())


def test_render_substitutes_every_retention_placeholder() -> None:
    migration = Migration(
        name="0001_x.sql",
        sql="TTL {events_days} and {txn_membership_days};",
        checksum="0" * 64,
    )
    assert render(migration, RetentionSettings(events_days=10, txn_membership_days=40)) == (
        "TTL 10 and 40;"
    )


# --- applying --------------------------------------------------------------------------------


def test_apply_runs_pending_migrations_in_order_and_records_them() -> None:
    client = FakeClickHouse()
    applied = apply_clickhouse_migrations(client, RetentionSettings(events_days=45))
    assert applied == ALL_MIGRATIONS
    assert client.commands[0].startswith(f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE}")
    assert "ENGINE = MergeTree" in client.commands[0] and "ORDER BY name" in client.commands[0]
    assert "CREATE TABLE IF NOT EXISTS events (" in client.commands[1]
    assert "INTERVAL 45 DAY" in client.commands[1]
    assert "CREATE TABLE IF NOT EXISTS event_identifiers (" in client.commands[2]
    assert "DROP TABLE IF EXISTS events SYNC;" in client.commands[3]
    assert "ENGINE = ReplacingMergeTree(ingested_at)" in client.commands[4]
    assert "INTERVAL 45 DAY" in client.commands[4]
    assert "DROP TABLE IF EXISTS event_identifiers SYNC;" in client.commands[5]
    assert "ENGINE = ReplacingMergeTree" in client.commands[6]
    assert (
        client.commands[7]
        .rstrip()
        .endswith(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS stored_at DateTime64(3, 'UTC') "
            "DEFAULT ingested_at;"
        )
    ), "ADR 0046"
    assert (
        client.commands[8]
        .rstrip()
        .endswith(
            "ALTER TABLE event_identifiers ADD COLUMN IF NOT EXISTS stored_at DateTime64(3, 'UTC') "
            "DEFAULT observed_at;"
        )
    ), "ADR 0046: event_identifiers has no ingested_at"
    assert [name for name, _at, _sum in client.rows] == applied
    assert all(at.tzinfo is not None for _name, at, _sum in client.rows)
    checksums = {m.name: m.checksum for m in load_clickhouse_migrations()}
    assert {name: checksum for name, _at, checksum in client.rows} == checksums
    # A second run finds nothing to do and runs no DDL.
    before = list(client.commands)
    assert apply_clickhouse_migrations(client, RetentionSettings(events_days=45)) == []
    assert client.commands == [*before, before[0]]


def test_apply_runs_only_the_missing_tail() -> None:
    client = FakeClickHouse()
    first = load_clickhouse_migrations()[0]
    client.rows.append((first.name, datetime.fromtimestamp(0, tz=UTC), first.checksum))
    assert apply_clickhouse_migrations(client, RetentionSettings()) == ALL_MIGRATIONS[1:]
    assert render(first, RetentionSettings()) not in client.commands


def test_apply_refuses_an_applied_migration_whose_file_changed() -> None:
    client = FakeClickHouse()
    client.rows.append(("0001_events.sql", datetime.fromtimestamp(0, tz=UTC), "f" * 64))
    with pytest.raises(MigrationError, match=r"0001_events\.sql.*checksum"):
        apply_clickhouse_migrations(client, RetentionSettings())
    assert len(client.commands) == 1, "nothing beyond the migration table was touched"


def test_apply_queries_the_ledger_with_parameters_only() -> None:
    client = FakeClickHouse()
    apply_clickhouse_migrations(client, RetentionSettings())
    for query, parameters in client.queries:
        assert "'" not in query, query
        if parameters:
            assert all(isinstance(v, str) for v in parameters.values())


# --- retention (spec 7.2 "migration job rewrites TTLs") -----------------------------------------


def _engine(days: int) -> str:
    return (
        "MergeTree PARTITION BY toYYYYMMDD(observed_at) ORDER BY (tenant_id, token, observed_at) "
        f"TTL toDateTime(observed_at) + toIntervalDay({days}) SETTINGS index_granularity = 8192"
    )


def test_current_ttl_days_parses_both_interval_spellings() -> None:
    assert current_ttl_days(_engine(30)) == 30
    assert current_ttl_days("MergeTree TTL toDateTime(observed_at) + INTERVAL 45 DAY") == 45
    assert current_ttl_days("MergeTree ORDER BY x") is None


def test_apply_retention_alters_only_tables_whose_ttl_differs() -> None:
    client = FakeClickHouse({"events": _engine(30), "event_identifiers": _engine(45)})
    changed = apply_retention(client, RetentionSettings(events_days=45))
    assert changed == ["events"]
    assert client.commands == [
        "ALTER TABLE events MODIFY TTL toDateTime(observed_at) + INTERVAL 45 DAY"
    ]
    assert apply_retention(client, RetentionSettings(events_days=45)) == []


def test_apply_retention_skips_tables_that_do_not_exist_yet() -> None:
    client = FakeClickHouse({"events": _engine(30)})
    assert apply_retention(client, RetentionSettings(events_days=30)) == []
    assert apply_retention(client, RetentionSettings(events_days=400, txn_membership_days=400)) == [
        "events"
    ]


def test_apply_retention_rewrites_an_unreadable_ttl() -> None:
    client = FakeClickHouse({"events": "MergeTree ORDER BY x", "event_identifiers": _engine(30)})
    assert apply_retention(client, RetentionSettings(events_days=30)) == ["events"]


# --- postgres scripts --------------------------------------------------------------------------


def test_postgres_scripts_form_one_linear_history() -> None:
    assert (POSTGRES_MIGRATIONS_DIR / "alembic.ini").is_file()
    assert (POSTGRES_MIGRATIONS_DIR / "env.py").is_file()
    assert (POSTGRES_MIGRATIONS_DIR / "script.py.mako").is_file()
    config = Config()
    config.set_main_option("script_location", str(POSTGRES_MIGRATIONS_DIR))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0004"]
    revisions = list(script.walk_revisions())
    assert [revision.revision for revision in revisions] == ["0004", "0003", "0002", "0001"]
    assert revisions[-1].down_revision is None
    assert [revision.down_revision for revision in revisions[:-1]] == ["0003", "0002", "0001"]


def test_migration_0004_builds_only_fixed_grant_statements() -> None:
    """Spec 14.7: the guarded GRANT blocks are fixed strings with the fixed role names."""
    path = POSTGRES_MIGRATIONS_DIR / "versions" / "0004_map_discovery.py"
    spec = importlib.util.spec_from_file_location("migration_0004", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    grants = module._grant_statements()
    assert len(grants) == 3
    for statement in grants:
        assert statement.startswith(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        )
        assert statement.endswith(" END IF; END $$;")
        assert ":" not in statement and "%" not in statement
    app, worker, ingest = grants
    assert "TO carto_app;" in app and "GRANT SELECT, INSERT ON audit_log TO carto_app;" in app
    assert "TO carto_worker;" in worker
    assert "UPDATE, DELETE" not in ingest and "audit_log" not in ingest
    assert "GRANT SELECT, INSERT, UPDATE ON templates, fields, systems, sources" in ingest
    assert len(module._revoke_statements()) == 3
    assert "RAISE EXCEPTION" in module.AUDIT_TRIGGER_FUNCTION
    assert "BEFORE UPDATE OR DELETE ON audit_log" in module.AUDIT_TRIGGER_ROWS


def test_clickhouse_migrations_dir_is_the_repository_layout_of_section_20() -> None:
    assert CLICKHOUSE_MIGRATIONS_DIR.name == "clickhouse"
    assert CLICKHOUSE_MIGRATIONS_DIR.parent.name == "migrations"
    assert CLICKHOUSE_MIGRATIONS_DIR.parent.parent.name == "core"


def test_a_missing_or_empty_migrations_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match="no ClickHouse migrations directory"):
        load_clickhouse_migrations(tmp_path / "missing")
    with pytest.raises(MigrationError, match="no ClickHouse migration files"):
        load_clickhouse_migrations(tmp_path)
    with pytest.raises(MigrationError, match="no Alembic environment"):
        alembic_config(tmp_path)


@pytest.mark.slow
def test_the_core_wheel_carries_the_migration_scripts(tmp_path: Path) -> None:
    """The service images install core as a wheel; ``carto-core migrate`` needs the scripts."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    root = Path(__file__).resolve().parents[2]
    subprocess.run(  # noqa: S603 - fixed arguments, uv from PATH
        [uv, "build", "--package", "carto-core", "--wheel", "--out-dir", str(tmp_path)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (wheel,) = tmp_path.glob("carto_core-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    expected = {
        f"carto_core/_migrations/clickhouse/{path.name}"
        for path in (root / "core" / "migrations" / "clickhouse").glob("*.sql")
    }
    assert expected
    assert expected <= names
    assert "carto_core/_migrations/postgres/env.py" in names
    assert "carto_core/_migrations/postgres/alembic.ini" in names
    assert any(name.startswith("carto_core/_migrations/postgres/versions/") for name in names)
