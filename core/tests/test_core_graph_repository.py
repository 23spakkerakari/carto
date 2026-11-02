"""The flow-graph repository contract (spec 7.3, 9.8; ADR 0061, 0066): one case list run
against the in-memory reference and against ``PostgresGraphRepository`` on a real PostgreSQL
(a container, skipped without Docker). A pass's node upsert never touches the reviewer's
columns; a rename bumps the version and refuses a stale one; replacing hops keeps a manual
confirmation; terminal nodes and entity statistics replace and upsert whole."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from carto_core.graph.model import (
    EntityStatsRow,
    HopRow,
    NodeRow,
    TerminalNodeRow,
    hop_id_for,
    node_id_for,
)
from carto_core.graph.repository import (
    GraphRepository,
    InMemoryGraphRepository,
    NotFoundError,
    PostgresGraphRepository,
    StaleVersionError,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
ORDER = node_id_for("sys_orders", "tpl_create")
PAY = node_id_for("sys_pay", "tpl_pay")
SHIP = node_id_for("sys_orders", "tpl_ship")
ENT = "ent_0123456789abcdef"


def hop(
    from_node: str, to_node: str, *, support: int = 10, confirmed: bool | None = None
) -> HopRow:
    return HopRow(
        id=hop_id_for(ENT, from_node, to_node),
        entity_id=ENT,
        from_node=from_node,
        to_node=to_node,
        support=support,
        p_next=0.5,
        latency_p50=60.0,
        latency_p95=120.0,
        latency_p99=None,
        error_count=1,
        human_actor_share=0.3,
        link_types=("exact", "composite"),
        window_start=NOW - timedelta(days=14),
        window_end=NOW,
        computed_at=NOW,
        is_manual_confirmed=confirmed,
    )


def test_ids_are_deterministic() -> None:
    assert node_id_for("sys_a", "tpl_1") == "sys_a/tpl_1"
    assert hop_id_for(ENT, ORDER, PAY) == hop_id_for(ENT, ORDER, PAY)
    assert hop_id_for(ENT, ORDER, PAY) != hop_id_for(ENT, PAY, ORDER)
    assert hop_id_for(None, ORDER, PAY) != hop_id_for(ENT, ORDER, PAY)
    assert hop_id_for(ENT, ORDER, PAY).startswith("hop_") and len(hop_id_for(ENT, ORDER, PAY)) == 20


def exercise(repository: GraphRepository, tenant: str) -> None:
    assert isinstance(repository, GraphRepository)
    assert repository.ping()
    assert repository.list_nodes(tenant) == []
    assert repository.get_node(tenant, ORDER) is None
    assert repository.list_hops(tenant) == []
    assert repository.terminal_nodes(tenant) == []
    assert repository.entity_stats(tenant) == {}
    assert repository.upsert_nodes(tenant, [], NOW) == 0

    # Nodes a pass met: new rows get version 1 and no display name.
    met = [
        NodeRow(ORDER, "sys_orders", "tpl_create", kind="db_row", first_seen=NOW, last_seen=NOW),
        NodeRow(PAY, "sys_pay", "tpl_pay", kind="log_line", first_seen=NOW, last_seen=LATER),
    ]
    assert repository.upsert_nodes(tenant, met, NOW) == 2
    nodes = repository.list_nodes(tenant)
    assert [node.id for node in nodes] == [ORDER, PAY]
    assert nodes[0].version == 1 and nodes[0].display_name is None
    assert nodes[0].kind == "db_row" and nodes[1].last_seen == LATER
    assert nodes[0].updated_at == NOW

    # A rename is the reviewer's; the next pass's upsert leaves it and the version alone and
    # widens the seen times only.
    renamed = repository.rename_node(
        tenant, ORDER, display_name="Order created", expected_version=1, now=LATER
    )
    assert renamed.display_name == "Order created" and renamed.version == 2
    assert renamed.updated_at == LATER
    with pytest.raises(StaleVersionError):
        repository.rename_node(tenant, ORDER, display_name="x", expected_version=1, now=LATER)
    with pytest.raises(NotFoundError):
        repository.rename_node(tenant, SHIP, display_name="x", expected_version=1, now=LATER)
    again = [
        NodeRow(
            ORDER,
            "sys_orders",
            "tpl_create",
            kind="",
            first_seen=NOW - timedelta(days=1),
            last_seen=LATER,
        ),
        NodeRow(SHIP, "sys_orders", "tpl_ship", kind="log_line", first_seen=LATER, last_seen=LATER),
    ]
    assert repository.upsert_nodes(tenant, again, LATER) == 1
    order = repository.get_node(tenant, ORDER)
    assert order is not None
    assert order.display_name == "Order created" and order.version == 2
    assert order.kind == "db_row", "an empty kind never overwrites a known one"
    assert order.first_seen == NOW - timedelta(days=1) and order.last_seen == LATER
    cleared = repository.rename_node(
        tenant, ORDER, display_name=None, expected_version=2, now=LATER
    )
    assert cleared.display_name is None and cleared.version == 3

    # Hops replace as a set; a manual confirmation survives a rollup that carries none.
    repository.replace_hops(tenant, [hop(ORDER, PAY, confirmed=True), hop(PAY, SHIP)])
    hops = repository.list_hops(tenant)
    assert [h.id for h in hops] == sorted(h.id for h in hops)
    by_id = {h.id: h for h in hops}
    first = by_id[hop_id_for(ENT, ORDER, PAY)]
    assert first.is_manual_confirmed is True and first.link_types == ("exact", "composite")
    assert first.latency_p99 is None and first.human_actor_share == 0.3
    assert first.window_start == NOW - timedelta(days=14) and first.computed_at == NOW
    repository.replace_hops(tenant, [hop(ORDER, PAY, support=11), hop(ORDER, SHIP)])
    kept = {h.id: h for h in repository.list_hops(tenant)}
    assert set(kept) == {hop_id_for(ENT, ORDER, PAY), hop_id_for(ENT, ORDER, SHIP)}
    assert kept[hop_id_for(ENT, ORDER, PAY)].support == 11
    assert kept[hop_id_for(ENT, ORDER, PAY)].is_manual_confirmed is True, "kept across rollups"
    assert kept[hop_id_for(ENT, ORDER, SHIP)].is_manual_confirmed is None
    repository.replace_hops(tenant, [hop(ORDER, PAY, confirmed=False)])
    assert repository.list_hops(tenant)[0].is_manual_confirmed is False
    repository.replace_hops(tenant, [])
    assert repository.list_hops(tenant) == []

    # Terminal nodes replace whole, ordered by entity then node.
    repository.replace_terminal_nodes(
        tenant,
        [TerminalNodeRow(ENT, SHIP, 0.97, NOW), TerminalNodeRow("ent_zz", PAY, 1.0, NOW)],
    )
    terminals = repository.terminal_nodes(tenant)
    assert [(t.entity_id, t.node_id, t.share) for t in terminals] == [
        (ENT, SHIP, 0.97),
        ("ent_zz", PAY, 1.0),
    ]
    repository.replace_terminal_nodes(tenant, [TerminalNodeRow(ENT, SHIP, 0.96, LATER)])
    assert [(t.node_id, t.share, t.computed_at) for t in repository.terminal_nodes(tenant)] == [
        (SHIP, 0.96, LATER)
    ]

    # Entity statistics upsert by entity.
    repository.upsert_entity_stats(tenant, [EntityStatsRow(ENT, 1800.0, 5400.0, 40, NOW)])
    repository.upsert_entity_stats(
        tenant,
        [
            EntityStatsRow(ENT, None, 1_209_600.0, 0, LATER),
            EntityStatsRow("ent_a", 10.0, 30.0, 3, LATER),
        ],
    )
    stats = repository.entity_stats(tenant)
    assert set(stats) == {ENT, "ent_a"}
    assert stats[ENT].p99_gap_seconds is None and stats[ENT].quiet_span_seconds == 1_209_600.0
    assert stats[ENT].settled_count == 0 and stats[ENT].computed_at == LATER
    assert stats["ent_a"].p99_gap_seconds == 10.0
    repository.upsert_entity_stats(tenant, [])

    # Tenants never mix.
    assert repository.list_nodes("someone-else") == []
    assert repository.list_hops("someone-else") == []
    assert repository.entity_stats("someone-else") == {}


def test_in_memory_graph_repository() -> None:
    exercise(InMemoryGraphRepository(), "default")


# -- PostgreSQL ------------------------------------------------------------------------------------


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
def graph_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    from testcontainers.core.config import testcontainers_config  # noqa: PLC0415

    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.migrations import apply_postgres_migrations  # noqa: PLC0415
    from carto_core.settings import PostgresSettings  # noqa: PLC0415

    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
    image = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text("carto_test\n", encoding="utf-8")
    with PostgresContainer(
        image,
        username="carto_test",
        password="carto_test",  # noqa: S106 - throwaway container credential
        dbname="carto_test",
        driver=None,
    ) as container:
        settings = PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database="carto_test",
            user="carto_test",
            password_file=password_file,
            sslmode="disable",
        )
        engine = create_engine_from_settings(settings)
        apply_postgres_migrations(engine)
        yield engine
        engine.dispose()


@pytest.mark.parametrize("marker", [pytest.param(None, marks=integration)])
def test_postgres_graph_repository(graph_engine: object, marker: None) -> None:
    from sqlalchemy import Engine  # noqa: PLC0415

    assert isinstance(graph_engine, Engine)
    exercise(PostgresGraphRepository(graph_engine), "pg-graph")
