"""Run ``ingest-api`` under uvicorn with mutual TLS (spec 12 "Internal", 14.4).

The listener presents the certificate ``carto-ctl pki init`` issued for ``ingest-api`` and
requires a client certificate signed by the same private CA (``ssl.CERT_REQUIRED``), so only the
edge gateway can reach it. TLS 1.2 is the floor (spec 14.4 "TLS 1.2 minimum, 1.3 preferred");
the context factory hook pins ``minimum_version`` because uvicorn's own options stop at the
protocol constant. uvicorn's logging configuration is switched off so its records flow through
the root logger that :func:`carto_common.logging.configure_logging` installs, redaction included
(spec 14.12). Access logs carry the request line and status, never a body. While it serves, a
:class:`~carto_core.ingest.ledger.LedgerPruner` deletes ledger rows past retention every hour
(ADR 0034).
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from carto_common.logging import configure_logging, get_logger
from carto_common.mtls import peer_role_h11_protocol
from carto_core.db.clickhouse import ClickHouseWriter, create_client
from carto_core.db.postgres import create_engine_from_settings
from carto_core.ingest.app import create_app
from carto_core.ingest.health import PostgresSourceHealthStore
from carto_core.ingest.ledger import LedgerPruner, PostgresBatchLedger
from carto_core.metadata import PostgresMetadataStore
from carto_core.settings import CoreConfigError

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from carto_core.settings import CoreSettings, IngestListenerSettings

__all__ = ["build_uvicorn_config", "harden_tls_context", "run_ingest_api"]

log = get_logger(component="ingest-api")


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
        msg = f"{setting} is required: ingest-api serves mutual TLS only (spec 14.4)"
        raise CoreConfigError(msg)
    if not path.is_file():
        msg = f"{setting} does not exist or is not a file: {path}"
        raise CoreConfigError(msg)
    return path


def check_listener_files(listener: IngestListenerSettings) -> tuple[Path, Path, Path | None]:
    """The certificate, key and client CA paths; raises on a missing or unusable one."""
    certificate = _require_file(listener.tls_cert_file, "ingest.tls_cert_file")
    key = _require_file(listener.tls_key_file, "ingest.tls_key_file")
    if listener.require_client_cert:
        return certificate, key, _require_file(listener.client_ca_file, "ingest.client_ca_file")
    return certificate, key, listener.client_ca_file


def build_uvicorn_config(
    app: ASGIApp, listener: IngestListenerSettings, *, log_level: str = "info"
) -> uvicorn.Config:
    """The uvicorn configuration for the listener settings; raises on a missing TLS file."""
    certificate, key, client_ca = check_listener_files(listener)
    cert_reqs = ssl.CERT_REQUIRED if listener.require_client_cert else ssl.CERT_NONE
    return uvicorn.Config(
        app,
        host=listener.host,
        port=listener.port,
        ssl_certfile=str(certificate),
        ssl_keyfile=str(key),
        ssl_ca_certs=str(client_ca) if client_ca is not None else None,
        ssl_cert_reqs=cert_reqs,
        ssl_version=ssl.PROTOCOL_TLS_SERVER,
        ssl_context_factory=harden_tls_context,
        log_config=None,
        log_level=log_level,
        access_log=True,
        # h11 bounds a request line plus headers at 16 KiB; httptools (the "auto" choice when
        # installed) accepts any size (spec 2.3 invariant 8).
        # h11, recording each connection's certificate role for the routes (ADR 0031).
        http=peer_role_h11_protocol(),
        h11_max_incomplete_event_size=16 * 1024,
        limit_concurrency=256,
        server_header=False,
        proxy_headers=False,
        lifespan="off",
    )


def run_ingest_api(settings: CoreSettings) -> int:
    """Connect to both stores, build the app and serve until the process is signalled."""
    configure_logging("ingest-api", settings.log_level, settings.log_json)
    check_listener_files(settings.ingest)  # refuse before any store connection is attempted
    app, ledger = _build_app(settings)
    config = build_uvicorn_config(app, settings.ingest, log_level=settings.log_level)
    if not settings.ingest.require_client_cert:
        log.warning("client certificates are not required; this is not a production setting")
    log.info(
        "ingest-api listening",
        host=settings.ingest.host,
        port=settings.ingest.port,
        tenant_id=settings.tenant_id,
    )
    pruner = LedgerPruner(ledger, settings.retention.events_days, lambda: datetime.now(UTC))
    pruner.start()
    try:
        uvicorn.Server(config).run()
    finally:
        pruner.stop()
    return 0


def _build_app(settings: CoreSettings) -> tuple[ASGIApp, PostgresBatchLedger]:
    client = create_client(settings.clickhouse)
    engine = create_engine_from_settings(settings.postgres)
    ledger = PostgresBatchLedger(engine)
    app = create_app(
        settings,
        ClickHouseWriter(client),
        ledger,
        PostgresSourceHealthStore(engine),
        metadata=PostgresMetadataStore(engine),
    )
    return app, ledger
