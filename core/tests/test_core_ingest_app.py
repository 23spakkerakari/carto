"""ingest-api (spec 5.3, 8.5, 12, 14.12): accepts the contract models only, claims every batch
in the ledger before writing it (ADR 0034), refuses implausible observed_at values (ADR 0036),
answers RFC 7807 problems without echoing input, caps bodies, takes zstd, records heartbeats,
exposes metrics, echoes request ids and never logs a body."""

from __future__ import annotations

import asyncio
import io
import json
import re
import threading
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import zstandard
from fastapi import FastAPI
from fastapi.routing import APIRoute

with warnings.catch_warnings():
    # starlette 1.7 deprecates httpx (vs httpx2) under its TestClient at import time; the
    # workspace pins httpx and adding httpx2 is a founder decision, so the one-off is scoped.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

import carto_core.ingest.app as ingest_app_module
from carto_common.ids import ULID_PATTERN, new_ulid
from carto_common.logging import configure_logging
from carto_common.mtls import ROLES, ActAsRole
from carto_common.settings import RetentionSettings
from carto_core.db.clickhouse import WriterError, WriteResult
from carto_core.ingest.app import (
    BUSY_RETRY_AFTER_SECONDS,
    MAX_RANGE_ERRORS,
    PROBLEM_CONTENT_TYPE,
    ROUTE_ROLES,
    ZSTD_MAX_WINDOW_BYTES,
    Problem,
    create_app,
    decompress_capped,
)
from carto_core.ingest.health import InMemorySourceHealthStore
from carto_core.ingest.ledger import (
    ClaimState,
    InMemoryBatchLedger,
    LedgerEntry,
    LedgerError,
)
from carto_core.settings import MIB, CoreSettings, IngestListenerSettings
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch, SourceHeartbeat, SourceStatus

NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
KIB = 1024
JSON = {"content-type": "application/json"}
EXAMPLE_TOKENS = [identifier.token for identifier in CanonicalEvent.example().identifiers]


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[list[CanonicalEvent]] = []
        self.fail = False
        self.permanent = False
        self.ready = True

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        if self.fail:
            raise WriterError("ClickHouse insert failed: DatabaseError", permanent=self.permanent)
        self.batches.append(list(events))
        return WriteResult(len(events), sum(len(event.identifiers) for event in events))

    def ping(self) -> bool:
        return self.ready


class FailingLedger(InMemoryBatchLedger):
    """Claims work; marking the batch done fails."""

    def complete(self, entry: LedgerEntry, done_at: datetime) -> None:
        raise LedgerError("ledger unavailable: OperationalError")


class DownLedger(InMemoryBatchLedger):
    def claim(self, entry: LedgerEntry, stale_before: datetime) -> Any:
        raise LedgerError("ledger claim failed: OperationalError")


@dataclass
class World:
    client: TestClient
    writer: FakeWriter
    ledger: InMemoryBatchLedger
    health: InMemorySourceHealthStore


def _settings(**ingest: Any) -> CoreSettings:
    return CoreSettings(ingest=IngestListenerSettings(**ingest))


def _world(
    settings: CoreSettings | None = None, ledger: InMemoryBatchLedger | None = None
) -> World:
    writer = FakeWriter()
    ledger = ledger if ledger is not None else InMemoryBatchLedger()
    health = InMemorySourceHealthStore()
    app = create_app(settings or _settings(), writer, ledger, health, clock=lambda: NOW)
    return World(TestClient(ActAsRole(app, "edge-gateway")), writer, ledger, health)


@pytest.fixture
def world() -> World:
    return _world()


def _event(index: int = 0, observed_at: datetime | None = None) -> CanonicalEvent:
    suffix = "ABCDEFGHJKMNPQRSTVWXYZ"[index]
    data = CanonicalEvent.example().model_dump() | {
        "event_id": f"01J9ZK8X5Q8V3N6M2T4R7W1Y0{suffix}"
    }
    if observed_at is not None:
        data["observed_at"] = observed_at
    return CanonicalEvent.model_validate(data)


