"""carto_edge.pipeline.forward: the mutual-TLS core client, the forwarder's delivery rules
(spec 8.5: zstd batches, idempotent by batch id, 2xx acks, permanent 4xx parks, 429, 5xx and
transport errors back off with jitter up to the cap), source health and heartbeats (spec 8.1
common requirements, 12 internal endpoints), and that no body ever reaches a log line."""

from __future__ import annotations

import json
import threading
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog

with warnings.catch_warnings():
    # starlette 1.7 deprecates httpx (vs httpx2) under its TestClient at import time; the
    # workspace pins httpx (see core/tests/test_core_ingest_app.py), so the one-off is scoped.
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_common.ids import is_ulid, new_ulid
from carto_common.mtls import ActAsRole
from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_core.db.clickhouse import WriteResult
from carto_core.ingest.app import create_app
from carto_core.ingest.health import InMemorySourceHealthStore
from carto_core.ingest.ledger import InMemoryBatchLedger
from carto_core.settings import CoreSettings
from carto_edge.config import CoreLinkSettings, SourceConfig, SourcesFile, SourceType, SystemConfig
from carto_edge.metrics import EdgeMetrics, default_metrics
from carto_edge.net.http import build_ssl_context
from carto_edge.pipeline import forward as forward_module
from carto_edge.pipeline.buffer import DiskBuffer, compress_batch
from carto_edge.pipeline.forward import (
    HEARTBEAT_PATH,
    INGEST_PATH,
    PARK_STATUSES,
    PARKED_PURGE_SECONDS,
    Forwarder,
    Heartbeater,
    SendReport,
    SourceHealth,
    build_core_client,
)
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch, SourceHeartbeat, SourceStatus

NOW = datetime(2026, 10, 10, 9, 0, 0, tzinfo=UTC)
CORE_URL = "https://ingest.internal:8443"
MARKER = "c-MARKER-77123"


def batch(events: int = 2, source_id: str = "src_wms_db") -> IngestBatch:
    example = CanonicalEvent.example().model_dump()
    example["attributes"] = {"status": MARKER}
    items = [
        CanonicalEvent.model_validate(
            example | {"event_id": f"01J9ZK8X5Q8V3N6M2T4R7W1Y{i:02d}", "source_id": source_id}
        )
        for i in range(events)
    ]
    return IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id=source_id,
        batch_id=new_ulid(),
        sent_at=NOW,
        events=items,
    )


@pytest.fixture
def buffer(tmp_path: Path) -> Iterator[DiskBuffer]:
    with DiskBuffer(tmp_path / "buffer.sqlite", max_bytes=10**9, backpressure_ratio=0.8) as buf:
        yield buf


Handler = Callable[[httpx.Request], httpx.Response]


def client_for(handler: Handler) -> httpx.Client:
    return build_core_client(CoreLinkSettings(url=CORE_URL), transport=httpx.MockTransport(handler))


def forwarder(
    buf: DiskBuffer,
    handler: Handler,
    metrics: EdgeMetrics | None = None,
    sleeps: list[float] | None = None,
) -> Forwarder:
    recorded = sleeps if sleeps is not None else []
    return Forwarder(
        buf,
        client_for(handler),
        metrics=metrics if metrics is not None else default_metrics(),
        clock=lambda: NOW,
        sleep=recorded.append,
        retry_max_seconds=30.0,
        rng=lambda: 0.5,  # jitter factor 1.0: the nominal exponential delay
    )


# ---------------------------------------------------------------------------------------------
# build_core_client
# ---------------------------------------------------------------------------------------------


def test_core_client_requires_a_url() -> None:
    with pytest.raises(ValueError, match=r"core\.url"):
        build_core_client(CoreLinkSettings())


def test_core_client_settings() -> None:
    client = build_core_client(
        CoreLinkSettings(url=CORE_URL, timeout_seconds=12.0),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    )
    with client:
        assert str(client.base_url).rstrip("/") == CORE_URL
        assert client.headers["User-Agent"] == "carto-edge"
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.timeout.connect == 12.0
        assert client.timeout.read == 12.0


