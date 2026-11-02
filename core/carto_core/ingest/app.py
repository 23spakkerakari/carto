"""The ``ingest-api`` FastAPI application (spec 5.3, 8.5, 12, 14.12, 16).

Endpoints, all internal (mutual TLS, never behind the ingress; see :mod:`.server`):

- ``POST /internal/ingest``: one :class:`~carto_schema.ingest.IngestBatch` as JSON, optionally
  ``Content-Encoding: zstd``. The body is capped on the wire and after decompression (spec 2.3
  invariant 8); a zstd body is exactly one frame whose window is at most
  :data:`ZSTD_MAX_WINDOW_BYTES`, and decompression and validation run in the threadpool, never
  on the event loop. At most ``ingest.max_inflight_batches`` requests are decoded or written at
  once; the next is answered 429 with ``Retry-After``. Only the contract models are accepted,
  so a raw value cannot arrive in a slot the schema does not have (spec 2.3 invariant 2). An
  event whose ``observed_at`` lies outside the plausible range (ADR 0036) refuses the batch
  with 422, so the edge parks it. Then the batch is claimed in the ledger (ADR 0034): a batch
  already written is acknowledged as a duplicate, one another delivery is writing is a 503, and
  otherwise the batch is written and marked done. A transient write failure releases the claim
  and is a 503 so the edge retries; ClickHouse refusing the data itself is a 422. Every 503
  carries ``Retry-After``.
  Before the ledger's ``done`` mark the batch's distinct templates are upserted into
  ``templates`` (text, kind, first and last seen, count added) inside the same transaction
  (ADR 0056), so a retried batch never double-counts.
- ``POST /internal/heartbeat``: one :class:`~carto_schema.ingest.SourceHeartbeat`, upserted into
  the source health store.
- ``POST /internal/inventory``: one :class:`~carto_schema.inventory.EdgeInventory` (ADR 0050),
  validated against the contract (a tenant this core does not serve is a 422) and replaced as a
  whole in the metadata store: systems, sources without secrets, the payload and the field
  policy pins.
- ``GET /healthz``, ``GET /readyz`` (no detail in the body), ``GET /metrics`` (Prometheus text).

Errors are RFC 7807 problem documents (``application/problem+json``) that carry locations and
messages but never the rejected input. Every response carries ``X-Request-Id`` (echoed when the
caller's is well formed, generated otherwise) and the id is bound into the log context. Request
and response bodies are never logged (spec 2.3 invariant 7, 14.12); the logger only ever sees
ids and counts.
"""

from __future__ import annotations

import io
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog.contextvars
import zstandard
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_common.mtls import ANY_ROLE, peer_role, role_allowed
from carto_core.db.clickhouse import WriterError
from carto_core.ingest.health import HealthStoreError, SourceHealthRecord
from carto_core.ingest.ledger import ClaimState, LedgerEntry, LedgerError
from carto_core.metadata import InMemoryMetadataStore, MetadataError, TemplateRecord
from carto_schema.ingest import IngestBatch, SourceHeartbeat
from carto_schema.inventory import EdgeInventory

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

    from carto_core.db.clickhouse import EventWriter
    from carto_core.ingest.health import SourceHealthStore
    from carto_core.ingest.ledger import BatchLedger
    from carto_core.metadata import MetadataStore
    from carto_core.settings import CoreSettings, IngestListenerSettings

