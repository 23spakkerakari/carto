"""The assembly index contract (spec 7.3, 9.7; ADR 0060): one case list run against the
in-memory reference and against ``PostgresAssemblyIndex`` on a real PostgreSQL (a container,
skipped without Docker). A write lands whole; lookups skip expired entries; a merge marks the
merged-away row; batch keys keep their first sighting; fan-out and refusals add; the settling
sweep touches surviving in-progress rows of the entity only; the watermark clears."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta

import pytest

from carto_core.assembler.index import (
    AssemblyIndex,
    InMemoryAssemblyIndex,
    PostgresAssemblyIndex,
)
from carto_core.assembler.model import (
    STATUS_PRECEDENCE,
    BatchKeyRow,
    IndexEntry,
    IndexWrite,
    MergeRecord,
    TransactionRow,
    TransactionStatus,
)

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
DAY = date(2026, 11, 2)
A = "01TXNAAAAAAAAAAAAAAAAAAAAA"
B = "01TXNBBBBBBBBBBBBBBBBBBBBB"
C = "01TXNCCCCCCCCCCCCCCCCCCCCC"
ENT = "ent_0123456789abcdef"
END = "sys_b/tpl/x/raw"


def txn(
    txn_id: str,
    *,
    entity: str | None = ENT,
    status: TransactionStatus = TransactionStatus.IN_PROGRESS,
    last_seen: datetime = NOW,
    count: int = 1,
) -> TransactionRow:
    return TransactionRow(
        id=txn_id,
        entity_id=entity,
        first_seen=NOW - timedelta(hours=1),
        last_seen=last_seen,
        current_node="sys_a/tpl_1",
        status=status,
        event_count=count,
    )


def entry(token: str, txn_id: str, *, expires_in: timedelta = timedelta(days=14)) -> IndexEntry:
    return IndexEntry(token=token, txn_id=txn_id, last_seen=NOW, expires_at=NOW + expires_in)


def exercise(index: AssemblyIndex, tenant: str) -> None:
    assert isinstance(index, AssemblyIndex)
    assert index.ping()
    assert index.lookup(tenant, ["t1.x"], NOW) == {}
    assert index.lookup(tenant, [], NOW) == {}
    assert index.batch_keys(tenant, ["t1.x"]) == {}
    assert index.fanout(tenant, ["t1.x"], DAY) == {}
    assert index.transactions(tenant, [A]) == {}
    assert index.watermark(tenant) is None
    index.apply(tenant, IndexWrite())

    # One micro-batch: two transactions, three live tokens, one expiring now, a batch key.
    index.apply(
        tenant,
        IndexWrite(
            transactions=(txn(A), txn(B, count=2)),
            entries=(
                entry("t1.a", A),
                entry("t1.b", B),
                entry("t1.old", B, expires_in=timedelta(0)),
            ),
            batch_keys=(BatchKeyRow("t1.file", "sys_f/tpl/name", 0, NOW),),
            fanout={("t1.a", DAY): 1, ("t1.b", DAY): 2},
        ),
    )
    live = index.lookup(tenant, ["t1.a", "t1.b", "t1.old", "t1.none"], NOW)
    assert set(live) == {"t1.a", "t1.b"}, "an entry expiring at now is not live"
    assert live["t1.a"].txn_id == A and live["t1.b"].expires_at == NOW + timedelta(days=14)
    assert index.lookup(tenant, ["t1.old"], NOW - timedelta(seconds=1)) != {}
    rows = index.transactions(tenant, [A, B, C])
    assert set(rows) == {A, B}
    assert rows[B].event_count == 2 and rows[B].survives and rows[B].updated_at is not None
    assert rows[A].status is TransactionStatus.IN_PROGRESS
    assert index.batch_keys(tenant, ["t1.file"])["t1.file"].field_ref == "sys_f/tpl/name"
    assert index.fanout(tenant, ["t1.a", "t1.b"], DAY) == {"t1.a": 1, "t1.b": 2}
    assert index.fanout(tenant, ["t1.a"], DAY + timedelta(days=1)) == {}

    # A second batch merges B into A, repoints B's token, adds fan-out, keeps the batch key's
    # first sighting, and counts a refused merge on a link.
    index.apply(
        tenant,
        IndexWrite(
            transactions=(txn(A, count=3),),
            entries=(entry("t1.b", A),),
            merges=(MergeRecord(from_txn=B, to_txn=A, merged_at=NOW, token_end=END),),
            batch_keys=(BatchKeyRow("t1.file", "sys_other/tpl/name", 9, NOW),),
            fanout={("t1.b", DAY): 1},
            refusals={"lnk_1": 2},
        ),
    )
    assert index.lookup(tenant, ["t1.b"], NOW)["t1.b"].txn_id == A
    rows = index.transactions(tenant, [A, B])
    assert rows[B].merged_into == A and not rows[B].survives
    assert rows[A].event_count == 3 and rows[A].survives
    assert index.batch_keys(tenant, ["t1.file"])["t1.file"].field_ref == "sys_f/tpl/name"
    assert index.fanout(tenant, ["t1.b"], DAY) == {"t1.b": 3}
    # Upserting the merged-away row again keeps merged_into (a replay never resurrects it).
    index.apply(tenant, IndexWrite(transactions=(txn(B, count=2),)))
    assert index.transactions(tenant, [B])[B].merged_into == A

    # Expiry deletes entries at or before the cut; the live ones stay.
    assert index.expire(tenant, NOW) == 1
    assert index.expire(tenant, NOW) == 0
    assert set(index.lookup(tenant, ["t1.a", "t1.b"], NOW)) == {"t1.a", "t1.b"}

    # The settling sweep: surviving in-progress rows of the entity with last_seen before the
    # cut; a merged-away row, another entity, an errored row and a recent row are untouched.
    index.apply(
        tenant,
        IndexWrite(
            transactions=(
                txn(A, last_seen=NOW - timedelta(hours=2)),
                txn(C, entity="ent_other", last_seen=NOW - timedelta(hours=2)),
                txn(
                    "01TXNDDDDDDDDDDDDDDDDDDDDD",
                    status=TransactionStatus.ERRORED,
                    last_seen=NOW - timedelta(hours=2),
                ),
                txn("01TXNEEEEEEEEEEEEEEEEEEEEE", last_seen=NOW - timedelta(minutes=5)),
                txn("01TXNFFFFFFFFFFFFFFFFFFFFF", entity=None, last_seen=NOW - timedelta(hours=2)),
            ),
        ),
    )
    assert index.settle(tenant, entity_id=ENT, quiet_before=NOW - timedelta(hours=1), now=NOW) == 1
    assert index.settle(tenant, entity_id=ENT, quiet_before=NOW - timedelta(hours=1), now=NOW) == 0
    assert index.settle(tenant, entity_id=None, quiet_before=NOW - timedelta(hours=1), now=NOW) == 1
    after = index.transactions(
        tenant, [A, B, C, "01TXNDDDDDDDDDDDDDDDDDDDDD", "01TXNEEEEEEEEEEEEEEEEEEEEE"]
    )
    assert after[A].status is TransactionStatus.COMPLETED
    assert after[B].status is TransactionStatus.IN_PROGRESS, "merged away: not swept"
    assert after[C].status is TransactionStatus.IN_PROGRESS, "another entity"
    assert after["01TXNDDDDDDDDDDDDDDDDDDDDD"].status is TransactionStatus.ERRORED
    assert after["01TXNEEEEEEEEEEEEEEEEEEEEE"].status is TransactionStatus.IN_PROGRESS

    # The watermark: set, read back to the millisecond, cleared.
    mark = NOW.replace(microsecond=123000)
    index.set_watermark(tenant, mark)
    assert index.watermark(tenant) == mark
    index.set_watermark(tenant, mark + timedelta(seconds=1))
    assert index.watermark(tenant) == mark + timedelta(seconds=1)
    index.set_watermark(tenant, None)
    assert index.watermark(tenant) is None
    with pytest.raises(ValueError, match="aware"):
        index.set_watermark(tenant, datetime(2026, 11, 2, 12, 0))

    # Tenants never mix.
    assert index.lookup("someone-else", ["t1.a"], NOW) == {}
    assert index.transactions("someone-else", [A]) == {}
    assert index.watermark("someone-else") is None


def test_status_precedence_is_the_spec_order() -> None:
    assert STATUS_PRECEDENCE == (
        TransactionStatus.STALLED,
        TransactionStatus.ERRORED,
        TransactionStatus.COMPLETED,
        TransactionStatus.IN_PROGRESS,
    )
    assert IndexWrite().is_empty and not IndexWrite(refusals={"x": 1}).is_empty


def test_in_memory_index() -> None:
    index = InMemoryAssemblyIndex()
    exercise(index, "default")
    assert index.refusals == {("default", "lnk_1"): 2}
    assert [record.from_txn for record in index.merges("default")] == [B]
    assert index.merges("other") == []


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
def index_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
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
def test_postgres_index(index_engine: object, marker: None) -> None:
    import sqlalchemy  # noqa: PLC0415
    from sqlalchemy import Engine  # noqa: PLC0415

    assert isinstance(index_engine, Engine)
    with index_engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "INSERT INTO key_links (tenant_id, id, field_a, form_a, field_b, form_b, "
                "link_type, role, direction, score, status) VALUES ('pg-index', 'lnk_1', "
                "'sys_a/tpl/x', 'raw', 'sys_b/tpl/x', 'raw', 'exact', 'transaction', 'a_to_b', "
                "0.9, 'accepted')"
            )
        )
    exercise(PostgresAssemblyIndex(index_engine), "pg-index")
    with index_engine.connect() as connection:
        refused = connection.execute(
            sqlalchemy.text(
                "SELECT ambiguous_merges FROM key_links "
                "WHERE tenant_id = 'pg-index' AND id = 'lnk_1'"
            )
        ).scalar_one()
        merges = connection.execute(
            sqlalchemy.text(
                "SELECT from_txn, to_txn, token_end FROM txn_merges WHERE tenant_id = 'pg-index'"
            )
        ).all()
    assert refused == 2
    assert [tuple(row) for row in merges] == [(B, A, END)]
