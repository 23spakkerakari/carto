"""Where a deployed link pass reads the profiler's output, and the profiler's estimators it
uses (plan M2 "How the engine runs" step 4; spec 9.1, 9.2; ADR 0047, 0048).

:class:`~carto_core.linker.run.LinkPass` takes a :class:`~carto_core.linker.run.ProfileSource`:
``profiles(tenant_id)`` for every ``(field_ref, form)`` end and ``window_sketch(tenant_id,
end)`` for the LSH index and the containment estimate. Package B keeps day sketches per end in
``field_sketches`` and writes one row per ``field_ref`` to ``fields`` (the best form,
:func:`carto_core.profiler.repository.best_profile_per_field`), so the per-end profiles the
linker needs come from the sketches, not from ``fields``:

- :class:`SketchProfileSource` merges each end's day sketches of the profiler's window
  (:func:`carto_core.profiler.sketches.merge_window`) and builds its profile with the same
  :func:`carto_core.profiler.quality.build_profile` and template event counts the profile pass
  uses, so ``q`` and ``is_candidate`` agree with the ``fields`` table. The merged window sketches
  of candidate ends are kept for the ``window_sketch`` calls of the same pass; any other end is
  read on demand.
- :class:`MetadataProfileSource` is the degraded source for an install whose profiler package is
  missing or broken: profiles from the ``fields`` table (one per form the edge reported, with
  the profiler's statistics when it ever ran) and no window sketches, so LSH finds nothing and
  only bridge pairs (co-occurrence, spec 9.4) are proposed. :func:`profile_source_for` picks and
  logs which one a worker got.
- :func:`profiler_seams` resolves the three estimators the pass can take from the profiler
  (bottom-k containment at the configured ``k``, the shape domain size, the MinHash decoder)
  and falls back to the linker's own defaults when the profiler package cannot be imported,
  again logging which.

Nothing here reads a token: sketches hold hashes, profiles hold counts and shapes.
"""

from __future__ import annotations

import functools
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Final

from carto_common.logging import get_logger
from carto_core.linker.candidates import (
    ContainmentFn,
    MinHashDecoder,
    default_containment,
    default_minhash_decoder,
)
from carto_core.linker.verify import DomainSizeFn, default_domain_size
from carto_core.metadata import MAX_PAGE
from carto_core.profiler.model import FieldProfile, ShapeShare, WindowSketch
from carto_core.store.model import FieldForm

if TYPE_CHECKING:
    from carto_core.metadata import MetadataStore
    from carto_core.profiler.repository import SketchRepository
    from carto_core.settings import ProfilerSettings
    from carto_core.store.model import EventStore, Window
    from carto_core.worker import WorkerContext

__all__ = [
    "MetadataProfileSource",
    "ProfilerSeams",
    "SketchProfileSource",
    "profile_source_for",
    "profiler_seams",
]

log = get_logger(component="linker")

PROFILER_SOURCE: Final = "profiler"
LINKER_DEFAULTS: Final = "linker defaults"


@dataclass(frozen=True, slots=True)
class ProfilerSeams:
    """The estimators a pass runs with and where they came from (``profiler`` or
    ``linker defaults``)."""

    containment: ContainmentFn
    domain_size: DomainSizeFn
    decode_minhash: MinHashDecoder
    source: str


def profiler_seams(settings: ProfilerSettings) -> ProfilerSeams:
    """The profiler's estimators when its package imports, else the linker's defaults."""
    try:
        from carto_core.profiler.shapes import domain_size  # noqa: PLC0415 - package B
        from carto_core.profiler.sketches import (  # noqa: PLC0415
            containment_estimate,
            deserialize_minhash,
        )
    except ImportError as exc:
        log.warning("profiler estimators unavailable; linker defaults in use", error=str(exc))
        return ProfilerSeams(
            default_containment, default_domain_size, default_minhash_decoder, LINKER_DEFAULTS
        )
    return ProfilerSeams(
        functools.partial(containment_estimate, k=settings.bottom_k),
        domain_size,
        deserialize_minhash,
        PROFILER_SOURCE,
    )


def _template_of(end: FieldForm) -> tuple[str, str]:
    system, _sep, rest = end.field_ref.partition("/")
    template, _sep, _path = rest.partition("/")
    return system, template


