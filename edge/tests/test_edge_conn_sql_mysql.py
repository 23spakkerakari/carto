"""carto_edge.connectors.sql for MySQL: against a container (spec 18.3 read-only tests, marked
``integration``, skipped when Docker is unreachable): SELECT-only login VERIFIED, INSERT grant
WRITE_CAPABLE, polling by watermark, and an INSERT through the connector's START TRANSACTION READ
ONLY session is refused. Against a fake connection (no Docker, security review 2026-11 H2
follow-ups 9 and 10): the ``SHOW GRANTS ... USING`` roles loop, role grants that repeat the direct
lines, and the ``CURRENT_ROLE()`` failure path."""

from __future__ import annotations

import socket
import ssl
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import ConnectorContext, ReadOnlyStatus, ResolvedHost
from carto_edge.connectors.sql import SqlConnector
from carto_edge.connectors.sql_dialects import CLOCK_SQL
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem

integration = pytest.mark.integration

QUERY = """
SELECT id, po_num, order_ref, status, warehouse_code, created_by, updated_at
FROM purchase_orders
WHERE updated_at > :watermark
AND updated_at <= :upper
ORDER BY updated_at
LIMIT :batch
"""
RO_PASSWORD = "ro-pw-0123456789"  # noqa: S105
RW_PASSWORD = "rw-pw-0123456789"  # noqa: S105
ROLE_PASSWORD = "role-pw-0123456789"  # noqa: S105
ROOT_PASSWORD = "root-pw-0123456789"  # noqa: S105


def docker_reachable() -> str | None:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            docker.from_env().ping()
    except Exception as exc:  # any failure means "no docker here"
        return f"Docker daemon unreachable: {type(exc).__name__}"
    return None


DOCKER_SKIP = docker_reachable()


@pytest.fixture(scope="module")
def mysql() -> Iterator[dict[str, Any]]:
    if DOCKER_SKIP:
        pytest.skip(DOCKER_SKIP)
    import pymysql  # noqa: PLC0415
    from testcontainers.community.mysql import MySqlContainer  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        container = MySqlContainer(
            "mysql:8.4",
            username="admin",
            password="admin-pw",  # noqa: S106
            dbname="wms",
            root_password=ROOT_PASSWORD,
        )
        try:
            container.start()
        except (OSError, RuntimeError, ValueError) as exc:
            # The daemon answered ping but cannot start a container (seen after a network
            # outage on Docker Desktop): the environment is unusable, not the connector.
            pytest.skip(f"Docker could not start the MySQL container: {type(exc).__name__}")
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(3306))
        root = pymysql.connect(
            host=host,
            port=port,
            user="root",
            password=ROOT_PASSWORD,
            database="wms",
            autocommit=True,
        )
        try:
            with root.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE purchase_orders (id INT AUTO_INCREMENT PRIMARY KEY, "
                    "po_num VARCHAR(32), order_ref VARCHAR(32), status VARCHAR(16), "
                    "warehouse_code VARCHAR(8), created_by VARCHAR(64), "
                    "updated_at DATETIME)"
                )
                base = datetime(2026, 9, 23, 22, 0, 0)
                for index in range(5):
                    cursor.execute(
                        "INSERT INTO purchase_orders "
                        "(po_num, order_ref, status, warehouse_code, created_by, updated_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (
                            f"88-21{index}",
                            f"SO-000447{index}",
                            "SHIPPED",
                            "DC-01",
                            "svc_wms",
                            base.replace(minute=index),
                        ),
                    )
                cursor.execute(f"CREATE USER 'carto_ro'@'%' IDENTIFIED BY '{RO_PASSWORD}'")
                cursor.execute("GRANT SELECT ON wms.purchase_orders TO 'carto_ro'@'%'")
                cursor.execute(f"CREATE USER 'carto_rw'@'%' IDENTIFIED BY '{RW_PASSWORD}'")
                cursor.execute("GRANT SELECT, INSERT ON wms.purchase_orders TO 'carto_rw'@'%'")
                # ADR 0037 item 3: INSERT reaches this login only through a role it holds but
                # has not activated; SHOW GRANTS alone lists the role, not the privilege.
                cursor.execute("CREATE ROLE 'wms_writer'")
                cursor.execute("GRANT INSERT ON wms.* TO 'wms_writer'")
                cursor.execute(f"CREATE USER 'carto_role'@'%' IDENTIFIED BY '{ROLE_PASSWORD}'")
                cursor.execute("GRANT SELECT ON wms.purchase_orders TO 'carto_role'@'%'")
                cursor.execute("GRANT 'wms_writer' TO 'carto_role'@'%'")
        finally:
            root.close()
        # caching_sha2_password (the MySQL 8.4 default) needs one full authentication per login
        # before the fast path works over plain TCP, and PyMySQL fails that exchange without
        # TLS. One TLS login per test user fills the server's cache. The connector itself uses
        # TLS by default; these tests turn it off only to avoid certificate setup.
        tls = ssl.create_default_context()
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        for user, secret in (
            ("carto_ro", RO_PASSWORD),
            ("carto_rw", RW_PASSWORD),
            ("carto_role", ROLE_PASSWORD),
        ):
            pymysql.connect(
                host=host, port=port, user=user, password=secret, database="wms", ssl=tls
            ).close()
        yield {"host": host, "port": port}
    finally:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            container.stop()


