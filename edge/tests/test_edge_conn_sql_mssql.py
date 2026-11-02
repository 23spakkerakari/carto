"""carto_edge.connectors.sql for SQL Server against a fake connection (ADR 0019: no container in
CI yet): the pyodbc connection string with ApplicationIntent=ReadOnly, the query timeout, the
HAS_PERMS_BY_NAME and server-role checks, polling by a closed time range with tie-safe pages and
cursor checkpoints (ADR 0035), and the typed cursor encoding."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorContext,
    ConnectorError,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ResolvedHost,
)
from carto_edge.connectors.sql import SqlConnector
from carto_edge.connectors.sql_dialects import CLOCK_SQL
from carto_edge.net.ssrf import SsrfError
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem
from carto_schema.event import EventKind

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
DB_NOW = datetime(2026, 10, 8, 12, 0, 0)  # SYSUTCDATETIME() comes back naive
UPPER = datetime(2026, 10, 8, 11, 59, 30)  # DB_NOW minus the default settle_seconds (30)
CLOCK = CLOCK_SQL["mssql"]
QUERY = (
    "SELECT TOP (:batch) id, po_num, order_ref, status, updated_at FROM dbo.purchase_orders "
    "WHERE updated_at > :watermark AND updated_at <= :upper ORDER BY updated_at"
)
ROWS = [
    {
        "id": 1,
        "po_num": "88-210",
        "order_ref": "SO-0004471",
        "status": "SHIPPED",
        "updated_at": datetime(2026, 9, 23, 22, 4, 3),
    },
    {
        "id": 2,
        "po_num": "88-211",
        "order_ref": "SO-0004472",
        "status": "OPEN",
        "updated_at": datetime(2026, 9, 23, 22, 5, 0),
    },
    {
        "id": 3,
        "po_num": "88-212",
        "order_ref": "SO-0004473",
        "status": "OPEN",
        "updated_at": datetime(2026, 9, 24, 1, 0, 0),
    },
]


class FakeDbapiConnection:
    """What the injected ``connect`` returns: records the plan and the pyodbc timeout."""

    def __init__(self, dialect: str, plan: Any) -> None:
        self.dialect = dialect
        self.plan = plan
        self.timeout = 0
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class FakeConnection:
    """The SQLAlchemy Connection surface the connector uses."""

    def __init__(
        self,
        dbapi: FakeDbapiConnection,
        responder: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
        log: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self.dbapi = dbapi
        self.responder = responder
        self.log = log
        self.committed = 0
        self.rolled_back = 0

    def execution_options(self, **options: Any) -> FakeConnection:
        self.log.append(("options", dict(options)))
        return self

    def exec_driver_sql(self, statement: str) -> None:
        self.log.append((statement, {}))

    def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        parameters = dict(params or {})
        self.log.append((sql, parameters))
        return FakeResult(self.responder(sql, parameters))

    def commit(self) -> None:
        self.committed += 1

    def rollback(self) -> None:
        self.rolled_back += 1


class FakeEngine:
    def __init__(
        self,
        drivername: str,
        creator: Callable[[], Any],
        responder: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
    ) -> None:
        self.drivername = drivername
        self.creator = creator
        self.responder = responder
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.connections: list[FakeConnection] = []
        self.disposed = False

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        dbapi = self.creator()
        connection = FakeConnection(dbapi, self.responder, self.log)
        self.connections.append(connection)
        try:
            yield connection
        finally:
            dbapi.close()

    def dispose(self) -> None:
        self.disposed = True


class Harness:
    def __init__(
        self,
        *,
        insert: int = 0,
        alter: int = 0,
        sysadmin: int = 0,
        database_delete: int = 0,
        linked_servers: tuple[str, ...] = (),
        clock: bool = True,
    ) -> None:
        self.insert, self.alter, self.sysadmin = insert, alter, sysadmin
        self.database_delete = database_delete
        self.linked_servers = linked_servers
        self.clock = clock
        self.engine: FakeEngine | None = None
        self.dbapi: list[FakeDbapiConnection] = []
        self.network = Network()

    def connect(self, dialect: str, plan: Any) -> FakeDbapiConnection:
        connection = FakeDbapiConnection(dialect, plan)
        self.dbapi.append(connection)
        return connection

    def engine_factory(self, drivername: str, creator: Callable[[], Any]) -> Any:
        self.engine = FakeEngine(drivername, creator, self.respond)
        return self.engine

    def polls(self) -> list[dict[str, Any]]:
        assert self.engine is not None
        return [params for sql, params in self.engine.log if sql == QUERY]

    def respond(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if sql == CLOCK:
            return [{"now": DB_NOW}] if self.clock else []
        if "'DATABASE'" in sql:
            assert params == {}
            assert "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE')" in sql
            return [
                {
                    "can_insert": 0,
                    "can_update": 0,
                    "can_delete": self.database_delete,
                    "can_alter": 0,
                }
            ]
        if "sys.servers" in sql:
            return [{"name": name} for name in self.linked_servers]
        if "HAS_PERMS_BY_NAME" in sql:
            assert params == {"t": "dbo.purchase_orders"}
            return [
                {
                    "can_insert": self.insert,
                    "can_update": 0,
                    "can_delete": 0,
                    "can_alter": self.alter,
                }
            ]
        if "IS_SRVROLEMEMBER" in sql:
            return [
                {"sysadmin": self.sysadmin, "db_owner": 0, "db_datawriter": 0, "db_ddladmin": 0}
            ]
        assert sql == QUERY
        assert set(params) == {"watermark", "upper", "batch"}
        selected = [
            row for row in ROWS if params["watermark"] < row["updated_at"] <= params["upper"]
        ]
        return selected[: params["batch"]]


class Secrets:
    def __init__(self, value: str = '{"username": "carto_ro", "password": "p}w;d"}') -> None:
        self.value = value

    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "local://wms-readonly"  # noqa: S105
        return self.value


class Network:
    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> ResolvedHost:
        self.calls.append((host, port))
        if self.refuse:
            raise SsrfError("refused by policy")
        return ResolvedHost(host=host, address="10.20.3.4", port=port)


def make(
    harness: Harness,
    config: dict[str, object] | None = None,
    secrets: Secrets | None = None,
    query: str = QUERY,
) -> SqlConnector:
    source = SourceConfig(
        id="src_wms_db",
        system="sys_warehouse",
        type=SourceType.SQL,
        config={
            "dialect": "mssql",
            "host": "sql.internal.example",
            "database": "wms",
            "query_timeout_seconds": 45,
            "batch": 2,
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": query,
                    "watermark_column": "updated_at",
                    "primary_key": "id",
                }
            ],
            **(config or {}),
        },
        secret_ref="local://wms-readonly",  # noqa: S106
    )
    return SqlConnector(
        source,
        ConnectorContext(secrets=secrets or Secrets(), network=harness.network),
        connect=harness.connect,
        engine_factory=harness.engine_factory,
        clock=lambda: NOW,
    )


async def collect_items(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[ReadItem]:
    return [item async for item in connector.read(cursor)]


async def collect(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [item for item in await collect_items(connector, cursor) if isinstance(item, RawRecord)]


def checkpoint(moment: str) -> CursorCheckpoint:
    return CursorCheckpoint(
        "src_wms_db", {"watermarks": {"purchase_orders": {"t": "datetime", "v": moment}}}
    )


def test_is_a_read_connector_and_checks_statements() -> None:
    connector = make(Harness())
    assert isinstance(connector, ReadConnector)
    assert connector.type == "sql"
    assert connector.tables == ("dbo.purchase_orders",)
    with pytest.raises(InvalidConfigError, match="single SELECT"):
        make(Harness(), query="UPDATE dbo.purchase_orders SET status = 'x' WHERE id > :watermark")


def test_a_datetime_watermark_query_must_read_a_closed_range() -> None:
    """ADR 0035 item 3: the template of a time-typed watermark binds :upper as well."""
    with pytest.raises(InvalidConfigError, match=":upper"):
        make(
            Harness(),
            query=(
                "SELECT TOP (:batch) id, updated_at FROM dbo.purchase_orders "
                "WHERE updated_at > :watermark ORDER BY updated_at"
            ),
        )


def test_needs_a_secret_ref() -> None:
    source = SourceConfig(
        id="s",
        system="sys_warehouse",
        type=SourceType.SQL,
        config={
            "dialect": "mssql",
            "host": "h",
            "database": "d",
            "queries": [{"name": "q", "sql": QUERY, "watermark_column": "updated_at"}],
        },
    )
    with pytest.raises(InvalidConfigError, match="secret_ref"):
        SqlConnector(source, ConnectorContext(secrets=Secrets(), network=Network()))


async def test_connection_string_read_only_intent_and_timeout() -> None:
    harness = Harness()
    connector = make(harness)
    await collect(connector)
    assert harness.engine is not None and harness.engine.drivername == "mssql+pyodbc"
    assert harness.network.calls[0] == ("sql.internal.example", 1433)
    dbapi = harness.dbapi[0]
    assert dbapi.dialect == "mssql"
    assert isinstance(dbapi.plan, str)
    assert "ApplicationIntent=ReadOnly" in dbapi.plan
    assert "Encrypt=yes" in dbapi.plan
    assert "TrustServerCertificate=no" in dbapi.plan
    assert "SERVER={sql.internal.example,1433}" in dbapi.plan
    assert "DATABASE={wms}" in dbapi.plan
    assert "UID={carto_ro}" in dbapi.plan
    assert "PWD={p}}w;d}" in dbapi.plan
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in dbapi.plan
    assert dbapi.timeout == 45
    assert dbapi.closed  # NullPool semantics: one connection per statement, closed afterwards
    # Each statement re-resolves the host through the policy and re-reads the login.
    assert len(harness.network.calls) == len(harness.dbapi)


async def test_query_timestamp_and_actor_columns_travel_with_every_record() -> None:
    """Spec Appendix A: a query's timestamp_column and actor_column reach the parser (7.1)."""
    harness = Harness()
    connector = make(harness)
    plain = await collect(connector)
    assert all(r.timestamp_field is None and r.actor_field is None for r in plain)
    harness = Harness()
    connector = make(
        harness,
        config={
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": QUERY,
                    "watermark_column": "updated_at",
                    "primary_key": "id",
                    "timestamp_column": "updated_at",
                    "actor_column": "created_by",
                }
            ]
        },
    )
    items = await collect_items(connector)
    records = [item for item in items if isinstance(item, RawRecord)]
    assert records
    assert all(r.timestamp_field == "updated_at" for r in records)
    assert all(r.actor_field == "created_by" for r in records)
    # The cursor travels in checkpoints, never on a record (ADR 0035 item 1).
    assert all(r.commit_cursor is None for r in records)
    assert any(isinstance(item, CursorCheckpoint) for item in items)


