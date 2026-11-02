"""carto-core profiler (spec 5.3, 9.1): day sketches per ``(field_ref, form)``, the window
merge, ``q(field)`` and the profile pass the worker and ``discover`` run (ADR 0047, 0055).

- :mod:`.model`: the data shapes (wave 0).
- :mod:`.sketches`: building, merging and serializing sketches; containment and distinct
  estimates.
- :mod:`.quality`: ``q(field)`` and :func:`build_profile`.
- :mod:`.shapes`: :func:`domain_size` for the chance ratio of spec 9.3.
- :mod:`.repository`: the Protocols the pass writes through and their in-memory and PostgreSQL
  implementations.
- :mod:`.run`: :class:`ProfilePass`.
"""

from __future__ import annotations

from carto_core.profiler.model import DaySketch, FieldProfile, LengthStats, ShapeShare, WindowSketch
from carto_core.profiler.quality import build_profile, identifier_quality, is_candidate
from carto_core.profiler.repository import (
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
)
from carto_core.profiler.run import ProfilePass, ProfileReport, profile_window, window_dates
from carto_core.profiler.shapes import domain_size
from carto_core.profiler.sketches import (
    DaySketchBuilder,
    containment_estimate,
    distinct_estimate,
    jaccard_estimate,
    merge_day,
    merge_window,
    token_hash,
)

__all__ = [
    "DaySketch",
    "DaySketchBuilder",
    "FieldProfile",
    "FieldProfileSink",
    "InMemoryFieldProfileSink",
    "InMemorySketchRepository",
    "InMemoryWatermarkStore",
    "LengthStats",
    "MetadataFieldProfileSink",
    "PostgresSketchRepository",
    "PostgresWatermarkStore",
    "ProfilePass",
    "ProfileReport",
    "RepositoryError",
    "ShapeShare",
    "SketchRepository",
    "WatermarkStore",
    "WindowSketch",
    "build_profile",
    "containment_estimate",
    "distinct_estimate",
    "domain_size",
    "identifier_quality",
    "is_candidate",
    "jaccard_estimate",
    "merge_day",
    "merge_window",
    "profile_window",
    "token_hash",
    "window_dates",
]
