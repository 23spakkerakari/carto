"""Row building for the spec 7.2 tables from the spec 7.1 example event (with ``stored_at``
from the writer's clock, ADR 0046), and the ClickHouse writer against a fake client
(column-oriented inserts, error wrapping, readiness)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError, DataError, OperationalError

from carto_core.db.clickhouse import (
    EVENT_COLUMNS,
    EVENT_IDENTIFIERS_TABLE,
    EVENTS_TABLE,
    IDENTIFIER_COLUMNS,
    MAX_PARTITION_DAYS,
    ClickHouseWriter,
    WriterError,
    WriteResult,
    columns_of,
    create_client,
    event_row,
    field_ref,
    identifier_rows,
    partition_day_groups,
    stored_at_now,
)
from carto_core.settings import ClickHouseSettings
from carto_schema.event import CanonicalEvent

OBSERVED = datetime(2026, 10, 6, 21, 12, 3, 412000, tzinfo=UTC)
INGESTED = datetime(2026, 10, 6, 21, 12, 9, 20000, tzinfo=UTC)
STORED = datetime(2026, 10, 7, 8, 0, 0, 123000, tzinfo=UTC)


class FakeClient:
    """Records inserts; optionally fails them the way clickhouse-connect does."""

    def __init__(self, *, fail: Exception | None = None, alive: bool = True) -> None:
        self.inserts: list[dict[str, Any]] = []
        self.fail = fail
        self.alive = alive

    def insert(
        self,
        table: str,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        *,
        column_oriented: bool = False,
    ) -> None:
        if self.fail is not None:
            raise self.fail
        self.inserts.append(
            {
                "table": table,
                "data": data,
                "column_names": list(column_names),
                "column_oriented": column_oriented,
            }
        )

    def ping(self) -> bool:
        if self.fail is not None:
            raise self.fail
        return self.alive


def test_event_row_reproduces_the_spec_example_column_for_column() -> None:
    row = event_row(CanonicalEvent.example(), STORED)
    assert len(row) == len(EVENT_COLUMNS) == 14
    assert dict(zip(EVENT_COLUMNS, row, strict=True)) == {
        "tenant_id": "default",
        "event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0A",
        "source_id": "src_wms_db",
        "system_id": "sys_warehouse",
        "kind": "row_change",
        "observed_at": OBSERVED,
        "ingested_at": INGESTED,
        "observed_at_quality": "source",
        "template_id": "tpl_4f1c9a",
        "severity": None,
        "attributes": {"status": "CREATED", "warehouse_code": "DC-03"},
        "actor_token": "t1.Hh3Vq6Zt1Nm4Rk8Pw2Ls7D",
        "actor_kind": "human",
        "stored_at": STORED,
    }


def test_enum_columns_are_plain_strings_not_enum_members() -> None:
    row = dict(zip(EVENT_COLUMNS, event_row(CanonicalEvent.example(), STORED), strict=True))
    for column in ("kind", "observed_at_quality", "actor_kind"):
        assert type(row[column]) is str, column


def test_identifier_rows_explode_one_row_per_identifier_with_field_ref() -> None:
    rows = identifier_rows(CanonicalEvent.example(), STORED)
    assert len(rows) == 4
    assert all(len(row) == len(IDENTIFIER_COLUMNS) == 9 for row in rows)
    as_dicts = [dict(zip(IDENTIFIER_COLUMNS, row, strict=True)) for row in rows]
    assert [row["field_ref"] for row in as_dicts] == [
        "sys_warehouse/tpl_4f1c9a/po_num",
        "sys_warehouse/tpl_4f1c9a/po_num",
        "sys_warehouse/tpl_4f1c9a/order_ref",
        "sys_warehouse/tpl_4f1c9a/order_ref",
    ]
    assert [row["form"] for row in as_dicts] == ["raw", "alnum", "raw", "digits.0"]
    assert [row["shape"] for row in as_dicts] == ["99-999", "99999", "AA-9999999", "9999"]
    assert as_dicts[0] == {
        "tenant_id": "default",
        "token": "t1.q8Jm0h3cR2VfZp4Lx9sT1w",
        "field_ref": "sys_warehouse/tpl_4f1c9a/po_num",
        "form": "raw",
        "shape": "99-999",
        "event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0A",
        "system_id": "sys_warehouse",
        "observed_at": OBSERVED,
        "stored_at": STORED,
    }


def test_stored_at_now_is_utc_at_millisecond_precision() -> None:
    """ADR 0046: the writer's wall clock, in UTC, truncated like every contract timestamp."""
    local = datetime(2026, 10, 7, 10, 0, 0, 123456, tzinfo=timezone(timedelta(hours=2)))
    assert stored_at_now(lambda: local) == STORED
    assert stored_at_now().tzinfo is UTC
    with pytest.raises(ValueError, match="aware"):
        stored_at_now(lambda: datetime(2026, 10, 7, 10))


