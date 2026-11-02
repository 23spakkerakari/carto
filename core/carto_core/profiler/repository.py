"""Where the profiler keeps its state: day sketches, field profiles and the watermark
(spec 7.3 ``field_sketches``, ``fields``, ``settings``; ADR 0046, 0047, 0056).

Three small Protocols, each with an in-memory double for the engine's unit tests and a
PostgreSQL implementation (SQLAlchemy Core ``text`` statements, ``ON CONFLICT`` upserts):

- :class:`SketchRepository` over ``field_sketches`` (migration 0004, package A), keyed
  ``(tenant_id, field_id, form, day)`` where ``field_id`` is the ``field_ref``
  (``system_id/template_id/path``, the ``fields`` primary key). ``minhash``, ``hll`` and
  ``bottom_k`` are BYTEA in the formats of :mod:`carto_core.profiler.sketches` (the bottom-k is
  the ascending ``uint64`` array packed big-endian); the histograms and the top token counts are
  JSONB objects whose keys are strings (JSON has no integer keys, and a 64-bit token hash does
  not survive a JSON number in every reader).
- :class:`FieldProfileSink`: ``update(tenant_id, profiles)`` writes the profiler-owned columns of
  ``fields`` (``count``, ``distinct_estimate``, ``null_rate``, ``shapes_json``, ``quality``,
  ``is_candidate``, ``last_seen``; ADR 0056: never ``class`` or ``policy``).
  :class:`MetadataFieldProfileSink` is the deployment sink: it hands
  :class:`~carto_core.metadata.FieldProfileUpdate` rows to
  ``MetadataStore.update_field_profiles`` (package A), which owns the SQL. ``fields`` is keyed by
  ``field_ref`` while a profile is per ``(field_ref, form)``, so the sink keeps one profile per
  field: the form with the highest ``q`` (ties: ``raw`` first, then form name), because that is
  the form the linker would use the field through. All forms of a field travel on the same
  events, so a pass that touches one form rebuilds the others too and the choice is made over
  fresh profiles.
- :class:`WatermarkStore`: the profiler's ``stored_at`` watermark (ADR 0046) in the ``settings``
  table (``(tenant_id, key)`` primary key, ``value_json`` JSONB) under
  :data:`WATERMARK_KEY`, as ``{"stored_at": "<ISO 8601 UTC>"}``.

Errors raise :class:`RepositoryError` with the driver's exception class name, never a row.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Collection, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.metadata import FieldProfileUpdate
from carto_core.profiler.model import DaySketch, FieldProfile
from carto_core.profiler.sketches import pack_bottom_k, unpack_bottom_k
from carto_core.store.model import FieldForm

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy import Engine

__all__ = [
    "WATERMARK_KEY",
    "FieldProfileSink",
    "FieldProfileWriter",
    "InMemoryFieldProfileSink",
    "InMemorySketchRepository",
    "InMemoryWatermarkStore",
    "MetadataFieldProfileSink",
    "PostgresSketchRepository",
    "PostgresWatermarkStore",
    "RepositoryError",
    "SketchRepository",
    "WatermarkStore",
    "best_profile_per_field",
    "profile_update",
    "row_to_sketch",
    "sketch_to_row",
]

WATERMARK_KEY: Final = "profiler.watermark"
"""The ``settings.key`` of the profiler's ``stored_at`` watermark."""

_WINDOW_FETCH_ROWS: Final = 256
"""Server-side cursor batch for the window read; a fetch size, not a spec threshold."""


class RepositoryError(Exception):
    """The repository could not be read or written. The message names the cause, never a row."""


# -- Protocols --------------------------------------------------------------------------------


@runtime_checkable
class SketchRepository(Protocol):
    """``field_sketches`` as the profile pass uses it."""

    def get_day(self, tenant_id: str, end: FieldForm, day: date) -> DaySketch | None: ...

    def get_days(
        self, tenant_id: str, keys: Collection[tuple[FieldForm, date]]
    ) -> dict[tuple[FieldForm, date], DaySketch]:
        """The stored sketches among ``keys`` (one round trip for a chunk's touched days)."""
        ...

    def put_days(self, tenant_id: str, sketches: Sequence[DaySketch]) -> None:
        """Upsert by ``(end, day)``."""
        ...

    def window_days(
        self,
        tenant_id: str,
        days: Collection[date],
        *,
        ends: Collection[FieldForm] | None = None,
    ) -> Iterator[tuple[FieldForm, tuple[DaySketch, ...]]]:
        """Per end (all ends, or only ``ends``), its day sketches among ``days`` ascending by
        day; ends with no sketch in ``days`` are not yielded."""
        ...

    def delete_before(self, tenant_id: str, day: date) -> int:
        """Drop the sketches of days before ``day``; return how many rows went."""
        ...

    def list_ends(self, tenant_id: str) -> list[FieldForm]:
        """Every end with at least one stored sketch, in :class:`FieldForm` order."""
        ...


