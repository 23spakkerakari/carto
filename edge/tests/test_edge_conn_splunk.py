"""carto_edge.connectors.splunk against an httpx MockTransport replaying the documented export
stream (spec 8.1.3, 18.2): index-time windows and their checkpoints (ADR 0035), dedupe across
overlapping windows, backfill chunking, the capability check and the single POST."""

from __future__ import annotations

import json
import logging
import tracemalloc
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorContext,
    ConnectorError,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ResolvedHost,
)
from carto_edge.connectors.splunk import (
    BACKFILL_QUEUE_RECORDS,
    MAX_JSON_BODY_BYTES,
    SplunkConnector,
)
from carto_edge.pipeline.model import CursorCheckpoint, RawRecord, ReadItem
from carto_schema.event import EventKind

TOKEN = "splunk-token-0123456789-secret"  # noqa: S105
NOW = datetime(2026, 10, 7, 10, 10, tzinfo=UTC)


def event(minute: int, cd: str, *, indexed_after: int = 2) -> dict[str, Any]:
    when = datetime(2026, 10, 7, 10, minute, tzinfo=UTC)
    return {
        "_bkt": "orders~1~X",
        "_cd": cd,
        "_indextime": str(int(when.timestamp()) + indexed_after),
        "_serial": "0",
        "_si": ["sh1", "orders"],
        "_sourcetype": "order_svc",
        "_time": when.isoformat(timespec="milliseconds"),
        "_raw": f"ts={when.isoformat()} level=INFO msg=order_created order_id=SO-{minute:04d}",
        "host": "orders-1",
        "source": "/var/log/order-svc.log",
        "sourcetype": "order_svc",
        "index": "orders",
        "linecount": "1",
        "splunk_server": "sh1",
        "order_id": f"SO-{minute:04d}",
    }


EVENTS = [event(0, "0:10"), event(3, "0:13"), event(6, "0:16"), event(9, "0:19")]


def locator(index: int) -> str:
    return f"splunk:{EVENTS[index]['_cd']}:{EVENTS[index]['_indextime']}"


class Secrets:
    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "vault://kv/carto/splunk-orders-token"  # noqa: S105
        return TOKEN


class Network:
    def resolve(self, host: str, port: int) -> ResolvedHost:
        return ResolvedHost(host=host, address="10.20.0.9", port=port)