async def test_polls_a_closed_range_in_tie_safe_pages_with_checkpoints() -> None:
    harness = Harness()
    connector = make(harness)
    items = await collect_items(connector)
    records = [item for item in items if isinstance(item, RawRecord)]
    assert [r.locator for r in records] == [
        "purchase_orders:row:1",
        "purchase_orders:row:2",
        "purchase_orders:row:3",
    ]
    assert records[0].kind is EventKind.ROW_CHANGE
    assert records[0].template_hint == "row_change purchase_orders"
    assert records[0].fields == ROWS[0]  # Python values, stringified later by the parser
    assert records[0].received_at == NOW
    # batch=2: a full page stops below its largest value (the trailing tie group is read again),
    # so every page emits one row and a checkpoint; the short last page moves to :upper.
    assert items == [
        records[0],
        checkpoint("2026-09-23T22:04:03"),
        records[1],
        checkpoint("2026-09-23T22:05:00"),
        records[2],
        checkpoint("2026-10-08T11:59:30"),
    ]
    polls = harness.polls()
    assert polls[0] == {"watermark": datetime(1970, 1, 1), "batch": 2, "upper": UPPER}
    assert [poll["watermark"] for poll in polls[1:]] == [
        datetime(2026, 9, 23, 22, 4, 3),
        datetime(2026, 9, 23, 22, 5),
    ]
    assert all(poll["upper"] == UPPER for poll in polls)
    # Round trip: nothing settled after the cursor, so no query runs and the cursor stays.
    assert await collect_items(connector, dict(items[-1].cursor)) == []  # type: ignore[union-attr]
    assert len(harness.polls()) == 3