class Secrets:
    def __init__(self, username: str, password: str) -> None:
        self.value = f"{username}:{password}"

    def resolve(self, secret_ref: str) -> str:
        return self.value


class Network:
    def __init__(self, address: str) -> None:
        self.address = address
        self.calls = 0

    def resolve(self, host: str, port: int) -> ResolvedHost:
        self.calls += 1
        return ResolvedHost(host=host, address=self.address, port=port)


def make(
    mysql: dict[str, Any], username: str, password: str, batch: int = 1000
) -> tuple[SqlConnector, Network]:
    host = mysql["host"]
    address = host if host.replace(".", "").isdigit() else socket.gethostbyname(host)
    network = Network(address)
    source = SourceConfig(
        id="src_wms_mysql",
        system="sys_warehouse",
        type=SourceType.SQL,
        config={
            "dialect": "mysql",
            "host": host,
            "port": mysql["port"],
            "database": "wms",
            "ssl": False,
            "insecure_transport": True,  # the Compose fixture has no TLS (ADR 0067)
            "query_timeout_seconds": 5,
            "batch": batch,
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": QUERY,
                    "watermark_column": "updated_at",
                    "primary_key": "id",
                }
            ],
        },
        secret_ref="local://wms-readonly",  # noqa: S106
    )
    return SqlConnector(
        source, ConnectorContext(secrets=Secrets(username, password), network=network)
    ), network