def test_field_ref_is_system_template_field() -> None:
    assert field_ref("sys_warehouse", "tpl_4f1c9a", "po_num") == "sys_warehouse/tpl_4f1c9a/po_num"


def test_nullable_actor_and_severity() -> None:
    bare = CanonicalEvent.model_validate(
        CanonicalEvent.example().model_dump() | {"actor": None, "severity": "error"}
    )
    row = dict(zip(EVENT_COLUMNS, event_row(bare, STORED), strict=True))
    assert row["actor_token"] is None
    assert row["actor_kind"] is None
    assert row["severity"] == "error"
    assert type(row["severity"]) is str


def test_timestamps_are_utc_aware_datetimes_at_millisecond_precision() -> None:
    row = dict(zip(EVENT_COLUMNS, event_row(CanonicalEvent.example(), STORED), strict=True))
    for column in ("observed_at", "ingested_at", "stored_at"):
        value = row[column]
        assert isinstance(value, datetime)
        assert value.tzinfo is UTC
        assert value.microsecond % 1000 == 0


def test_columns_of_transposes_rows_into_one_list_per_column() -> None:
    event = CanonicalEvent.example()
    other = CanonicalEvent.model_validate(
        event.model_dump() | {"event_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0B"}
    )
    columns = columns_of([event_row(event, STORED), event_row(other, STORED)], len(EVENT_COLUMNS))
    assert len(columns) == 14
    assert columns[1] == ["01J9ZK8X5Q8V3N6M2T4R7W1Y0A", "01J9ZK8X5Q8V3N6M2T4R7W1Y0B"]
    assert columns_of([], len(EVENT_COLUMNS)) == [[] for _ in range(14)]


def test_writer_inserts_both_tables_column_oriented() -> None:
    client = FakeClient()
    writer = ClickHouseWriter(client, clock=lambda: STORED)
    event = CanonicalEvent.example()
    result = writer.write_events([event, event])
    assert result == WriteResult(events=2, identifiers=8)
    assert [insert["table"] for insert in client.inserts] == [EVENTS_TABLE, EVENT_IDENTIFIERS_TABLE]
    events_insert, identifiers_insert = client.inserts
    assert events_insert["column_names"] == list(EVENT_COLUMNS)
    assert events_insert["column_oriented"] is True
    assert len(events_insert["data"]) == 14
    assert all(len(column) == 2 for column in events_insert["data"])
    assert events_insert["data"][-1] == [STORED, STORED], "stored_at from the clock"
    assert identifiers_insert["column_names"] == list(IDENTIFIER_COLUMNS)
    assert identifiers_insert["column_oriented"] is True
    assert len(identifiers_insert["data"]) == 9
    assert all(len(column) == 8 for column in identifiers_insert["data"])
    assert identifiers_insert["data"][-1] == [STORED] * 8


def test_writer_without_a_clock_stamps_the_wall_clock() -> None:
    client = FakeClient()
    before = datetime.now(UTC)
    ClickHouseWriter(client).write_events([CanonicalEvent.example()])
    (stored_at,) = client.inserts[0]["data"][-1]
    assert stored_at.tzinfo is UTC
    assert before - timedelta(seconds=1) <= stored_at <= datetime.now(UTC)


def test_writer_skips_the_identifier_insert_when_there_are_none() -> None:
    client = FakeClient()
    event = CanonicalEvent.model_validate(
        CanonicalEvent.example().model_dump() | {"identifiers": []}
    )
    assert ClickHouseWriter(client).write_events([event]) == WriteResult(events=1, identifiers=0)
    assert [insert["table"] for insert in client.inserts] == [EVENTS_TABLE]


def test_writer_with_no_events_writes_nothing() -> None:
    client = FakeClient()
    assert ClickHouseWriter(client).write_events([]) == WriteResult(events=0, identifiers=0)
    assert client.inserts == []


@pytest.mark.parametrize(
    "failure",
    [DatabaseError("Code: 241. DB::Exception: Memory limit"), OSError("connection reset")],
)
def test_writer_wraps_client_failures_without_event_contents(failure: Exception) -> None:
    writer = ClickHouseWriter(FakeClient(fail=failure))
    with pytest.raises(WriterError) as info:
        writer.write_events([CanonicalEvent.example()])
    message = str(info.value)
    assert type(failure).__name__ in message
    assert "t1." not in message
    assert "CREATED" not in message


