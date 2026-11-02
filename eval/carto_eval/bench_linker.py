"""``carto-eval bench-linker`` / ``make bench-linker``: the spec 17 scale, measured and reported,
not gating (plan M2 "Acceptance"; ADR 0048).

The benchmark builds a synthetic install of ``systems x fields`` candidate fields (default 50 x
20 = 1,000) with real day sketches (:class:`~carto_core.profiler.sketches.DaySketchBuilder`
over synthetic identifier rows, merged into window sketches), plants a chain of overlapping
fields across neighbouring systems so LSH has pairs to find, and times:

1. **candidate generation**: :class:`~carto_core.linker.candidates.CandidateGenerator` over
   the 1,000 window sketches (the MinHash LSH Ensemble index and queries), with an empty store
   so no bridge pairs join;
2. **verification**, capped: the first ``verify_cap`` candidates by estimated containment,
   each one :func:`~carto_core.linker.verify.verify` call against an
   :class:`~carto_core.store.memory.InMemoryEventStore` holding the events of the fields those
   candidates touch (``tokens`` events per field).

It prints the counts, both times and the per-pair verification cost extrapolated to the
``max_candidates_per_run`` cap. The in-memory store answers a join in Python over its rows, so
the second figure bounds the engine's own work per pair, not ClickHouse's; the first figure is
the LSH cost the deployment pays as it is. Nothing here reads data: the tokens are synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from carto_common.ids import derive_ulid
from carto_core.linker.candidates import CandidateGenerator, CandidateSet
from carto_core.linker.profile_source import profiler_seams
from carto_core.linker.verify import verify
from carto_core.profiler.model import FieldProfile, WindowSketch
from carto_core.profiler.quality import build_profile
from carto_core.profiler.sketches import DaySketchBuilder, merge_window
from carto_core.settings import CoreSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import FieldForm, IdentifierRow, Window
from carto_schema.event import CanonicalEvent

__all__ = ["BenchResult", "SyntheticInstall", "build_install", "run_bench"]

DAY: Final = date(2026, 10, 12)
"""A Monday; the window ends a week later."""
AS_OF: Final = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)
OVERLAP_SHARE: Final = 0.6
"""Share of a field's tokens shared with its partner in the next system."""
TEMPLATE: Final = "tpl_bench"
BASE_MS: Final = 1_790_000_000_000


@dataclass(frozen=True, slots=True)
class SyntheticInstall:
    profiles: tuple[FieldProfile, ...]
    sketches: dict[FieldForm, WindowSketch]
    tokens: dict[FieldForm, tuple[str, ...]]
    build_seconds: float


@dataclass(frozen=True, slots=True)
class BenchResult:
    systems: int
    fields: int
    tokens_per_field: int
    planted_pairs: int
    candidates: int
    lsh_pairs: int
    truncated: int
    build_seconds: float
    candidate_seconds: float
    verified: int
    verify_seconds: float
    store_events: int
    cap: int

    @property
    def seconds_per_pair(self) -> float | None:
        return self.verify_seconds / self.verified if self.verified else None

    def lines(self) -> list[str]:
        per_pair = self.seconds_per_pair
        extrapolated = (
            f"{per_pair * self.cap / 60:.1f} min for the {self.cap:,} pair cap"
            if per_pair is not None
            else "n/a"
        )
        return [
            (
                f"bench-linker: {self.systems} systems x {self.fields // self.systems} fields = "
                f"{self.fields:,} candidate fields, {self.tokens_per_field} tokens each, "
                f"{self.planted_pairs:,} planted overlaps"
            ),
            f"  sketches    built in {self.build_seconds:.1f} s",
            (
                f"  candidates  {self.candidates:,} ({self.lsh_pairs:,} LSH pairs, "
                f"{self.truncated:,} beyond the cap) in {self.candidate_seconds:.1f} s"
            ),
            (
                f"  verify      {self.verified:,} pairs in {self.verify_seconds:.1f} s against "
                f"{self.store_events:,} in-memory events"
                + (f" ({per_pair * 1000:.1f} ms per pair; {extrapolated})" if per_pair else "")
            ),
            "  measured, not gating (spec 17 budgets 30 min for a full pass; ADR 0048)",
        ]


def _token(value: str) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return f"t1.{body[:22]}"


def _field(system: int, index: int) -> FieldForm:
    return FieldForm(f"sys_{system:02d}/{TEMPLATE}/field_{index:02d}", "raw")


def _rows(end: FieldForm, tokens: Sequence[str], day: date) -> Iterator[IdentifierRow]:
    base = datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC)
    for offset, token in enumerate(tokens):
        at = base + timedelta(seconds=offset)
        yield IdentifierRow(
            token=token,
            field_ref=end.field_ref,
            form=end.form,
            shape="A9999999",
            event_id=derive_ulid(BASE_MS, end.field_ref, str(offset)),
            system_id=end.system_id,
            observed_at=at,
            stored_at=at,
        )