@runtime_checkable
class FieldProfileSink(Protocol):
    def update(self, tenant_id: str, profiles: Sequence[FieldProfile]) -> None: ...


@runtime_checkable
class FieldProfileWriter(Protocol):
    """The slice of :class:`carto_core.metadata.MetadataStore` the profiler writes through."""

    def update_field_profiles(
        self, tenant_id: str, profiles: Iterable[FieldProfileUpdate]
    ) -> int: ...


@runtime_checkable
class WatermarkStore(Protocol):
    def get(self, tenant_id: str) -> datetime | None: ...

    def set(self, tenant_id: str, at: datetime) -> None: ...


# -- in-memory doubles ------------------------------------------------------------------------


class InMemorySketchRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, FieldForm, date], DaySketch] = {}

    def get_day(self, tenant_id: str, end: FieldForm, day: date) -> DaySketch | None:
        with self._lock:
            return self._rows.get((tenant_id, end, day))

    def get_days(
        self, tenant_id: str, keys: Collection[tuple[FieldForm, date]]
    ) -> dict[tuple[FieldForm, date], DaySketch]:
        with self._lock:
            found = {key: self._rows.get((tenant_id, *key)) for key in keys}
        return {key: sketch for key, sketch in found.items() if sketch is not None}

    def put_days(self, tenant_id: str, sketches: Sequence[DaySketch]) -> None:
        with self._lock:
            for sketch in sketches:
                self._rows[(tenant_id, sketch.end, sketch.day)] = sketch

    def window_days(
        self,
        tenant_id: str,
        days: Collection[date],
        *,
        ends: Collection[FieldForm] | None = None,
    ) -> Iterator[tuple[FieldForm, tuple[DaySketch, ...]]]:
        wanted_days = set(days)
        wanted_ends = set(ends) if ends is not None else None
        grouped: dict[FieldForm, list[DaySketch]] = defaultdict(list)
        with self._lock:
            for (tenant, end, day), sketch in self._rows.items():
                if tenant != tenant_id or day not in wanted_days:
                    continue
                if wanted_ends is not None and end not in wanted_ends:
                    continue
                grouped[end].append(sketch)
        for end in sorted(grouped):
            yield end, tuple(sorted(grouped[end], key=lambda sketch: sketch.day))

    def delete_before(self, tenant_id: str, day: date) -> int:
        with self._lock:
            gone = [key for key in self._rows if key[0] == tenant_id and key[2] < day]
            for key in gone:
                del self._rows[key]
        return len(gone)

    def list_ends(self, tenant_id: str) -> list[FieldForm]:
        with self._lock:
            return sorted({end for (tenant, end, _day) in self._rows if tenant == tenant_id})

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)


class InMemoryFieldProfileSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.profiles: dict[tuple[str, FieldForm], FieldProfile] = {}
        self.updates = 0
        """How many ``update`` calls wrote at least one profile (the tests count writes)."""

    def update(self, tenant_id: str, profiles: Sequence[FieldProfile]) -> None:
        if not profiles:
            return
        with self._lock:
            self.updates += 1
            for profile in profiles:
                self.profiles[(tenant_id, profile.end)] = profile

    def get(self, tenant_id: str, end: FieldForm) -> FieldProfile | None:
        with self._lock:
            return self.profiles.get((tenant_id, end))


# -- the fields table through the metadata store ---------------------------------------------


def _form_rank(form: str) -> tuple[int, str]:
    return (0 if form == "raw" else 1, form)


def best_profile_per_field(profiles: Iterable[FieldProfile]) -> list[FieldProfile]:
    """One profile per ``field_ref``: the highest ``q``; ties go to ``raw``, then form name.
    Ordered by ``field_ref``."""
    best: dict[str, FieldProfile] = {}
    for profile in profiles:
        current = best.get(profile.end.field_ref)
        if current is None or (-profile.quality, _form_rank(profile.end.form)) < (
            -current.quality,
            _form_rank(current.end.form),
        ):
            best[profile.end.field_ref] = profile
    return [best[ref] for ref in sorted(best)]


