"""Profiler repositories (spec 7.3 ``field_sketches``, ``settings``; ADR 0047): the in-memory
doubles, the row mapping both ways, and the PostgreSQL implementations against a container
(``integration``; the ``field_sketches`` and ``settings`` tables come from migration 0004,
package A, so the container part skips until that migration is in the tree)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import docker  # type: ignore[import-untyped]
import pytest
import sqlalchemy
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from carto_core.db.postgres import create_engine_from_settings
from carto_core.metadata import FieldProfileUpdate, InMemoryMetadataStore
from carto_core.migrations import apply_postgres_migrations
from carto_core.profiler.model import DaySketch, FieldProfile
from carto_core.profiler.quality import build_profile
from carto_core.profiler.repository import (
    WATERMARK_KEY,
    FieldProfileSink,
    InMemoryFieldProfileSink,
    InMemorySketchRepository,
    InMemoryWatermarkStore,
    MetadataFieldProfileSink,
    PostgresSketchRepository,
    PostgresWatermarkStore,
    RepositoryError,
    SketchRepository,
    WatermarkStore,
    best_profile_per_field,
    profile_update,
    row_to_sketch,
    sketch_to_row,
)
from carto_core.profiler.sketches import DaySketchBuilder, merge_window, pack_bottom_k
from carto_core.settings import PostgresSettings, ProfilerSettings
from carto_core.store.model import FieldForm, IdentifierRow

TENANT = "default"
A = FieldForm("sys_a/tpl_1/order_ref", "raw")
B = FieldForm("sys_b/tpl_2/order_id", "norm")
SMALL = ProfilerSettings(minhash_permutations=16, bottom_k=64)
NOON = datetime(2026, 10, 17, 12, tzinfo=UTC)


def sketch(end: FieldForm, day: date, n: int) -> DaySketch:
    builder = DaySketchBuilder(end, day, SMALL)
    for i in range(n):
        builder.add(
            IdentifierRow(
                token=f"t1.{i:022d}",
                field_ref=end.field_ref,
                form=end.form,
                shape="AA-9999999" if i % 2 else "9999",
                event_id=f"ev{i}",
                system_id=end.system_id,
                observed_at=NOON + timedelta(minutes=i),
                stored_at=NOON,
            )
        )
    return builder.build()


# -- in-memory doubles ------------------------------------------------------------------------


def test_in_memory_doubles_satisfy_the_protocols() -> None:
    assert isinstance(InMemorySketchRepository(), SketchRepository)
    assert isinstance(InMemoryFieldProfileSink(), FieldProfileSink)
    assert isinstance(InMemoryWatermarkStore(), WatermarkStore)


def test_in_memory_repository_round_trips_and_groups_by_end() -> None:
    repo = InMemorySketchRepository()
    d1, d2, d3 = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)
    repo.put_days(TENANT, [sketch(A, d2, 3), sketch(A, d1, 2), sketch(B, d2, 4), sketch(A, d3, 1)])
    repo.put_days("other", [sketch(A, d2, 9)])
    assert repo.get_day(TENANT, A, d1) is not None
    assert repo.get_day(TENANT, B, d1) is None
    assert set(repo.get_days(TENANT, [(A, d1), (B, d2), (B, d3)])) == {(A, d1), (B, d2)}
    assert repo.get_days(TENANT, []) == {}
    grouped = list(repo.window_days(TENANT, [d1, d2]))
    assert [end for end, _ in grouped] == [A, B]
    assert [s.day for s in grouped[0][1]] == [d1, d2]
    assert [end for end, _ in repo.window_days(TENANT, [d1, d2, d3], ends=[B])] == [B]
    assert list(repo.window_days(TENANT, [date(2026, 1, 1)])) == []
    assert repo.list_ends(TENANT) == [A, B]
    assert repo.delete_before(TENANT, d2) == 1
    assert repo.get_day(TENANT, A, d1) is None
    assert repo.get_day("other", A, d2) is not None
    assert len(repo) == 4
    # An upsert replaces the day.
    repo.put_days(TENANT, [sketch(A, d2, 7)])
    current = repo.get_day(TENANT, A, d2)
    assert current is not None and current.count == 7


def test_in_memory_profile_sink_counts_writes() -> None:
    sink = InMemoryFieldProfileSink()
    sink.update(TENANT, [])
    assert sink.updates == 0
    window = merge_window([sketch(A, date(2026, 10, 17), 5)], settings=SMALL)
    profile = build_profile(window, 10, SMALL)
    sink.update(TENANT, [profile])
    assert sink.updates == 1
    assert sink.get(TENANT, A) == profile
    assert sink.get(TENANT, B) is None


def _profile(end: FieldForm, n: int, quality: float) -> FieldProfile:

    window = merge_window([sketch(end, date(2026, 10, 17), n)], settings=SMALL)
    return replace(build_profile(window, n * 2, SMALL), quality=quality)


def test_best_profile_per_field_prefers_quality_then_raw() -> None:
    a_raw = _profile(FieldForm("sys_a/tpl_1/ref", "raw"), 10, 0.5)
    a_norm = _profile(FieldForm("sys_a/tpl_1/ref", "norm"), 10, 0.9)
    a_alnum = _profile(FieldForm("sys_a/tpl_1/ref", "alnum"), 10, 0.9)
    b_digits = _profile(FieldForm("sys_a/tpl_1/num", "digits.0"), 10, 0.2)
    b_raw = _profile(FieldForm("sys_a/tpl_1/num", "raw"), 10, 0.2)
    chosen = best_profile_per_field([a_raw, a_alnum, a_norm, b_digits, b_raw])
    assert [p.end for p in chosen] == [b_raw.end, a_alnum.end]
    assert best_profile_per_field([]) == []


def test_profile_update_carries_only_the_profiler_columns() -> None:
    profile = _profile(A, 20, 0.7)
    update = profile_update(profile)
    assert isinstance(update, FieldProfileUpdate)
    assert update.field_ref == A.field_ref
    assert update.count == 20
    assert update.distinct_estimate == profile.distinct
    assert update.null_rate == pytest.approx(0.5)
    assert update.shapes == tuple((s.shape, s.share) for s in profile.shapes)
    assert update.quality == 0.7
    assert update.is_candidate == profile.is_candidate
    assert update.last_seen == profile.last_seen
    assert not hasattr(update, "field_class") and not hasattr(update, "policy")


def test_metadata_sink_writes_one_fields_row_per_field_and_wraps_errors() -> None:
    metadata = InMemoryMetadataStore()
    sink = MetadataFieldProfileSink(metadata)
    assert isinstance(sink, FieldProfileSink)
    sink.update(TENANT, [])
    assert metadata.get_field(TENANT, A.field_ref) is None
    low = _profile(A, 10, 0.3)
    high = _profile(FieldForm(A.field_ref, "norm"), 10, 0.8)
    sink.update(TENANT, [low, high])
    record = metadata.get_field(TENANT, A.field_ref)
    assert record is not None
    assert record.quality == 0.8
    assert record.count == 10
    assert record.field_class == "identifier" and record.policy == "tokenize"
    assert record.shapes == tuple((s.shape, s.share) for s in high.shapes)

    class Broken:
        def update_field_profiles(self, tenant_id: str, profiles: object) -> int:
            msg = "boom: hunter2"
            raise RuntimeError(msg)

    with pytest.raises(RepositoryError, match=r"fields update failed: RuntimeError$"):
        MetadataFieldProfileSink(Broken()).update(TENANT, [low])


def test_in_memory_watermark_store() -> None:
    marks = InMemoryWatermarkStore()
    assert marks.get(TENANT) is None
    marks.set(TENANT, NOON)
    assert marks.get(TENANT) == NOON
    assert marks.get("other") is None
    with pytest.raises(ValueError, match="timezone-aware"):
        marks.set(TENANT, datetime(2026, 10, 17, 12))


# -- row mapping ------------------------------------------------------------------------------


def test_row_mapping_round_trips_through_json_and_bytea_shapes() -> None:
    original = sketch(A, date(2026, 10, 17), 40)
    row = sketch_to_row(TENANT, original)
    assert row["tenant_id"] == TENANT
    assert row["field_id"] == A.field_ref
    assert row["form"] == A.form
    assert row["bottom_k"] == pack_bottom_k(original.bottom_k)
    assert set(json.loads(row["shape_counts"])) == {"AA-9999999", "9999"}
    assert all(isinstance(k, str) for k in json.loads(row["top_token_counts"]))
    # psycopg decodes JSONB to dicts; the mapping also accepts the text form.
    as_text = row_to_sketch(row)
    decoded = dict(row)
    for key in ("shape_counts", "length_counts", "top_token_counts"):
        decoded[key] = json.loads(row[key])
    decoded["day"] = datetime(2026, 10, 17, tzinfo=UTC)
    decoded["minhash"] = memoryview(row["minhash"])
    as_dict = row_to_sketch(decoded)
    for restored in (as_text, as_dict):
        assert restored == original


def test_row_mapping_refuses_a_non_object_json_column() -> None:
    row = sketch_to_row(TENANT, sketch(A, date(2026, 10, 17), 3))
    row["shape_counts"] = "[1, 2]"
    with pytest.raises(RepositoryError, match="not an object"):
        row_to_sketch(row)


# -- PostgreSQL (integration) -----------------------------------------------------------------


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


POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlalchemy.Engine]:
    if not _docker_available():
        pytest.skip("Docker is unreachable")
    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with PostgresContainer(
        POSTGRES_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, driver=None
    ) as container:
        settings = PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            sslmode="disable",
        )
        engine = create_engine_from_settings(settings)
        apply_postgres_migrations(engine)
        inspector = sqlalchemy.inspect(engine)
        if not inspector.has_table("field_sketches") or not inspector.has_table("settings"):
            engine.dispose()
            pytest.skip("migration 0004 (package A: field_sketches, settings) is not applied")
        yield engine
        engine.dispose()


@pytest.mark.integration
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_postgres_sketch_repository(engine: sqlalchemy.Engine) -> None:
    repo = PostgresSketchRepository(engine)
    assert isinstance(repo, SketchRepository)
    d1, d2, d3 = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)
    rows = [sketch(A, d1, 30), sketch(A, d2, 20), sketch(B, d2, 10), sketch(B, d3, 5)]
    repo.put_days(TENANT, rows)
    repo.put_days("other", [sketch(A, d2, 1)])
    assert repo.get_day(TENANT, A, d1) == rows[0]
    assert repo.get_day(TENANT, A, d3) is None
    found = repo.get_days(TENANT, [(A, d1), (B, d3), (B, d1)])
    assert found == {(A, d1): rows[0], (B, d3): rows[3]}
    grouped = list(repo.window_days(TENANT, [d1, d2, d3]))
    assert grouped == [(A, (rows[0], rows[1])), (B, (rows[2], rows[3]))]
    assert list(repo.window_days(TENANT, [d2, d3], ends=[B])) == [(B, (rows[2], rows[3]))]
    assert list(repo.window_days(TENANT, [], ends=[B])) == []
    assert repo.list_ends(TENANT) == [A, B]
    # Upsert replaces the day in place.
    replacement = sketch(A, d2, 25)
    repo.put_days(TENANT, [replacement])
    assert repo.get_day(TENANT, A, d2) == replacement
    assert repo.delete_before(TENANT, d2) == 1
    assert repo.get_day(TENANT, A, d1) is None
    assert repo.get_day("other", A, d2) is not None
    repo.put_days(TENANT, [])


@pytest.mark.integration
@pytest.mark.filterwarnings("ignore::ResourceWarning")
def test_postgres_watermark_store(engine: sqlalchemy.Engine) -> None:
    marks = PostgresWatermarkStore(engine)
    assert isinstance(marks, WatermarkStore)
    assert marks.get(TENANT) is None
    marks.set(TENANT, NOON)
    assert marks.get(TENANT) == NOON
    marks.set(TENANT, NOON + timedelta(minutes=5))
    assert marks.get(TENANT) == NOON + timedelta(minutes=5)
    assert marks.get("other") is None
    with engine.connect() as connection:
        value = connection.execute(
            sqlalchemy.text("SELECT value_json FROM settings WHERE tenant_id = :t AND key = :k"),
            {"t": TENANT, "k": WATERMARK_KEY},
        ).scalar_one()
    assert value == {"stored_at": (NOON + timedelta(minutes=5)).isoformat()}


def test_postgres_errors_never_carry_rows_or_credentials() -> None:
    """Every method wraps the driver error in RepositoryError naming only the exception class;
    port 1 refuses at once, so this needs no container."""
    broken = sqlalchemy.create_engine(
        "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none?connect_timeout=1"
    )
    repo = PostgresSketchRepository(broken)
    marks = PostgresWatermarkStore(broken)
    day = date(2026, 10, 17)
    one = sketch(A, day, 3)
    calls: list[Callable[[], object]] = [
        lambda: repo.get_day(TENANT, A, day),
        lambda: repo.get_days(TENANT, [(A, day)]),
        lambda: repo.put_days(TENANT, [one]),
        lambda: list(repo.window_days(TENANT, [day])),
        lambda: list(repo.window_days(TENANT, [day], ends=[A])),
        lambda: repo.delete_before(TENANT, day),
        lambda: repo.list_ends(TENANT),
        lambda: marks.get(TENANT),
        lambda: marks.set(TENANT, NOON),
    ]
    for call in calls:
        with pytest.raises(RepositoryError, match=r"failed: \w+$") as info:
            call()
        assert "hunter2" not in str(info.value)
        assert "nobody" not in str(info.value)
    # Empty inputs never touch the database.
    assert repo.get_days(TENANT, []) == {}
    repo.put_days(TENANT, [])
    assert list(repo.window_days(TENANT, [])) == []
    assert list(repo.window_days(TENANT, [day], ends=[])) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        marks.set(TENANT, datetime(2026, 10, 17, 12))
    broken.dispose()