async def test_an_empty_range_still_moves_the_cursor_to_upper() -> None:
    """ADR 0035 item 3: a fully read range commits even when it held no row; the untyped cursor
    of the first M1 build is read as the configured type."""
    harness = Harness()
    connector = make(harness)
    legacy: dict[str, object] = {"watermarks": {"purchase_orders": "2026-09-24T01:00:00"}}
    assert await collect_items(connector, legacy) == [checkpoint("2026-10-08T11:59:30")]
    assert harness.polls() == [
        {"watermark": datetime(2026, 9, 24, 1, 0), "batch": 2, "upper": UPPER}
    ]


async def test_a_cursor_of_another_type_is_refused() -> None:
    harness = Harness()
    with pytest.raises(ConnectorError, match="watermark_type"):
        await collect_items(
            make(harness), {"watermarks": {"purchase_orders": {"t": "int", "v": "5"}}}
        )


async def test_backfill_days_sets_the_initial_watermark() -> None:
    harness = Harness()
    source = make(harness).source.model_copy(update={"backfill_days": 14})
    # 14 days before 2026-10-08T00:30Z is 2026-09-24T00:30: only row 3 (01:00) is newer.
    clock = lambda: datetime(2026, 10, 8, 0, 30, tzinfo=UTC)  # noqa: E731
    connector = SqlConnector(
        source,
        ConnectorContext(secrets=Secrets(), network=harness.network),
        connect=harness.connect,
        engine_factory=harness.engine_factory,
        clock=clock,
    )
    records = await collect(connector)
    assert [r.locator for r in records] == ["purchase_orders:row:3"]