def profile_update(profile: FieldProfile) -> FieldProfileUpdate:
    """The ``fields`` columns of one profile (ADR 0056: the profiler-owned ones only)."""
    return FieldProfileUpdate(
        field_ref=profile.end.field_ref,
        count=profile.count,
        distinct_estimate=profile.distinct,
        null_rate=profile.null_rate,
        shapes=tuple((share.shape, share.share) for share in profile.shapes),
        quality=profile.quality,
        is_candidate=profile.is_candidate,
        last_seen=profile.last_seen,
    )


class MetadataFieldProfileSink:
    """:class:`FieldProfileSink` over ``MetadataStore.update_field_profiles`` (module docstring)."""

    __slots__ = ("_metadata",)

    def __init__(self, metadata: FieldProfileWriter) -> None:
        self._metadata = metadata

    def update(self, tenant_id: str, profiles: Sequence[FieldProfile]) -> None:
        if not profiles:
            return
        updates = [profile_update(profile) for profile in best_profile_per_field(profiles)]
        try:
            self._metadata.update_field_profiles(tenant_id, updates)
        except Exception as exc:
            msg = f"fields update failed: {type(exc).__name__}"
            raise RepositoryError(msg) from exc


class InMemoryWatermarkStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._marks: dict[str, datetime] = {}

    def get(self, tenant_id: str) -> datetime | None:
        with self._lock:
            return self._marks.get(tenant_id)

    def set(self, tenant_id: str, at: datetime) -> None:
        if at.tzinfo is None:
            msg = "watermark must be timezone-aware"
            raise ValueError(msg)
        with self._lock:
            self._marks[tenant_id] = at


# -- row mapping ------------------------------------------------------------------------------


def _json_counts(counts: Mapping[Any, int]) -> str:
    return json.dumps({str(key): int(value) for key, value in counts.items()}, sort_keys=True)