@pytest.mark.parametrize(
    ("failure", "permanent"),
    [
        (DatabaseError("Code: 53. DB::Exception: Type mismatch", code=53), True),
        (DatabaseError("Code: 6. DB::Exception: Cannot parse text", code=6), True),
        (DataError("cannot serialize a value"), True),
        (DatabaseError("Code: 241. DB::Exception: Memory limit", code=241), False),
        (DatabaseError("Code: 252. DB::Exception: Too many parts", code=252), False),
        (DatabaseError("Code: 60. DB::Exception: Unknown table", code=60), False),
        (OperationalError("Code: 53. after a retry", code=53), True),
        (OperationalError("connection refused"), False),
        (DatabaseError("no code"), False),
        (OSError("connection reset"), False),
    ],
)
def test_a_refusal_of_the_data_is_permanent_and_the_rest_is_transient(
    failure: Exception, permanent: bool
) -> None:
    """ADR 0036: a permanent error is answered 422 and parks the batch; anything else is 503.
    The server's code decides, whatever class clickhouse-connect raises it as."""
    with pytest.raises(WriterError) as info:
        ClickHouseWriter(FakeClient(fail=failure)).write_events([CanonicalEvent.example()])
    assert info.value.permanent is permanent


def test_the_error_code_is_named_in_the_message() -> None:
    failure = DatabaseError("Code: 53. DB::Exception: Type mismatch for t1.secret", code=53)
    with pytest.raises(WriterError) as info:
        ClickHouseWriter(FakeClient(fail=failure)).write_events([CanonicalEvent.example()])
    assert str(info.value) == "ClickHouse insert failed: DatabaseError (code 53)"


def _on_day(day: int, index: int = 0) -> CanonicalEvent:
    event = CanonicalEvent.example()
    observed = datetime(2026, 1, 1, 12, tzinfo=UTC) + timedelta(days=day, seconds=index)
    return event.model_copy(update={"observed_at": observed})


def test_partition_day_groups_span_at_most_the_limit_oldest_first() -> None:
    events = [_on_day(day) for day in range(200, -1, -1)]  # 201 days, newest first
    groups = list(partition_day_groups(events))
    assert [len(group) for group in groups] == [90, 90, 21]
    days = [{event.observed_at.date() for event in group} for group in groups]
    assert all(len(group_days) <= MAX_PARTITION_DAYS for group_days in days)
    assert max(days[0]) < min(days[1]) and max(days[1]) < min(days[2])
    assert sum(len(group) for group in groups) == len(events)


def test_partition_days_are_utc_days_and_order_within_a_day_is_kept() -> None:
    late = _on_day(0).model_copy(update={"observed_at": datetime(2026, 1, 1, 23, 30, tzinfo=UTC)})
    local = late.observed_at.astimezone(datetime.now().astimezone().tzinfo)
    first, second = _on_day(0, 1), _on_day(0, 0)
    (one_day,) = partition_day_groups(
        [first, late.model_copy(update={"observed_at": local}), second]
    )
    assert one_day[0] is first and one_day[2] is second


def test_a_batch_over_many_days_is_inserted_in_day_groups() -> None:
    client = FakeClient()
    events = [_on_day(day) for day in range(MAX_PARTITION_DAYS + 5)]
    result = ClickHouseWriter(client).write_events(events)
    assert result == WriteResult(events=95, identifiers=95 * 4)
    assert [insert["table"] for insert in client.inserts] == [
        EVENTS_TABLE,
        EVENT_IDENTIFIERS_TABLE,
        EVENTS_TABLE,
        EVENT_IDENTIFIERS_TABLE,
    ]
    sizes = [len(insert["data"][0]) for insert in client.inserts]
    assert sizes == [90, 360, 5, 20]


def test_writer_ping_reports_the_client_state() -> None:
    assert ClickHouseWriter(FakeClient(alive=True)).ping() is True
    assert ClickHouseWriter(FakeClient(alive=False)).ping() is False
    assert ClickHouseWriter(FakeClient(fail=OSError("down"))).ping() is False


def test_create_client_never_opens_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """One client is shared by the threadpool: a session id would serialize requests and fail
    concurrent ones with SESSION_IS_LOCKED, so the client is created without one."""
    seen: dict[str, Any] = {}

    def fake_get_client(**kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr("clickhouse_connect.get_client", fake_get_client)
    create_client(ClickHouseSettings(url="http://127.0.0.1:8123", secure=False))
    assert seen["autogenerate_session_id"] is False
    assert "session_id" not in seen