async def test_backfill_window() -> None:
    harness = Harness()
    records = [
        r
        async for r in make(harness).backfill(
            datetime(2026, 9, 23, 22, 4, 30, tzinfo=UTC), datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
        )
    ]
    assert [r.locator for r in records] == ["purchase_orders:row:2"]
    assert harness.polls()[0]["upper"] == datetime(2026, 9, 24, 0, 0)


async def test_read_only_login_is_verified() -> None:
    harness = Harness()
    result = await make(harness).test()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.can_enable
    assert result.visible == ("dbo.purchase_orders",)
    names = [check.name for check in result.checks]
    assert names == [
        "query purchase_orders",
        "privileges dbo.purchase_orders",
        "privileges database",
        "privileges role",
        "privileges linked servers",
    ]
    assert harness.engine is not None
    log = harness.engine.log
    assert any(sql == CLOCK for sql, _ in log)  # :upper comes from the database clock
    first_poll = harness.polls()[0]
    assert first_poll["batch"] == 1
    assert first_poll["upper"] == UPPER
    assert any("HAS_PERMS_BY_NAME(:t, 'OBJECT', 'INSERT')" in sql for sql, _ in log)
    assert any("IS_SRVROLEMEMBER('sysadmin')" in sql for sql, _ in log)
    # ADR 0037 item 2: database-scope writes and linked servers are checked as well.
    assert any("HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT')" in sql for sql, _ in log)
    assert any(sql == "SELECT name FROM sys.servers WHERE server_id <> 0" for sql, _ in log)