class Splunk:
    """The mock search head: records every request, serves export, context and roles."""

    def __init__(
        self, capabilities: list[str] | None = None, roles: list[str] | None = None
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.capabilities = (
            capabilities if capabilities is not None else ["search", "rtsearch", "list_inputs"]
        )
        self.roles = roles if roles is not None else ["carto_reader"]
        self.status = 200
        self.statuses: list[int] = []  # answered first, one per request
        self.events = list(EVENTS)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.statuses:
            return httpx.Response(self.statuses.pop(0), text="busy")
        if self.status != 200:
            return httpx.Response(self.status, text="denied")
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        path = request.url.path
        if path == "/services/search/jobs/export":
            assert request.method == "POST"
            form = parse_qs(request.read().decode())
            assert form["output_mode"] == ["json"]
            earliest = datetime.fromisoformat(form["earliest_time"][0])
            latest = datetime.fromisoformat(form["latest_time"][0])
            search = form["search"][0]
            lines = [json.dumps({"preview": True, "offset": 0, "result": EVENTS[0]})]
            selected = [
                e for e in self.events if earliest <= datetime.fromisoformat(e["_time"]) <= latest
            ]
            if "index_earliest" in form:  # index time: inclusive start, exclusive end
                first = datetime.fromisoformat(form["index_earliest"][0]).timestamp()
                end = datetime.fromisoformat(form["index_latest"][0]).timestamp()
                selected = [e for e in selected if first <= int(e["_indextime"]) < end]
            if search.endswith("| head 1"):
                selected = selected[:1]
            for offset, item in enumerate(selected):
                last = offset == len(selected) - 1
                lines.append(
                    json.dumps(
                        {"preview": False, "offset": offset, "lastrow": last, "result": item}
                    )
                )
            lines.append("this is not json")
            lines.append(json.dumps({"messages": [{"type": "INFO", "text": "done"}]}))
            return httpx.Response(200, content=("\n".join(lines) + "\n").encode())
        if path == "/services/authentication/current-context":
            assert request.method == "GET"
            return httpx.Response(
                200, json={"entry": [{"content": {"username": "carto", "roles": self.roles}}]}
            )
        if path.startswith("/services/authorization/roles/"):
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "entry": [
                        {
                            "content": {
                                "capabilities": self.capabilities,
                                "imported_capabilities": ["search"],
                                "srchIndexesAllowed": ["orders"],
                                "imported_srchIndexesAllowed": ["shipping"],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(
    splunk: Splunk,
    config: dict[str, object] | None = None,
    backfill_days: int | None = None,
    clock: Clock | None = None,
    sleeps: list[float] | None = None,
) -> SplunkConnector:
    source = SourceConfig(
        id="src_orders_splunk",
        system="sys_orders",
        type=SourceType.SPLUNK,
        config={
            "base_url": "https://splunk.internal.example:8089",
            "search": "search index=orders sourcetype=order_svc",
            "window_minutes": 5,
            "overlap_minutes": 2,
            "max_concurrency": 2,
            **(config or {}),
        },
        secret_ref="vault://kv/carto/splunk-orders-token",  # noqa: S106
        backfill_days=backfill_days,
    )
    return SplunkConnector(
        source,
        ConnectorContext(secrets=Secrets(), network=Network()),
        transport=httpx.MockTransport(splunk),
        clock=clock if clock is not None else Clock(),
        sleep=Sleeper(sleeps if sleeps is not None else []),
    )


class Sleeper:
    """Records the retry delays instead of waiting."""

    def __init__(self, delays: list[float]) -> None:
        self.delays = delays

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def collect_items(
    connector: SplunkConnector, cursor: dict[str, object] | None = None
) -> list[ReadItem]:
    return [item async for item in connector.read(cursor)]


async def collect(
    connector: SplunkConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [item for item in await collect_items(connector, cursor) if isinstance(item, RawRecord)]


def checkpoints(items: list[ReadItem]) -> list[str]:
    return [str(item.cursor["window_end"]) for item in items if isinstance(item, CursorCheckpoint)]


def exports(splunk: Splunk) -> list[dict[str, list[str]]]:
    return [
        parse_qs(request.read().decode())
        for request in splunk.requests
        if request.url.path.endswith("/export")
    ]


def test_is_a_read_connector() -> None:
    connector = make(Splunk())
    assert isinstance(connector, ReadConnector)
    assert connector.type == "splunk"


@pytest.mark.parametrize(
    "config",
    [
        {"base_url": "https://user:pw@splunk.example:8089"},
        {"base_url": "https://splunk.example:8089/services"},
        {"base_url": "ftp://splunk.example"},
        {"base_url": "http://splunk.example:8089"},
        {"search": ""},
        {"search": "index=orders | outputlookup carriers.csv"},
        {"search": "index=orders | collect index=main"},
        {"search": "index=orders | delete"},
        {"search": "index=orders | sendemail to=ops@example.com"},
        {"search": "| rest /services/server/info"},
        {"window_minutes": 0},
        {"bogus": 1},
    ],
)
def test_invalid_configs(config: dict[str, object]) -> None:
    with pytest.raises(InvalidConfigError):
        make(Splunk(), config)


def test_needs_a_secret_ref() -> None:
    source = SourceConfig(
        id="s",
        system="sys_orders",
        type=SourceType.SPLUNK,
        config={"base_url": "https://x.example", "search": "search x"},
    )
    with pytest.raises(InvalidConfigError, match="secret_ref"):
        SplunkConnector(source, ConnectorContext(secrets=Secrets(), network=Network()))


def test_plaintext_needs_the_admin_flag(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        make(Splunk(), {"base_url": "http://splunk.example:8089", "allow_plaintext": True})
    assert any("plaintext" in record.getMessage() for record in caplog.records)


async def test_read_without_cursor_covers_the_last_settled_window_of_index_time() -> None:
    splunk = Splunk()
    connector = make(splunk)
    items = await collect_items(connector)
    # now 10:10, settle 60 s: the window is [10:04, 10:09) of index time. The 10:09 event was
    # indexed at 10:09:02, after the window, and waits for the next poll.
    assert len(items) == 2
    first, done = items
    assert isinstance(first, RawRecord)
    assert first.locator == locator(2)
    assert done == CursorCheckpoint(
        "src_orders_splunk", {"window_end": "2026-10-07T10:09:00+00:00"}
    )
    assert first.kind is EventKind.LOG
    assert first.text == EVENTS[2]["_raw"]
    assert first.fields == {
        "host": "orders-1",
        "source": "/var/log/order-svc.log",
        "sourcetype": "order_svc",
        "index": "orders",
        "_time": EVENTS[2]["_time"],
        "order_id": "SO-0006",
    }
    assert first.received_at == NOW
    assert first.commit_cursor is None  # the checkpoint after the window carries the cursor
    (form,) = exports(splunk)
    assert form["index_earliest"] == ["2026-10-07T10:04:00+00:00"]
    assert form["index_latest"] == ["2026-10-07T10:09:00+00:00"]
    # Event time only bounds the buckets: 24 hours of lag back, a day of clock skew ahead.
    assert form["earliest_time"] == ["2026-10-06T10:04:00+00:00"]
    assert form["latest_time"] == ["2026-10-08T10:09:00+00:00"]
    assert form["search"] == ["search index=orders sourcetype=order_svc"]
    await connector.close()


async def test_cursor_resumes_with_overlap_and_dedupes_across_windows() -> None:
    clock = Clock()
    connector = make(Splunk(), clock=clock)
    first = await collect_items(connector)
    assert checkpoints(first) == ["2026-10-07T10:09:00+00:00"]
    # Same moment again: nothing has settled since the cursor, so nothing is searched.
    assert await collect_items(connector, {"window_end": checkpoints(first)[-1]}) == []
    # Five minutes later the overlap re-reads from 10:07; the 10:06 event is in the dedupe set
    # (it was indexed at 10:06:02, outside the overlap anyway) and the 10:09 one is new.
    clock.now = NOW + timedelta(minutes=5)
    again = await collect_items(connector, {"window_end": checkpoints(first)[-1]})
    assert [r.locator for r in again if isinstance(r, RawRecord)] == [locator(3)]
    assert checkpoints(again) == ["2026-10-07T10:12:00+00:00", "2026-10-07T10:14:00+00:00"]
    # A fresh instance (restart) with a cursor at 10:08 re-reads from 10:06 of index time; core
    # dedupes what comes back twice by event_id.
    fresh = make(Splunk())
    assert [
        r.locator for r in await collect(fresh, {"window_end": "2026-10-07T10:08:00+00:00"})
    ] == [locator(2)]


async def test_a_quiet_search_still_moves_the_cursor_every_window() -> None:
    """Review finding C3: the cursor used to move only on a window's last record, so a search
    that matched nothing for longer than one read re-searched the same span forever."""
    splunk = Splunk()
    splunk.events = []
    clock = Clock()
    connector = make(splunk, {"max_windows_per_read": 6}, clock=clock)
    cursor: dict[str, object] = {"window_end": "2026-10-06T22:00:00+00:00"}  # 12 h ago
    seen: list[str] = []
    for _ in range(30):
        items = await collect_items(connector, cursor)
        marks = checkpoints(items)
        if not marks:
            break
        assert not [item for item in items if isinstance(item, RawRecord)]
        seen.extend(marks)
        cursor = {"window_end": marks[-1]}
    assert cursor == {"window_end": "2026-10-07T10:09:00+00:00"}  # caught up to now - settle
    assert seen == sorted(set(seen))  # strictly forward, one checkpoint per window


async def test_an_event_indexed_late_is_still_read() -> None:
    """ADR 0035 item 2: an event indexed long after its own time (forwarder backlog) is found
    by the window of its index time, not lost behind an event-time window."""
    splunk = Splunk()
    late = event(1, "0:99", indexed_after=7 * 60)  # 10:01 event, indexed at 10:08
    splunk.events = [late]
    connector = make(splunk)
    first = await collect(connector, {"window_end": "2026-10-07T10:04:00+00:00"})
    assert [r.locator for r in first] == [f"splunk:0:99:{late['_indextime']}"]


async def test_settle_and_event_lag_are_configurable() -> None:
    splunk = Splunk()
    connector = make(splunk, {"settle_seconds": 0, "max_event_lag_hours": 2})
    items = await collect_items(connector)
    assert checkpoints(items) == ["2026-10-07T10:10:00+00:00"]
    (form,) = exports(splunk)
    assert form["earliest_time"] == ["2026-10-07T08:05:00+00:00"]
    assert [r.locator for r in items if isinstance(r, RawRecord)] == [locator(2), locator(3)]


async def test_initial_read_honours_backfill_days_in_windows() -> None:
    splunk = Splunk()
    connector = make(splunk, {"window_minutes": 60, "max_windows_per_read": 2}, backfill_days=1)
    items = await collect_items(connector)
    assert len(exports(splunk)) == 2  # capped per read
    # The first two hourly windows of yesterday hold nothing, and still move the cursor.
    assert checkpoints(items) == ["2026-10-06T11:09:00+00:00", "2026-10-06T12:09:00+00:00"]


async def test_backfill_chunks_with_bounded_concurrency() -> None:
    splunk = Splunk()
    connector = make(splunk, {"backfill_chunk_minutes": 60, "max_concurrency": 2})
    start = datetime(2026, 10, 7, 8, 0, tzinfo=UTC)
    records = [r async for r in connector.backfill(start, start + timedelta(hours=3))]
    forms = exports(splunk)
    assert len(forms) == 3
    assert all("index_earliest" not in form for form in forms)  # backfill is by event time
    assert [r.locator for r in records] == [locator(0), locator(1), locator(2), locator(3)]
    assert all(r.commit_cursor is None for r in records)


async def test_search_prefix_is_added() -> None:
    splunk = Splunk()
    connector = make(splunk, {"search": "index=orders"})
    await collect(connector)
    form = parse_qs(splunk.requests[0].read().decode())
    assert form["search"] == ["search index=orders"]


async def test_malformed_cursor_is_an_error() -> None:
    with pytest.raises(ConnectorError, match="window_end"):
        await collect(make(Splunk()), {"window_end": "yesterday"})


async def test_http_error_is_a_connector_error() -> None:
    splunk = Splunk()
    splunk.status = 401
    with pytest.raises(ConnectorError, match="401"):
        await collect(make(splunk))


# ---------------------------------------------------------------------------------------------
# the test method
# ---------------------------------------------------------------------------------------------


async def test_reader_role_is_verified_and_lists_indexes() -> None:
    splunk = Splunk()
    result = await make(splunk).test()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.can_enable
    assert result.visible == ("orders", "shipping")
    names = [check.name for check in result.checks]
    assert names == ["authentication", "capabilities", "search"]
    assert all(check.ok for check in result.checks)
    # Spec 8.1: the export is the only POST; everything else is GET.
    methods = {(r.method, r.url.path) for r in splunk.requests}
    posts = {path for method, path in methods if method == "POST"}
    assert posts == {"/services/search/jobs/export"}
    assert ("GET", "/services/authentication/current-context") in methods
    assert ("GET", "/services/authorization/roles/carto_reader") in methods


@pytest.mark.parametrize(
    "capability",
    [
        "edit_user",
        "delete_by_keyword",
        "admin_all_objects",
        "change_own_password",
        "restart_splunkd",
        "output_file",
        "schedule_search",
        "rest_properties_set",
        "a_capability_from_a_future_release",
    ],
)
async def test_write_capability_makes_the_token_write_capable(capability: str) -> None:
    result = await make(Splunk(capabilities=["search", capability])).test()
    assert not result.ok
    assert result.read_only is ReadOnlyStatus.WRITE_CAPABLE
    assert not result.can_enable
    assert capability in result.problems[0]


async def test_rtsearch_is_not_a_write_capability() -> None:
    result = await make(Splunk(capabilities=["rtsearch"])).test()
    assert result.read_only is ReadOnlyStatus.VERIFIED


async def test_role_lookup_failure_is_reported_not_raised() -> None:
    splunk = Splunk()
    splunk.status = 403
    result = await make(splunk).test()
    assert not result.ok
    assert result.checks[0].name == "authentication" and not result.checks[0].ok


async def test_token_never_appears_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        connector = make(Splunk())
        await collect(connector)
        await connector.test()
    assert TOKEN not in caplog.text


# ---------------------------------------------------------------------------------------------
# Review findings C13 and C15: byte limits, streamed backfill, 5xx retries
# ---------------------------------------------------------------------------------------------


class Pulled:
    """An async body that counts the bytes the client actually pulled."""

    def __init__(self, parts: list[tuple[bytes, int]]) -> None:
        self.parts = parts
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk, times in self.parts:
            for _ in range(times):
                self.pulled += len(chunk)
                yield chunk


def streaming(path_bodies: dict[str, Pulled]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = path_bodies.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def streaming_connector(transport: httpx.MockTransport) -> SplunkConnector:
    return SplunkConnector(
        make(Splunk()).source,
        ConnectorContext(secrets=Secrets(), network=Network()),
        transport=transport,
        clock=Clock(),
        sleep=Sleeper([]),
    )


async def test_an_endless_line_is_skipped_without_being_held_in_memory() -> None:
    chunk = b"x" * 65_536
    good = json.dumps({"preview": False, "result": EVENTS[2]}).encode()
    body = Pulled([(chunk, 512), (b"\n" + good + b"\n", 1)])  # a 32 MiB line, then one event
    connector = streaming_connector(streaming({"/services/search/jobs/export": body}))
    tracemalloc.start()
    try:
        records = await collect(connector)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert [r.locator for r in records] == [locator(2)]
    assert body.pulled > 32 * 1024 * 1024
    assert peak < 16 * 1024 * 1024  # never the whole line


async def test_a_json_body_is_read_only_up_to_its_limit() -> None:
    body = Pulled([(b"{" + b" " * 65_535, 1024)])  # 64 MiB that never ends as JSON
    connector = streaming_connector(streaming({"/services/authentication/current-context": body}))
    result = await connector.test()
    assert not result.ok
    assert "exceeds" in result.checks[0].detail
    assert body.pulled <= MAX_JSON_BODY_BYTES + 2 * 65_536


async def test_backfill_streams_a_large_chunk_through_a_bounded_queue() -> None:
    splunk = Splunk()
    splunk.events = [event(1, f"0:{n}") for n in range(3 * BACKFILL_QUEUE_RECORDS)]
    connector = make(splunk, {"backfill_chunk_minutes": 60})
    start = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    stream = connector.backfill(start, start + timedelta(hours=1))
    first = await anext(stream)
    assert first.locator == f"splunk:0:0:{splunk.events[0]['_indextime']}"
    # The producer stops when its queue is full instead of collecting the whole chunk.
    assert connector._sequence <= BACKFILL_QUEUE_RECORDS + 1
    rest = [record async for record in stream]
    assert len(rest) == 3 * BACKFILL_QUEUE_RECORDS - 1
    await connector.close()


async def test_backfill_keeps_chunk_order_and_raises_a_failed_chunk() -> None:
    splunk = Splunk()
    connector = make(splunk, {"backfill_chunk_minutes": 5, "max_concurrency": 3})
    start = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    records = [r async for r in connector.backfill(start, start + timedelta(minutes=15))]
    assert [r.locator for r in records] == [locator(0), locator(1), locator(2), locator(3)]
    splunk.status = 403
    with pytest.raises(ConnectorError, match="403"):
        [r async for r in make(splunk).backfill(start, start + timedelta(minutes=15))]


async def test_server_errors_are_retried_with_backoff() -> None:
    splunk = Splunk()
    splunk.statuses = [503, 502]
    sleeps: list[float] = []
    records = await collect(make(splunk, sleeps=sleeps))
    assert [r.locator for r in records] == [locator(2)]
    assert len(sleeps) == 2
    assert len(exports(splunk)) == 3


async def test_persistent_server_errors_trip_the_circuit_breaker() -> None:
    splunk = Splunk()
    splunk.status = 503
    connector = make(splunk, {"failure_threshold": 2})
    for _ in range(2):
        with pytest.raises(ConnectorError, match="HTTP 503"):
            await collect(connector)
    assert len(exports(splunk)) == 2 * 3  # every read tried three times
    with pytest.raises(ConnectorError, match="circuit breaker"):
        await collect(connector)
    assert len(exports(splunk)) == 6  # the open breaker sent nothing
