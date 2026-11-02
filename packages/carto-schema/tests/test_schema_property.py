"""Property tests (spec 18.1): generated events, batches and heartbeats round-trip through JSON,
and the exported JSON Schemas accept whatever the models accept."""

from __future__ import annotations

import json
import re
import string
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from operator import attrgetter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

from carto_schema.event import (
    SOURCE_ID_PATTERN,
    TEMPLATE_ID_PATTERN,
    TENANT_ID_PATTERN,
    ULID_PATTERN,
    Actor,
    ActorKind,
    CanonicalEvent,
    EventKind,
    Identifier,
    ObservedAtQuality,
    Redaction,
    Severity,
    name_carries_no_value,
)
from carto_schema.forms import (
    SHAPE_MAX_LEN,
    SHAPE_MAX_RUN,
    TOKEN_PATTERN,
    is_form,
    parse_form,
    shape,
    token_domain,
)
from carto_schema.ingest import IngestBatch, SourceHeartbeat, SourceStatus

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
ISO_MILLIS_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BASE64URL = string.ascii_letters + string.digits + "-_"
ALL_FORMS = [
    "raw",
    "norm",
    "alnum",
    "date",
    "amount",
    *(f"digits.{k}" for k in range(3)),
    *(f"phonetic.{k}" for k in range(8)),
]
ZONES: list[tzinfo] = [
    UTC,
    timezone(timedelta(hours=-4)),
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=14)),
    timezone(timedelta(hours=-12)),
    ZoneInfo("America/New_York"),
    ZoneInfo("Asia/Kolkata"),
]