def test_core_client_builds_mutual_tls_from_the_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca = create_ca()
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca.certificate_pem)
    write_pem(
        issue_certificate(ca, "edge-gateway", server=False),
        tmp_path / "edge.pem",
        tmp_path / "edge.key",
    )
    link = CoreLinkSettings(
        url=CORE_URL,
        ca_file=ca_file,
        cert_file=tmp_path / "edge.pem",
        key_file=tmp_path / "edge.key",
    )
    calls: list[dict[str, Any]] = []
    real = build_ssl_context

    def spy(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(forward_module, "build_ssl_context", spy)
    with build_core_client(link):
        pass
    assert calls == [
        {
            "ca_file": ca_file,
            "client_cert": (tmp_path / "edge.pem", tmp_path / "edge.key"),
            "verify": True,
        }
    ]


def test_core_client_refuses_half_a_client_certificate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cert_file and"):
        build_core_client(CoreLinkSettings(url=CORE_URL, cert_file=tmp_path / "edge.pem"))


# ---------------------------------------------------------------------------------------------
# Forwarder
# ---------------------------------------------------------------------------------------------


def test_idle_when_the_buffer_is_empty(buffer: DiskBuffer) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing to send")

    assert forwarder(buffer, handler).send_once() == SendReport(idle=True)


def test_2xx_acks_the_row_and_sends_the_stored_payload(buffer: DiskBuffer) -> None:
    sent = batch()
    buffer.append(sent)
    stored = buffer.oldest()
    assert stored is not None
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"accepted": 2, "duplicate": False})

    metrics = default_metrics()
    report = forwarder(buffer, handler, metrics).send_once()
    assert report == SendReport(sent=True, status=200, batch_id=sent.batch_id)
    assert buffer.depth() == 0
    assert metrics.get("carto_edge_batches_sent_total") == 1
    (request,) = seen
    assert request.method == "POST"
    assert request.url == httpx.URL(CORE_URL + INGEST_PATH)
    assert request.headers["content-type"] == "application/json"
    assert request.headers["content-encoding"] == "zstd"
    assert request.headers["x-request-id"] == sent.batch_id
    assert request.content == stored.payload


@pytest.mark.parametrize("status", sorted(PARK_STATUSES))
def test_permanent_refusals_park_the_row(buffer: DiskBuffer, status: int) -> None:
    sent = batch()
    buffer.append(sent)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"title": "refused", "detail": MARKER})

    metrics = default_metrics()
    with structlog.testing.capture_logs() as logs:
        report = forwarder(buffer, handler, metrics).send_once()
    assert report.parked
    assert report.status == status
    assert buffer.depth() == 0
    assert buffer.parked() == 1
    assert metrics.get("carto_edge_batches_parked_total") == 1
    warning = next(entry for entry in logs if entry["log_level"] == "warning")
    assert warning["status"] == status
    assert warning["batch_id"] == sent.batch_id
    assert warning["source_id"] == "src_wms_db"
    assert warning["events"] == 2
    assert MARKER not in repr(logs)


def test_429_honours_retry_after_up_to_the_cap(buffer: DiskBuffer) -> None:
    buffer.append(batch())
    answers = iter([("7", 429), ("9999", 429), (None, 429), ("soon", 429)])

    def handler(request: httpx.Request) -> httpx.Response:
        retry_after, status = next(answers)
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return httpx.Response(status, headers=headers)

    fwd = forwarder(buffer, handler)
    first = fwd.send_once()
    assert first.failed
    assert first.status == 429
    assert first.retry_after_seconds == 7.0
    assert fwd.send_once().retry_after_seconds == 30.0  # capped at retry_max_seconds
    third = fwd.send_once().retry_after_seconds
    assert third is not None
    assert 0 < third <= 30.0
    fourth = fwd.send_once().retry_after_seconds
    assert fourth is not None
    assert 0 < fourth <= 30.0
    assert buffer.depth() == 1  # nothing is lost while core throttles


