"""The in-memory :class:`~carto_core.store.model.EventStore`: the reference semantics and the
engine's unit-test double (plan M2 decision 1). It is not a deployment option; ClickHouse is the
only store a service runs against (founder decision 3). Everything here is exact and
deliberately simple, so the ClickHouse queries are checked against it by the contract tests.

Events are added as :class:`~carto_schema.event.CanonicalEvent` objects with the time they
were stored. Adding an event a second time keeps the first copy's ``stored_at`` and ignores the
rest, which is what the deduplication rule of the Protocol says a redelivered batch looks like.
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from carto_core.store.model import (
    CooccurrenceRow,
    EventKeys,
    FieldForm,
    HourlyVolume,
    IdentifierRow,
    PairJoin,
    TemplateCount,
    Window,
)
from carto_schema.event import CanonicalEvent

__all__ = ["CONCENTRATION_TOP_N", "InMemoryEventStore", "quantile_exact_low"]

CONCENTRATION_TOP_N = 10
"""Spec 9.3 ``concentration``: the share of matches from the top 10 tokens."""


def quantile_exact_low(values: Sequence[float], level: float) -> float | None:
    """The element at index ``floor(level * (n - 1))`` of the sorted values; ``None`` if empty.
    The ClickHouse store indexes a sorted ``groupArray`` the same way, so both implementations
    agree exactly (ClickHouse's own ``quantileExactLow`` picks another element for small arrays
    at levels like 0.99)."""
    if not values:
        return None
    ordered = sorted(values)
    index = math.floor(level * (len(ordered) - 1))
    return float(ordered[index])


def _field_ref(system_id: str, template_id: str, path: str) -> str:
    return f"{system_id}/{template_id}/{path}"


@dataclass(frozen=True, slots=True)
class _Stored:
    event: CanonicalEvent
    stored_at: datetime


def _hour(at: datetime) -> datetime:
    return at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


class InMemoryEventStore:
    """See the module docstring. Thread-safe for the engine's worker and the tests."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], _Stored] = {}

    # -- loading ------------------------------------------------------------------------------

    def add_events(
        self, events: Iterable[CanonicalEvent], stored_at: datetime | None = None
    ) -> int:
        """Store the events; return how many were new (a repeated ``event_id`` is ignored)."""
        at = stored_at if stored_at is not None else datetime.now(UTC)
        if at.tzinfo is None:
            msg = "stored_at must be timezone-aware"
            raise ValueError(msg)
        added = 0
        with self._lock:
            for event in events:
                key = (event.tenant_id, event.event_id)
                if key in self._events:
                    continue
                self._events[key] = _Stored(event, at)
                added += 1
        return added

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)

    def _in_window(self, tenant_id: str, window: Window) -> list[_Stored]:
        with self._lock:
            return [
                item
                for (tenant, _event_id), item in self._events.items()
                if tenant == tenant_id and window.contains(item.event.observed_at)
            ]

    # -- EventStore ---------------------------------------------------------------------------

    def ping(self) -> bool:
        return True

    def max_stored_at(self, tenant_id: str) -> datetime | None:
        with self._lock:
            times = [
                item.stored_at
                for (tenant, _event_id), item in self._events.items()
                if tenant == tenant_id and item.event.identifiers
            ]
        return max(times) if times else None

    def identifier_rows(
        self,
        tenant_id: str,
        window: Window,
        *,
        stored_after: datetime | None = None,
        forms: frozenset[str] | None = None,
    ) -> Iterator[IdentifierRow]:
        for item in self._in_window(tenant_id, window):
            if stored_after is not None and item.stored_at <= stored_after:
                continue
            event = item.event
            seen: set[tuple[str, str]] = set()
            for identifier in event.identifiers:
                if forms is not None and identifier.form not in forms:
                    continue
                ref = _field_ref(event.system_id, event.template_id, identifier.field)
                if (ref, identifier.form) in seen:
                    continue
                seen.add((ref, identifier.form))
                yield IdentifierRow(
                    token=identifier.token,
                    field_ref=ref,
                    form=identifier.form,
                    shape=identifier.shape,
                    event_id=event.event_id,
                    system_id=event.system_id,
                    observed_at=event.observed_at,
                    stored_at=item.stored_at,
                )

    def template_counts(self, tenant_id: str, window: Window) -> list[TemplateCount]:
        counts: dict[tuple[str, str, datetime], int] = defaultdict(int)
        for item in self._in_window(tenant_id, window):
            event = item.event
            day = event.observed_at.astimezone(UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            counts[(event.system_id, event.template_id, day)] += 1
        return [
            TemplateCount(system_id=system, template_id=template, day=day.date(), events=n)
            for (system, template, day), n in sorted(counts.items())
        ]

    def _end_rows(
        self, tenant_id: str, end: FieldForm, window: Window
    ) -> dict[str, tuple[datetime, int]]:
        """Per token of one end: earliest ``observed_at`` and distinct event count."""
        per_token: dict[str, tuple[datetime, int]] = {}
        for row in self.identifier_rows(tenant_id, window, forms=frozenset({end.form})):
            if row.field_ref != end.field_ref:
                continue
            first, count = per_token.get(row.token, (row.observed_at, 0))
            per_token[row.token] = (min(first, row.observed_at), count + 1)
        return per_token

    def pair_join(self, tenant_id: str, a: FieldForm, b: FieldForm, window: Window) -> PairJoin:
        rows_a = self._end_rows(tenant_id, a, window)
        rows_b = self._end_rows(tenant_id, b, window)
        shared = sorted(set(rows_a) & set(rows_b))
        if not shared:
            return PairJoin(
                n_a=len(rows_a),
                n_b=len(rows_b),
                n_match=0,
                lag_median_seconds=None,
                lag_iqr_seconds=None,
                lag_positive_share=None,
                fanout_ab_median=None,
                fanout_ab_p99=None,
                fanout_ba_median=None,
                fanout_ba_p99=None,
                concentration=None,
            )
        lags = [(rows_b[t][0] - rows_a[t][0]) / timedelta(seconds=1) for t in shared]
        fan_ab = [float(rows_b[t][1]) for t in shared]
        fan_ba = [float(rows_a[t][1]) for t in shared]
        products = sorted((rows_a[t][1] * rows_b[t][1] for t in shared), reverse=True)
        total = sum(products)
        q25 = quantile_exact_low(lags, 0.25)
        q75 = quantile_exact_low(lags, 0.75)
        return PairJoin(
            n_a=len(rows_a),
            n_b=len(rows_b),
            n_match=len(shared),
            lag_median_seconds=quantile_exact_low(lags, 0.5),
            lag_iqr_seconds=(q75 - q25) if q25 is not None and q75 is not None else None,
            lag_positive_share=sum(1 for lag in lags if lag > 0) / len(lags),
            fanout_ab_median=quantile_exact_low(fan_ab, 0.5),
            fanout_ab_p99=quantile_exact_low(fan_ab, 0.99),
            fanout_ba_median=quantile_exact_low(fan_ba, 0.5),
            fanout_ba_p99=quantile_exact_low(fan_ba, 0.99),
            concentration=sum(products[:CONCENTRATION_TOP_N]) / total if total else None,
        )

    def cooccurrences(
        self, tenant_id: str, window: Window, *, min_events: int = 1
    ) -> list[CooccurrenceRow]:
        events: dict[tuple[FieldForm, FieldForm], int] = defaultdict(int)
        partners_a: dict[tuple[FieldForm, FieldForm], dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        partners_b: dict[tuple[FieldForm, FieldForm], dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        systems: dict[tuple[FieldForm, FieldForm], str] = {}
        for item in self._in_window(tenant_id, window):
            event = item.event
            ends: dict[FieldForm, str] = {}
            for identifier in event.identifiers:
                end = FieldForm(
                    _field_ref(event.system_id, event.template_id, identifier.field),
                    identifier.form,
                )
                ends.setdefault(end, identifier.token)
            ordered = sorted(ends)
            for index, first in enumerate(ordered):
                for second in ordered[index + 1 :]:
                    key = (first, second)
                    events[key] += 1
                    systems[key] = event.system_id
                    partners_a[key][ends[first]].add(ends[second])
                    partners_b[key][ends[second]].add(ends[first])
        rows: list[CooccurrenceRow] = []
        for key, n in sorted(events.items()):
            if n < min_events:
                continue
            by_a = partners_a[key]
            by_b = partners_b[key]
            rows.append(
                CooccurrenceRow(
                    a=key[0],
                    b=key[1],
                    system_id=systems[key],
                    events=n,
                    distinct_a=len(by_a),
                    single_b_a=sum(1 for partners in by_a.values() if len(partners) == 1),
                    distinct_b=len(by_b),
                    single_a_b=sum(1 for partners in by_b.values() if len(partners) == 1),
                )
            )
        return rows

    def hourly_volumes(self, tenant_id: str, window: Window) -> list[HourlyVolume]:
        counts: dict[tuple[str, datetime], int] = defaultdict(int)
        for item in self._in_window(tenant_id, window):
            counts[(item.event.system_id, _hour(item.event.observed_at))] += 1
        return [
            HourlyVolume(system_id=system, hour=hour, events=n)
            for (system, hour), n in sorted(counts.items())
        ]

    def events_with_keys(
        self,
        tenant_id: str,
        system_id: str,
        window: Window,
        *,
        forms: frozenset[str] | None = None,
    ) -> Iterator[EventKeys]:
        for item in self._in_window(tenant_id, window):
            event = item.event
            if event.system_id != system_id:
                continue
            triples: list[tuple[str, str, str]] = []
            seen: set[tuple[str, str]] = set()
            for identifier in event.identifiers:
                if forms is not None and identifier.form not in forms:
                    continue
                ref = _field_ref(event.system_id, event.template_id, identifier.field)
                if (ref, identifier.form) in seen:
                    continue
                seen.add((ref, identifier.form))
                triples.append((ref, identifier.form, identifier.token))
            yield EventKeys(
                event_id=event.event_id,
                system_id=event.system_id,
                template_id=event.template_id,
                observed_at=event.observed_at,
                attributes=dict(event.attributes),
                identifiers=tuple(triples),
            )
