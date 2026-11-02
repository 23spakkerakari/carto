"""Run edge-gateway under uvicorn with mutual TLS (spec 8.1.2, 8.1.6, 8.5, 12, 14.4).

The listener presents the certificate ``carto-ctl pki init`` issued for edge-gateway and
requires a client certificate signed by the install's private CA (``ssl.CERT_REQUIRED``): the
collector, core's API and webhook senders all authenticate with one. TLS 1.2 is the floor
(spec 14.4); :func:`harden_tls_context` pins it through uvicorn's context factory hook. uvicorn's
logging configuration is off (``log_config=None``) so its records reach the root handler
:func:`carto_common.logging.configure_logging` installs, redaction included (spec 14.12).
carto-edge never imports carto_core (spec 5.2: separate trust zones and images), so these
helpers mirror ``carto_core.ingest.server`` rather than reuse it.

:func:`run_gateway` refuses to start, before opening any store, when the sources file is
missing or invalid or a listener file is missing (exit 2), or when the keys cannot be loaded
(exit 1; ``carto-ctl key init`` creates them). Then it opens the disk buffer and the cursor
store, builds the ingestor, the reveal service and, when ``core.url`` is set, the forwarder,
heartbeater and inventory reporter threads (ADR 0050), runs the poll scheduler and a
housekeeping loop (stale batches sealed
every second, templates and field statistics persisted every ``gateway.stats_flush_seconds``)
inside the application lifespan, and serves until signalled. Shutdown stops the tasks and
threads, seals every partial batch into the buffer and closes the stores.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import uvicorn

from carto_common.logging import configure_logging, get_logger
from carto_common.mtls import peer_role_h11_protocol
from carto_edge.audit import AuditChainError
from carto_edge.config import ConfigError, EdgeSettings, GatewaySettings, load_sources_file
from carto_edge.gateway.app import create_gateway_app
from carto_edge.keys import KeyManagementError
from carto_edge.metrics import default_metrics
from carto_edge.pipeline.buffer import DiskBuffer
from carto_edge.runtime import (
    EdgeRuntime,
    build_connector_context,
    build_reveal_service,
    build_runtime,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp

__all__ = [
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_USAGE",
    "GatewayConfigError",
    "build_uvicorn_config",
    "check_listener_files",
    "harden_tls_context",
    "run_gateway",
    "run_housekeeping",
]

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
EXIT_USAGE: Final = 2
LOG_LEVEL: Final = "info"
HOUSEKEEPING_SECONDS: Final = 1.0
THREAD_JOIN_SECONDS: Final = 10.0
TASK_STOP_SECONDS: Final = 10.0

MAX_HEADER_BYTES: Final = 16 * 1024
MAX_CONNECTIONS: Final = 256

log = get_logger(component="edge-gateway")


class GatewayConfigError(ValueError):
    """The gateway listener settings are unusable; the message names the setting or file."""


def harden_tls_context(
    config: uvicorn.Config, default_factory: Callable[[], ssl.SSLContext]
) -> ssl.SSLContext:
    """uvicorn ``ssl_context_factory``: the default context with TLS 1.2 as the floor."""
    del config
    context = default_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _require_file(path: Path | None, setting: str) -> Path:
    if path is None:
        msg = f"gateway.{setting} is required: edge-gateway serves mutual TLS only (spec 14.4)"
        raise GatewayConfigError(msg)
    if not path.is_file():
        msg = f"gateway.{setting} does not exist or is not a file: {path}"
        raise GatewayConfigError(msg)
    return path


def check_listener_files(gateway: GatewaySettings) -> tuple[Path, Path, Path]:
    """The certificate, key and client CA paths; all three are required."""
    certificate = _require_file(gateway.tls_cert_file, "tls_cert_file")
    key = _require_file(gateway.tls_key_file, "tls_key_file")
    client_ca = _require_file(gateway.tls_client_ca_file, "tls_client_ca_file")
    return certificate, key, client_ca


def build_uvicorn_config(
    app: ASGIApp, gateway: GatewaySettings, *, log_level: str = LOG_LEVEL
) -> uvicorn.Config:
    """The uvicorn configuration for the listener; raises :class:`GatewayConfigError` on a
    missing TLS file. The lifespan is on: the poll scheduler and housekeeping run in it."""
    certificate, key, client_ca = check_listener_files(gateway)
    return uvicorn.Config(
        app,
        host=gateway.host,
        port=gateway.port,
        ssl_certfile=str(certificate),
        ssl_keyfile=str(key),
        ssl_ca_certs=str(client_ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        ssl_version=ssl.PROTOCOL_TLS_SERVER,
        ssl_context_factory=harden_tls_context,
        log_config=None,
        log_level=log_level,
        access_log=False,  # the app logs its own access line without paths or queries
        # h11 bounds a request line plus headers at 16 KiB; httptools (the "auto" choice when
        # installed) accepts any size. limit_concurrency answers 503 beyond it (spec 2.3 #8).
        # h11, recording each connection's certificate role for the routes (ADR 0031).
        http=peer_role_h11_protocol(),
        h11_max_incomplete_event_size=MAX_HEADER_BYTES,
        limit_concurrency=MAX_CONNECTIONS,
        server_header=False,
        proxy_headers=False,
        lifespan="on",
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def run_housekeeping(
    stop: asyncio.Event,
    flush_stale: Callable[[datetime], object],
    flush_runtime: Callable[[], object],
    *,
    stats_flush_seconds: float,
    interval_seconds: float = HOUSEKEEPING_SECONDS,
    clock: Callable[[], datetime] = _utc_now,
) -> None:
    """Until ``stop`` is set: every ``interval_seconds`` seal the batches older than
    ``core.batch_flush_seconds`` (``flush_stale(now)``), and every ``stats_flush_seconds``
    persist templates and field statistics (``flush_runtime()``). Both run in a worker thread;
    a failure is logged by type and the loop goes on."""
    loop = asyncio.get_running_loop()
    last_stats = loop.time()
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        if stop.is_set():
            break
        try:
            await asyncio.to_thread(flush_stale, clock())
            if loop.time() - last_stats >= stats_flush_seconds:
                last_stats = loop.time()
                await asyncio.to_thread(flush_runtime)
        except Exception as exc:  # a failed tick must not end the loop; the type says enough
            log.warning("gateway.housekeeping_failed", error=type(exc).__name__)


async def _stop_tasks(tasks: list[asyncio.Task[object]], timeout: float) -> None:
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        error = None if task.cancelled() else task.exception()
        if error is not None:
            log.warning("gateway.task_failed", task=task.get_name(), error=type(error).__name__)


def run_gateway(settings: EdgeSettings) -> int:
    """Serve edge-gateway until the process is signalled (see the module docstring)."""
    configure_logging("edge-gateway")
    if settings.sources_file is None:
        log.error("gateway.sources_file_missing", setting="sources_file")
        return EXIT_USAGE
    try:
        sources = load_sources_file(settings.sources_file)
    except ConfigError as exc:
        log.error("gateway.sources_file_invalid", error=str(exc))
        return EXIT_USAGE
    try:
        check_listener_files(settings.gateway)
    except GatewayConfigError as exc:
        log.error("gateway.listener_invalid", error=str(exc))
        return EXIT_USAGE
    try:
        runtime = build_runtime(settings, sources, mode="gateway")
    except KeyManagementError as exc:
        log.error("gateway.keys_unavailable", error=str(exc))
        return EXIT_FAILURE
    except AuditChainError as exc:
        log.error("gateway.audit_chain_broken", error=str(exc))
        return EXIT_FAILURE
    try:
        return _serve(settings, runtime)
    finally:
        runtime.close()


def _serve(settings: EdgeSettings, runtime: EdgeRuntime) -> int:
    # The D1 stages (plan M1 wave 2) are imported here so this module imports without them.
    from carto_edge.gateway.scheduler import PollScheduler  # noqa: PLC0415
    from carto_edge.pipeline.batch import Ingestor  # noqa: PLC0415
    from carto_edge.pipeline.forward import (  # noqa: PLC0415
        Forwarder,
        Heartbeater,
        InventoryReporter,
        SourceHealth,
        build_core_client,
        edge_id_for,
    )
    from carto_edge.state import CursorStore  # noqa: PLC0415

    core = settings.core
    buffer = DiskBuffer(
        settings.buffer_db_file, settings.buffer.max_bytes, settings.buffer.backpressure_ratio
    )
    cursors = CursorStore(settings.cursors_db_file)
    try:
        metrics = default_metrics()
        ingestor = Ingestor(runtime, buffer, cursors, link=core, metrics=metrics)
        reveal = build_reveal_service(runtime)
        health = SourceHealth()
        stop = threading.Event()
        threads: list[threading.Thread] = []
        client = build_core_client(core) if core.url is not None else None
        if client is not None:
            forwarder = Forwarder(
                buffer, client, metrics=metrics, retry_max_seconds=core.retry_max_seconds
            )
            heartbeater = Heartbeater(
                client, settings.tenant_id, runtime.sources, buffer, health, metrics=metrics
            )
            threads.append(
                threading.Thread(
                    target=forwarder.run, args=(stop,), name="edge-forwarder", daemon=True
                )
            )
            threads.append(
                threading.Thread(
                    target=heartbeater.run,
                    args=(stop, core.heartbeat_seconds),
                    name="edge-heartbeater",
                    daemon=True,
                )
            )
            inventory = InventoryReporter(
                client,
                settings.tenant_id,
                runtime.sources,
                metrics=metrics,
                edge_id=edge_id_for(core),
                sources_path=settings.sources_file,
                default_poll_seconds=settings.gateway.poll_seconds,
            )
            threads.append(
                threading.Thread(
                    target=inventory.run,
                    args=(stop, core.heartbeat_seconds),
                    name="edge-inventory",
                    daemon=True,
                )
            )
        else:
            log.warning(
                "gateway.core_link_unset",
                detail="core.url is not set: batches stay in the disk buffer, no heartbeats",
            )
        started = threading.Event()

        @contextlib.asynccontextmanager
        async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
            stop_tasks = asyncio.Event()
            scheduler = PollScheduler(
                runtime,
                ingestor,
                cursors,
                build_connector_context(runtime),
                health,
                metrics=metrics,
                default_poll_seconds=settings.gateway.poll_seconds,
            )
            tasks: list[asyncio.Task[object]] = [
                asyncio.create_task(scheduler.run(stop_tasks), name="edge-poll-scheduler"),
                asyncio.create_task(
                    run_housekeeping(
                        stop_tasks,
                        ingestor.flush_stale,
                        runtime.flush,
                        stats_flush_seconds=settings.gateway.stats_flush_seconds,
                    ),
                    name="edge-housekeeping",
                ),
            ]
            started.set()
            try:
                yield
            finally:
                started.clear()
                stop_tasks.set()
                await _stop_tasks(tasks, TASK_STOP_SECONDS)
                await scheduler.close()

        app = create_gateway_app(
            settings,
            runtime,
            ingestor,
            reveal,
            metrics,
            ready=started.is_set,
            lifespan=lifespan,
            health=health,
        )
        config = build_uvicorn_config(app, settings.gateway, log_level=LOG_LEVEL)
        for thread in threads:
            thread.start()
        log.info(
            "gateway.starting",
            host=settings.gateway.host,
            port=settings.gateway.port,
            tenant_id=settings.tenant_id,
            sources=len(runtime.sources.sources),
            core_link=client is not None,
        )
        try:
            uvicorn.Server(config).run()
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=THREAD_JOIN_SECONDS)
            sealed = ingestor.flush()
            log.info("gateway.stopped", batches_sealed=sealed)
            if client is not None:
                client.close()
    finally:
        buffer.close()
        cursors.close()
    return EXIT_OK
