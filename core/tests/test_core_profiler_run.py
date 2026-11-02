"""The profile pass against the in-memory event store (spec 9.1, 18.1; ADR 0046, 0047): an
incremental pass followed by one with no new rows writes nothing; a full pass equals the
incremental passes' result; rows observed outside the window are ignored; the watermark follows
``stored_at``; the null rate comes from template counts; incompatible stored sketches are
rebuilt; a full pass drops days before the window."""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
import sqlalchemy

from carto_common.ids import derive_ulid
from carto_core.metadata import InMemoryMetadataStore
from carto_core.profiler.model import DaySketch
from carto_core.profiler.repository import (
    InMemoryFieldProfileSink,
    InMemorySketchRepository,
    InMemoryWatermarkStore,
    MetadataFieldProfileSink,
    PostgresSketchRepository,
    PostgresWatermarkStore,
)
from carto_core.profiler.run import ProfilePass, ProfileReport, profile_window, window_dates
from carto_core.profiler.sketches import SKETCH_VERSION
from carto_core.settings import CoreSettings, ProfilerSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import FieldForm, Window
from carto_core.worker import WorkerContext
from carto_schema.event import CanonicalEvent

TENANT = "default"
NOW = datetime(2026, 10, 18, 12, tzinfo=UTC)
STORED_1 = NOW - timedelta(hours=2)
STORED_2 = NOW - timedelta(hours=1)
ORDER = FieldForm("sys_a/tpl_1/order_ref", "raw")
PAYMENT = FieldForm("sys_b/tpl_2/order_id", "raw")
SETTINGS = ProfilerSettings(
    minhash_permutations=16, bottom_k=64, min_distinct=5, read_batch_rows=1000
)


def tok(i: int) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(str(i).encode()).digest()).decode()[:22]
    return f"t1.{body}"


def event(
    end: FieldForm | None,
    i: int,
    *,
    observed_at: datetime,
    system: str = "sys_a",
    template: str = "tpl_1",
    token_index: int | None = None,
) -> CanonicalEvent:
    if end is not None:
        system, template, path = end.field_ref.split("/", 2)
        identifiers = [
            {
                "field": path,
                "form": end.form,
                "token": tok(token_index if token_index is not None else i),
                "shape": "AA-9999999",
                "len": 10,
            }
        ]
    else:
        identifiers = []
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(1_790_000_000_000 + i, system, template, str(i)),
        "tenant_id": TENANT,
        "system_id": system,
        "template_id": template,
        "observed_at": observed_at,
        "identifiers": identifiers,
    }
    return CanonicalEvent.model_validate(data)


class Rig:
    def __init__(self, settings: ProfilerSettings = SETTINGS) -> None:
        self.store = InMemoryEventStore()
        self.sketches = InMemorySketchRepository()
        self.profiles = InMemoryFieldProfileSink()
        self.watermarks = InMemoryWatermarkStore()
        self.pass_ = ProfilePass(
            self.store, self.sketches, self.profiles, self.watermarks, settings
        )

    def add(self, events: Sequence[CanonicalEvent], stored_at: datetime) -> None:
        self.store.add_events(events, stored_at=stored_at)

    def run(self, *, full: bool = False, now: datetime = NOW) -> ProfileReport:
        return self.pass_.run(TENANT, full=full, now=now)

    def days(self) -> dict[tuple[FieldForm, date], DaySketch]:
        days = window_dates(profile_window(NOW, SETTINGS))
        return {
            (end, sketch.day): sketch
            for end, sketches in self.sketches.window_days(TENANT, days)
            for sketch in sketches
        }


def batch(end: FieldForm, start: int, n: int, at: datetime) -> list[CanonicalEvent]:
    return [
        event(end, i, observed_at=at + timedelta(seconds=i % 600)) for i in range(start, start + n)
    ]


# -- window -----------------------------------------------------------------------------------


def test_profile_window_is_day_aligned_with_one_day_ahead() -> None:
    window = profile_window(NOW, SETTINGS)
    assert window == Window(
        start=datetime(2026, 10, 12, tzinfo=UTC), end=datetime(2026, 10, 20, tzinfo=UTC)
    )
    assert window_dates(window) == tuple(date(2026, 10, 12 + i) for i in range(8))
    with pytest.raises(ValueError, match="timezone-aware"):
        profile_window(datetime(2026, 10, 18, 12), SETTINGS)


def test_profile_window_follows_window_days() -> None:
    window = profile_window(NOW, ProfilerSettings(window_days=1))
    assert window_dates(window) == (date(2026, 10, 18), date(2026, 10, 19))


# -- passes -----------------------------------------------------------------------------------


