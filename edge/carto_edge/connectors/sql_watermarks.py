"""Typed SQL watermarks and the page rules that advance them (ADR 0035 items 3 to 5).

A watermark has one of four types, set per query with ``watermark_type``:

- ``datetime`` (``timestamp``, ``timestamptz``, ``datetime``, ``datetime2``): a naive
  :class:`~datetime.datetime` in the query's ``watermark_timezone`` (default UTC). Values the
  driver returns with an offset are converted to that zone.
- ``date``: a :class:`~datetime.date`.
- ``int``: an :class:`int` (an integral :class:`~decimal.Decimal` is accepted).
- ``decimal``: a :class:`~decimal.Decimal` (an :class:`int` is accepted).

In the cursor a watermark is stored as ``{"t": <type>, "v": <ISO text or digits>}``, so a
restart decodes exactly the type it encoded (a Decimal no longer comes back as a string, a date
no longer as a datetime). The cursor written by the first M1 build (an untyped ISO string or a
JSON number) is still read, as the configured type.

:func:`plan_page` turns one page of rows into what the connector does next:

- A page shorter than ``:batch`` holds every row up to ``:upper`` (time types) or every row left
  (``int``/``decimal``): all rows are emitted, and the cursor moves to ``:upper``, or to the
  largest value on the page, even when the page is empty.
- A full page: the cursor stops at the largest value strictly below the page's maximum, the
  rows up to it are emitted, and the trailing tie group is read again by the next page.
- A full page that is a single tie group is re-run with a doubled ``:batch`` (up to 16 times the
  configured batch), then fails with a :class:`WatermarkError` naming the query.

Every value on a page must be of the configured type and the page must come back in ascending
watermark order (the template's ``ORDER BY``); otherwise the cursor could skip rows, so the read
fails with a :class:`WatermarkError` instead of warning. Messages name the query, the column and
the types, never a value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from carto_edge.connectors.base import ConnectorError

__all__ = [
    "MAX_BATCH_FACTOR",
    "TIME_TYPES",
    "PagePlan",
    "Watermark",
    "WatermarkError",
    "WatermarkType",
    "coerce",
    "decode",
    "encode",
    "initial",
    "plan_page",
    "upper_bound",
]

WatermarkType = Literal["datetime", "date", "int", "decimal"]
Watermark = datetime | date | int | Decimal
TIME_TYPES: Final = frozenset({"datetime", "date"})
MAX_BATCH_FACTOR: Final = 16
EPOCH: Final = datetime(1970, 1, 1)  # noqa: DTZ001 - naive in the query's watermark zone
FIRST_DATE: Final = date(1, 1, 1)


class WatermarkError(ConnectorError):
    """A watermark that cannot be compared, decoded or advanced safely."""


def initial(kind: WatermarkType, since: datetime | None, zone: ZoneInfo) -> Watermark:
    """The watermark of a query with no cursor: the epoch, ``0001-01-01`` or ``0``; for time
    types ``since`` (the source's ``backfill_days`` before now) when given."""
    if kind == "datetime":
        return EPOCH if since is None else since.astimezone(zone).replace(tzinfo=None)
    if kind == "date":
        return FIRST_DATE if since is None else since.astimezone(zone).date()
    if kind == "int":
        return 0
    return Decimal(0)


def upper_bound(kind: WatermarkType, clock: datetime, zone: ZoneInfo) -> Watermark:
    """``:upper`` from the database clock already moved back by the settle delay. A date range
    ends with the last whole day: rows dated today are read once the day is over."""
    local = clock.astimezone(zone).replace(tzinfo=None)
    if kind == "date":
        return local.date() - timedelta(days=1)
    return local


def coerce(kind: WatermarkType, value: object, zone: ZoneInfo) -> Watermark | None:
    """A row's watermark as the configured type; ``None`` for SQL NULL. Raises
    :class:`WatermarkError` for a value of another type (the message names types only)."""
    if value is None:
        return None
    if kind == "datetime":
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value.astimezone(zone).replace(tzinfo=None)
            return value
    elif kind == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
    elif kind == "int":
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
            return int(value)
    elif isinstance(value, Decimal):
        if value.is_finite():
            return value
    elif isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    msg = f"a {type(value).__name__} value is not a {kind} watermark"
    raise WatermarkError(msg)


def encode(kind: WatermarkType, value: Watermark) -> dict[str, str]:
    if isinstance(value, datetime | date):
        return {"t": kind, "v": value.isoformat()}
    return {"t": kind, "v": str(value)}


def _parse(kind: WatermarkType, text: str) -> Watermark:
    try:
        if kind == "datetime":
            moment = datetime.fromisoformat(text)
            return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment
        if kind == "date":
            return date.fromisoformat(text)
        if kind == "int":
            return int(text)
        number = Decimal(text)
    except (ValueError, InvalidOperation) as exc:
        msg = f"the cursor holds no valid {kind} watermark"
        raise WatermarkError(msg) from exc
    if not number.is_finite():
        msg = "the cursor holds no valid decimal watermark"
        raise WatermarkError(msg)
    return number


def decode(kind: WatermarkType, raw: object) -> Watermark:
    """A watermark read back from the cursor. A typed value of another type than configured is
    an error (the admin changed ``watermark_type``: reset the cursor deliberately)."""
    if isinstance(raw, Mapping):
        stored = raw.get("t")
        text = raw.get("v")
        if stored != kind:
            msg = f"the cursor holds a {stored} watermark but the query's watermark_type is {kind}"
            raise WatermarkError(msg)
        if not isinstance(text, str):
            msg = f"the cursor holds no valid {kind} watermark"
            raise WatermarkError(msg)
        return _parse(kind, text)
    # The untyped cursor of the first M1 build: ISO text or a JSON number.
    if isinstance(raw, str):
        return _parse(kind, raw)
    if isinstance(raw, int) and not isinstance(raw, bool) and kind in {"int", "decimal"}:
        return raw if kind == "int" else Decimal(raw)
    msg = f"the cursor holds no valid {kind} watermark"
    raise WatermarkError(msg)


@dataclass(frozen=True, slots=True)
class PagePlan:
    """What to do with one page: emit ``rows[:emit]``; then ``advance`` (the new watermark, or
    ``None`` to keep it); ``done`` ends the query for this read; ``retry_batch`` (set alone)
    re-runs the same watermark with a larger ``:batch``."""

    emit: int
    advance: Watermark | None
    done: bool
    retry_batch: int | None = None


def plan_page(
    *,
    query: str,
    column: str,
    values: Sequence[Watermark | None],
    batch: int,
    configured_batch: int,
    watermark: Watermark,
    upper: Watermark | None,
) -> PagePlan:
    """ADR 0035 items 3 and 4 for one page. ``values`` are the page's watermarks in row order,
    already coerced; ``upper`` is ``None`` for ``int``/``decimal`` queries."""
    present: list[Any] = [value for value in values if value is not None]
    if any(later < earlier for earlier, later in pairwise(present)):
        msg = f"query {query!r}: rows must come back ordered by {column} (ORDER BY {column})"
        raise WatermarkError(msg)
    if len(values) < batch:
        if upper is not None:
            return PagePlan(len(values), upper, done=True)
        top = max(present, default=None)
        return PagePlan(len(values), top if top is not None and top > watermark else None, True)
    if len(present) != len(values):
        msg = f"query {query!r}: a full page has rows without a {column} value to advance on"
        raise WatermarkError(msg)
    top = present[-1]
    below = [value for value in present if value < top]
    if not below:
        ceiling = configured_batch * MAX_BATCH_FACTOR
        if batch >= ceiling:
            msg = (
                f"query {query!r}: more than {ceiling} rows share one {column} value; the "
                "cursor cannot advance past them (raise batch, or order by a finer column)"
            )
            raise WatermarkError(msg)
        return PagePlan(0, None, done=False, retry_batch=min(batch * 2, ceiling))
    stop = below[-1]
    emit = sum(1 for value in present if value <= stop)
    return PagePlan(emit, stop, done=False)