class SketchProfileSource:
    """Per-end profiles and window sketches from the profiler's ``field_sketches`` (module
    docstring). Raises ``ImportError`` at construction when the profiler package is missing."""

    __slots__ = (
        "_build_profile",
        "_clock",
        "_days",
        "_events",
        "_merge_window",
        "_profile_window",
        "_settings",
        "_sketch_compatible",
        "_sketches",
        "_window_dates",
        "_windows",
    )

    def __init__(
        self,
        sketches: SketchRepository,
        events: EventStore,
        settings: ProfilerSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from carto_core.profiler.quality import build_profile  # noqa: PLC0415 - package B
        from carto_core.profiler.run import profile_window, window_dates  # noqa: PLC0415
        from carto_core.profiler.sketches import merge_window, sketch_compatible  # noqa: PLC0415

        self._sketches = sketches
        self._events = events
        self._settings = settings
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._build_profile = build_profile
        self._profile_window = profile_window
        self._window_dates = window_dates
        self._merge_window = merge_window
        self._sketch_compatible = sketch_compatible
        self._days: dict[str, tuple[date, ...]] = {}
        self._windows: dict[tuple[str, FieldForm], WindowSketch] = {}

    def _window(self) -> tuple[Window, tuple[date, ...]]:
        window = self._profile_window(self._clock(), self._settings)
        return window, self._window_dates(window)

    def profiles(self, tenant_id: str) -> Sequence[FieldProfile]:
        window, days = self._window()
        self._days[tenant_id] = days
        for key in [key for key in self._windows if key[0] == tenant_id]:
            del self._windows[key]
        template_events: dict[tuple[str, str], int] = defaultdict(int)
        for count in self._events.template_counts(tenant_id, window):
            template_events[(count.system_id, count.template_id)] += count.events
        profiles: list[FieldProfile] = []
        for end, sketches in self._sketches.window_days(tenant_id, days):
            usable = [s for s in sketches if self._sketch_compatible(s, self._settings)]
            if not usable:
                continue
            merged = self._merge_window(usable, settings=self._settings)
            profile = self._build_profile(
                merged, template_events.get(_template_of(end), 0), self._settings
            )
            if profile.is_candidate:
                self._windows[(tenant_id, end)] = merged
            profiles.append(profile)
        log.info(
            "profiles read from sketches",
            tenant_id=tenant_id,
            ends=len(profiles),
            candidates=len([p for p in profiles if p.is_candidate]),
        )
        return profiles

    def window_sketch(self, tenant_id: str, end: FieldForm) -> WindowSketch | None:
        cached = self._windows.get((tenant_id, end))
        if cached is not None:
            return cached
        days = self._days.get(tenant_id)
        if days is None:
            _window, days = self._window()
        for _end, sketches in self._sketches.window_days(tenant_id, days, ends=(end,)):
            usable = [s for s in sketches if self._sketch_compatible(s, self._settings)]
            if usable:
                return self._merge_window(usable, settings=self._settings)
        return None


class MetadataProfileSource:
    """Profiles from the ``fields`` table and no sketches (module docstring): the source a
    worker runs with when the profiler package is unavailable."""

    __slots__ = ("_metadata",)

    def __init__(self, metadata: MetadataStore) -> None:
        self._metadata = metadata

    def profiles(self, tenant_id: str) -> Sequence[FieldProfile]:
        profiles: list[FieldProfile] = []
        after: str | None = None
        while True:
            rows = self._metadata.list_fields(tenant_id, after=after, limit=MAX_PAGE)
            for row in rows:
                shapes = tuple(ShapeShare(shape, share) for shape, share in row.shapes)
                profiles.extend(
                    FieldProfile(
                        end=FieldForm(row.field_ref, form),
                        count=row.count,
                        distinct=row.distinct_estimate,
                        null_rate=row.null_rate,
                        shapes=shapes,
                        length_stats=None,
                        top_share=0.0,
                        quality=row.quality if row.quality is not None else 0.0,
                        is_candidate=row.is_candidate,
                        first_seen=row.first_seen,
                        last_seen=row.last_seen,
                    )
                    for form in row.forms or ("raw",)
                )
            if len(rows) < MAX_PAGE:
                return profiles
            after = rows[-1].field_ref

    def window_sketch(self, tenant_id: str, end: FieldForm) -> WindowSketch | None:
        del tenant_id, end
        return None


def profile_source_for(context: WorkerContext) -> SketchProfileSource | MetadataProfileSource:
    """The source a worker's link pass reads: the profiler's sketches when the package imports,
    else the ``fields`` table, logged either way."""
    try:
        from carto_core.profiler.repository import PostgresSketchRepository  # noqa: PLC0415

        source = SketchProfileSource(
            PostgresSketchRepository(context.engine),
            context.events,
            context.settings.profiler,
            clock=context.clock,
        )
    except ImportError as exc:
        log.warning(
            "profiler package unavailable; link pass reads profiles from the fields table "
            "and proposes bridge pairs only",
            error=str(exc),
        )
        return MetadataProfileSource(context.metadata)
    log.info("link pass reads profiles from field_sketches")
    return source