__all__ = [
    "BUSY_RETRY_AFTER_SECONDS",
    "MAX_RANGE_ERRORS",
    "METRIC_NAMES",
    "PROBLEM_CONTENT_TYPE",
    "REQUEST_ID_HEADER",
    "ROUTE_ROLES",
    "ZSTD_MAX_WINDOW_BYTES",
    "IngestMetrics",
    "Problem",
    "RequestIdMiddleware",
    "batch_templates",
    "create_app",
    "decompress_capped",
    "out_of_range_errors",
]

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"
REQUEST_ID_HEADER: Final = "x-request-id"
_REQUEST_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DECOMPRESS_CHUNK: Final = 64 * 1024
_JSON_MEDIA_TYPE: Final = "application/json"
ZSTD_MAX_WINDOW_BYTES: Final = 8 * 1024 * 1024
"""Largest zstd window a body may ask for. The window is allocated before any output, so a
tiny frame declaring a 2 GiB window would otherwise cost that much memory; the edge compresses
at level 3 (a 2 MiB window at most)."""
_ZSTD_MAGIC: Final = b"\x28\xb5\x2f\xfd"
_BLOCK_HEADER_BYTES: Final = 3
_CHECKSUM_BYTES: Final = 4
_RLE_BLOCK: Final = 1
_RESERVED_BLOCK: Final = 3
RETRY_AFTER_SECONDS: Final = 1
"""``Retry-After`` on a 429 (in-flight cap) and on a 503 for an unavailable store. The edge
honours it on a 429, capped, and backs off on its own after a 503."""
BUSY_RETRY_AFTER_SECONDS: Final = 5
"""``Retry-After`` on the 503 for a batch another delivery is still writing (ADR 0034)."""
MAX_RANGE_ERRORS: Final = 20
"""Out-of-range events listed in one 422; the rest are counted in its detail."""

METRIC_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("carto_ingest_batches_total", "Batches accepted and written to ClickHouse."),
    ("carto_ingest_events_total", "Events written to ClickHouse."),
    ("carto_ingest_duplicates_total", "Batches acknowledged as already in the ledger."),
    ("carto_ingest_errors_total", "Internal requests answered with a problem (4xx or 5xx)."),
    ("carto_heartbeats_total", "Source heartbeats recorded."),
    ("carto_ingest_mtls_refused_total", "Requests refused for the client certificate's role."),
    ("carto_ingest_busy_total", "Requests answered 429 because too many were in flight."),
    ("carto_ingest_claims_busy_total", "Batches answered 503 because another delivery held them."),
    ("carto_ingest_claims_taken_over_total", "Stale ledger claims taken over and rewritten."),
    ("carto_ingest_out_of_range_total", "Batches refused for an observed_at out of range."),
    ("carto_ingest_refused_total", "Batches refused because ClickHouse rejected their data."),
    ("carto_inventories_total", "Edge inventories recorded."),
)
ROUTE_ROLES: Final[dict[tuple[str, str], frozenset[str]]] = {
    ("POST", "/internal/ingest"): frozenset({"edge-gateway"}),
    ("POST", "/internal/heartbeat"): frozenset({"edge-gateway"}),
    ("POST", "/internal/inventory"): frozenset({"edge-gateway"}),
    ("GET", "/healthz"): frozenset({ANY_ROLE}),
    ("GET", "/readyz"): frozenset({ANY_ROLE}),
    ("GET", "/metrics"): frozenset({ANY_ROLE}),
}
"""The service roles each route accepts (ADR 0031); a route missing here is refused."""

log = get_logger(component="ingest-api")


class Problem(Exception):  # noqa: N818 - an RFC 7807 problem document, raised to answer with
    """An RFC 7807 problem to answer with. ``detail`` never carries request content."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str = "",
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {title}")
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors
        self.headers = headers


class IngestMetrics:
    """Hand-rolled counters rendered in the Prometheus text format (spec 16)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {name: 0 for name, _help in METRIC_NAMES}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
        lines: list[str] = []
        for name, help_text in METRIC_NAMES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {values[name]}")
        return "\n".join(lines) + "\n"