def _payload(events: Sequence[CanonicalEvent] | None = None, **overrides: Any) -> dict[str, Any]:
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=NOW,
        events=list(events) if events is not None else [_event()],
    )
    data: dict[str, Any] = json.loads(batch.model_dump_json())
    return data | overrides


def _heartbeat(**overrides: Any) -> dict[str, Any]:
    heartbeat = SourceHeartbeat(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        sent_at=NOW,
        status=SourceStatus.DEGRADED,
        last_success_at=NOW,
        lag_seconds=12.5,
        error_count=2,
        buffer_depth=40,
        oldest_buffered_at=None,
        message="splunk export slow",
    )
    data: dict[str, Any] = json.loads(heartbeat.model_dump_json())
    return data | overrides


def _post(world: World, path: str, payload: dict[str, Any], **kwargs: Any) -> Any:
    return world.client.post(path, content=json.dumps(payload).encode(), headers=JSON, **kwargs)


def _assert_problem(response: Any, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert body["title"]
    assert body["request_id"] == response.headers["x-request-id"]
    return body


# --- accepting batches ---------------------------------------------------------------------------


def test_valid_batch_is_accepted_and_written_once(world: World) -> None:
    payload = _payload([_event(0), _event(1)])
    response = _post(world, "/internal/ingest", payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2, "duplicate": False}
    assert response.headers["content-type"].startswith("application/json")
    assert len(world.writer.batches) == 1
    assert [event.event_id for event in world.writer.batches[0]] == [
        payload["events"][0]["event_id"],
        payload["events"][1]["event_id"],
    ]
    (entry,) = world.ledger.entries
    assert entry == LedgerEntry("default", payload["batch_id"], "src_wms_db", 2, NOW)
    assert world.ledger.state_of("default", payload["batch_id"]) == "done"


def test_same_batch_id_twice_is_a_duplicate_and_not_rewritten(world: World) -> None:
    payload = _payload()
    assert _post(world, "/internal/ingest", payload).status_code == 200
    again = _post(world, "/internal/ingest", payload)
    assert again.status_code == 200
    assert again.json() == {"accepted": 1, "duplicate": True}
    assert len(world.writer.batches) == 1
    assert len(world.ledger.entries) == 1
    metrics = world.client.get("/metrics").text
    assert "carto_ingest_duplicates_total 1\n" in metrics
    assert "carto_ingest_batches_total 1\n" in metrics


def _claim(ledger: InMemoryBatchLedger, batch_id: str, at: datetime) -> None:
    entry = LedgerEntry("default", batch_id, "src_wms_db", 1, at)
    assert ledger.claim(entry, at - timedelta(seconds=120)).state is ClaimState.CLAIMED


def test_a_retry_while_the_first_delivery_writes_is_503_and_writes_nothing() -> None:
    """ADR 0034 item 1: a fresh pending claim means another delivery is writing the batch."""
    ledger = InMemoryBatchLedger()
    world = _world(ledger=ledger)
    payload = _payload()
    _claim(ledger, payload["batch_id"], NOW - timedelta(seconds=30))
    response = _post(world, "/internal/ingest", payload)
    _assert_problem(response, 503)
    assert response.headers["retry-after"] == str(BUSY_RETRY_AFTER_SECONDS)
    assert world.writer.batches == []
    assert ledger.state_of("default", payload["batch_id"]) == "pending"
    assert "carto_ingest_claims_busy_total 1\n" in world.client.get("/metrics").text


def test_a_stale_claim_is_taken_over_and_the_batch_written() -> None:
    """ADR 0034 item 1: a claim older than the claim timeout belongs to a dead writer."""
    ledger = InMemoryBatchLedger()
    world = _world(_settings(claim_timeout_seconds=60), ledger=ledger)
    payload = _payload()
    _claim(ledger, payload["batch_id"], NOW - timedelta(seconds=61))
    response = _post(world, "/internal/ingest", payload)
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 1, "duplicate": False}
    assert len(world.writer.batches) == 1
    assert ledger.state_of("default", payload["batch_id"]) == "done"
    assert "carto_ingest_claims_taken_over_total 1\n" in world.client.get("/metrics").text


