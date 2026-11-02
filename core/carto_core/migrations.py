"""Schema migrations for both core stores (spec 7.2, 7.3, 14.10, Section 20).

ClickHouse: ``core/migrations/clickhouse/NNNN_name.sql`` holds one statement per file, applied in
name order exactly once and recorded in :data:`MIGRATION_TABLE` with the sha256 of the file. A
recorded file whose checksum changed is refused: a migration is edited by adding a new file,
never by rewriting an applied one. Retention placeholders (``{events_days}``) render from
:class:`~carto_common.settings.RetentionSettings`, and :func:`apply_retention` rewrites the
``TTL`` of the tables that carry one when the configured days differ from what the server has
(spec 7.2 "Retention TTLs are set from configuration at install and on change (migration job
rewrites TTLs)"). The only SQL built from values is that ``ALTER``, from a validated integer and a
fixed table name; lookups use server-side query parameters (spec 14.7).

PostgreSQL: Alembic scripts under ``core/migrations/postgres`` run through the Alembic API on an
engine built from settings (``carto-core migrate``); the password never lands in ``alembic.ini``.

``carto-core migrate`` runs both stores under :func:`migration_lock`, a PostgreSQL session
advisory lock with a fixed key, so two migrate jobs started together (a Compose restart, two
pods) never interleave. ClickHouse has no lock of its own; its ledger row is written right
after each statement runs.

The wheel carries both script directories as ``carto_core/_migrations`` (``core/pyproject.toml``),
so the service images find them; a source checkout uses ``core/migrations``. A missing or empty
directory is an error, never "up to date".
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

import sqlalchemy
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import SQLAlchemyError

from carto_common.logging import get_logger

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from carto_common.settings import RetentionSettings

__all__ = [
    "CLICKHOUSE_MIGRATIONS_DIR",
    "MIGRATION_LOCK_KEY",
    "MIGRATION_TABLE",
    "POSTGRES_MIGRATIONS_DIR",
    "TTL_TABLES",
    "Migration",
    "MigrationError",
    "apply_clickhouse_migrations",
    "apply_postgres_migrations",
    "apply_retention",
    "current_ttl_days",
    "load_clickhouse_migrations",
    "migration_lock",
    "render",
]

_PACKAGED_DIR: Final = Path(__file__).resolve().parent / "_migrations"
_SOURCE_DIR: Final = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATIONS_DIR: Final = _PACKAGED_DIR if _PACKAGED_DIR.is_dir() else _SOURCE_DIR
CLICKHOUSE_MIGRATIONS_DIR: Final = _MIGRATIONS_DIR / "clickhouse"
POSTGRES_MIGRATIONS_DIR: Final = _MIGRATIONS_DIR / "postgres"

MIGRATION_TABLE: Final = "schema_migrations"
MIGRATION_TABLE_DDL: Final = (
    f"CREATE TABLE IF NOT EXISTS {MIGRATION_TABLE} ("
    "name String, applied_at DateTime64(3, 'UTC'), checksum String"
    ") ENGINE = MergeTree ORDER BY name"
)

TTL_TABLES: Final[tuple[tuple[str, str], ...]] = (
    ("events", "events_days"),
    ("event_identifiers", "events_days"),
)
"""Tables with a retention TTL and the retention setting that governs it (spec 7.2, 14.10).
``txn_events`` joins with the assembler (M3)."""

_FILE_PATTERN: Final = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_PLACEHOLDER: Final = re.compile(r"\{([a-z_]+)\}")
_RETENTION_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"events_days", "txn_membership_days"})
_TTL_PATTERN: Final = re.compile(
    r"TTL toDateTime\(observed_at\) \+ (?:toIntervalDay\((\d+)\)|INTERVAL (\d+) DAY)"
)
_MAX_MIGRATION_BYTES: Final = 256 * 1024

MIGRATION_LOCK_KEY: Final = 0x636172746F6D6967
"""The advisory lock key of ``carto-core migrate``: the ASCII bytes of ``cartomig``."""

log = get_logger(component="migrations")


class MigrationError(Exception):
    """A migration file, its ledger entry or the server state is not what the runner expects."""


class _QueryResult(Protocol):
    @property
    def result_rows(self) -> Any: ...


class ClickHouseClientLike(Protocol):
    """The three clickhouse-connect calls the runner uses (a fake stands in for tests)."""

    def command(self, cmd: str, parameters: dict[str, Any] | None = None) -> Any: ...

    def query(self, query: str, parameters: dict[str, Any] | None = None) -> _QueryResult: ...

    def insert(
        self, table: str, data: Sequence[Sequence[Any]], column_names: Sequence[str]
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class Migration:
    """One SQL file: its name, its text with ``\\n`` line endings, the sha256 of that text."""

    name: str
    sql: str
    checksum: str


def load_clickhouse_migrations(directory: Path = CLICKHOUSE_MIGRATIONS_DIR) -> list[Migration]:
    """Read ``NNNN_name.sql`` files in order; numbers must run 0001, 0002, ... without gaps."""
    if not directory.is_dir():
        msg = f"no ClickHouse migrations directory at {directory}"
        raise MigrationError(msg)
    found: list[tuple[int, Migration]] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILE_PATTERN.match(path.name)
        if match is None:
            msg = f"migration file name {path.name!r} must look like 0001_name.sql"
            raise MigrationError(msg)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            msg = f"cannot read migration {path.name}"
            raise MigrationError(msg) from exc
        if len(raw) > _MAX_MIGRATION_BYTES:
            msg = f"migration {path.name} is larger than {_MAX_MIGRATION_BYTES} bytes"
            raise MigrationError(msg)
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        found.append((int(match.group(1)), Migration(path.name, text, checksum)))
    for expected, (number, migration) in enumerate(found, start=1):
        if number != expected:
            msg = f"migration {migration.name} breaks the sequence; expected number {expected:04d}"
            raise MigrationError(msg)
    if not found:
        msg = f"no ClickHouse migration files in {directory}"
        raise MigrationError(msg)
    return [migration for _number, migration in found]


def render(migration: Migration, retention: RetentionSettings) -> str:
    """Substitute the retention placeholders; any other ``{name}`` is an error."""
    values = {
        "events_days": retention.events_days,
        "txn_membership_days": retention.txn_membership_days,
    }

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in _RETENTION_PLACEHOLDERS:
            msg = f"migration {migration.name} uses unknown placeholder {{{name}}}"
            raise MigrationError(msg)
        return str(int(values[name]))

    return _PLACEHOLDER.sub(substitute, migration.sql)


def _applied(client: ClickHouseClientLike) -> dict[str, str]:
    query = f"SELECT name, checksum FROM {MIGRATION_TABLE} ORDER BY name"  # noqa: S608  # nosec B608 - constant table name
    result = client.query(query)
    return {str(name): str(checksum) for name, checksum in result.result_rows}


def apply_clickhouse_migrations(
    client: ClickHouseClientLike,
    retention: RetentionSettings,
    *,
    directory: Path = CLICKHOUSE_MIGRATIONS_DIR,
    now: datetime | None = None,
) -> list[str]:
    """Create the ledger table, run every pending file in order, return the names applied."""
    migrations = load_clickhouse_migrations(directory)
    client.command(MIGRATION_TABLE_DDL)
    applied = _applied(client)
    done: list[str] = []
    for migration in migrations:
        recorded = applied.get(migration.name)
        if recorded is not None:
            if recorded != migration.checksum:
                msg = (
                    f"migration {migration.name} was applied with another checksum; applied "
                    "migrations are never edited, add a new file instead"
                )
                raise MigrationError(msg)
            continue
        client.command(render(migration, retention))
        applied_at = now if now is not None else datetime.now(UTC)
        client.insert(
            MIGRATION_TABLE,
            [[migration.name, applied_at, migration.checksum]],
            column_names=["name", "applied_at", "checksum"],
        )
        done.append(migration.name)
    return done


def current_ttl_days(engine_full: str) -> int | None:
    """The day count in a table's ``TTL toDateTime(observed_at) + ...`` clause, if readable."""
    match = _TTL_PATTERN.search(engine_full)
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def apply_retention(client: ClickHouseClientLike, retention: RetentionSettings) -> list[str]:
    """``ALTER TABLE ... MODIFY TTL`` where the configured days differ; return the tables changed.

    Tables that do not exist yet are skipped (their migration renders the TTL on creation).
    """
    changed: list[str] = []
    for table, setting in TTL_TABLES:
        result = client.query(
            "SELECT engine_full FROM system.tables "
            "WHERE database = currentDatabase() AND name = {name:String}",
            parameters={"name": table},
        )
        rows = list(result.result_rows)
        if not rows:
            continue
        days = int(getattr(retention, setting))
        if current_ttl_days(str(rows[0][0])) == days:
            continue
        client.command(
            f"ALTER TABLE {table} MODIFY TTL toDateTime(observed_at) + INTERVAL {days} DAY"
        )
        changed.append(table)
    return changed


