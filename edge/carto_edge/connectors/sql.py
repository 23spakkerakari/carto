"""SQL polling connector for PostgreSQL, MySQL and SQL Server (spec 8.1.4).

Admin-written query templates (Appendix A) are checked by ``sqlglot`` at construction
(:func:`carto_edge.connectors.sql_dialects.check_statement`: one SELECT, ``:watermark``,
``:batch`` and, for time-typed watermarks, ``:upper``; base tables, CTEs and allowlisted
functions only, ADR 0037), run through SQLAlchemy 2 core ``text()`` on synchronous engines
driven with ``asyncio.to_thread``, inside read-only sessions (PostgreSQL
``default_transaction_read_only`` plus ``BEGIN READ ONLY``; MySQL ``START TRANSACTION READ
ONLY``; SQL Server ``ApplicationIntent=ReadOnly``). ``test()`` runs every query with ``batch=1``
and then inspects the login's privileges on every referenced table, at database scope and
through its roles; a login that can INSERT, UPDATE, DELETE or alter anything, or that sees a
linked server, makes the result ``WRITE_CAPABLE`` and the source cannot be enabled.

Each poll opens a fresh DBAPI connection (``NullPool``) through a ``creator``, so credentials are
resolved at use (spec 14.3) and the host is validated by the network policy at every connect
(spec 14.7): PostgreSQL pins the address with ``hostaddr``; MySQL and SQL Server connect by
name after validation (ADR 0018).

Cursors (ADR 0035, :mod:`carto_edge.connectors.sql_watermarks`): each query has a typed
watermark (``watermark_type``: ``datetime``, ``date``, ``int`` or ``decimal``). A time-typed
query reads the closed range ``(:watermark, :upper]`` where ``:upper`` is the database's own
clock, read at the start of the poll, minus ``settle_seconds`` (default 30); when the range is
fully read the cursor moves to ``:upper``, even if it held no row. Integer and decimal queries
advance on the rows found. On a full page the cursor stops below the page's largest value so a
tie group is never split. The cursor ``{"watermarks": {name: {"t", "v"}}}`` travels in a
:class:`~carto_edge.pipeline.model.CursorCheckpoint` after every page that moved it.

Records: ``fields`` is the row as Python values (the parser stringifies), locator
``<query name>:row:<primary key>`` (ADR 0006), kind ``row_change``, template hint
``row_change <query name>`` (ADR 0016).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from carto_edge.config import SourceConfig
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorError,
    Cursor,
    InvalidConfigError,
    ReadOnlyStatus,
    TestCheck,
    TestResult,
)
from carto_edge.connectors.registry import register, validate_config_model
from carto_edge.connectors.sql_dialects import (
    CLOCK_SQL,
    CheckedStatement,
    Dialect,
    PrivilegeQuery,
    check_statement,
    mssql_connection_string,
    mysql_connect_kwargs,
    mysql_grants_using_sql,
    mysql_roles,
    mysql_session_statements,
    postgres_connect_kwargs,
    privilege_queries,
    write_findings,
)
from carto_edge.connectors.sql_watermarks import (
    TIME_TYPES,
    Watermark,
    WatermarkError,
    WatermarkType,
    coerce,
    decode,
    encode,
    initial,
    plan_page,
    upper_bound,
)
from carto_edge.net.retry import CircuitBreaker
from carto_edge.net.ssrf import SsrfError
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem
from carto_edge.secrets import Credentials, SecretError, parse_credentials
from carto_schema.event import EventKind

__all__ = ["ConnectionPlan", "SqlConfig", "SqlConnector", "SqlQueryConfig"]

logger = logging.getLogger(__name__)

DEFAULT_PORTS: Final[dict[str, int]] = {"postgresql": 5432, "mysql": 3306, "mssql": 1433}
DRIVERS: Final[dict[str, str]] = {
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mssql": "mssql+pyodbc",
}
MAX_ERROR_CHARS: Final = 200

SslMode = Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"]
ConnectionPlan = dict[str, Any] | str
"""psycopg / PyMySQL keyword arguments, or the pyodbc connection string."""
DbapiConnect = Callable[[str, ConnectionPlan], Any]
"""``(dialect, plan) -> DBAPI connection``; injectable for the SQL Server fake."""
EngineFactory = Callable[[str, Callable[[], Any]], Engine]
"""``(drivername, creator) -> Engine``; injectable for tests."""


class SqlQueryConfig(ConnectorConfig):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    sql: str = Field(min_length=1, max_length=16_384)
    watermark_column: str = Field(min_length=1, max_length=128)
    watermark_type: WatermarkType = Field(
        default="datetime",
        description="datetime, date, int or decimal (ADR 0035); time types need :upper.",
    )
    watermark_timezone: str = Field(
        default="UTC",
        max_length=64,
        description="IANA zone of naive watermark values; :upper is computed in it.",
    )
    timestamp_column: str | None = Field(default=None, max_length=128)
    actor_column: str | None = Field(default=None, max_length=128)
    primary_key: str | None = Field(default=None, max_length=128)

    @field_validator("watermark_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = "watermark_timezone must be an IANA time zone name"
            raise ValueError(msg) from exc
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.watermark_timezone)

    @property
    def time_range(self) -> bool:
        """Whether the template reads the closed range ``(:watermark, :upper]``."""
        return self.watermark_type in TIME_TYPES


class SqlConfig(ConnectorConfig):
    dialect: Dialect
    host: str = Field(min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    sslmode: SslMode = Field(default="verify-full", description="PostgreSQL libpq sslmode.")
    ssl: bool = Field(default=True, description="MySQL: TLS with certificate verification.")
    encrypt: bool = Field(default=True, description="SQL Server: Encrypt=yes.")
    trust_server_certificate: bool = False
    driver: str = Field(default="ODBC Driver 18 for SQL Server", min_length=1, max_length=128)
    ca_file: str | None = Field(default=None, max_length=4096)
    poll_seconds: int = Field(default=60, ge=5, le=86_400)
    query_timeout_seconds: int = Field(default=30, ge=1, le=3600)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)
    batch: int = Field(default=1000, ge=1, le=100_000)
    max_pages_per_read: int = Field(default=100, ge=1, le=10_000)
    settle_seconds: int = Field(
        default=30,
        ge=0,
        le=86_400,
        description=":upper is the database clock minus this (ADR 0035): late commits settle.",
    )
    failure_threshold: int = Field(default=5, ge=1, le=100)
    breaker_reset_seconds: float = Field(default=60.0, gt=0, le=3600)
    queries: list[SqlQueryConfig] = Field(min_length=1, max_length=64)

    @field_validator("host")
    @classmethod
    def _plain_host(cls, value: str) -> str:
        if "@" in value or "/" in value or any(char.isspace() for char in value):
            msg = "host must be a bare host name or address"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _unique_query_names(self) -> SqlConfig:
        names = [query.name for query in self.queries]
        if len(set(names)) != len(names):
            msg = "query names must be unique"
            raise ValueError(msg)
        return self


def _dbapi_connect(dialect: str, plan: ConnectionPlan) -> Any:
    if dialect == "postgresql":
        import psycopg  # noqa: PLC0415 - driver imports stay lazy

        if not isinstance(plan, dict):
            msg = "internal: the PostgreSQL connection plan must be keyword arguments"
            raise TypeError(msg)
        return psycopg.connect(**plan)
    if dialect == "mysql":
        import pymysql  # noqa: PLC0415

        if not isinstance(plan, dict):
            msg = "internal: the MySQL connection plan must be keyword arguments"
            raise TypeError(msg)
        return pymysql.connect(**plan)
    import pyodbc  # noqa: PLC0415

    if not isinstance(plan, str):
        msg = "internal: the SQL Server connection plan must be a connection string"
        raise TypeError(msg)
    return pyodbc.connect(plan)


def _default_engine(drivername: str, creator: Callable[[], Any]) -> Engine:
    return create_engine(f"{drivername}://", creator=creator, poolclass=NullPool)


def _error_text(exc: BaseException) -> str:
    """A short, credential-free description of a database error."""
    origin = getattr(exc, "orig", None)
    source = origin if isinstance(origin, BaseException) else exc
    first_line = str(source).splitlines()[0] if str(source) else ""
    return f"{type(source).__name__}: {first_line[:MAX_ERROR_CHARS]}"


@register("sql")
class SqlConnector:
    """Spec 8.1.4."""

    type: ClassVar[str] = "sql"

    def __init__(
        self,
        source: SourceConfig,
        context: ConnectorContext,
        *,
        connect: DbapiConnect | None = None,
        engine_factory: EngineFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.context = context
        self.config = self.validate_config(source.config)
        if source.secret_ref is None:
            msg = f"source {source.id!r}: a sql source needs a secret_ref for its login"
            raise InvalidConfigError(msg)
        self.statements: dict[str, CheckedStatement] = {}
        for query in self.config.queries:
            try:
                self.statements[query.name] = check_statement(
                    query.sql, self.config.dialect, upper=query.time_range
                )
            except InvalidConfigError as exc:
                msg = f"source {source.id!r} query {query.name!r}: {exc}"
                raise InvalidConfigError(msg) from exc
        self._connect = connect if connect is not None else _dbapi_connect
        self._engine_factory = engine_factory if engine_factory is not None else _default_engine
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._engine: Engine | None = None
        self._breaker = CircuitBreaker(
            self.config.failure_threshold, self.config.breaker_reset_seconds
        )
        self._sequence = 0

    def validate_config(self, cfg: Mapping[str, Any]) -> SqlConfig:
        return validate_config_model(SqlConfig, cfg, self.source.id)

    @property
    def port(self) -> int:
        return self.config.port or DEFAULT_PORTS[self.config.dialect]

    @property
    def tables(self) -> tuple[str, ...]:
        names: list[str] = []
        for statement in self.statements.values():
            names.extend(table for table in statement.tables if table not in names)
        return tuple(names)

    # -- connections ----------------------------------------------------------------------------

    def _credentials(self) -> Credentials:
        try:
            return parse_credentials(self.context.secrets.resolve(self.source.secret_ref or ""))
        except SecretError as exc:
            msg = f"source {self.source.id!r}: login secret is unusable: {exc}"
            raise ConnectorError(msg) from exc

    def connection_plan(self) -> ConnectionPlan:
        """Validate the host through the network policy, resolve the login and build the driver
        arguments. Called on every new DBAPI connection."""
        cfg = self.config
        try:
            resolved = self.context.network.resolve(cfg.host, self.port)
        except SsrfError as exc:
            msg = f"source {self.source.id!r}: {exc}"
            raise ConnectorError(msg) from exc
        credentials = self._credentials()
        if cfg.dialect == "postgresql":
            return postgres_connect_kwargs(
                host=cfg.host,
                resolved=resolved,
                port=self.port,
                database=cfg.database,
                credentials=credentials,
                sslmode=cfg.sslmode,
                ca_file=cfg.ca_file,
                connect_timeout_seconds=cfg.connect_timeout_seconds,
                query_timeout_seconds=cfg.query_timeout_seconds,
            )
        if cfg.dialect == "mysql":
            return mysql_connect_kwargs(
                host=cfg.host,
                port=self.port,
                database=cfg.database,
                credentials=credentials,
                ssl=cfg.ssl,
                ca_file=cfg.ca_file,
                connect_timeout_seconds=cfg.connect_timeout_seconds,
                query_timeout_seconds=cfg.query_timeout_seconds,
            )
        return mssql_connection_string(
            host=cfg.host,
            port=self.port,
            database=cfg.database,
            credentials=credentials,
            driver=cfg.driver,
            encrypt=cfg.encrypt,
            trust_server_certificate=cfg.trust_server_certificate,
            connect_timeout_seconds=cfg.connect_timeout_seconds,
        )

    def _creator(self) -> Any:
        connection = self._connect(self.config.dialect, self.connection_plan())
        if self.config.dialect == "mssql":
            connection.timeout = self.config.query_timeout_seconds  # pyodbc query timeout
        return connection

    def _engine_or_create(self) -> Engine:
        if self._engine is None:
            self._engine = self._engine_factory(DRIVERS[self.config.dialect], self._creator)
        return self._engine

    @contextmanager
    def _read_only_session(self) -> Iterator[Connection]:
        """A connection inside a read-only transaction (spec 8.1.4 layer 2)."""
        engine = self._engine_or_create()
        dialect = self.config.dialect
        with engine.connect() as connection:
            if dialect == "postgresql":
                # psycopg emits BEGIN READ ONLY for this option; default_transaction_read_only
                # in the connection options backs it up.
                session = connection.execution_options(postgresql_readonly=True)
                try:
                    yield session
                    session.commit()
                except BaseException:
                    session.rollback()
                    raise
            elif dialect == "mysql":
                session = connection.execution_options(isolation_level="AUTOCOMMIT")
                for statement in mysql_session_statements(self.config.query_timeout_seconds):
                    session.exec_driver_sql(statement)
                session.exec_driver_sql("SET SESSION time_zone = '+00:00'")
                session.exec_driver_sql("START TRANSACTION READ ONLY")
                try:
                    yield session
                    session.exec_driver_sql("COMMIT")
                except BaseException:
                    session.exec_driver_sql("ROLLBACK")
                    raise
            else:
                try:
                    yield connection
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    def _run(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            with self._read_only_session() as connection:
                result = connection.execute(text(sql), dict(params))
                return [dict(row) for row in result.mappings().all()]
        except SQLAlchemyError as exc:
            msg = f"source {self.source.id!r}: query failed: {_error_text(exc)}"
            raise ConnectorError(msg) from exc

    async def _run_async(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        return await self._breaker.call(lambda: asyncio.to_thread(self._run, sql, params))

    async def _database_now(self) -> datetime:
        """The database's own clock as an aware UTC datetime (ADR 0035 item 3), read through
        the same read-only session as the queries."""
        rows = await self._run_async(CLOCK_SQL[self.config.dialect], {})
        value = next(iter(rows[0].values()), None) if rows else None
        if not isinstance(value, datetime):
            msg = f"source {self.source.id!r}: the database clock did not return a timestamp"
            raise ConnectorError(msg)
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    async def _settled_clock(self) -> datetime | None:
        """Database clock minus ``settle_seconds``; ``None`` when no query reads a time range."""
        if not any(query.time_range for query in self.config.queries):
            return None
        return await self._database_now() - timedelta(seconds=self.config.settle_seconds)

    # -- records --------------------------------------------------------------------------------

    def _initial_watermark(self, query: SqlQueryConfig) -> Watermark:
        since = None
        if self.source.backfill_days:
            since = self._clock() - timedelta(days=self.source.backfill_days)
        return initial(query.watermark_type, since, query.zone)

    def _record(self, query: SqlQueryConfig, row: dict[str, Any], now: datetime) -> RawRecord:
        key_column = query.primary_key or next(iter(row), "")
        key_value = row.get(key_column)
        self._sequence += 1
        locator = (
            f"{query.name}:row:{key_value}"
            if key_value is not None
            else f"{query.name}:row:seq:{self._sequence}"
        )
        return RawRecord(
            source_id=self.source.id,
            system_id=self.source.system,
            kind=EventKind.ROW_CHANGE,
            locator=locator,
            received_at=now,
            fields=row,
            sequence=self._sequence,
            size_bytes=sum(len(str(value)) for value in row.values() if value is not None),
            template_hint=f"row_change {query.name}",
            timestamp_field=query.timestamp_column,
            actor_field=query.actor_column,
        )

    def _decode_cursor(self, cursor: Cursor | None) -> dict[str, Watermark]:
        raw = cursor.get("watermarks") if cursor else None
        if not isinstance(raw, Mapping):
            return {}
        watermarks: dict[str, Watermark] = {}
        for query in self.config.queries:
            if query.name not in raw:
                continue
            try:
                watermarks[query.name] = decode(query.watermark_type, raw[query.name])
            except WatermarkError as exc:
                msg = f"source {self.source.id!r} query {query.name!r}: {exc}"
                raise WatermarkError(msg) from exc
        return watermarks

    def _checkpoint(self, watermarks: Mapping[str, Watermark]) -> CursorCheckpoint:
        kinds = {query.name: query.watermark_type for query in self.config.queries}
        return CursorCheckpoint(
            self.source.id,
            {
                "watermarks": {
                    name: encode(kinds[name], value)
                    for name, value in watermarks.items()
                    if name in kinds
                }
            },
        )

    async def _pages(
        self,
        query: SqlQueryConfig,
        watermark: Watermark,
        upper: Watermark | None,
        now: datetime,
    ) -> AsyncIterator[RawRecord | Watermark]:
        """Records of one query from ``watermark`` (up to ``upper`` for a time range), each
        page followed by the watermark it moved to (ADR 0035 items 3 and 4)."""
        batch = self.config.batch
        statement = self.statements[query.name]
        for _page in range(self.config.max_pages_per_read):
            params: dict[str, Any] = {"watermark": watermark, "batch": batch}
            if upper is not None:
                params["upper"] = upper
            rows = await self._run_async(statement.sql, params)
            try:
                values = [
                    coerce(query.watermark_type, row.get(query.watermark_column), query.zone)
                    for row in rows
                ]
                plan = plan_page(
                    query=query.name,
                    column=query.watermark_column,
                    values=values,
                    batch=batch,
                    configured_batch=self.config.batch,
                    watermark=watermark,
                    upper=upper,
                )
            except WatermarkError as exc:
                msg = f"source {self.source.id!r} query {query.name!r}: {exc}"
                raise WatermarkError(msg) from exc
            if plan.retry_batch is not None:
                batch = plan.retry_batch
                continue
            for row in rows[: plan.emit]:
                yield self._record(query, row, now)
            if plan.advance is not None and plan.advance > watermark:  # type: ignore[operator]
                watermark = plan.advance
                yield watermark
            if plan.done:
                return
            batch = self.config.batch

    async def read(self, cursor: Cursor | None) -> AsyncIterator[ReadItem]:
        now = self._clock()
        watermarks = self._decode_cursor(cursor)
        settled = await self._settled_clock()
        total = 0
        started = time.monotonic()
        for query in self.config.queries:
            watermark = watermarks.get(query.name, self._initial_watermark(query))
            upper: Watermark | None = None
            if settled is not None and query.time_range:
                upper = upper_bound(query.watermark_type, settled, query.zone)
                if not upper > watermark:  # type: ignore[operator]
                    continue  # nothing settled since the cursor; never move it backwards
            async for item in self._pages(query, watermark, upper, now):
                if isinstance(item, RawRecord):
                    total += 1
                    yield item
                else:
                    watermarks[query.name] = item
                    yield self._checkpoint(watermarks)
        logger.info(
            "sql poll source_id=%s queries=%d records=%d seconds=%.1f",
            self.source.id,
            len(self.config.queries),
            total,
            time.monotonic() - started,
        )

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        """Rows of the time-typed queries in ``[start, end]``; integer and decimal watermarks
        carry no time, so their queries are skipped."""
        now = self._clock()
        for query in self.config.queries:
            if not query.time_range:
                logger.info(
                    "sql backfill skips query source_id=%s query=%s watermark_type=%s",
                    self.source.id,
                    query.name,
                    query.watermark_type,
                )
                continue
            lower: Watermark
            if query.watermark_type == "date":
                lower = start.astimezone(query.zone).date() - timedelta(days=1)
                upper: Watermark = end.astimezone(query.zone).date()
            else:
                lower = start.astimezone(query.zone).replace(tzinfo=None)
                upper = end.astimezone(query.zone).replace(tzinfo=None)
            async for item in self._pages(query, lower, upper, now):
                if isinstance(item, RawRecord):
                    yield item

    async def close(self) -> None:
        engine, self._engine = self._engine, None
        if engine is not None:
            await asyncio.to_thread(engine.dispose)

    # -- test() ---------------------------------------------------------------------------------

    async def test(self) -> TestResult:
        checks: list[TestCheck] = []
        problems: list[str] = []
        settled: datetime | None = None
        try:
            settled = await self._settled_clock()
        except ConnectorError as exc:
            checks.append(TestCheck("database clock", False, str(exc)))
            problems.append(f"the database clock could not be read: {exc}")
        for query in self.config.queries:
            params: dict[str, Any] = {"watermark": self._initial_watermark(query), "batch": 1}
            if query.time_range:
                if settled is None:
                    continue
                params["upper"] = upper_bound(query.watermark_type, settled, query.zone)
            try:
                rows = await self._run_async(self.statements[query.name].sql, params)
            except ConnectorError as exc:
                checks.append(TestCheck(f"query {query.name}", False, str(exc)))
                problems.append(f"query {query.name!r} failed: {exc}")
                continue
            columns = ", ".join(rows[0].keys()) if rows else "no rows yet"
            checks.append(TestCheck(f"query {query.name}", True, columns))
        findings: list[str] = []
        current_role: object = None
        pending = list(privilege_queries(self.config.dialect, self.tables))
        while pending:
            privilege_query = pending.pop(0)
            try:
                rows = await self._run_async(privilege_query.sql, privilege_query.params)
            except ConnectorError as exc:
                checks.append(TestCheck(f"privileges {privilege_query.name}", False, str(exc)))
                problems.append(f"privilege check {privilege_query.name!r} failed: {exc}")
                continue
            if self.config.dialect == "mysql":
                if privilege_query.name == "roles":
                    current_role = next(iter(rows[0].values()), None) if rows else None
                elif privilege_query.name == "grants":
                    # ADR 0037 item 3: the login's roles carry privileges SHOW GRANTS lists
                    # only when asked USING them.
                    lines = [str(value) for row in rows for value in row.values()]
                    roles = mysql_roles(current_role, lines)
                    if roles:
                        pending.append(
                            PrivilegeQuery(
                                "grants through roles", mysql_grants_using_sql(roles), {}
                            )
                        )
            found = [
                finding
                for finding in write_findings(self.config.dialect, privilege_query, rows)
                if finding not in findings
            ]
            findings.extend(found)
            label = privilege_query.table or privilege_query.name
            checks.append(
                TestCheck(
                    f"privileges {label}", not found, "; ".join(found) if found else "read-only"
                )
            )
        if findings:
            problems.append(
                "the login can write, so the source cannot be enabled (spec 8.1.4): "
                + "; ".join(findings)
            )
        read_only = ReadOnlyStatus.WRITE_CAPABLE if findings else ReadOnlyStatus.VERIFIED
        return TestResult(
            ok=not problems,
            read_only=read_only,
            checks=tuple(checks),
            problems=tuple(problems),
            visible=self.tables,
        )