def test_a_failed_write_releases_the_claim_so_the_retry_is_taken_at_once(world: World) -> None:
    payload = _payload()
    world.writer.fail = True
    response = _post(world, "/internal/ingest", payload)
    body = _assert_problem(response, 503)
    assert "retry" in body["detail"]
    assert response.headers["retry-after"] == "1"
    assert world.ledger.state_of("default", payload["batch_id"]) is None
    world.writer.fail = False
    retry = _post(world, "/internal/ingest", payload)
    assert retry.status_code == 200
    assert retry.json() == {"accepted": 1, "duplicate": False}
    assert len(world.writer.batches) == 1


def test_clickhouse_refusing_the_data_is_a_422_so_the_edge_parks_the_batch(world: World) -> None:
    """ADR 0036 item 2: a permanent error would fail every retry and stall the source."""
    payload = _payload()
    world.writer.fail = world.writer.permanent = True
    body = _assert_problem(_post(world, "/internal/ingest", payload), 422)
    assert body["title"] == "event store refused the batch"
    assert world.ledger.state_of("default", payload["batch_id"]) is None
    assert "carto_ingest_refused_total 1\n" in world.client.get("/metrics").text


def test_a_ledger_failure_after_the_write_is_503_and_the_retry_writes_again() -> None:
    """The replacing tables merge the second copy (ADR 0034)."""
    world = _world(ledger=FailingLedger())
    payload = _payload()
    response = _post(world, "/internal/ingest", payload)
    _assert_problem(response, 503)
    assert response.headers["retry-after"] == "1"
    assert len(world.writer.batches) == 1
    assert world.ledger.entries == ()


def test_an_unreachable_ledger_is_503_before_anything_is_written() -> None:
    world = _world(ledger=DownLedger())
    response = _post(world, "/internal/ingest", _payload())
    _assert_problem(response, 503)
    assert response.headers["retry-after"] == "1"
    assert world.writer.batches == []


# --- the plausible observed_at range (ADR 0036) ------------------------------------------------


def test_events_at_the_ends_of_the_plausible_range_are_accepted(world: World) -> None:
    lower, upper = RetentionSettings().observed_at_range(NOW)
    payload = _payload([_event(0, lower), _event(1, upper)])
    response = _post(world, "/internal/ingest", payload)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("offset", "kind"),
    [
        (-timedelta(days=31, milliseconds=1), "observed_at_too_old"),
        (timedelta(days=1, milliseconds=1), "observed_at_future"),
        (datetime(1970, 1, 1, tzinfo=UTC) - NOW, "observed_at_too_old"),
        (datetime(2099, 1, 1, tzinfo=UTC) - NOW, "observed_at_future"),
    ],
)
def test_an_event_outside_the_range_refuses_the_batch_with_422(
    world: World, offset: timedelta, kind: str
) -> None:
    payload = _payload([_event(0), _event(1, NOW + offset)])
    response = _post(world, "/internal/ingest", payload)
    body = _assert_problem(response, 422)
    assert body["errors"] == [
        {
            "loc": ["events", 1, "observed_at"],
            "msg": "observed_at is outside the plausible range",
            "type": kind,
        }
    ]
    assert payload["events"][1]["observed_at"][:10] not in response.text, "the time is not echoed"
    assert world.writer.batches == []
    assert world.ledger.rows == (), "refused before the claim"
    assert "carto_ingest_out_of_range_total 1\n" in world.client.get("/metrics").text


def test_the_range_follows_the_configured_retention() -> None:
    settings = CoreSettings(retention=RetentionSettings(events_days=7))
    world = _world(settings)
    response = _post(world, "/internal/ingest", _payload([_event(0, NOW - timedelta(days=9))]))
    _assert_problem(response, 422)


def test_a_422_lists_a_bounded_number_of_out_of_range_events(world: World) -> None:
    old = NOW - timedelta(days=400)
    payload = _payload([_event(index, old) for index in range(MAX_RANGE_ERRORS + 2)])
    body = _assert_problem(_post(world, "/internal/ingest", payload), 422)
    assert len(body["errors"]) == MAX_RANGE_ERRORS
    assert body["detail"].startswith(f"{MAX_RANGE_ERRORS + 2} events")


