"""The profile pass (spec 9.1; plan M2 "How the engine runs" step 3; ADR 0046, 0047).

:class:`ProfilePass.run` is the body of the ``profile`` job (:class:`carto_core.jobs.JobKind`):
``carto-core worker`` runs it every ``update_interval_seconds`` incrementally and once a night
in full; ``carto-core discover`` runs one full pass over a loaded bundle. Both are one code path:

1. The window is the last ``window_days`` UTC days up to and including ``now``'s day, aligned to
   day starts so a day sketch always covers a whole day, plus one day ahead: ingest accepts
   ``observed_at`` up to a day in the future (ADR 0036), and a row observed "tomorrow" must
   land in tomorrow's sketch rather than wait for the rebuild. The read window is therefore
   ``[start of (today - window_days + 1), start of (today + 2))``; rows observed before it are
   ignored whatever their ``stored_at``.
2. An incremental pass reads identifier rows with ``stored_at`` after the stored watermark
   (ADR 0046) in chunks of ``read_batch_rows``; a full pass ignores the watermark, drops the
   sketches of days before the window and rebuilds every ``(end, day)`` it meets from scratch
   (the first chunk that touches a key starts from nothing instead of the stored sketch).
   Within a chunk rows are grouped by ``(end, day)``, sketched, merged into the stored day
   sketch (one bulk read of the touched keys) and written back, so memory is bounded by one
   chunk plus the sketches it touches, never by the table.
3. Every end touched (every stored end on a full pass) gets its profile rebuilt from the merge
   of its day sketches in the window and the template event counts of the same days (the
   null-rate denominator), and written through the :class:`FieldProfileSink`.
4. The watermark moves to the latest ``stored_at`` read. A writer whose ``stored_at`` precedes
   that but which commits after the read is missed until the nightly full pass; a lateness
   allowance would re-read rows and double-count them, so the full pass is the correction.

The pass logs counts only (spec 2.3 invariant 7): rows, ends, sketches, profiles.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from carto_common.logging import get_logger
from carto_core.profiler.quality import build_profile
from carto_core.profiler.repository import (
    MetadataFieldProfileSink,
    PostgresSketchRepository,
    PostgresWatermarkStore,
)
from carto_core.profiler.sketches import (
    DaySketchBuilder,
    merge_day,
    merge_window,
    sketch_compatible,
)
from carto_core.store.model import FieldForm, Window

if TYPE_CHECKING:
    from carto_core.profiler.model import DaySketch, FieldProfile
    from carto_core.profiler.repository import FieldProfileSink, SketchRepository, WatermarkStore
    from carto_core.settings import ProfilerSettings
    from carto_core.store.model import EventStore, IdentifierRow
    from carto_core.worker import WorkerContext

__all__ = ["ProfilePass", "ProfileReport", "profile_window", "window_dates"]

_log = get_logger(component="profiler")


@dataclass(frozen=True, slots=True)
class ProfileReport:
    """What one pass did; the worker logs it and decides whether to queue a link pass."""

    rows_read: int
    ends_touched: int
    profiles_written: int
    new_sketches: bool
    watermark: datetime | None
    full: bool
    window_start: date
    window_end: date
    """The last day the pass reads (today plus one, ADR 0036 future allowance), inclusive."""


def profile_window(now: datetime, settings: ProfilerSettings) -> Window:
    """The read window of a pass at ``now`` (module docstring, step 1)."""
    if now.tzinfo is None:
        msg = "now must be timezone-aware"
        raise ValueError(msg)
    today = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=settings.window_days - 1)
    return Window(start=start, end=today + timedelta(days=2))


def window_dates(window: Window) -> tuple[date, ...]:
    """Every UTC day the window touches, ascending."""
    first = window.start.astimezone(UTC).date()
    last = (window.end.astimezone(UTC) - timedelta(microseconds=1)).date()
    return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))


def _template_of(end: FieldForm) -> tuple[str, str]:
    system, _sep, rest = end.field_ref.partition("/")
    template, _sep, _path = rest.partition("/")
    return system, template


class ProfilePass:
    """See the module docstring. One instance per worker; ``run`` is re-entrant per tenant."""

    __slots__ = ("_profiles", "_settings", "_sketches", "_store", "_watermarks")

    def __init__(
        self,
        store: EventStore,
        sketches: SketchRepository,
        profiles: FieldProfileSink,
        watermarks: WatermarkStore,
        settings: ProfilerSettings,
    ) -> None:
        self._store = store
        self._sketches = sketches
        self._profiles = profiles
        self._watermarks = watermarks
        self._settings = settings

    @classmethod
    def from_context(cls, context: WorkerContext) -> ProfilePass:
        """The pass as ``carto-core worker`` and ``discover`` build it: sketches and the
        watermark in PostgreSQL through ``context.engine``, profiles through
        ``context.metadata``, rows from ``context.events``."""
        return cls(
            store=context.events,
            sketches=PostgresSketchRepository(context.engine),
            profiles=MetadataFieldProfileSink(context.metadata),
            watermarks=PostgresWatermarkStore(context.engine),
            settings=context.settings.profiler,
        )

    def run(self, tenant_id: str, *, full: bool, now: datetime) -> ProfileReport:
        window = profile_window(now, self._settings)
        days = window_dates(window)
        watermark = None if full else self._watermarks.get(tenant_id)
        if full:
            dropped = self._sketches.delete_before(tenant_id, days[0])
            _log.info("profiler.sketches_dropped", tenant_id=tenant_id, rows=dropped)

        rows_read = 0
        latest: datetime | None = None
        touched_ends: set[FieldForm] = set()
        rebuilt: set[tuple[FieldForm, date]] = set()
        rows = self._store.identifier_rows(tenant_id, window, stored_after=watermark)
        for chunk in _chunks(rows, self._settings.read_batch_rows):
            builders: dict[tuple[FieldForm, date], DaySketchBuilder] = {}
            for row in chunk:
                rows_read += 1
                latest = row.stored_at if latest is None or row.stored_at > latest else latest
                key = (FieldForm(row.field_ref, row.form), row.observed_at.astimezone(UTC).date())
                builder = builders.get(key)
                if builder is None:
                    builder = builders[key] = DaySketchBuilder(key[0], key[1], self._settings)
                builder.add(row)
            self._merge_chunk(tenant_id, builders, full=full, rebuilt=rebuilt)
            touched_ends.update(end for end, _day in builders)
            _log.info(
                "profiler.chunk", tenant_id=tenant_id, rows=len(chunk), sketches=len(builders)
            )

        ends = None if full else touched_ends
        written = 0
        if full or touched_ends:
            written = self._rebuild_profiles(tenant_id, window, days, ends)

        new_watermark = watermark
        if latest is not None:
            previous = self._watermarks.get(tenant_id)
            new_watermark = latest if previous is None or latest > previous else previous
            if new_watermark != previous:
                self._watermarks.set(tenant_id, new_watermark)
        elif full:
            new_watermark = self._watermarks.get(tenant_id)

        report = ProfileReport(
            rows_read=rows_read,
            ends_touched=len(touched_ends),
            profiles_written=written,
            new_sketches=rows_read > 0,
            watermark=new_watermark,
            full=full,
            window_start=days[0],
            window_end=days[-1],
        )
        _log.info(
            "profiler.pass",
            tenant_id=tenant_id,
            full=full,
            rows=rows_read,
            ends=report.ends_touched,
            profiles=written,
        )
        return report

    def _merge_chunk(
        self,
        tenant_id: str,
        builders: dict[tuple[FieldForm, date], DaySketchBuilder],
        *,
        full: bool,
        rebuilt: set[tuple[FieldForm, date]],
    ) -> None:
        if not builders:
            return
        # On a full pass a key met for the first time starts from nothing; a stored sketch of
        # another version or size (ADR 0047) is rebuilt from the new rows alone as well.
        wanted = [key for key in builders if not full or key in rebuilt]
        existing = self._sketches.get_days(tenant_id, wanted) if wanted else {}
        merged: list[DaySketch] = []
        for key, builder in builders.items():
            current = existing.get(key)
            if current is not None and not sketch_compatible(current, self._settings):
                current = None
            merged.append(merge_day(current, builder.build(), settings=self._settings))
            rebuilt.add(key)
        self._sketches.put_days(tenant_id, merged)

    def _rebuild_profiles(
        self,
        tenant_id: str,
        window: Window,
        days: tuple[date, ...],
        ends: set[FieldForm] | None,
    ) -> int:
        template_events: dict[tuple[str, str], int] = defaultdict(int)
        for count in self._store.template_counts(tenant_id, window):
            template_events[(count.system_id, count.template_id)] += count.events
        written = 0
        batch: list[FieldProfile] = []
        for end, sketches in self._sketches.window_days(tenant_id, days, ends=ends):
            usable = [sketch for sketch in sketches if sketch_compatible(sketch, self._settings)]
            if not usable:
                continue
            profile = build_profile(
                merge_window(usable, settings=self._settings),
                template_events.get(_template_of(end), 0),
                self._settings,
            )
            batch.append(profile)
            if len(batch) >= _PROFILE_BATCH:
                self._profiles.update(tenant_id, batch)
                written += len(batch)
                batch = []
        if batch:
            self._profiles.update(tenant_id, batch)
            written += len(batch)
        return written


_PROFILE_BATCH = 500
"""Profiles per sink write; a batch size, not a spec threshold."""


def _chunks(rows: Iterator[IdentifierRow], size: int) -> Iterator[list[IdentifierRow]]:
    chunk: list[IdentifierRow] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