def test_first_pass_sketches_profiles_and_sets_the_watermark() -> None:
    rig = Rig()
    rig.add(batch(ORDER, 0, 40, NOW - timedelta(days=1)), STORED_1)
    rig.add(
        [event(None, 900 + i, observed_at=NOW - timedelta(days=1)) for i in range(10)], STORED_1
    )
    report = rig.run()
    assert report == ProfileReport(
        rows_read=40,
        ends_touched=1,
        profiles_written=1,
        new_sketches=True,
        watermark=STORED_1,
        full=False,
        window_start=date(2026, 10, 12),
        window_end=date(2026, 10, 19),
    )
    assert rig.watermarks.get(TENANT) == STORED_1
    sketch = rig.sketches.get_day(TENANT, ORDER, date(2026, 10, 17))
    assert sketch is not None
    assert sketch.count == 40
    profile = rig.profiles.get(TENANT, ORDER)
    assert profile is not None
    assert profile.count == 40
    assert profile.null_rate == pytest.approx(0.2)
    assert profile.top_shape == "AA-9999999"
    assert profile.is_candidate


def test_second_pass_without_new_rows_writes_nothing() -> None:
    rig = Rig()
    rig.add(batch(ORDER, 0, 30, NOW - timedelta(hours=5)), STORED_1)
    rig.run()
    before = (dict(rig.days()), dict(rig.profiles.profiles), rig.profiles.updates)
    report = rig.run()
    assert report.rows_read == 0
    assert report.new_sketches is False
    assert report.profiles_written == 0
    assert report.ends_touched == 0
    assert report.watermark == STORED_1
    assert (dict(rig.days()), dict(rig.profiles.profiles), rig.profiles.updates) == before


def test_incremental_passes_equal_one_full_pass() -> None:
    incremental = Rig()
    full = Rig()
    first = batch(ORDER, 0, 1500, NOW - timedelta(days=2)) + batch(
        PAYMENT, 0, 700, NOW - timedelta(days=1)
    )
    second = batch(ORDER, 1500, 1200, NOW - timedelta(days=1)) + batch(PAYMENT, 700, 900, NOW)
    for rig in (incremental, full):
        rig.add(first, STORED_1)
    incremental.run()
    for rig in (incremental, full):
        rig.add(second, STORED_2)
    report = incremental.run()
    assert report.rows_read == 2100
    assert report.ends_touched == 2
    assert report.watermark == STORED_2
    # The full pass starts from a repository holding a stale sketch that must not survive.
    bogus = full.sketches
    bogus.put_days(TENANT, [_stale_sketch(ORDER, date(2026, 10, 16))])
    full_report = full.run(full=True)
    assert full_report.rows_read == 4300
    assert full_report.full is True
    assert full_report.new_sketches is True
    assert full_report.watermark == STORED_2
    assert full.days() == incremental.days()
    assert full.profiles.profiles == incremental.profiles.profiles
    assert full_report.profiles_written == 2


def _stale_sketch(end: FieldForm, day: date, *, version: int = SKETCH_VERSION) -> DaySketch:
    return DaySketch(
        end=end,
        day=day,
        count=999_999,
        shape_counts={"X": 1},
        length_counts={1: 1},
        minhash=b"\x00" * (SETTINGS.minhash_permutations * 8),
        hll=b"\x00" * 4096,
        bottom_k=(1,),
        sketch_version=version,
    )


def test_rows_outside_the_window_are_ignored() -> None:
    rig = Rig()
    old = batch(ORDER, 0, 20, NOW - timedelta(days=30))
    far = batch(ORDER, 100, 20, NOW + timedelta(days=5))
    ahead = batch(ORDER, 200, 20, NOW + timedelta(hours=20))
    rig.add(old + far + ahead, STORED_1)
    report = rig.run()
    assert report.rows_read == 20
    assert rig.days().keys() == {(ORDER, date(2026, 10, 19))}
    assert rig.watermarks.get(TENANT) == STORED_1
    assert report.watermark == STORED_1


def test_full_pass_drops_days_before_the_window_and_rebuilds_stale_rows() -> None:
    rig = Rig()
    rig.sketches.put_days(
        TENANT,
        [
            _stale_sketch(ORDER, date(2026, 9, 1)),
            _stale_sketch(ORDER, date(2026, 10, 11)),
            _stale_sketch(ORDER, date(2026, 10, 17)),
        ],
    )
    rig.add(batch(ORDER, 0, 25, NOW - timedelta(days=1)), STORED_1)
    report = rig.run(full=True)
    assert report.rows_read == 25
    assert rig.sketches.list_ends(TENANT) == [ORDER]
    remaining = rig.sketches.get_days(
        TENANT,
        [(ORDER, date(2026, 9, 1)), (ORDER, date(2026, 10, 11)), (ORDER, date(2026, 10, 17))],
    )
    assert set(remaining) == {(ORDER, date(2026, 10, 17))}
    assert remaining[(ORDER, date(2026, 10, 17))].count == 25


