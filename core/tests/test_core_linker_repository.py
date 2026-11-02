"""Link and entity repositories (spec 7.3 ``key_links``, ``key_link_decisions``, ``entities``,
``entity_fields``): the in-memory reference semantics run everywhere; the same cases run
against PostgreSQL in a container (migration 0004) under the ``integration`` marker, plus the
atomicity of a decision (a refused version leaves no decision row) and the JSONB round trip of
the evidence."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from carto_core.linker.model import (
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
    RejectReason,
)
from carto_core.linker.repository import (
    DecisionWrite,
    Entity,
    EntityRepository,
    InMemoryEntityRepository,
    InMemoryLinkRepository,
    LinkRepository,
    NotFoundError,
    PostgresEntityRepository,
    PostgresLinkRepository,
    StaleVersionError,
    features_from_evidence,
)
from carto_core.store.model import FieldForm

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TENANT = "default"
A = FieldForm("sys_a/tpl_a/order_id", "raw")
B = FieldForm("sys_b/tpl_b/order_ref", "digits.0")
C = FieldForm("sys_c/tpl_c/ref", "alnum")
FEATURES = PairFeatures(
    n_a=600,
    n_b=570,
    n_match=570,
    c_ab=0.95,
    c_ba=1.0,
    lag_median=7200.0,
    lag_iqr=3600.0,
    lag_positive_share=1.0,
    fanout_ab_median=1.0,
    fanout_ab_p99=1.0,
    fanout_ba_median=1.0,
    fanout_ba_p99=1.0,
    concentration=0.0175,
    chance_ratio=0.06,
    domain_size=1e4,
    pairing_consistency=None,
)


def key_link(
    link_id: str = "lnk_ab", a: FieldForm = A, b: FieldForm = B, **over: object
) -> KeyLink:
    base = KeyLink(
        id=link_id,
        a=a,
        b=b,
        link_type=LinkType.EXACT,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=0.93,
        status=LinkStatus.PROPOSED,
        features=FEATURES,
        affected_volume=570,
        first_proposed_at=NOW,
        extra={"sentence": "95% of A match a B.", "shape_a": "9999", "shape_b": "9999"},
    )
    return replace(base, **over)  # type: ignore[arg-type]


def test_features_round_trip_through_evidence() -> None:
    assert features_from_evidence(FEATURES.as_evidence()) == FEATURES
    sparse = features_from_evidence({"n_match": 3})
    assert sparse.n_match == 3 and sparse.c_ab == 0.0 and sparse.lag_median is None


# -- the shared case list ------------------------------------------------------------------------


def exercise_links(repository: LinkRepository, tenant: str) -> None:
    assert repository.get(tenant, "lnk_ab") is None
    assert repository.list_links(tenant) == []

    stored = repository.upsert(tenant, key_link(), NOW)
    assert stored.version == 1
    assert stored.updated_at == NOW
    assert stored.first_proposed_at == NOW
    assert stored.features == FEATURES
    assert stored.extra["sentence"] == "95% of A match a B."
    assert repository.get(tenant, "lnk_ab") == stored
    assert repository.find_by_ends(tenant, A, B) == stored
    assert repository.find_by_ends(tenant, B, A) is None, "ends are stored in one order"

    later = NOW + timedelta(hours=1)
    refreshed = repository.upsert(
        tenant, key_link(score=0.8, first_proposed_at=later, extra={"sentence": "80%"}), later
    )
    assert refreshed.version == 2
    assert refreshed.score == 0.8
    assert refreshed.first_proposed_at == NOW, "the first proposal time is kept"
    assert refreshed.updated_at == later
    assert refreshed.extra["sentence"] == "80%"
    assert len(repository.list_links(tenant)) == 1

    other = repository.upsert(tenant, key_link("lnk_bc", B, C, role=LinkRole.ASSOCIATION), NOW)
    assert other.version == 1
    assert [link.id for link in repository.list_links(tenant)] == ["lnk_ab", "lnk_bc"]
    assert repository.list_links(tenant, statuses=[LinkStatus.ACCEPTED]) == []
    assert repository.list_links("other-tenant") == []

    with pytest.raises(NotFoundError):
        repository.decide(
            tenant,
            "lnk_missing",
            expected_version=1,
            write=DecisionWrite(LinkStatus.ACCEPTED, "alice", NOW, 0.8),
        )
    with pytest.raises(StaleVersionError):
        repository.decide(
            tenant,
            "lnk_ab",
            expected_version=1,
            write=DecisionWrite(LinkStatus.ACCEPTED, "alice", NOW, 0.8),
        )
    assert repository.decisions(tenant, "lnk_ab") == [], "a refused decision leaves no row"

    decided_at = NOW + timedelta(hours=2)
    accepted = repository.decide(
        tenant,
        "lnk_ab",
        expected_version=2,
        write=DecisionWrite(
            LinkStatus.ACCEPTED, "alice", decided_at, 0.8, note="ok", baseline_match_rate=0.95
        ),
    )
    assert accepted.version == 3
    assert accepted.status == LinkStatus.ACCEPTED
    assert accepted.decided_by == "alice" and accepted.decided_at == decided_at
    assert accepted.baseline_match_rate == pytest.approx(0.95)
    assert accepted.role == LinkRole.TRANSACTION, "no role in the write keeps the stored one"
    assert accepted.updated_at == decided_at
    assert repository.list_links(tenant, statuses=[LinkStatus.ACCEPTED]) == [accepted]

    rejected = repository.decide(
        tenant,
        "lnk_ab",
        expected_version=3,
        write=DecisionWrite(
            LinkStatus.REJECTED,
            "bob",
            decided_at,
            0.8,
            role=LinkRole.ASSOCIATION,
            reject_reason=RejectReason.COINCIDENCE,
        ),
    )
    assert rejected.version == 4
    assert rejected.role == LinkRole.ASSOCIATION
    assert rejected.reject_reason == RejectReason.COINCIDENCE
    assert rejected.baseline_match_rate == pytest.approx(0.95), "None keeps the baseline"
    decisions = repository.decisions(tenant, "lnk_ab")
    assert [(d.version, d.status, d.decided_by, d.note) for d in decisions] == [
        (3, LinkStatus.ACCEPTED, "alice", "ok"),
        (4, LinkStatus.REJECTED, "bob", None),
    ]
    assert decisions[1].reject_reason == RejectReason.COINCIDENCE
    assert decisions[0].score_at_decision == pytest.approx(0.8)
    assert all(d.key_link_id == "lnk_ab" and d.tenant_id == tenant for d in decisions)
    assert repository.decisions(tenant, "lnk_bc") == []

    changed = repository.assign_entities(
        tenant, {"lnk_ab": "ent_1", "lnk_bc": "ent_1", "lnk_missing": "ent_1"}, NOW
    )
    assert changed == 2
    assert repository.assign_entities(tenant, {"lnk_ab": "ent_1"}, NOW) == 0
    assigned = repository.get(tenant, "lnk_ab")
    assert assigned is not None
    assert assigned.entity_id == "ent_1"
    assert assigned.version == 4, "a family assignment does not bump the version"
    assert repository.assign_entities(tenant, {"lnk_bc": None}, NOW) == 1
    cleared = repository.get(tenant, "lnk_bc")
    assert cleared is not None and cleared.entity_id is None

    # A re-proposal overwrites the decision columns but keeps the stored id and first proposal.
    back = repository.upsert(
        tenant, key_link("lnk_other_id", status=LinkStatus.PROPOSED, score=0.99), NOW
    )
    assert back.id == "lnk_ab"
    assert back.version == 5
    assert back.status == LinkStatus.PROPOSED
    assert back.reject_reason is None and back.decided_by is None
    assert back.first_proposed_at == NOW
    assert repository.ping()


def exercise_entities(repository: EntityRepository, tenant: str) -> None:
    assert repository.list_entities(tenant) == []
    assert repository.get(tenant, "ent_1") is None
    first = Entity(id="ent_1", members=(A, B), suggested_name="Order")
    second = Entity(id="ent_2", members=(C,), name="Thing", name_confirmed=True, description="d")
    repository.replace_all(tenant, [first, second], NOW)
    listed = repository.list_entities(tenant)
    assert [entity.id for entity in listed] == ["ent_1", "ent_2"]
    assert listed[0].members == (A, B)
    assert listed[0].suggested_name == "Order" and listed[0].name is None
    assert not listed[0].name_confirmed
    assert listed[0].display_name == "Order"
    assert listed[0].updated_at == NOW
    assert listed[1].name == "Thing" and listed[1].name_confirmed and listed[1].description == "d"
    assert repository.get(tenant, "ent_2") == listed[1]
    assert repository.list_entities("other-tenant") == []

    later = NOW + timedelta(minutes=5)
    named = repository.set_name(tenant, "ent_1", name="Sales order", description=None, now=later)
    assert named.name == "Sales order" and named.name_confirmed
    assert named.suggested_name == "Order"
    assert named.updated_at == later
    assert named.display_name == "Sales order"
    with pytest.raises(NotFoundError):
        repository.set_name(tenant, "ent_missing", name="x", description=None, now=later)

    grown = Entity(id="ent_1", members=(A, B, C), name="Sales order", name_confirmed=True)
    repository.replace_all(tenant, [grown], later)
    remaining = repository.list_entities(tenant)
    assert [entity.id for entity in remaining] == ["ent_1"]
    assert remaining[0].members == (A, B, C)
    assert remaining[0].name == "Sales order"
    repository.replace_all(tenant, [], later)
    assert repository.list_entities(tenant) == []
    assert repository.ping()


# -- in memory -----------------------------------------------------------------------------------


def test_in_memory_link_repository() -> None:
    exercise_links(InMemoryLinkRepository(), TENANT)


def test_in_memory_entity_repository() -> None:
    exercise_entities(InMemoryEntityRepository(), TENANT)


def test_in_memory_repositories_isolate_tenants() -> None:
    links = InMemoryLinkRepository()
    links.upsert("t1", key_link(), NOW)
    links.upsert("t2", key_link(), NOW)
    assert len(links.list_links("t1")) == 1 and len(links.list_links("t2")) == 1
    assert links.get("t2", "lnk_ab") is not None
    entities = InMemoryEntityRepository()
    entities.replace_all("t1", [Entity(id="ent_1", members=(A,))], NOW)
    assert entities.get("t2", "ent_1") is None


# -- PostgreSQL ----------------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415

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
def postgres_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[object]:
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
def test_postgres_link_repository(postgres_engine: object, marker: None) -> None:
    from sqlalchemy import Engine  # noqa: PLC0415

    assert isinstance(postgres_engine, Engine)
    exercise_links(PostgresLinkRepository(postgres_engine), "pg-links")


@pytest.mark.parametrize("marker", [pytest.param(None, marks=integration)])
def test_postgres_entity_repository(postgres_engine: object, marker: None) -> None:
    from sqlalchemy import Engine  # noqa: PLC0415

    assert isinstance(postgres_engine, Engine)
    exercise_entities(PostgresEntityRepository(postgres_engine), "pg-entities")


@pytest.mark.parametrize("marker", [pytest.param(None, marks=integration)])
def test_postgres_evidence_is_jsonb_and_decisions_are_atomic(
    postgres_engine: object, marker: None
) -> None:
    import sqlalchemy  # noqa: PLC0415
    from sqlalchemy import Engine  # noqa: PLC0415

    assert isinstance(postgres_engine, Engine)
    tenant = "pg-atomic"
    repository = PostgresLinkRepository(postgres_engine)
    stored = repository.upsert(tenant, key_link(), NOW)
    with postgres_engine.connect() as connection:
        value = connection.execute(
            sqlalchemy.text(
                "SELECT evidence_json ->> 'c_ab', evidence_json -> 'extra' ->> 'sentence' "
                "FROM key_links WHERE tenant_id = :tenant_id AND id = :id"
            ),
            {"tenant_id": tenant, "id": stored.id},
        ).one()
    assert float(value[0]) == pytest.approx(0.95)
    assert value[1] == "95% of A match a B."
    with pytest.raises(StaleVersionError):
        repository.decide(
            tenant,
            stored.id,
            expected_version=99,
            write=DecisionWrite(LinkStatus.ACCEPTED, "alice", NOW, 0.9),
        )
    with postgres_engine.connect() as connection:
        rows = connection.execute(
            sqlalchemy.text("SELECT count(*) FROM key_link_decisions WHERE tenant_id = :tenant_id"),
            {"tenant_id": tenant},
        ).scalar_one()
    assert rows == 0
    unchanged = repository.get(tenant, stored.id)
    assert unchanged is not None and unchanged.version == 1