# --- rejecting what is not the contract --------------------------------------------------


def test_events_of_another_source_are_rejected_without_echoing_input(world: World) -> None:
    payload = _payload()
    payload["events"][0]["source_id"] = "src_other"
    response = _post(world, "/internal/ingest", payload)
    body = _assert_problem(response, 422)
    assert body["errors"], body
    assert all(set(error) == {"loc", "msg", "type"} for error in body["errors"])
    text = response.text
    assert "input" not in json.dumps(body["errors"])
    for token in EXAMPLE_TOKENS:
        assert token not in text
    assert "CREATED" not in text and "DC-03" not in text
    assert world.writer.batches == []
    assert world.ledger.entries == ()


def test_a_raw_value_in_an_identifier_slot_is_rejected(world: World) -> None:
    payload = _payload()
    payload["events"][0]["identifiers"][0]["token"] = "SO-0004471"  # noqa: S105 - planted
    body = _assert_problem(_post(world, "/internal/ingest", payload), 422)
    assert "SO-0004471" not in json.dumps(body)
    assert world.writer.batches == []


def test_unknown_keys_and_empty_batches_are_rejected(world: World) -> None:
    _assert_problem(_post(world, "/internal/ingest", _payload(extra="x")), 422)
    _assert_problem(_post(world, "/internal/ingest", _payload() | {"events": []}), 422)


def test_malformed_json_is_a_400_problem(world: World) -> None:
    response = world.client.post("/internal/ingest", content=b"{not json", headers=JSON)
    body = _assert_problem(response, 400)
    assert "not json" not in json.dumps(body)


def test_empty_body_is_a_400_problem(world: World) -> None:
    _assert_problem(world.client.post("/internal/ingest", content=b"", headers=JSON), 400)


def test_other_tenant_is_refused(world: World) -> None:
    payload = _payload()
    payload["tenant_id"] = "other"
    payload["events"][0]["tenant_id"] = "other"
    _assert_problem(_post(world, "/internal/ingest", payload), 403)
    assert world.writer.batches == []


def test_wrong_media_type_and_unsupported_encoding_are_415(world: World) -> None:
    raw = json.dumps(_payload()).encode()
    _assert_problem(
        world.client.post("/internal/ingest", content=raw, headers={"content-type": "text/plain"}),
        415,
    )
    _assert_problem(
        world.client.post(
            "/internal/ingest", content=raw, headers=JSON | {"content-encoding": "gzip"}
        ),
        415,
    )


# --- zstd and size caps (spec 8.5, 2.3 invariant 8) --------------------------------------------


def test_zstd_body_is_accepted(world: World) -> None:
    payload = _payload()
    compressed = zstandard.ZstdCompressor(level=3).compress(json.dumps(payload).encode())
    response = world.client.post(
        "/internal/ingest", content=compressed, headers=JSON | {"content-encoding": "zstd"}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 1, "duplicate": False}
    assert len(world.writer.batches) == 1


def test_oversized_decompressed_body_is_413() -> None:
    world = _world(_settings(max_body_bytes=64 * KIB, max_decompressed_bytes=64 * KIB))
    bomb = zstandard.ZstdCompressor(level=3).compress(b"[" + b"0," * (200 * KIB))
    assert len(bomb) < 64 * KIB
    response = world.client.post(
        "/internal/ingest", content=bomb, headers=JSON | {"content-encoding": "zstd"}
    )
    _assert_problem(response, 413)
    assert world.writer.batches == []


def test_oversized_wire_body_is_413() -> None:
    world = _world(_settings(max_body_bytes=64 * KIB, max_decompressed_bytes=64 * KIB))
    response = world.client.post("/internal/ingest", content=b"0" * (70 * KIB), headers=JSON)
    _assert_problem(response, 413)


def test_malformed_zstd_is_a_400_problem(world: World) -> None:
    response = world.client.post(
        "/internal/ingest",
        content=b"\x28\xb5\x2f\xfd garbage",
        headers=JSON | {"content-encoding": "zstd"},
    )
    _assert_problem(response, 400)