def test_incremental_pass_rebuilds_an_incompatible_stored_sketch() -> None:
    rig = Rig()
    rig.sketches.put_days(TENANT, [_stale_sketch(ORDER, date(2026, 10, 17), version=0)])
    rig.add(batch(ORDER, 0, 12, NOW - timedelta(days=1)), STORED_1)
    rig.run()
    sketch = rig.sketches.get_day(TENANT, ORDER, date(2026, 10, 17))
    assert sketch is not None
    assert sketch.count == 12
    assert sketch.sketch_version == SKETCH_VERSION
    profile = rig.profiles.get(TENANT, ORDER)
    assert profile is not None
    assert profile.count == 12


def test_incremental_pass_merges_into_the_stored_day() -> None:
    rig = Rig()
    rig.add(batch(ORDER, 0, 10, NOW - timedelta(days=1)), STORED_1)
    rig.run()
    rig.add(batch(ORDER, 10, 15, NOW - timedelta(days=1, hours=3)), STORED_2)
    report = rig.run()
    assert report.rows_read == 15
    sketch = rig.sketches.get_day(TENANT, ORDER, date(2026, 10, 17))
    assert sketch is not None
    assert sketch.count == 25
    assert sketch.first_seen == NOW - timedelta(days=1, hours=3) + timedelta(seconds=10)
    profile = rig.profiles.get(TENANT, ORDER)
    assert profile is not None
    assert profile.count == 25


def test_only_touched_ends_are_reprofiled_incrementally() -> None:
    rig = Rig()
    rig.add(batch(ORDER, 0, 10, NOW - timedelta(days=1)) + batch(PAYMENT, 0, 10, NOW), STORED_1)
    rig.run()
    rig.add(batch(PAYMENT, 10, 5, NOW), STORED_2)
    report = rig.run()
    assert report.ends_touched == 1
    assert report.profiles_written == 1
    order = rig.profiles.get(TENANT, ORDER)
    payment = rig.profiles.get(TENANT, PAYMENT)
    assert order is not None and order.count == 10
    assert payment is not None and payment.count == 15


def test_watermark_never_moves_backwards() -> None:
    rig = Rig()
    rig.add(batch(ORDER, 0, 5, NOW), STORED_2)
    rig.run()
    rig.watermarks.set(TENANT, STORED_2)
    rig.add(batch(ORDER, 5, 5, NOW), STORED_1)
    report = rig.run()
    # Rows stored before the watermark are not "new" and the watermark stays put.
    assert report.rows_read == 0
    assert report.watermark == STORED_2
    rig.add(batch(ORDER, 10, 5, NOW), STORED_1)
    rig.run(full=True)
    assert rig.watermarks.get(TENANT) == STORED_2


def test_from_context_wires_the_postgres_repositories_and_the_metadata_sink() -> None:
    engine = sqlalchemy.create_engine("postgresql+psycopg://nobody@127.0.0.1:1/none")
    context = WorkerContext(
        settings=CoreSettings(_env_file=None, profiler=SETTINGS),
        tenant_id=TENANT,
        events=InMemoryEventStore(),
        metadata=InMemoryMetadataStore(),
        engine=engine,
        clock=lambda: NOW,
    )
    built = ProfilePass.from_context(context)
    assert isinstance(built, ProfilePass)
    assert isinstance(built._sketches, PostgresSketchRepository)
    assert isinstance(built._watermarks, PostgresWatermarkStore)
    assert isinstance(built._profiles, MetadataFieldProfileSink)
    assert built._store is context.events
    assert built._settings == SETTINGS
    engine.dispose()


def test_pass_writes_fields_through_the_metadata_store() -> None:
    metadata = InMemoryMetadataStore()
    rig = Rig()
    pass_ = ProfilePass(
        rig.store, rig.sketches, MetadataFieldProfileSink(metadata), rig.watermarks, SETTINGS
    )
    rig.add(batch(ORDER, 0, 30, NOW - timedelta(days=1)), STORED_1)
    report = pass_.run(TENANT, full=False, now=NOW)
    assert report.profiles_written == 1
    record = metadata.get_field(TENANT, ORDER.field_ref)
    assert record is not None
    assert record.count == 30
    assert record.is_candidate
    assert record.quality is not None and record.quality > 0.0
    assert record.field_class == "identifier"


def test_full_pass_over_an_empty_store_reports_nothing_and_keeps_the_watermark() -> None:
    rig = Rig()
    report = rig.run(full=True)
    assert report.rows_read == 0
    assert report.watermark is None
    assert report.profiles_written == 0
    assert rig.profiles.updates == 0