async def collect_items(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[ReadItem]:
    return [item async for item in connector.read(cursor)]


async def collect(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [item for item in await collect_items(connector, cursor) if isinstance(item, RawRecord)]


@integration
async def test_select_only_login_is_verified(mysql: dict[str, Any]) -> None:
    connector, network = make(mysql, "carto_ro", RO_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert result.problems == ()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.visible == ("purchase_orders",)
    assert network.calls >= 2  # validated at every connect (ADR 0018)


@integration
async def test_insert_grant_is_refused(mysql: dict[str, Any]) -> None:
    connector, _network = make(mysql, "carto_rw", RW_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert not result.can_enable
    assert "INSERT on `wms`.`purchase_orders`" in result.problems[-1]


@integration
async def test_an_insert_grant_held_through_a_role_is_refused(mysql: dict[str, Any]) -> None:
    """ADR 0037 item 3: SHOW GRANTS FOR CURRENT_USER() USING the login's roles."""
    connector, _network = make(mysql, "carto_role", ROLE_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert "INSERT on `wms`.*" in result.problems[-1]
    names = [check.name for check in result.checks]
    assert names[-3:] == [
        "privileges roles",
        "privileges grants",
        "privileges grants through roles",
    ]


@integration
async def test_polling_by_watermark(mysql: dict[str, Any]) -> None:
    connector, _network = make(mysql, "carto_ro", RO_PASSWORD, batch=2)
    try:
        items = await collect_items(connector)
        records = [item for item in items if isinstance(item, RawRecord)]
        assert [r.locator for r in records] == [f"purchase_orders:row:{n}" for n in range(1, 6)]
        assert records[0].fields is not None and records[0].fields["po_num"] == "88-210"
        assert isinstance(records[0].fields["updated_at"], datetime)
        # ADR 0035: the cursor travels in checkpoints and ends at :upper (the database clock
        # minus the settle delay), past every row; a later read finds nothing.
        assert all(r.commit_cursor is None for r in records)
        checkpoints = [item for item in items if isinstance(item, CursorCheckpoint)]
        assert checkpoints and checkpoints[-1].source_id == "src_wms_mysql"
        final = checkpoints[-1].cursor["watermarks"]["purchase_orders"]
        assert final["t"] == "datetime"
        assert datetime.fromisoformat(final["v"]) > datetime(2026, 9, 23, 22, 4)
        assert await collect(connector, dict(checkpoints[-1].cursor)) == []
    finally:
        await connector.close()


@integration
async def test_insert_through_the_connector_session_is_refused(mysql: dict[str, Any]) -> None:
    connector, _network = make(mysql, "carto_rw", RW_PASSWORD)

    def attempt() -> None:
        with connector._read_only_session() as connection:
            assert connection.execute(text("SELECT @@SESSION.max_execution_time")).scalar() == 5000
            connection.execute(text("INSERT INTO purchase_orders (po_num) VALUES ('x')"))

    try:
        with pytest.raises(DBAPIError, match="READ ONLY"):
            attempt()
        assert len(await collect(connector)) == 5
    finally:
        await connector.close()


# ---------------------------------------------------------------------------------------------
# Fake connection (no Docker): the roles loop and the CURRENT_ROLE() failure path
# ---------------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
DB_NOW = datetime(2026, 10, 8, 12, 0, 0)  # UTC_TIMESTAMP(6) comes back naive
CLOCK = CLOCK_SQL["mysql"]
FAKE_QUERY = (
    "SELECT id, po_num, status, updated_at FROM purchase_orders "
    "WHERE updated_at > :watermark AND updated_at <= :upper ORDER BY updated_at LIMIT :batch"
)
FAKE_ROWS = [
    {"id": 1, "po_num": "88-210", "status": "SHIPPED", "updated_at": datetime(2026, 9, 23, 22, 4)},
    {"id": 2, "po_num": "88-211", "status": "OPEN", "updated_at": datetime(2026, 9, 23, 22, 5)},
]
DIRECT_LINES = [
    "GRANT USAGE ON *.* TO `carto_role`@`%`",
    "GRANT SELECT ON `wms`.`purchase_orders` TO `carto_role`@`%`",
    "GRANT `wms_writer`@`%` TO `carto_role`@`%`",
]
ROLES_SQL = "SELECT CURRENT_ROLE() AS roles"
GRANTS_SQL = "SHOW GRANTS FOR CURRENT_USER()"
USING_SQL = "SHOW GRANTS FOR CURRENT_USER() USING `wms_writer`@`%`"


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> FakeResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)


class FakeConnection:
    """The SQLAlchemy Connection surface the connector uses on MySQL."""

    def __init__(
        self,
        responder: Callable[[str, dict[str, Any]], list[dict[str, Any]]],
        log: list[tuple[str, dict[str, Any]]],
    ) -> None:
        self.responder = responder
        self.log = log

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

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        dbapi = self.creator()
        try:
            yield FakeConnection(self.responder, self.log)
        finally:
            dbapi.close()

    def dispose(self) -> None:
        return None


class FakeDbapiConnection:
    def __init__(self, dialect: str, plan: Any) -> None:
        self.dialect = dialect
        self.plan = plan

    def close(self) -> None:
        return None


class FakeMysql:
    """Answers the connector's statements: the clock, the poll, ``CURRENT_ROLE()``, ``SHOW
    GRANTS`` and ``SHOW GRANTS ... USING``."""

    def __init__(
        self,
        *,
        current_role: str | None = "NONE",
        direct_lines: list[str] | None = None,
        role_lines: list[str] | None = None,
        roles_fail: bool = False,
    ) -> None:
        self.current_role = current_role
        self.direct_lines = DIRECT_LINES if direct_lines is None else direct_lines
        self.role_lines = role_lines if role_lines is not None else list(self.direct_lines)
        self.roles_fail = roles_fail
        self.engine: FakeEngine | None = None
        self.network = FakeNetwork()

    def connect(self, dialect: str, plan: Any) -> FakeDbapiConnection:
        assert dialect == "mysql"
        assert isinstance(plan, dict)
        return FakeDbapiConnection(dialect, plan)

    def engine_factory(self, drivername: str, creator: Callable[[], Any]) -> Any:
        self.engine = FakeEngine(drivername, creator, self.respond)
        return self.engine

    def statements(self) -> list[str]:
        assert self.engine is not None
        return [sql for sql, _ in self.engine.log]

    def respond(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        if sql == CLOCK:
            return [{"now": DB_NOW}]
        if sql == ROLES_SQL:
            if self.roles_fail:
                raise OperationalError(
                    sql, {}, Exception("FUNCTION wms.CURRENT_ROLE does not exist")
                )
            return [{"roles": self.current_role}]
        if sql == GRANTS_SQL:
            return [{"Grants for carto_role@%": line} for line in self.direct_lines]
        if sql.startswith("SHOW GRANTS FOR CURRENT_USER() USING "):
            return [{"Grants for carto_role@%": line} for line in self.role_lines]
        assert sql == FAKE_QUERY
        assert set(params) == {"watermark", "upper", "batch"}
        return FAKE_ROWS[: params["batch"]]


class FakeSecrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "local://wms-readonly"  # noqa: S105
        return "carto_role:role-pw"


class FakeNetwork:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> ResolvedHost:
        self.calls.append((host, port))
        return ResolvedHost(host=host, address="10.20.3.6", port=port)


def make_fake(fake: FakeMysql) -> SqlConnector:
    source = SourceConfig(
        id="src_wms_mysql",
        system="sys_warehouse",
        type=SourceType.SQL,
        config={
            "dialect": "mysql",
            "host": "mysql.internal.example",
            "database": "wms",
            "query_timeout_seconds": 5,
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": FAKE_QUERY,
                    "watermark_column": "updated_at",
                    "primary_key": "id",
                }
            ],
        },
        secret_ref="local://wms-readonly",  # noqa: S106
    )
    return SqlConnector(
        source,
        ConnectorContext(secrets=FakeSecrets(), network=fake.network),
        connect=fake.connect,
        engine_factory=fake.engine_factory,
        clock=lambda: NOW,
    )


async def test_fake_read_only_login_with_a_read_only_role_is_verified() -> None:
    """ADR 0037 item 3 without Docker: the role from the GRANT line is expanded with SHOW GRANTS
    ... USING, inside the read-only session."""
    fake = FakeMysql(
        role_lines=[*DIRECT_LINES, "GRANT SELECT ON `wms`.`warehouses` TO `carto_role`@`%`"]
    )
    result = await make_fake(fake).test()
    assert result.problems == ()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.visible == ("purchase_orders",)
    assert [check.name for check in result.checks] == [
        "query purchase_orders",
        "privileges roles",
        "privileges grants",
        "privileges grants through roles",
    ]
    assert all(check.ok for check in result.checks)
    statements = fake.statements()
    assert USING_SQL in statements
    assert "SET SESSION max_execution_time = 5000" in statements
    assert "START TRANSACTION READ ONLY" in statements
    assert fake.network.calls and fake.network.calls[0] == ("mysql.internal.example", 3306)


async def test_fake_active_roles_join_the_granted_ones() -> None:
    fake = FakeMysql(current_role="`auditor`@`localhost`")
    await make_fake(fake).test()
    assert (
        "SHOW GRANTS FOR CURRENT_USER() USING `auditor`@`localhost`, `wms_writer`@`%`"
        in fake.statements()
    )


async def test_fake_role_grants_that_repeat_the_direct_lines_still_fail_their_check() -> None:
    """Security review 2026-11 (H2 follow-up 9): SHOW GRANTS ... USING repeats the direct lines,
    so a per-query check built from the deduplicated list read "read-only" whenever the login's
    own grants already carried the finding. The check reads its own result; the problem list is
    deduplicated."""
    direct = [
        "GRANT USAGE ON *.* TO `carto_role`@`%`",
        "GRANT SELECT, INSERT ON `wms`.`purchase_orders` TO `carto_role`@`%`",
        "GRANT `wms_writer`@`%` TO `carto_role`@`%`",
    ]
    fake = FakeMysql(
        direct_lines=direct, role_lines=[*direct, "GRANT UPDATE ON `wms`.* TO `carto_role`@`%`"]
    )
    result = await make_fake(fake).test()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    checks = {check.name: check for check in result.checks}
    assert not checks["privileges grants"].ok
    assert "INSERT on `wms`.`purchase_orders`" in checks["privileges grants"].detail
    through = checks["privileges grants through roles"]
    assert not through.ok
    assert "login has INSERT on `wms`.`purchase_orders`" in through.detail
    assert "login has UPDATE on `wms`.*" in through.detail
    assert result.problems[-1].count("login has INSERT on `wms`.`purchase_orders`") == 1
    assert "login has UPDATE on `wms`.*" in result.problems[-1]


async def test_fake_role_grants_that_only_repeat_the_direct_lines_are_write_findings() -> None:
    direct = [
        "GRANT USAGE ON *.* TO `carto_role`@`%`",
        "GRANT SELECT, DELETE ON `wms`.`purchase_orders` TO `carto_role`@`%`",
        "GRANT `wms_writer`@`%` TO `carto_role`@`%`",
    ]
    result = await make_fake(FakeMysql(direct_lines=direct, role_lines=direct)).test()
    through = next(c for c in result.checks if c.name == "privileges grants through roles")
    assert not through.ok
    assert "DELETE" in through.detail


async def test_fake_current_role_failure_fails_the_test() -> None:
    """MySQL 5.7 has no CURRENT_ROLE() (ADR 0041 Consequences): the roles check fails, the test
    is not ok and the source cannot be enabled; the direct grants are still read."""
    fake = FakeMysql(roles_fail=True)
    result = await make_fake(fake).test()
    assert not result.ok
    assert not result.can_enable
    roles = next(check for check in result.checks if check.name == "privileges roles")
    assert not roles.ok
    assert "CURRENT_ROLE" in roles.detail
    assert any("privilege check 'roles' failed" in problem for problem in result.problems)
    assert any(check.name == "privileges grants" and check.ok for check in result.checks)
    # The role named on the GRANT line is still expanded; only the active roles are unknown.
    assert USING_SQL in fake.statements()
    assert all("role-pw" not in problem for problem in result.problems)