def test_5xx_and_transport_errors_back_off_exponentially(buffer: DiskBuffer) -> None:
    buffer.append(batch())
    answers: list[int | Exception] = [
        503,
        500,
        httpx.ConnectError("connection refused"),
        502,
        503,
        503,
        503,
        200,
        503,
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer)

    metrics = default_metrics()
    fwd = forwarder(buffer, handler, metrics)
    delays = [fwd.send_once().retry_after_seconds for _ in range(7)]
    assert delays == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert metrics.get("carto_edge_send_errors_total") == 7
    assert fwd.send_once().sent
    buffer.append(batch())
    assert fwd.send_once().retry_after_seconds == 0.5  # success reset the backoff


def test_a_long_outage_keeps_the_backoff_at_the_cap(buffer: DiskBuffer) -> None:
    buffer.append(batch())
    fwd = forwarder(buffer, lambda _request: httpx.Response(503))
    for _ in range(1100):  # past 2**1024, where an unbounded exponent would overflow
        report = fwd.send_once()
    assert report.retry_after_seconds == 30.0


def test_transport_error_report_and_log_carry_no_payload(buffer: DiskBuffer) -> None:
    buffer.append(batch())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out reading {MARKER}")

    with structlog.testing.capture_logs() as logs:
        report = forwarder(buffer, handler).send_once()
    assert report.failed
    assert report.status is None
    assert any(entry.get("error") == "ReadTimeout" for entry in logs)
    assert MARKER not in repr(logs)


@pytest.mark.parametrize("status", [404, 408])
def test_unexpected_4xx_is_retried_not_parked(buffer: DiskBuffer, status: int) -> None:
    """408 is core's body deadline (ADR 0073): the batch is fine, the connection was not, so
    it is sent again after a backoff like any status outside PARK_STATUSES."""
    buffer.append(batch())
    report = forwarder(buffer, lambda _request: httpx.Response(status)).send_once()
    assert report.failed
    assert not report.parked
    assert report.status == status
    assert report.retry_after_seconds is not None and report.retry_after_seconds > 0
    assert buffer.depth() == 1
    assert buffer.parked() == 0
    assert status not in PARK_STATUSES