def _large_window_frame() -> bytes:
    """A few dozen bytes whose header asks for a 128 MiB window: decompressing it allocates
    the window before a byte comes out (the review measured +129 MiB per request)."""
    params = zstandard.ZstdCompressionParameters(window_log=27, enable_ldm=True)
    buffer = io.BytesIO()
    with zstandard.ZstdCompressor(compression_params=params).stream_writer(
        buffer, closefd=False
    ) as writer:
        writer.write(b"{" + b" " * 1000)
    frame = buffer.getvalue()
    assert zstandard.get_frame_parameters(frame).window_size == 128 * MIB
    return frame


def test_a_frame_asking_for_a_window_above_8_mib_is_refused() -> None:
    with pytest.raises(Problem) as info:
        decompress_capped(_large_window_frame(), 16 * MIB)
    assert info.value.status == 400
    assert ZSTD_MAX_WINDOW_BYTES == 8 * MIB


def test_a_default_level_frame_is_well_inside_the_window_cap() -> None:
    data = json.dumps(_payload()).encode() * 2000
    frame = zstandard.ZstdCompressor(level=3).compress(data)
    assert decompress_capped(frame, 64 * MIB) == data


@pytest.mark.parametrize(
    "trailer",
    [
        zstandard.ZstdCompressor(level=3).compress(b'{"forged": true}'),  # a second frame
        b"junk",  # bytes after the frame
        b"\x50\x2a\x4d\x18\x04\x00\x00\x00skip",  # a skippable frame
    ],
)
def test_a_body_with_anything_after_its_one_frame_is_a_400(world: World, trailer: bytes) -> None:
    """The edge sends one frame. Core read only the first and ignored the rest, while the edge
    reads across frames: the two must agree, so anything after the frame is refused."""
    frame = zstandard.ZstdCompressor(level=3).compress(json.dumps(_payload()).encode())
    response = world.client.post(
        "/internal/ingest", content=frame + trailer, headers=JSON | {"content-encoding": "zstd"}
    )
    _assert_problem(response, 400)
    assert world.writer.batches == []


def test_a_frame_with_a_checksum_and_unknown_size_is_accepted() -> None:
    data = json.dumps(_payload()).encode()
    buffer = io.BytesIO()
    compressor = zstandard.ZstdCompressor(level=3, write_checksum=True)
    with compressor.stream_writer(buffer, closefd=False) as writer:
        writer.write(data)
    assert decompress_capped(buffer.getvalue(), MIB) == data
    with pytest.raises(Problem):
        decompress_capped(buffer.getvalue()[:-1], MIB)