def build_install(
    *,
    systems: int,
    fields_per_system: int,
    tokens_per_field: int,
    seed: int,
    settings: CoreSettings,
) -> SyntheticInstall:
    """Fields, their tokens, one day sketch each (merged into a window sketch) and profiles.
    Field ``i`` of system ``s + 1`` shares :data:`OVERLAP_SHARE` of its tokens with field ``i``
    of system ``s``; the rest are its own."""
    started = time.perf_counter()
    rng = random.Random(seed)  # noqa: S311 - synthetic benchmark data, not security
    tokens: dict[FieldForm, tuple[str, ...]] = {}
    for system in range(systems):
        for index in range(fields_per_system):
            end = _field(system, index)
            own = [_token(f"{end.field_ref}:{n}:{rng.random()}") for n in range(tokens_per_field)]
            if system > 0:
                previous = tokens[_field(system - 1, index)]
                shared = int(OVERLAP_SHARE * tokens_per_field)
                own[:shared] = previous[:shared]
            tokens[end] = tuple(own)
    sketches: dict[FieldForm, WindowSketch] = {}
    profiles: list[FieldProfile] = []
    for end, values in tokens.items():
        builder = DaySketchBuilder(end, DAY, settings.profiler)
        builder.add_all(_rows(end, values, DAY))
        window = merge_window([builder.build()], settings=settings.profiler)
        sketches[end] = window
        profiles.append(build_profile(window, len(values), settings.profiler))
    return SyntheticInstall(
        profiles=tuple(profiles),
        sketches=sketches,
        tokens=tokens,
        build_seconds=time.perf_counter() - started,
    )


def _event(end: FieldForm, token: str, offset: int) -> CanonicalEvent:
    at = datetime(DAY.year, DAY.month, DAY.day, 9, 0, tzinfo=UTC) + timedelta(seconds=offset)
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(BASE_MS, end.field_ref, str(offset)),
        "system_id": end.system_id,
        "source_id": f"src_{end.system_id.removeprefix('sys_')}",
        "template_id": TEMPLATE,
        "observed_at": at,
        "ingested_at": at,
        "attributes": {},
        "identifiers": [
            {
                "field": end.field_ref.rsplit("/", 1)[-1],
                "form": end.form,
                "token": token,
                "shape": "A9999999",
                "len": 8,
            }
        ],
    }
    return CanonicalEvent.model_validate(data)


def _populate(
    store: InMemoryEventStore, install: SyntheticInstall, ends: Sequence[FieldForm]
) -> int:
    added = 0
    for end in ends:
        added += store.add_events(
            (_event(end, token, offset) for offset, token in enumerate(install.tokens[end])),
            stored_at=AS_OF,
        )
    return added


def run_bench(
    *,
    systems: int = 50,
    fields_per_system: int = 20,
    tokens_per_field: int = 150,
    verify_cap: int = 100,
    seed: int = 1,
    settings: CoreSettings | None = None,
) -> BenchResult:
    """Build, time candidate generation, time capped verification (module docstring)."""
    core = settings if settings is not None else CoreSettings(_env_file=None)
    install = build_install(
        systems=systems,
        fields_per_system=fields_per_system,
        tokens_per_field=tokens_per_field,
        seed=seed,
        settings=core,
    )
    seams = profiler_seams(core.profiler)
    generator = CandidateGenerator(
        core.linker,
        core.profiler,
        containment=seams.containment,
        decode_minhash=seams.decode_minhash,
    )
    window = Window(start=AS_OF - timedelta(days=core.profiler.window_days), end=AS_OF)
    empty = InMemoryEventStore()
    started = time.perf_counter()
    candidates: CandidateSet = generator.generate(
        "default",
        store=empty,
        window=window,
        profiles=install.profiles,
        sketch_of=install.sketches.get,
    )
    candidate_seconds = time.perf_counter() - started
    chosen = list(candidates.candidates[:verify_cap])
    touched = sorted({end for candidate in chosen for end in (candidate.a, candidate.b)})
    store = InMemoryEventStore()
    events = _populate(store, install, touched)
    index = {profile.end: profile for profile in install.profiles}
    started = time.perf_counter()
    for candidate in chosen:
        verify(
            store, "default", candidate, window, domain_size_fn=seams.domain_size, profiles=index
        )
    verify_seconds = time.perf_counter() - started
    return BenchResult(
        systems=systems,
        fields=systems * fields_per_system,
        tokens_per_field=tokens_per_field,
        planted_pairs=(systems - 1) * fields_per_system,
        candidates=len(candidates.candidates),
        lsh_pairs=candidates.lsh_pairs,
        truncated=candidates.truncated,
        build_seconds=install.build_seconds,
        candidate_seconds=candidate_seconds,
        verified=len(chosen),
        verify_seconds=verify_seconds,
        store_events=events,
        cap=core.linker.max_candidates_per_run,
    )