def test_run_drains_the_buffer_and_stops_promptly(buffer: DiskBuffer) -> None:
    for _ in range(3):
        buffer.append(batch())
    metrics = default_metrics()
    fwd = Forwarder(
        buffer,
        client_for(lambda _request: httpx.Response(200, json={"accepted": 2})),
        metrics=metrics,
    )
    stop = threading.Event()
    thread = threading.Thread(target=fwd.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while buffer.depth() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert buffer.depth() == 0
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert metrics.get("carto_edge_batches_sent_total") == 3
    assert metrics.get("carto_edge_buffer_depth") == 0
    assert metrics.get("carto_edge_buffer_oldest_age_seconds") == 0


def test_gauges_report_the_oldest_pending_batch(buffer: DiskBuffer) -> None:
    buffer.append(batch())  # created at NOW
    metrics = default_metrics()
    fwd = Forwarder(
        buffer,
        client_for(lambda _request: httpx.Response(503)),
        metrics=metrics,
        clock=lambda: NOW + timedelta(seconds=42),
    )
    fwd.refresh_gauges()
    assert metrics.get("carto_edge_buffer_depth") == 1
    assert metrics.get("carto_edge_buffer_bytes") == buffer.bytes()
    assert metrics.get("carto_edge_buffer_oldest_age_seconds") == 42


def test_drain_sends_until_idle_and_sleeps_between_failures(buffer: DiskBuffer) -> None:
    buffer.append(batch())
    buffer.append(batch())
    answers = [503, 200, 200]
    sleeps: list[float] = []
    fwd = forwarder(buffer, lambda _request: httpx.Response(answers.pop(0)), sleeps=sleeps)
    assert fwd.drain(max_failures=3) == 2
    assert sleeps == [0.5]
    assert buffer.depth() == 0


def test_drain_gives_up_after_consecutive_failures(buffer: DiskBuffer) -> None:
    buffer.append(batch())
    sleeps: list[float] = []
    fwd = forwarder(buffer, lambda _request: httpx.Response(503), sleeps=sleeps)
    assert fwd.drain(max_failures=2) == 0
    assert len(sleeps) == 1
    assert buffer.depth() == 1


# --- parked batches expire (ADR 0073) ----------------------------------------------------------


def _park_at(buf: DiskBuffer, parked_at: datetime) -> IngestBatch:
    """A batch appended and parked at ``parked_at`` (the purge ages parked rows from when
    they were parked, ADR 0073)."""
    stored = batch()
    row_id = buf.append_bytes(
        compress_batch(stored),
        source_id=stored.source_id,
        batch_id=stored.batch_id,
        events=len(stored.events),
        created_at=parked_at,
    )
    assert buf.park(row_id, at=parked_at, reason="http_422")
    return stored


def test_parked_batches_older_than_the_age_are_purged_and_younger_ones_stay(
    buffer: DiskBuffer,
) -> None:
    """A persistent 422 parked every batch until the 20 GB buffer was full; parked rows now
    expire after ``parked_max_age_days``. One warning per pass, count and oldest id only."""
    old = _park_at(buffer, NOW - timedelta(days=7, seconds=1))
    older = _park_at(buffer, NOW - timedelta(days=30))
    young = _park_at(buffer, NOW - timedelta(days=6, hours=23))
    pending = batch()
    buffer.append(pending)
    bytes_before = buffer.bytes()
    purged_bytes = len(compress_batch(old)) + len(compress_batch(older))
    metrics = default_metrics()
    fwd = Forwarder(
        buffer,
        client_for(lambda _request: httpx.Response(503)),
        metrics=metrics,
        clock=lambda: NOW,
        parked_max_age_days=7,
    )
    with structlog.testing.capture_logs() as logs:
        assert fwd.purge_parked(force=True) == 2
    assert buffer.parked() == 1
    assert buffer.depth() == 1
    assert buffer.bytes() == bytes_before - purged_bytes
    assert metrics.get("carto_edge_parked_purged_total") == 2
    (warning,) = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warning["event"] == "forward.parked_purged"
    assert warning["purged"] == 2
    assert warning["oldest_batch_id"] == older.batch_id
    assert warning["max_age_days"] == 7
    assert MARKER not in repr(logs)
    assert young.batch_id not in repr(logs) and pending.batch_id not in repr(logs)
    # Nothing to purge: no warning, no count.
    with structlog.testing.capture_logs() as logs:
        assert fwd.purge_parked(force=True) == 0
    assert not [entry for entry in logs if entry["log_level"] == "warning"]
    assert metrics.get("carto_edge_parked_purged_total") == 2


def test_the_purge_runs_once_per_period_from_the_loop(buffer: DiskBuffer) -> None:
    _park_at(buffer, NOW - timedelta(days=8))
    ticks = {"now": 1000.0}
    fwd = Forwarder(
        buffer,
        client_for(lambda _request: httpx.Response(503)),
        metrics=default_metrics(),
        clock=lambda: NOW,
        parked_max_age_days=7,
        monotonic=lambda: ticks["now"],
    )
    assert fwd.purge_parked() == 1, "the first pass runs at once"
    _park_at(buffer, NOW - timedelta(days=8))
    assert fwd.purge_parked() == 0, "within the period: nothing runs"
    ticks["now"] += PARKED_PURGE_SECONDS
    assert fwd.purge_parked() == 1
    assert buffer.parked() == 0


def test_run_purges_parked_batches_past_the_age(buffer: DiskBuffer) -> None:
    _park_at(buffer, NOW - timedelta(days=8))
    metrics = default_metrics()
    fwd = Forwarder(
        buffer,
        client_for(lambda _request: httpx.Response(200, json={"accepted": 2})),
        metrics=metrics,
        clock=lambda: NOW,
        parked_max_age_days=7,
    )
    stop = threading.Event()
    thread = threading.Thread(target=fwd.run, args=(stop,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while buffer.parked() and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=2.0)
    assert buffer.parked() == 0
    assert metrics.get("carto_edge_parked_purged_total") == 1


def test_a_parked_batch_is_stamped_with_the_status_and_the_purge_records_it(
    buffer: DiskBuffer,
) -> None:
    """ADR 0073: the forwarder parks with its own clock and the status as the reason; the purge
    is aged from that time and leaves the batch in the buffer's purged ledger (id, events,
    reason, when; no payload), so the data loss is on file, not only in a log line."""
    sent = batch(events=3)
    buffer.append(sent)
    clock = {"now": NOW}
    metrics = default_metrics()
    fwd = Forwarder(
        buffer,
        client_for(lambda _request: httpx.Response(422, json={"title": "refused"})),
        metrics=metrics,
        clock=lambda: clock["now"],
        parked_max_age_days=7,
    )
    assert fwd.send_once().parked
    assert fwd.purge_parked(force=True) == 0, "parked just now"
    clock["now"] = NOW + timedelta(days=7)
    assert fwd.purge_parked(force=True) == 0, "exactly the age: not yet"
    clock["now"] = NOW + timedelta(days=7, seconds=1)
    with structlog.testing.capture_logs() as logs:
        assert fwd.purge_parked(force=True) == 1
    (entry,) = buffer.purged_ledger()
    assert (entry.batch_id, entry.events, entry.parked_reason) == (sent.batch_id, 3, "http_422")
    assert entry.purged_at == clock["now"]
    assert metrics.get("carto_edge_parked_purged_total") == 1
    assert MARKER not in repr(logs)


def test_the_parked_age_is_validated(buffer: DiskBuffer) -> None:
    client = client_for(lambda _request: httpx.Response(503))
    for days in (0, -1, 366):
        with pytest.raises(ValueError, match="parked_max_age_days"):
            Forwarder(buffer, client, metrics=default_metrics(), parked_max_age_days=days)
    assert Forwarder(buffer, client, metrics=default_metrics()).parked_max_age_days == 7


class FakeWriter:
    def __init__(self) -> None:
        self.events: list[CanonicalEvent] = []

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        self.events.extend(events)
        return WriteResult(len(events), sum(len(event.identifiers) for event in events))

    def ping(self) -> bool:
        return True


def test_ingest_api_accepts_what_the_forwarder_sends(buffer: DiskBuffer) -> None:
    writer = FakeWriter()
    health = InMemorySourceHealthStore()
    app = create_app(CoreSettings(), writer, InMemoryBatchLedger(), health, clock=lambda: NOW)
    client = TestClient(ActAsRole(app, "edge-gateway"))
    try:
        sent = batch(events=3)
        buffer.append(sent)
        fwd = Forwarder(buffer, client, metrics=default_metrics(), clock=lambda: NOW)
        assert fwd.send_once().sent
        assert [event.event_id for event in writer.events] == [e.event_id for e in sent.events]
        buffer.append(sent)  # the same batch again: core answers 200 duplicate
        assert fwd.send_once().sent
        assert len(writer.events) == 3

        source_health = SourceHealth()
        source_health.record_read("src_wms_db", records=3, lag_seconds=1.5, at=NOW)
        beats = Heartbeater(
            client,
            "default",
            sources_file(),
            buffer,
            source_health,
            metrics=default_metrics(),
            clock=lambda: NOW,
        )
        assert beats.send() == 2
        stored = health.get("default", "src_wms_db")
        assert stored is not None
        assert stored.status == "ok"
        assert stored.lag_seconds == 1.5
        assert stored.last_success_at == NOW
    finally:
        client.close()


# ---------------------------------------------------------------------------------------------
# SourceHealth and Heartbeater
# ---------------------------------------------------------------------------------------------


def sources_file() -> SourcesFile:
    return SourcesFile(
        systems=[SystemConfig(id="sys_warehouse", name="Warehouse")],
        sources=[
            SourceConfig(
                id="src_wms_db", system="sys_warehouse", type=SourceType.UPLOAD, config={}
            ),
            SourceConfig(id="src_wms_log", system="sys_warehouse", type=SourceType.OTLP),
            SourceConfig(id="src_off", system="sys_warehouse", type=SourceType.OTLP, enabled=False),
        ],
    )


def test_source_health_status_from_consecutive_errors() -> None:
    health = SourceHealth()
    assert health.status("src_web") is SourceStatus.OK
    health.record_error("src_web", NOW)
    assert health.status("src_web") is SourceStatus.DEGRADED
    health.record_error("src_web", NOW)
    assert health.status("src_web") is SourceStatus.DEGRADED
    health.record_error("src_web")
    assert health.status("src_web") is SourceStatus.FAILING
    health.record_read("src_web", records=10, lag_seconds=3.0, at=NOW)
    assert health.status("src_web") is SourceStatus.OK
    health.record_read("src_web", records=5, lag_seconds=None, at=NOW + timedelta(seconds=60))
    state = health.snapshot()["src_web"]
    assert state.error_count == 0
    assert state.records == 15
    assert state.last_success_at == NOW + timedelta(seconds=60)
    assert state.last_error_at == NOW
    assert state.lag_seconds is None
    assert state.status is SourceStatus.OK


def test_heartbeat_per_enabled_source(buffer: DiskBuffer) -> None:
    buffer.append(batch())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"recorded": True})

    health = SourceHealth()
    health.record_read("src_wms_db", records=4, lag_seconds=2.5, at=NOW)
    for _ in range(3):
        health.record_error("src_wms_log", NOW)
    metrics = default_metrics()
    beats = Heartbeater(
        client_for(handler),
        "default",
        sources_file(),
        buffer,
        health,
        metrics=metrics,
        clock=lambda: NOW,
    )
    assert beats.send() == 2
    assert metrics.get("carto_edge_heartbeats_total") == 2
    bodies = {}
    for request in seen:
        assert request.url.path == HEARTBEAT_PATH
        assert request.headers["content-type"] == "application/json"
        assert is_ulid(request.headers["x-request-id"])
        beat = SourceHeartbeat.model_validate_json(request.content)
        bodies[beat.source_id] = beat
    assert set(bodies) == {"src_wms_db", "src_wms_log"}
    ok = bodies["src_wms_db"]
    assert ok.status is SourceStatus.OK
    assert ok.last_success_at == NOW
    assert ok.lag_seconds == 2.5
    assert ok.error_count == 0
    assert ok.buffer_depth == 1
    assert ok.oldest_buffered_at == NOW
    assert ok.message == ""
    failing = bodies["src_wms_log"]
    assert failing.status is SourceStatus.FAILING
    assert failing.error_count == 3
    assert failing.last_success_at is None
    assert failing.lag_seconds == 0.0
    assert json.loads(seen[0].content)["tenant_id"] == "default"