@contextmanager
def migration_lock(engine: Engine) -> Iterator[None]:
    """Hold the PostgreSQL session advisory lock :data:`MIGRATION_LOCK_KEY` for the block.

    A second holder waits (and logs that it waits) until the first releases it. The lock lives
    on its own autocommit connection, so no transaction stays open while it is held. It is
    released explicitly, and the connection is discarded when the release fails: a pooled
    connection would otherwise keep the lock.
    """
    parameters = {"key": MIGRATION_LOCK_KEY}
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        taken = connection.execute(
            sqlalchemy.text("SELECT pg_try_advisory_lock(:key)"), parameters
        ).scalar_one()
        if not taken:
            log.info("migration lock held by another migrate run, waiting")
            connection.execute(sqlalchemy.text("SELECT pg_advisory_lock(:key)"), parameters)
        try:
            yield
        finally:
            try:
                connection.execute(sqlalchemy.text("SELECT pg_advisory_unlock(:key)"), parameters)
            except SQLAlchemyError:
                connection.invalidate()
                raise


def alembic_config(directory: Path = POSTGRES_MIGRATIONS_DIR) -> Config:
    """An Alembic configuration pointing at the scripts, with no URL (the engine is injected)."""
    if not (directory / "env.py").is_file():
        msg = f"no Alembic environment at {directory}"
        raise MigrationError(msg)
    config = Config()
    config.set_main_option("script_location", str(directory))
    return config


def apply_postgres_migrations(engine: Engine, *, directory: Path = POSTGRES_MIGRATIONS_DIR) -> None:
    """``alembic upgrade head`` through the API on a connection from ``engine``."""
    config = alembic_config(directory)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
