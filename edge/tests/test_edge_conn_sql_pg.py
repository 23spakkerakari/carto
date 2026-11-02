"""carto_edge.connectors.sql against PostgreSQL in a container (spec 18.3 read-only tests): a
SELECT-only login passes with VERIFIED, a login with INSERT fails with WRITE_CAPABLE, polling by
watermark yields rows with ADR 0006 locators, and an INSERT through the connector's own session
is refused because the session is read-only. Skipped when Docker is unreachable."""

from __future__ import annotations

import socket
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import ConnectorContext, ReadOnlyStatus, ResolvedHost
from carto_edge.connectors.sql import SqlConnector
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem

pytestmark = pytest.mark.integration

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
def postgres() -> Iterator[dict[str, Any]]:
    if DOCKER_SKIP:
        pytest.skip(DOCKER_SKIP)
    import psycopg  # noqa: PLC0415
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        container = PostgresContainer(
            "postgres:16-alpine",
            username="admin",
            password="admin-pw",  # noqa: S106
            dbname="wms",
            driver=None,
        )
        try:
            container.start()
        except (OSError, RuntimeError, ValueError) as exc:
            # The daemon answered ping but cannot start a container (seen after a network
            # outage on Docker Desktop): the environment is unusable, not the connector.
            pytest.skip(f"Docker could not start the PostgreSQL container: {type(exc).__name__}")
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        with psycopg.connect(
            host=host,
            port=port,
            user="admin",
            password="admin-pw",  # noqa: S106
            dbname="wms",
            autocommit=True,
        ) as admin:
            admin.execute(
                "CREATE TABLE purchase_orders (id serial PRIMARY KEY, po_num text, order_ref text, "
                "status text, warehouse_code text, created_by text, updated_at timestamp)"
            )
            base = datetime(2026, 9, 23, 22, 0, 0)
            for index in range(5):
                admin.execute(
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
            admin.execute(f"CREATE ROLE carto_ro LOGIN PASSWORD '{RO_PASSWORD}'")
            admin.execute("GRANT SELECT ON purchase_orders TO carto_ro")
            admin.execute(f"CREATE ROLE carto_rw LOGIN PASSWORD '{RW_PASSWORD}'")
            admin.execute("GRANT SELECT, INSERT ON purchase_orders TO carto_rw")
            admin.execute("GRANT USAGE ON SEQUENCE purchase_orders_id_seq TO carto_rw")
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

    def resolve(self, host: str, port: int) -> ResolvedHost:
        return ResolvedHost(host=host, address=self.address, port=port)


def make(postgres: dict[str, Any], username: str, password: str, batch: int = 1000) -> SqlConnector:
    host = postgres["host"]
    address = host if host.replace(".", "").isdigit() else socket.gethostbyname(host)
    source = SourceConfig(
        id="src_wms_db",
        system="sys_warehouse",
        type=SourceType.SQL,
        config={
            "dialect": "postgresql",
            "host": host,
            "port": postgres["port"],
            "database": "wms",
            "sslmode": "disable",
            "query_timeout_seconds": 5,
            "batch": batch,
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": QUERY,
                    "watermark_column": "updated_at",
                    "timestamp_column": "updated_at",
                    "actor_column": "created_by",
                    "primary_key": "id",
                }
            ],
        },
        secret_ref="local://wms-readonly",  # noqa: S106
    )
    return SqlConnector(
        source, ConnectorContext(secrets=Secrets(username, password), network=Network(address))
    )


async def collect_items(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[ReadItem]:
    return [item async for item in connector.read(cursor)]


async def collect(
    connector: SqlConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [item for item in await collect_items(connector, cursor) if isinstance(item, RawRecord)]


async def test_select_only_login_is_verified(postgres: dict[str, Any]) -> None:
    connector = make(postgres, "carto_ro", RO_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert result.problems == ()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.can_enable
    assert result.visible == ("purchase_orders",)
    assert all(check.ok for check in result.checks)


async def test_insert_grant_is_refused(postgres: dict[str, Any]) -> None:
    connector = make(postgres, "carto_rw", RW_PASSWORD)
    try:
        result = await connector.test()
    finally:
        await connector.close()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert not result.can_enable
    assert "login can INSERT on purchase_orders" in result.problems[-1]


async def test_polling_by_watermark(postgres: dict[str, Any]) -> None:
    connector = make(postgres, "carto_ro", RO_PASSWORD, batch=2)
    try:
        items = await collect_items(connector)
        records = [item for item in items if isinstance(item, RawRecord)]
        assert [r.locator for r in records] == [f"purchase_orders:row:{n}" for n in range(1, 6)]
        assert records[0].template_hint == "row_change purchase_orders"
        assert records[0].fields is not None
        assert records[0].fields["po_num"] == "88-210"
        assert isinstance(records[0].fields["updated_at"], datetime)
        # ADR 0035: the cursor travels in checkpoints and ends at :upper (the database clock
        # minus the settle delay), past every row; a later read finds nothing.
        assert all(r.commit_cursor is None for r in records)
        checkpoints = [item for item in items if isinstance(item, CursorCheckpoint)]
        assert checkpoints and checkpoints[-1].source_id == "src_wms_db"
        final = checkpoints[-1].cursor["watermarks"]["purchase_orders"]
        assert final["t"] == "datetime"
        assert datetime.fromisoformat(final["v"]) > datetime(2026, 9, 23, 22, 4)
        assert await collect(connector, dict(checkpoints[-1].cursor)) == []
        window = [
            r
            async for r in connector.backfill(
                datetime(2026, 9, 23, 22, 1, 30, tzinfo=UTC),
                datetime(2026, 9, 23, 22, 3, 30, tzinfo=UTC),
            )
        ]
        assert [r.locator for r in window] == ["purchase_orders:row:3", "purchase_orders:row:4"]
    finally:
        await connector.close()


async def test_insert_through_the_connector_session_is_refused(postgres: dict[str, Any]) -> None:
    connector = make(postgres, "carto_rw", RW_PASSWORD)  # the grant allows it; the session must not

    def attempt() -> None:
        with connector._read_only_session() as connection:
            assert connection.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
            assert connection.execute(text("SHOW statement_timeout")).scalar() == "5s"
            assert connection.execute(text("SHOW TimeZone")).scalar() == "UTC"
            connection.execute(text("INSERT INTO purchase_orders (po_num) VALUES ('x')"))

    try:
        with pytest.raises(DBAPIError, match="read-only"):
            attempt()
        rows = await collect(connector)
        assert len(rows) == 5  # nothing was written
    finally:
        await connector.close()