def test_heartbeat_failures_are_logged_by_status_and_type_only(buffer: DiskBuffer) -> None:
    answers: list[int | Exception] = [503, httpx.ConnectError(f"refused {MARKER}")]

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, text=MARKER)

    metrics = default_metrics()
    beats = Heartbeater(
        client_for(handler),
        "default",
        sources_file(),
        buffer,
        SourceHealth(),
        metrics=metrics,
    )
    with structlog.testing.capture_logs() as logs:
        assert beats.send() == 0
    assert metrics.get("carto_edge_heartbeats_total") == 0
    assert any(entry.get("status") == 503 for entry in logs)
    assert any(entry.get("error") == "ConnectError" for entry in logs)
    assert MARKER not in repr(logs)


def test_heartbeater_run_stops_promptly(buffer: DiskBuffer) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(200)

    beats = Heartbeater(
        client_for(handler),
        "default",
        sources_file(),
        buffer,
        SourceHealth(),
        metrics=default_metrics(),
    )
    stop = threading.Event()
    thread = threading.Thread(target=beats.run, args=(stop, 30.0), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while count < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert count == 2  # one round at start, then the 30 s wait was interrupted


@pytest.mark.parametrize("status", [403, 415])
def test_deployment_wide_refusals_are_retried_not_parked(buffer: DiskBuffer, status: int) -> None:
    """403 (tenant) and 415 (media type) would park every batch in seconds; they back off."""
    buffer.append(batch())
    buffer.append(batch())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"title": "refused"})

    metrics = default_metrics()
    sender = forwarder(buffer, handler, metrics)
    first = sender.send_once()
    second = sender.send_once()
    assert first.failed and second.failed
    assert not first.parked and not second.parked
    assert first.retry_after_seconds is not None and first.retry_after_seconds > 0
    assert buffer.depth() == 2 and buffer.parked() == 0
    assert metrics.get("carto_edge_send_errors_total") == 2
    assert status not in PARK_STATUSES