class RequestIdMiddleware:
    """Echo a well-formed ``X-Request-Id`` or generate a ULID; bind it to the log context."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID_PATTERN.fullmatch(incoming) else new_ulid()
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)


def _zstd_frame_size(data: bytes) -> int:
    """The compressed size of the zstd frame at the start of ``data``, read from its block
    headers (RFC 8878 section 3.1.1). Raises :class:`zstandard.ZstdError` or ``ValueError`` on
    anything that is not a well-formed frame (a skippable frame included)."""
    if not data.startswith(_ZSTD_MAGIC):
        msg = "not a zstd frame"
        raise ValueError(msg)
    position = zstandard.frame_header_size(data)
    has_checksum = zstandard.get_frame_parameters(data).has_checksum
    while True:
        header = data[position : position + _BLOCK_HEADER_BYTES]
        if len(header) < _BLOCK_HEADER_BYTES:
            msg = "truncated zstd block header"
            raise ValueError(msg)
        value = int.from_bytes(header, "little")
        last, block_type, size = value & 1, (value >> 1) & 3, value >> 3
        if block_type == _RESERVED_BLOCK:
            msg = "reserved zstd block type"
            raise ValueError(msg)
        position += _BLOCK_HEADER_BYTES + (1 if block_type == _RLE_BLOCK else size)
        if last:
            break
    position += _CHECKSUM_BYTES if has_checksum else 0
    if position > len(data):
        msg = "truncated zstd frame"
        raise ValueError(msg)
    return position


def decompress_capped(data: bytes, limit: int) -> bytes:
    """zstd-decompress ``data``: exactly one frame, a window of at most
    :data:`ZSTD_MAX_WINDOW_BYTES`, at most ``limit`` bytes out. Anything after the frame (a
    second frame, a skippable frame, stray bytes) and any malformed frame is a 400; a result
    above ``limit`` is a 413."""
    try:
        if _zstd_frame_size(data) != len(data):
            raise Problem(400, "bad request", "a zstd body must be exactly one frame")
    except (zstandard.ZstdError, ValueError) as exc:
        raise Problem(400, "bad request", "malformed zstd body") from exc
    out = bytearray()
    decompressor = zstandard.ZstdDecompressor(max_window_size=ZSTD_MAX_WINDOW_BYTES)
    try:
        with decompressor.stream_reader(io.BytesIO(data), read_across_frames=False) as reader:
            while True:
                chunk = reader.read(_DECOMPRESS_CHUNK)
                if not chunk:
                    break
                out += chunk
                if len(out) > limit:
                    raise Problem(413, "payload too large", "decompressed body exceeds the cap")
    except zstandard.ZstdError as exc:
        raise Problem(400, "bad request", "malformed zstd body") from exc
    return bytes(out)


def out_of_range_errors(
    batch: IngestBatch, lower: datetime, upper: datetime
) -> tuple[list[dict[str, Any]], int]:
    """The problem entries of the events whose ``observed_at`` lies outside ``[lower, upper]``
    (ADR 0036), at most :data:`MAX_RANGE_ERRORS`, and how many there are. An entry names the
    event's position and whether it is too old or in the future, never its time."""
    errors: list[dict[str, Any]] = []
    count = 0
    for index, event in enumerate(batch.events):
        if lower <= event.observed_at <= upper:
            continue
        count += 1
        if len(errors) < MAX_RANGE_ERRORS:
            reason = "too_old" if event.observed_at < lower else "future"
            errors.append(
                {
                    "loc": ["events", index, "observed_at"],
                    "msg": "observed_at is outside the plausible range",
                    "type": f"observed_at_{reason}",
                }
            )
    return errors, count


def batch_templates(batch: IngestBatch) -> list[TemplateRecord]:
    """The distinct templates of a batch with their text, kind, seen range and event count
    (ADR 0056), in first-appearance order."""
    records: dict[tuple[str, str], TemplateRecord] = {}
    for event in batch.events:
        key = (event.system_id, event.template_id)
        current = records.get(key)
        if current is None:
            records[key] = TemplateRecord(
                system_id=event.system_id,
                template_id=event.template_id,
                template_text=event.template_text,
                kind=event.kind.value,
                first_seen=event.observed_at,
                last_seen=event.observed_at,
                count=1,
            )
            continue
        records[key] = TemplateRecord(
            system_id=current.system_id,
            template_id=current.template_id,
            template_text=current.template_text,
            kind=current.kind,
            first_seen=min(current.first_seen, event.observed_at),
            last_seen=max(current.last_seen, event.observed_at),
            count=current.count + 1,
        )
    return list(records.values())


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", "")
    return str(value) if value else ""


