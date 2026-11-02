"""Blocking for composite matching (spec 9.5 step 2; plan M2 package D).

The matcher reads the events of the earlier system A and the later system B over the window
through :meth:`~carto_core.store.model.EventStore.events_with_keys`, restricted to the forms
composite matching compares (:data:`COMPOSITE_FORMS`: ``date``, ``amount``, every
``phonetic.<k>``, every ``digits.<k>`` and ``alnum``, spec 8.4). A pair ``(a, b)`` is a
candidate when the two events share a ``date`` token or an ``amount`` token and ``b`` was
observed within ``composite_window_business_days`` business days after ``a``
(:func:`business_days_between`: Monday to Friday in UTC until M4 adds calendars). Blocking stops
at ``composite_max_pairs`` and reports the cap; the count is the only thing logged.

Everything kept per event is a token, a form, a field ref, a template id, a timestamp or a
low-cardinality attribute value; nothing here is written anywhere (spec 2.3 invariant 2).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Final

from carto_schema.forms import DIGITS_MAX_K, PHONETIC_MAX_K

if TYPE_CHECKING:
    from carto_core.store.model import EventKeys

__all__ = [
    "AMOUNT_FORM",
    "COMPOSITE_FORMS",
    "DATE_FORM",
    "PARTIAL_FORMS",
    "PHONETIC_FORMS",
    "SECONDS_PER_DAY",
    "BlockedPairs",
    "Side",
    "SideEvent",
    "block_pairs",
    "business_days_between",
    "load_side",
    "within_business_days",
]

DATE_FORM: Final = "date"
AMOUNT_FORM: Final = "amount"
PHONETIC_FORMS: Final[frozenset[str]] = frozenset(
    f"phonetic.{k}" for k in range(PHONETIC_MAX_K + 1)
)
PARTIAL_FORMS: Final[frozenset[str]] = frozenset(
    {"alnum", *(f"digits.{k}" for k in range(DIGITS_MAX_K + 1))}
)
"""Identifier forms that can overlap partially (spec 9.5 component 5): the digit runs and the
letters-and-digits form share the ``id`` token domain with ``raw`` (spec 8.4)."""
COMPOSITE_FORMS: Final[frozenset[str]] = frozenset(
    {DATE_FORM, AMOUNT_FORM, *PHONETIC_FORMS, *PARTIAL_FORMS}
)
SECONDS_PER_DAY: Final = 86_400


def _weekdays_before(day: date) -> int:
    """Weekdays (Monday to Friday) with an ordinal strictly below ``day``'s. Ordinal 1 is a
    Monday, so the count is whole weeks times five plus the weekdays of the partial week."""
    full_weeks, remainder = divmod(day.toordinal() - 1, 7)
    return full_weeks * 5 + min(remainder, 5)


def business_days_between(start: datetime, end: datetime) -> int | None:
    """The number of business days in ``(start, end]`` by UTC calendar date: the weekdays
    strictly after ``start``'s date up to and including ``end``'s. Zero when both fall on the
    same date or ``end`` falls on the weekend right after ``start``; None when ``end`` precedes
    ``start``."""
    if end < start:
        return None
    first = start.astimezone(UTC).date()
    last = end.astimezone(UTC).date()
    one_day = timedelta(days=1)
    return _weekdays_before(last + one_day) - _weekdays_before(first + one_day)


def within_business_days(start: datetime, end: datetime, days: int) -> bool:
    """Whether ``end`` is at or after ``start`` and at most ``days`` business days later."""
    between = business_days_between(start, end)
    return between is not None and between <= days


@dataclass(frozen=True, slots=True)
class SideEvent:
    """One event of one side with the keys composite matching compares."""

    event_id: str
    template_id: str
    observed_at: datetime
    dates: Mapping[str, str]
    """``field_ref -> token`` of the ``date`` form."""
    amounts: Mapping[str, str]
    """``field_ref -> token`` of the ``amount`` form."""
    names: Mapping[str, frozenset[str]]
    """``field_ref -> every phonetic.<k> token`` of a name-class field."""
    partials: Mapping[tuple[str, str], str]
    """``(field_ref, form) -> token`` over :data:`PARTIAL_FORMS`."""
    attributes: Mapping[str, str]


@dataclass(slots=True)
class Side:
    """The events of one system in the window, indexed for blocking."""

    system_id: str
    events: list[SideEvent] = field(default_factory=list)
    by_date: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    by_amount: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))

    def add(self, event: SideEvent) -> None:
        index = len(self.events)
        self.events.append(event)
        for token in set(event.dates.values()):
            self.by_date[token].append(index)
        for token in set(event.amounts.values()):
            self.by_amount[token].append(index)

    def __len__(self) -> int:
        return len(self.events)


def side_event(keys: EventKeys) -> SideEvent:
    dates: dict[str, str] = {}
    amounts: dict[str, str] = {}
    names: dict[str, set[str]] = defaultdict(set)
    partials: dict[tuple[str, str], str] = {}
    for field_ref, form, token in keys.identifiers:
        if form == DATE_FORM:
            dates[field_ref] = token
        elif form == AMOUNT_FORM:
            amounts[field_ref] = token
        elif form in PHONETIC_FORMS:
            names[field_ref].add(token)
        elif form in PARTIAL_FORMS:
            partials[(field_ref, form)] = token
    return SideEvent(
        event_id=keys.event_id,
        template_id=keys.template_id,
        observed_at=keys.observed_at,
        dates=dates,
        amounts=amounts,
        names={ref: frozenset(tokens) for ref, tokens in names.items()},
        partials=partials,
        attributes=dict(keys.attributes),
    )


def load_side(system_id: str, events: Iterable[EventKeys]) -> Side:
    """Index a stream of :class:`~carto_core.store.model.EventKeys` for blocking."""
    side = Side(system_id=system_id)
    for keys in events:
        side.add(side_event(keys))
    return side


@dataclass(frozen=True, slots=True)
class BlockedPairs:
    """The candidate pairs of one system pair as indexes into the two sides."""

    pairs: tuple[tuple[int, int], ...]
    capped: bool
    """True when blocking stopped at the cap; ``len(pairs)`` is then the cap."""


def block_pairs(a: Side, b: Side, *, window_business_days: int, max_pairs: int) -> BlockedPairs:
    """Spec 9.5 step 2: every ``(a, b)`` sharing a ``date`` or ``amount`` token with ``b``
    observed within ``window_business_days`` business days after ``a``, up to ``max_pairs``.
    The pairs are ordered by B event, then A event, so a capped run is deterministic."""
    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[int, int]] = []
    for j, event_b in enumerate(b.events):
        candidates: set[int] = set()
        for token in set(event_b.dates.values()):
            candidates.update(a.by_date.get(token, ()))
        for token in set(event_b.amounts.values()):
            candidates.update(a.by_amount.get(token, ()))
        for i in sorted(candidates):
            if (i, j) in seen:
                continue
            if not within_business_days(
                a.events[i].observed_at, event_b.observed_at, window_business_days
            ):
                continue
            seen.add((i, j))
            pairs.append((i, j))
            if len(pairs) >= max_pairs:
                return BlockedPairs(pairs=tuple(pairs), capped=True)
    return BlockedPairs(pairs=tuple(pairs), capped=False)