def test_decompression_and_validation_run_off_the_event_loop(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 5 MB batch takes about a second to decompress and validate; on the event loop it
    stalls every other request, health checks included."""
    seen: list[bool] = []
    original = ingest_app_module._decode

    def recording(*args: Any, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            seen.append(False)
        else:
            seen.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest_app_module, "_decode", recording)
    compressed = zstandard.ZstdCompressor(level=3).compress(json.dumps(_payload()).encode())
    response = world.client.post(
        "/internal/ingest", content=compressed, headers=JSON | {"content-encoding": "zstd"}
    )
    assert response.status_code == 200, response.text
    assert _post(world, "/internal/heartbeat", _heartbeat()).status_code == 200
    assert seen == [False, False], "decoded on the event loop"


class BlockingWriter(FakeWriter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        self.entered.set()
        assert self.release.wait(10)
        return super().write_events(events)


def test_batches_in_flight_are_bounded_and_the_rest_get_429() -> None:
    writer = BlockingWriter()
    settings = _settings(max_inflight_batches=1)
    app = create_app(
        settings, writer, InMemoryBatchLedger(), InMemorySourceHealthStore(), clock=lambda: NOW
    )
    client = TestClient(ActAsRole(app, "edge-gateway"))
    first: dict[str, Any] = {}

    def send_first() -> None:
        first["response"] = client.post(
            "/internal/ingest", content=json.dumps(_payload()).encode(), headers=JSON
        )

    thread = threading.Thread(target=send_first)
    thread.start()
    try:
        assert writer.entered.wait(10)
        refused = client.post(
            "/internal/ingest", content=json.dumps(_payload()).encode(), headers=JSON
        )
        body = _assert_problem(refused, 429)
        assert refused.headers["retry-after"] == "1"
        assert "retry" in body["detail"]
    finally:
        writer.release.set()
        thread.join(10)
    assert first["response"].status_code == 200
    again = client.post("/internal/ingest", content=json.dumps(_payload()).encode(), headers=JSON)
    assert again.status_code == 200, again.text


# --- heartbeats --------------------------------------------------------------------------------


def test_heartbeat_is_stored(world: World) -> None:
    response = _post(world, "/internal/heartbeat", _heartbeat())
    assert response.status_code == 200, response.text
    assert response.json() == {"recorded": True}
    record = world.health.get("default", "src_wms_db")
    assert record is not None
    assert record.status == "degraded"
    assert record.lag_seconds == 12.5
    assert record.error_count == 2
    assert record.buffer_depth == 40
    assert record.oldest_buffered_at is None
    assert record.message == "splunk export slow"
    assert record.received_at == NOW
    assert "carto_heartbeats_total 1\n" in world.client.get("/metrics").text


def test_heartbeat_upsert_keeps_the_latest(world: World) -> None:
    _post(world, "/internal/heartbeat", _heartbeat())
    _post(world, "/internal/heartbeat", _heartbeat(status="ok", error_count=0))
    record = world.health.get("default", "src_wms_db")
    assert record is not None and record.status == "ok" and record.error_count == 0


def test_an_older_heartbeat_never_overwrites_a_newer_one(world: World) -> None:
    """Two heartbeats in flight can commit out of order: the one the edge sent last wins."""
    newer = _heartbeat(status="ok", error_count=0, sent_at="2026-10-08T11:59:30.000Z")
    older = _heartbeat(status="failing", error_count=9, sent_at="2026-10-08T11:58:30.000Z")
    assert _post(world, "/internal/heartbeat", newer).status_code == 200
    assert _post(world, "/internal/heartbeat", older).status_code == 200
    record = world.health.get("default", "src_wms_db")
    assert record is not None
    assert (record.status, record.error_count) == ("ok", 0)
    assert record.sent_at == datetime(2026, 10, 8, 11, 59, 30, tzinfo=UTC)


def test_a_heartbeat_from_the_future_is_ordered_by_its_arrival(world: World) -> None:
    """An edge clock running ahead cannot pin the row: sent_at is capped at the arrival time."""
    ahead = _heartbeat(status="failing", sent_at="2027-01-01T00:00:00.000Z")
    assert _post(world, "/internal/heartbeat", ahead).status_code == 200
    record = world.health.get("default", "src_wms_db")
    assert record is not None and record.sent_at == NOW
    assert _post(world, "/internal/heartbeat", _heartbeat(status="ok")).status_code == 200
    record = world.health.get("default", "src_wms_db")
    assert record is not None and record.status == "ok"


def test_invalid_heartbeat_is_a_422_problem(world: World) -> None:
    _assert_problem(_post(world, "/internal/heartbeat", _heartbeat(lag_seconds=-1)), 422)
    _assert_problem(_post(world, "/internal/heartbeat", _heartbeat(tenant_id="other")), 403)
    assert world.health.get("default", "src_wms_db") is None


# --- health, readiness, metrics, request ids ---------------------------------------------------


def test_healthz_and_readyz(world: World) -> None:
    assert world.client.get("/healthz").json() == {"status": "ok"}
    ready = world.client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    world.writer.ready = False
    not_ready = world.client.get("/readyz")
    assert not_ready.status_code == 503
    assert not_ready.json() == {"status": "unavailable"}, "no detail in the body (spec 12)"


def test_metrics_are_prometheus_text(world: World) -> None:
    _post(world, "/internal/ingest", _payload([_event(0), _event(1), _event(2)]))
    _post(world, "/internal/ingest", _payload() | {"events": []})
    response = world.client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    for name in (
        "carto_ingest_batches_total",
        "carto_ingest_events_total",
        "carto_ingest_duplicates_total",
        "carto_ingest_errors_total",
        "carto_heartbeats_total",
    ):
        assert f"# TYPE {name} counter\n" in text
        assert f"# HELP {name} " in text
    assert "carto_ingest_batches_total 1\n" in text
    assert "carto_ingest_events_total 3\n" in text
    assert "carto_ingest_errors_total 1\n" in text


def test_request_id_is_echoed_or_generated(world: World) -> None:
    echoed = world.client.get("/healthz", headers={"x-request-id": "edge-7f3a.42"})
    assert echoed.headers["x-request-id"] == "edge-7f3a.42"
    generated = world.client.get("/healthz")
    assert ULID_PATTERN.fullmatch(generated.headers["x-request-id"])
    replaced = world.client.get("/healthz", headers={"x-request-id": "bad id!"})
    assert ULID_PATTERN.fullmatch(replaced.headers["x-request-id"])
    too_long = world.client.get("/healthz", headers={"x-request-id": "a" * 200})
    assert ULID_PATTERN.fullmatch(too_long.headers["x-request-id"])


def test_unknown_routes_and_methods_are_problems(world: World) -> None:
    _assert_problem(world.client.get("/nope"), 404)
    _assert_problem(world.client.get("/internal/ingest"), 405)


def test_no_interactive_docs_on_the_internal_api(world: World) -> None:
    assert world.client.get("/docs").status_code == 404
    assert world.client.get("/openapi.json").status_code == 404


# --- spec 2.3 invariant 7: bodies are never logged --------------------------------------------


def test_request_bodies_and_event_contents_never_reach_the_logs() -> None:
    buffer = io.StringIO()
    configure_logging("ingest-api-test", level="debug", stream=buffer)
    world = _world()
    payload = _payload()
    payload["events"][0]["attributes"]["status"] = "LEAKCHECKVALUE"
    assert _post(world, "/internal/ingest", payload).status_code == 200
    payload["events"][0]["source_id"] = "src_other"
    assert _post(world, "/internal/ingest", payload).status_code == 422
    world.writer.fail = True
    assert _post(world, "/internal/ingest", _payload()).status_code == 503
    logs = buffer.getvalue()
    assert "batch" in logs, "something was logged"
    assert "LEAKCHECKVALUE" not in logs
    assert "DC-03" not in logs
    for token in EXAMPLE_TOKENS:
        assert token not in logs
    assert not re.search(r"\"(events|identifiers|attributes)\"\s*:\s*[\[{]", logs), (
        "a serialized body reached the logs"
    )


# ---------------------------------------------------------------------------------------------
# Service roles (ADR 0031)
# ---------------------------------------------------------------------------------------------


def _inner_app(client: TestClient) -> FastAPI:
    """The FastAPI app behind a test client's role wrapper."""
    app = client.app
    inner = app.app if isinstance(app, ActAsRole) else app
    assert isinstance(inner, FastAPI)
    return inner


def test_every_ingest_route_lists_its_roles(world: World) -> None:
    inner = _inner_app(world.client)
    routes = {
        (method, route.path)
        for route in inner.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert routes == set(ROUTE_ROLES)


@pytest.mark.parametrize("path", ["/internal/ingest", "/internal/heartbeat"])
def test_only_the_edge_gateway_may_send_batches_and_heartbeats(world: World, path: str) -> None:
    inner = _inner_app(world.client)
    for role in [*sorted(ROLES - {"edge-gateway"}), None]:
        response = TestClient(ActAsRole(inner, role)).post(path, content=b"{}")
        assert response.status_code == 403, role
        assert response.json()["title"] == "forbidden"
    assert world.writer is not None
    metrics = inner.state.metrics
    assert metrics.get("carto_ingest_mtls_refused_total") == len(ROLES)


def test_ingest_health_accepts_any_install_certificate(world: World) -> None:
    inner = _inner_app(world.client)
    client = TestClient(ActAsRole(inner, None))
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