def _problem_response(request: Request, problem: Problem) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": problem.title,
        "status": problem.status,
        "detail": problem.detail,
        "instance": request.url.path,
        "request_id": _request_id(request),
    }
    if problem.errors is not None:
        body["errors"] = problem.errors
    return JSONResponse(
        body, status_code=problem.status, media_type=PROBLEM_CONTENT_TYPE, headers=problem.headers
    )


def _validation_problem(exc: ValidationError) -> Problem:
    """422 with locations, messages and types; the rejected input stays out (spec 14.12)."""
    errors = [
        {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]
    if errors and all(error["type"] == "json_invalid" for error in errors):
        return Problem(400, "bad request", "body is not valid JSON")
    return Problem(422, "request body failed validation", "see errors", errors=errors)


def _parse[T: BaseModel](model: type[T], data: bytes) -> T:
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        raise _validation_problem(exc) from None


def _decode[T: BaseModel](model: type[T], data: bytes, zstd: bool, limit: int) -> T:
    """Decompress (when ``zstd``) and validate: CPU-bound, so it runs in the threadpool."""
    if zstd:
        data = decompress_capped(data, limit)
    return _parse(model, data)


async def _read_body(request: Request, listener: IngestListenerSettings) -> tuple[bytes, bool]:
    """Read a body capped on the wire; return it with whether it is zstd-compressed."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type and media_type != _JSON_MEDIA_TYPE:
        raise Problem(415, "unsupported media type", f"send {_JSON_MEDIA_TYPE}")
    encoding = request.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity", "zstd"):
        raise Problem(415, "unsupported content encoding", "send zstd or no content encoding")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise Problem(400, "bad request", "content-length is not a number") from None
        if length > listener.max_body_bytes:
            raise Problem(413, "payload too large", "body exceeds the wire cap")
    buffer = bytearray()
    async for chunk in request.stream():
        buffer += chunk
        if len(buffer) > listener.max_body_bytes:
            raise Problem(413, "payload too large", "body exceeds the wire cap")
    if not buffer:
        raise Problem(400, "bad request", "empty body")
    return bytes(buffer), encoding == "zstd"


def create_app(
    settings: CoreSettings,
    writer: EventWriter,
    ledger: BatchLedger,
    health: SourceHealthStore,
    *,
    metadata: MetadataStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the application with its stores injected (tests pass in-memory ones). Without a
    ``metadata`` store (a smoke path, never ``ingest-api``, whose server wires PostgreSQL)
    templates and inventories are kept in memory and a warning says so."""
    now = clock if clock is not None else lambda: datetime.now(UTC)
    if metadata is None:
        log.warning("no metadata store configured; templates and inventories stay in memory")
        metadata = InMemoryMetadataStore()
    templates = metadata
    listener = settings.ingest
    tenant_id = settings.tenant_id
    retention = settings.retention
    claim_timeout = timedelta(seconds=listener.claim_timeout_seconds)
    metrics = IngestMetrics()

    async def authorize(request: Request) -> None:
        """Every route: the connection's certificate role must be one the route lists."""
        route = getattr(request.scope.get("route"), "path", None)
        role = peer_role(request.scope)
        if not role_allowed(ROUTE_ROLES, request.method, route, role):
            metrics.inc("carto_ingest_mtls_refused_total")
            log.warning(
                "request.role_refused", method=request.method, route=route, role=role or "none"
            )
            raise Problem(403, "forbidden", "this client certificate may not call this route")

    app = FastAPI(
        title="carto ingest-api",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(authorize)],
    )
    app.add_middleware(RequestIdMiddleware)
    app.state.metrics = metrics
    # A threading semaphore, not an asyncio one: it is only ever tried, never waited on, and it
    # stays correct whichever event loop or thread serves the request.
    inflight = threading.BoundedSemaphore(listener.max_inflight_batches)

    @contextmanager
    def admitted() -> Iterator[None]:
        if not inflight.acquire(blocking=False):
            metrics.inc("carto_ingest_busy_total")
            raise Problem(
                429,
                "too many requests",
                "too many batches in flight, retry later",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        try:
            yield
        finally:
            inflight.release()

    async def receive[T: BaseModel](request: Request, model: type[T]) -> T:
        raw, zstd = await _read_body(request, listener)
        return await run_in_threadpool(_decode, model, raw, zstd, listener.max_decompressed_bytes)

    def unavailable(title: str, retry_after: int = RETRY_AFTER_SECONDS) -> Problem:
        return Problem(
            503, title, "retry the batch later", headers={"Retry-After": str(retry_after)}
        )

    def check_range(batch: IngestBatch, at: datetime) -> None:
        """ADR 0036: refuse the batch when an event's observed_at is implausible."""
        lower, upper = retention.observed_at_range(at)
        errors, count = out_of_range_errors(batch, lower, upper)
        if count:
            metrics.inc("carto_ingest_out_of_range_total")
            log.warning(
                "batch refused, observed_at out of range",
                tenant_id=batch.tenant_id,
                source_id=batch.source_id,
                batch_id=batch.batch_id,
                out_of_range=count,
            )
            raise Problem(
                422,
                "observed_at out of range",
                f"{count} events lie outside the plausible range; see errors",
                errors=errors,
            )

    def process_batch(batch: IngestBatch) -> bool:
        """Range check, claim, write, mark done (ADR 0034, 0036). True for a duplicate."""
        received_at = now()
        check_range(batch, received_at)
        context = {
            "tenant_id": batch.tenant_id,
            "source_id": batch.source_id,
            "batch_id": batch.batch_id,
            "events": len(batch.events),
        }
        entry = LedgerEntry(
            batch.tenant_id, batch.batch_id, batch.source_id, len(batch.events), received_at
        )
        try:
            claim = ledger.claim(entry, received_at - claim_timeout)
        except LedgerError as exc:
            log.warning("batch claim failed", error=type(exc).__name__, **context)
            raise unavailable("ledger unavailable") from exc
        if claim.state is ClaimState.DONE:
            metrics.inc("carto_ingest_duplicates_total")
            log.info("batch already in the ledger", **context)
            return True
        if claim.state is ClaimState.BUSY:
            metrics.inc("carto_ingest_claims_busy_total")
            log.info("batch is being written by another delivery", **context)
            raise unavailable("batch in progress", BUSY_RETRY_AFTER_SECONDS)
        if claim.took_over:
            metrics.inc("carto_ingest_claims_taken_over_total")
            log.warning("stale batch claim taken over", **context)
        try:
            result = writer.write_events(batch.events)
        except WriterError as exc:
            log.warning("batch write failed", error=str(exc), permanent=exc.permanent, **context)
            try:
                ledger.release(batch.tenant_id, batch.batch_id, claim.claimed_at)
            except LedgerError as release_exc:
                log.warning("batch claim release failed", error=type(release_exc).__name__)
            if exc.permanent:
                metrics.inc("carto_ingest_refused_total")
                raise Problem(
                    422, "event store refused the batch", "the batch cannot be written as sent"
                ) from exc
            raise unavailable("event store unavailable") from exc
        records = batch_templates(batch)

        def record_templates(connection: Connection | None) -> None:
            templates.upsert_templates(batch.tenant_id, records, connection=connection)

        try:
            ledger.complete(entry, now(), after=record_templates)
        except (LedgerError, MetadataError) as exc:
            # Written but not marked done: the retry writes it again and the replacing tables
            # merge the copies (ADR 0034); the template counts roll back with the mark.
            log.warning("batch ledger failed", error=type(exc).__name__, **context)
            raise unavailable("ledger unavailable") from exc
        metrics.inc("carto_ingest_batches_total")
        metrics.inc("carto_ingest_events_total", result.events)
        log.info(
            "batch accepted", identifiers=result.identifiers, templates=len(records), **context
        )
        return False

    @app.exception_handler(Problem)
    async def on_problem(request: Request, exc: Problem) -> Response:
        if request.url.path.startswith("/internal/"):
            metrics.inc("carto_ingest_errors_total")
        return _problem_response(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        title = "not found" if exc.status_code == 404 else "request refused"
        return _problem_response(request, Problem(exc.status_code, title))

    @app.exception_handler(RequestValidationError)
    async def on_request_validation(request: Request, _exc: RequestValidationError) -> Response:
        return _problem_response(request, Problem(422, "request failed validation"))

    @app.exception_handler(Exception)
    async def on_unhandled(request: Request, exc: Exception) -> Response:
        log.error("unhandled error", error=type(exc).__name__, path=request.url.path)
        return _problem_response(request, Problem(500, "internal error"))

    @app.post("/internal/ingest")
    async def ingest(request: Request) -> Response:
        with admitted():
            batch = await receive(request, IngestBatch)
            if batch.tenant_id != tenant_id:
                raise Problem(403, "forbidden", "batch is for a tenant this core does not serve")
            duplicate = await run_in_threadpool(process_batch, batch)
        return JSONResponse({"accepted": len(batch.events), "duplicate": duplicate})

    @app.post("/internal/heartbeat")
    async def heartbeat(request: Request) -> Response:
        with admitted():
            beat = await receive(request, SourceHeartbeat)
        if beat.tenant_id != tenant_id:
            raise Problem(403, "forbidden", "heartbeat is for a tenant this core does not serve")
        record = SourceHealthRecord.from_heartbeat(beat, now())
        try:
            await run_in_threadpool(health.upsert, record)
        except HealthStoreError as exc:
            log.warning(
                "heartbeat store failed",
                error=type(exc).__name__,
                tenant_id=beat.tenant_id,
                source_id=beat.source_id,
            )
            raise Problem(
                503,
                "health store unavailable",
                "retry the heartbeat later",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            ) from exc
        metrics.inc("carto_heartbeats_total")
        log.info(
            "heartbeat recorded",
            tenant_id=beat.tenant_id,
            source_id=beat.source_id,
            status=beat.status.value,
        )
        return JSONResponse({"recorded": True})

    @app.post("/internal/inventory")
    async def inventory(request: Request) -> Response:
        with admitted():
            report = await receive(request, EdgeInventory)
        if report.tenant_id != tenant_id:
            raise Problem(
                422,
                "inventory tenant mismatch",
                "inventory is for a tenant this core does not serve",
            )
        try:
            result = await run_in_threadpool(templates.replace_inventory, report, now())
        except MetadataError as exc:
            log.warning(
                "inventory store failed",
                error=type(exc).__name__,
                tenant_id=report.tenant_id,
                edge_id=report.edge_id,
            )
            raise Problem(
                503,
                "metadata store unavailable",
                "retry the inventory later",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            ) from exc
        metrics.inc("carto_inventories_total")
        log.info(
            "inventory recorded",
            tenant_id=report.tenant_id,
            edge_id=report.edge_id,
            systems=result.systems,
            sources=result.sources,
            disabled=result.disabled,
            pins=result.pins,
            fields_pinned=result.fields_pinned,
        )
        return JSONResponse(
            {
                "recorded": True,
                "systems": result.systems,
                "sources": result.sources,
                "pins": result.pins,
            }
        )

    @app.get("/healthz")
    async def healthz() -> Response:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> Response:
        ready = await run_in_threadpool(
            lambda: writer.ping() and ledger.ping() and health.ping() and templates.ping()
        )
        if ready:
            return JSONResponse({"status": "ready"})
        return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.get("/metrics")
    async def metrics_text() -> Response:
        return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")

    return app