def _load_validator(name: str) -> Any:
    schema = json.loads((SCHEMAS_DIR / f"{name}.v1.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


EVENT_VALIDATOR = _load_validator("canonical_event")
BATCH_VALIDATOR = _load_validator("ingest_batch")
HEARTBEAT_VALIDATOR = _load_validator("source_heartbeat")

# -- strategies -----------------------------------------------------------------------------------

ulids = st.tuples(st.sampled_from("01234567"), st.text(CROCKFORD, min_size=25, max_size=25)).map(
    "".join
)
tokens = st.tuples(
    st.integers(min_value=1, max_value=9999), st.text(BASE64URL, min_size=22, max_size=22)
).map(lambda parts: f"t{parts[0]}.{parts[1]}")
forms = st.sampled_from(ALL_FORMS)
shapes = st.text(min_size=1, max_size=SHAPE_MAX_LEN).map(shape)
aware_datetimes = st.datetimes(
    min_value=datetime(1971, 1, 1),
    max_value=datetime(2199, 12, 31),
    timezones=st.sampled_from(ZONES),
)
tenant_ids = st.from_regex(TENANT_ID_PATTERN, fullmatch=True)
source_ids = st.from_regex(SOURCE_ID_PATTERN, fullmatch=True)
template_ids = st.from_regex(TEMPLATE_ID_PATTERN, fullmatch=True)

# Names and templates the model accepts: no segment that looks like a value (ADR 0027).
names = st.text(min_size=1, max_size=32).filter(name_carries_no_value)
template_texts = st.text(max_size=200).filter(lambda text: not re.search(r"[0-9]{6}", text))

identifiers = st.builds(
    Identifier,
    field=names,
    form=forms,
    token=tokens,
    shape=shapes,
    len=st.integers(min_value=1, max_value=100_000),
)
identifier_lists = st.lists(identifiers, max_size=6, unique_by=attrgetter("field", "form"))
attributes = st.dictionaries(
    names.filter(lambda name: len(name) <= 16), st.text(max_size=48), max_size=6
)
actors = st.builds(Actor, token=tokens, kind=st.sampled_from(list(ActorKind)))
redactions = st.builds(
    Redaction,
    policy_version=st.text(min_size=1, max_size=32),
    entities_masked=st.integers(min_value=0, max_value=10_000),
)


def events(
    tenant_id: st.SearchStrategy[str] = tenant_ids,
    source_id: st.SearchStrategy[str] = source_ids,
) -> st.SearchStrategy[CanonicalEvent]:
    """Canonical events, optionally pinned to one tenant and source for batches."""
    return st.builds(
        CanonicalEvent,
        schema_version=st.just("1"),
        event_id=ulids,
        tenant_id=tenant_id,
        source_id=source_id,
        system_id=source_ids,
        kind=st.sampled_from(list(EventKind)),
        observed_at=aware_datetimes,
        ingested_at=aware_datetimes,
        observed_at_quality=st.sampled_from(list(ObservedAtQuality)),
        template_id=template_ids,
        template_text=template_texts,
        severity=st.none() | st.sampled_from(list(Severity)),
        attributes=attributes,
        identifiers=identifier_lists,
        actor=st.none() | actors,
        dropped_fields=st.lists(names, max_size=4),
        redaction=redactions,
    )


@st.composite
def batches(draw: st.DrawFn) -> IngestBatch:
    tenant_id = draw(tenant_ids)
    source_id = draw(source_ids)
    return IngestBatch(
        schema_version="1",
        tenant_id=tenant_id,
        source_id=source_id,
        batch_id=draw(ulids),
        sent_at=draw(aware_datetimes),
        events=draw(
            st.lists(events(st.just(tenant_id), st.just(source_id)), min_size=1, max_size=4)
        ),
    )


heartbeats = st.builds(
    SourceHeartbeat,
    schema_version=st.just("1"),
    tenant_id=tenant_ids,
    source_id=source_ids,
    sent_at=aware_datetimes,
    status=st.sampled_from(list(SourceStatus)),
    last_success_at=st.none() | aware_datetimes,
    lag_seconds=st.floats(min_value=0, max_value=1e9, allow_nan=False, allow_infinity=False),
    error_count=st.integers(min_value=0, max_value=10**6),
    buffer_depth=st.integers(min_value=0, max_value=10**9),
    oldest_buffered_at=st.none() | aware_datetimes,
    message=st.text(max_size=100),
)


# -- round trips ----------------------------------------------------------------------------------


@given(events())
@settings(deadline=None, max_examples=100)
def test_event_round_trips_through_json(event: CanonicalEvent) -> None:
    text = event.model_dump_json()
    again = CanonicalEvent.model_validate_json(text)
    assert again == event
    assert again.model_dump_json() == text
    assert CanonicalEvent.model_validate(event.model_dump(mode="json")) == event
    assert CanonicalEvent.model_validate(event.model_dump()) == event
    for stamp in (event.observed_at, event.ingested_at):
        assert stamp.tzinfo is UTC
        assert stamp.microsecond % 1000 == 0
    dumped = json.loads(text)
    assert ISO_MILLIS_Z.fullmatch(dumped["observed_at"])
    assert ISO_MILLIS_Z.fullmatch(dumped["ingested_at"])


@given(events())
@settings(deadline=None, max_examples=100)
def test_exported_schema_accepts_generated_events(event: CanonicalEvent) -> None:
    EVENT_VALIDATOR.validate(json.loads(event.model_dump_json()))


@given(batches())
@settings(deadline=None, max_examples=50)
def test_batch_round_trips_through_json(batch: IngestBatch) -> None:
    text = batch.model_dump_json()
    assert IngestBatch.model_validate_json(text) == batch
    assert IngestBatch.model_validate_json(text).model_dump_json() == text
    assert all(
        e.tenant_id == batch.tenant_id and e.source_id == batch.source_id for e in batch.events
    )
    BATCH_VALIDATOR.validate(json.loads(text))


@given(heartbeats)
@settings(deadline=None, max_examples=100)
def test_heartbeat_round_trips_through_json(heartbeat: SourceHeartbeat) -> None:
    text = heartbeat.model_dump_json()
    assert SourceHeartbeat.model_validate_json(text) == heartbeat
    assert SourceHeartbeat.model_validate_json(text).model_dump_json() == text
    dumped = json.loads(text)
    for key in ("sent_at", "last_success_at", "oldest_buffered_at"):
        assert dumped[key] is None or ISO_MILLIS_Z.fullmatch(dumped[key])
    HEARTBEAT_VALIDATOR.validate(dumped)


# -- timestamps -----------------------------------------------------------------------------------


@given(aware_datetimes)
@settings(deadline=None, max_examples=100)
def test_any_aware_datetime_normalizes_to_utc_milliseconds(stamp: datetime) -> None:
    data = CanonicalEvent.example().model_dump(mode="json") | {"observed_at": stamp}
    event = CanonicalEvent.model_validate(data)
    assert event.observed_at.tzinfo is UTC
    # Compare UTC instants: an aware datetime inside a DST fold never compares equal across zones.
    expected = stamp.astimezone(UTC)
    assert event.observed_at == expected.replace(microsecond=expected.microsecond // 1000 * 1000)
    assert ISO_MILLIS_Z.fullmatch(json.loads(event.model_dump_json())["observed_at"])
    assert CanonicalEvent.model_validate_json(event.model_dump_json()) == event


# -- forms, tokens, ulids, shapes ------------------------------------------------------------------


@given(forms)
@settings(deadline=None, max_examples=100)
def test_form_names_parse_and_rebuild(name: str) -> None:
    assert is_form(name)
    base, index = parse_form(name)
    assert name == (base if index is None else f"{base}.{index}")
    assert token_domain(name) in {"id", "date", "amt", "ph"}


@given(ulids, tokens)
@settings(deadline=None, max_examples=100)
def test_generated_ids_match_the_patterns(ulid: str, token: str) -> None:
    assert ULID_PATTERN.fullmatch(ulid)
    assert TOKEN_PATTERN.fullmatch(token)


@given(st.text(max_size=300))
@settings(deadline=None, max_examples=100)
def test_shape_properties(value: str) -> None:
    result = shape(value)
    assert len(result) <= min(len(value), SHAPE_MAX_LEN)
    assert shape(result) == result
    for char in result:
        assert char in "9A" or not (char.isdigit() or char.isalpha())
    if "+" not in value:
        assert re.search(rf"(.)\1{{{SHAPE_MAX_RUN}}}", result, flags=re.DOTALL) is None