async def test_an_unreadable_database_clock_fails_the_test_and_the_read() -> None:
    harness = Harness(clock=False)
    result = await make(harness).test()
    assert not result.ok
    assert [check.name for check in result.checks] == [
        "database clock",
        "privileges dbo.purchase_orders",
        "privileges database",
        "privileges role",
        "privileges linked servers",
    ]
    assert "database clock" in result.problems[0]
    assert harness.polls() == []  # a time-range query never runs without :upper
    with pytest.raises(ConnectorError, match="database clock"):
        await collect_items(make(Harness(clock=False)))


async def test_insert_permission_makes_the_login_write_capable() -> None:
    result = await make(Harness(insert=1, alter=1)).test()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert not result.can_enable
    assert "login can INSERT on dbo.purchase_orders" in result.problems[-1]
    assert "login can ALTER on dbo.purchase_orders" in result.problems[-1]
    assert "spec 8.1.4" in result.problems[-1]


async def test_sysadmin_makes_the_login_write_capable() -> None:
    result = await make(Harness(sysadmin=1)).test()
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert "sysadmin" in result.problems[-1]


async def test_a_database_scope_write_permission_makes_the_login_write_capable() -> None:
    """ADR 0037 item 2: DELETE granted on the database reaches every table, present or future,
    so HAS_PERMS_BY_NAME on the referenced tables alone would miss it."""
    result = await make(Harness(database_delete=1)).test()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert "login can DELETE at database scope" in result.problems[-1]
    database = next(check for check in result.checks if check.name == "privileges database")
    assert not database.ok


async def test_a_login_that_sees_a_linked_server_is_write_capable() -> None:
    """ADR 0037 item 2: a visible linked server is a path to writes on another server."""
    result = await make(Harness(linked_servers=("WMS-ARCHIVE",))).test()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert "login sees linked server WMS-ARCHIVE" in result.problems[-1]


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            (
                "SELECT TOP (:batch) id, updated_at FROM dbo.purchase_orders WITH (NOLOCK) "
                "WHERE updated_at > :watermark AND updated_at <= :upper ORDER BY updated_at"
            ),
            "table hints",
        ),
        (
            (
                "SELECT TOP (:batch) * FROM OPENQUERY(lnk, 'DELETE FROM x') "
                "WHERE updated_at > :watermark AND updated_at <= :upper"
            ),
            "OPENQUERY",
        ),
        (
            (
                "SELECT TOP (:batch) NEXT VALUE FOR dbo.seq, updated_at FROM dbo.purchase_orders "
                "WHERE updated_at > :watermark AND updated_at <= :upper"
            ),
            "NEXT VALUE FOR",
        ),
        (
            (
                "SELECT TOP (:batch) * FROM dbo.purchase_orders CROSS APPLY OPENJSON(payload) "
                "WHERE updated_at > :watermark AND updated_at <= :upper"
            ),
            "openjson",
        ),
    ],
)
def test_adr_0037_refusals_reach_the_source_config(sql: str, reason: str) -> None:
    with pytest.raises(InvalidConfigError, match=reason):
        make(Harness(), query=sql)


async def test_ssrf_refusal_and_bad_secret_are_connector_errors() -> None:
    harness = Harness()
    harness.network = Network(refuse=True)
    with pytest.raises(ConnectorError, match="refused"):
        await collect(make(harness))
    with pytest.raises(ConnectorError, match="login secret"):
        await collect(make(Harness(), secrets=Secrets("no-separator")))


async def test_close_disposes_the_engine() -> None:
    harness = Harness()
    connector = make(harness)
    await collect(connector)
    await connector.close()
    assert harness.engine is not None and harness.engine.disposed