def _load_json(value: object) -> dict[str, Any]:
    """A JSONB column arrives as a ``dict`` from psycopg, or as text from a driver that does not
    decode it; both are accepted and nothing else is."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes):
        loaded = json.loads(value)
        if isinstance(loaded, dict):
            return loaded
    msg = "field_sketches JSON column is not an object"
    raise RepositoryError(msg)


def sketch_to_row(tenant_id: str, sketch: DaySketch) -> dict[str, Any]:
    """The bind parameters of one ``field_sketches`` upsert."""
    return {
        "tenant_id": tenant_id,
        "field_id": sketch.end.field_ref,
        "form": sketch.end.form,
        "day": sketch.day,
        "count": sketch.count,
        "shape_counts": _json_counts(sketch.shape_counts),
        "length_counts": _json_counts(sketch.length_counts),
        "minhash": sketch.minhash,
        "hll": sketch.hll,
        "bottom_k": pack_bottom_k(sketch.bottom_k),
        "top_token_counts": _json_counts(sketch.top_token_counts),
        "first_seen": sketch.first_seen,
        "last_seen": sketch.last_seen,
        "sketch_version": sketch.sketch_version,
    }


def row_to_sketch(row: Mapping[Any, Any]) -> DaySketch:
    """A :class:`DaySketch` from one selected ``field_sketches`` row (column names as keys)."""
    day = row["day"]
    if isinstance(day, datetime):
        day = day.date()
    return DaySketch(
        end=FieldForm(field_ref=str(row["field_id"]), form=str(row["form"])),
        day=day,
        count=int(row["count"]),
        shape_counts={str(k): int(v) for k, v in _load_json(row["shape_counts"]).items()},
        length_counts={int(k): int(v) for k, v in _load_json(row["length_counts"]).items()},
        minhash=bytes(row["minhash"]),
        hll=bytes(row["hll"]),
        bottom_k=unpack_bottom_k(bytes(row["bottom_k"])),
        top_token_counts={int(k): int(v) for k, v in _load_json(row["top_token_counts"]).items()},
        first_seen=row.get("first_seen"),
        last_seen=row.get("last_seen"),
        sketch_version=int(row["sketch_version"]),
    )


# -- PostgreSQL -------------------------------------------------------------------------------

_UPSERT_SKETCH = sqlalchemy.text(
    "INSERT INTO field_sketches (tenant_id, field_id, form, day, count, shape_counts, "
    "length_counts, minhash, hll, bottom_k, top_token_counts, first_seen, last_seen, "
    "sketch_version, created_at, updated_at) VALUES (:tenant_id, :field_id, :form, :day, :count, "
    "CAST(:shape_counts AS JSONB), CAST(:length_counts AS JSONB), :minhash, :hll, :bottom_k, "
    "CAST(:top_token_counts AS JSONB), :first_seen, :last_seen, :sketch_version, now(), now()) "
    "ON CONFLICT (tenant_id, field_id, form, day) DO UPDATE SET count = EXCLUDED.count, "
    "shape_counts = EXCLUDED.shape_counts, length_counts = EXCLUDED.length_counts, "
    "minhash = EXCLUDED.minhash, hll = EXCLUDED.hll, bottom_k = EXCLUDED.bottom_k, "
    "top_token_counts = EXCLUDED.top_token_counts, first_seen = EXCLUDED.first_seen, "
    "last_seen = EXCLUDED.last_seen, sketch_version = EXCLUDED.sketch_version, updated_at = now()"
)
_SELECT_DAY = sqlalchemy.text(
    "SELECT tenant_id, field_id, form, day, count, shape_counts, length_counts, minhash, hll, "
    "bottom_k, top_token_counts, first_seen, last_seen, sketch_version FROM field_sketches "
    "WHERE tenant_id = :tenant_id AND field_id = :field_id AND form = :form AND day = :day"
)
_SELECT_DAYS = sqlalchemy.text(
    "SELECT tenant_id, field_id, form, day, count, shape_counts, length_counts, minhash, hll, "
    "bottom_k, top_token_counts, first_seen, last_seen, sketch_version FROM field_sketches "
    "WHERE tenant_id = :tenant_id AND day = ANY(:days) ORDER BY field_id, form, day"
)
_SELECT_DAYS_OF_ENDS = sqlalchemy.text(
    "SELECT tenant_id, field_id, form, day, count, shape_counts, length_counts, minhash, hll, "
    "bottom_k, top_token_counts, first_seen, last_seen, sketch_version FROM field_sketches "
    "WHERE tenant_id = :tenant_id AND day = ANY(:days) AND field_id = ANY(:field_ids) "
    "ORDER BY field_id, form, day"
)
_DELETE_BEFORE = sqlalchemy.text(
    "DELETE FROM field_sketches WHERE tenant_id = :tenant_id AND day < :day"
)
_LIST_ENDS = sqlalchemy.text(
    "SELECT DISTINCT field_id, form FROM field_sketches WHERE tenant_id = :tenant_id "
    "ORDER BY field_id, form"
)
_SELECT_SETTING = sqlalchemy.text(
    "SELECT value_json FROM settings WHERE tenant_id = :tenant_id AND key = :key"
)
_UPSERT_SETTING = sqlalchemy.text(
    "INSERT INTO settings (tenant_id, key, value_json, created_at, updated_at) VALUES "
    "(:tenant_id, :key, CAST(:value_json AS JSONB), now(), now()) "
    "ON CONFLICT (tenant_id, key) DO UPDATE SET value_json = EXCLUDED.value_json, "
    "updated_at = now()"
)


def _wrap(action: str, exc: SQLAlchemyError) -> RepositoryError:
    return RepositoryError(f"{action} failed: {type(exc).__name__}")


class PostgresSketchRepository:
    """``field_sketches`` (migration 0004). Upserts run in one transaction per ``put_days``."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_day(self, tenant_id: str, end: FieldForm, day: date) -> DaySketch | None:
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(
                        _SELECT_DAY,
                        {
                            "tenant_id": tenant_id,
                            "field_id": end.field_ref,
                            "form": end.form,
                            "day": day,
                        },
                    )
                    .mappings()
                    .first()
                )
        except SQLAlchemyError as exc:
            raise _wrap("field_sketches lookup", exc) from exc
        return row_to_sketch(row) if row is not None else None

    def get_days(
        self, tenant_id: str, keys: Collection[tuple[FieldForm, date]]
    ) -> dict[tuple[FieldForm, date], DaySketch]:
        if not keys:
            return {}
        wanted = set(keys)
        days = sorted({day for _end, day in wanted})
        field_ids = sorted({end.field_ref for end, _day in wanted})
        try:
            with self._engine.connect() as connection:
                rows = (
                    connection.execute(
                        _SELECT_DAYS_OF_ENDS,
                        {"tenant_id": tenant_id, "days": days, "field_ids": field_ids},
                    )
                    .mappings()
                    .all()
                )
        except SQLAlchemyError as exc:
            raise _wrap("field_sketches lookup", exc) from exc
        found: dict[tuple[FieldForm, date], DaySketch] = {}
        for row in rows:
            sketch = row_to_sketch(row)
            key = (sketch.end, sketch.day)
            if key in wanted:
                found[key] = sketch
        return found

    def put_days(self, tenant_id: str, sketches: Sequence[DaySketch]) -> None:
        if not sketches:
            return
        params = [sketch_to_row(tenant_id, sketch) for sketch in sketches]
        try:
            with self._engine.begin() as connection:
                connection.execute(_UPSERT_SKETCH, params)
        except SQLAlchemyError as exc:
            raise _wrap("field_sketches upsert", exc) from exc

    def window_days(
        self,
        tenant_id: str,
        days: Collection[date],
        *,
        ends: Collection[FieldForm] | None = None,
    ) -> Iterator[tuple[FieldForm, tuple[DaySketch, ...]]]:
        day_list = sorted(set(days))
        if not day_list or (ends is not None and not ends):
            return
        wanted_ends = set(ends) if ends is not None else None
        params: dict[str, Any] = {"tenant_id": tenant_id, "days": day_list}
        statement = _SELECT_DAYS
        if wanted_ends is not None:
            statement = _SELECT_DAYS_OF_ENDS
            params["field_ids"] = sorted({end.field_ref for end in wanted_ends})
        # Rows arrive ordered by (field_id, form, day) through a server-side cursor, so one
        # end's days are adjacent and a group is complete when the end changes; only one end's
        # sketches are in memory at a time (spec 17: a thousand candidate fields).
        try:
            with self._engine.connect() as connection:
                result = connection.execute(
                    statement.execution_options(yield_per=_WINDOW_FETCH_ROWS), params
                ).mappings()
                current: FieldForm | None = None
                group: list[DaySketch] = []
                for row in result:
                    sketch = row_to_sketch(row)
                    if wanted_ends is not None and sketch.end not in wanted_ends:
                        continue
                    if current is not None and sketch.end != current:
                        yield current, tuple(group)
                        group = []
                    current = sketch.end
                    group.append(sketch)
                if current is not None:
                    yield current, tuple(group)
        except SQLAlchemyError as exc:
            raise _wrap("field_sketches window read", exc) from exc

    def delete_before(self, tenant_id: str, day: date) -> int:
        try:
            with self._engine.begin() as connection:
                result = connection.execute(_DELETE_BEFORE, {"tenant_id": tenant_id, "day": day})
        except SQLAlchemyError as exc:
            raise _wrap("field_sketches delete", exc) from exc
        return int(result.rowcount or 0)

    def list_ends(self, tenant_id: str) -> list[FieldForm]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_LIST_ENDS, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _wrap("field_sketches end listing", exc) from exc
        return [FieldForm(field_ref=str(row[0]), form=str(row[1])) for row in rows]


class PostgresWatermarkStore:
    """The ``settings`` row ``(tenant_id, profiler.watermark)`` (migration 0004)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, tenant_id: str) -> datetime | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    _SELECT_SETTING, {"tenant_id": tenant_id, "key": WATERMARK_KEY}
                ).first()
        except SQLAlchemyError as exc:
            raise _wrap("watermark lookup", exc) from exc
        if row is None:
            return None
        value = _load_json(row[0]).get("stored_at")
        if not isinstance(value, str):
            msg = "watermark setting holds no stored_at"
            raise RepositoryError(msg)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            msg = "watermark setting is not an ISO 8601 timestamp"
            raise RepositoryError(msg) from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def set(self, tenant_id: str, at: datetime) -> None:
        if at.tzinfo is None:
            msg = "watermark must be timezone-aware"
            raise ValueError(msg)
        value = json.dumps({"stored_at": at.astimezone(UTC).isoformat()})
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    _UPSERT_SETTING,
                    {"tenant_id": tenant_id, "key": WATERMARK_KEY, "value_json": value},
                )
        except SQLAlchemyError as exc:
            raise _wrap("watermark upsert", exc) from exc
