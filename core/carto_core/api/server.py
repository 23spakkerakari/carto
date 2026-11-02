"""Run ``carto-core api`` under uvicorn with TLS (spec 12, 14.4; ADR 0052).

The listener presents the certificate ``carto-ctl pki init`` issued for ``api`` with TLS 1.2
as the floor (the same context hook as ``ingest-api``). With ``api.require_client_cert`` it
also requires a client certificate signed by ``api.client_ca_file`` (``ssl.CERT_REQUIRED``):
browsers never reach this listener, only the web nginx does (role ``web``), and the
:class:`~carto_core.api.app.PeerRoleMiddleware` then checks the certificate's role per request
(``web`` and ``api`` everywhere, any certificate of the install CA on the health routes),
recorded by the same h11 protocol hook as the ingest listener (ADR 0031). Plain HTTP needs
``api.insecure_http`` and is for a TLS-terminating proxy on a private network or tests. uvicorn
adds no ``Server`` header, trusts no proxy headers (the socket address is what the audit log
records in M2) and logs through the root logger, redaction included (spec 14.12).

The identity stores (sessions, users, role bindings, audit) are the PostgreSQL ones of
:mod:`carto_core.api.stores_postgres` and :mod:`carto_core.audit_postgres`. The data ports
(links, entities, systems, sources, fields) are the adapters of :mod:`carto_core.api.data_ports`
over the metadata store (package A) and the linker's repositories and review service (package
C), built by :func:`data_ports` on the same engine; ``run_api`` takes a ``ports_factory`` so a
test can serve other ports.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import uvicorn

from carto_common.logging import configure_logging, get_logger
from carto_common.mtls import peer_role_h11_protocol
from carto_core.api.app import create_app
from carto_core.api.data_ports import DataPorts, build_data_ports
from carto_core.api.ports import ApiPorts
from carto_core.api.stores_postgres import (
    PostgresRoleBindingStore,
    PostgresSessionStore,
    PostgresUserStore,
)
from carto_core.audit_postgres import PostgresAuditLog
from carto_core.db.postgres import create_engine_from_settings
from carto_core.ingest.server import harden_tls_context
from carto_core.settings import CoreConfigError

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from starlette.types import ASGIApp

    from carto_core.settings import ApiSettings, CoreSettings

__all__ = [
    "DataPorts",
    "build_ports",
    "build_uvicorn_config",
    "check_listener_files",
    "data_ports",
    "run_api",
]

log = get_logger(component="api")


def data_ports(engine: Engine, settings: CoreSettings) -> DataPorts:
    """The production data ports: :func:`carto_core.api.data_ports.build_data_ports` over the
    engine the identity stores share (``key_links``, ``entities``, ``systems``, ``sources``,
    ``fields`` of migrations 0001 and 0004)."""
    return build_data_ports(engine, settings)


def build_ports(engine: Engine, data: DataPorts) -> ApiPorts:
    sessions = PostgresSessionStore(engine)
    audit = PostgresAuditLog(engine)
    return ApiPorts(
        links=data.links,
        entities=data.entities,
        systems=data.systems,
        sources=data.sources,
        fields=data.fields,
        audit=audit,
        audit_recorder=audit,
        sessions=sessions,
        login_attempts=sessions,
        users=PostgresUserStore(engine),
        role_bindings=PostgresRoleBindingStore(engine),
    )


def _require_file(path: Path | None, setting: str) -> Path:
    if path is None:
        msg = f"{setting} is required unless api.insecure_http is set (spec 14.4)"
        raise CoreConfigError(msg)
    if not path.is_file():
        msg = f"{setting} does not exist or is not a file: {path}"
        raise CoreConfigError(msg)
    return path


def check_listener_files(listener: ApiSettings) -> tuple[Path, Path, Path | None] | None:
    """The certificate, key and client CA paths of a TLS listener (``None`` for plain HTTP);
    raises on a missing or unusable one."""
    if listener.insecure_http:
        return None
    certificate = _require_file(listener.tls_cert_file, "api.tls_cert_file")
    key = _require_file(listener.tls_key_file, "api.tls_key_file")
    client_ca = (
        _require_file(listener.client_ca_file, "api.client_ca_file")
        if listener.require_client_cert
        else listener.client_ca_file
    )
    return certificate, key, client_ca


def build_uvicorn_config(
    app: ASGIApp, listener: ApiSettings, *, log_level: str = "info"
) -> uvicorn.Config:
    tls: dict[str, object] = {}
    files = check_listener_files(listener)
    if files is not None:
        certificate, key, client_ca = files
        tls = {
            "ssl_certfile": str(certificate),
            "ssl_keyfile": str(key),
            "ssl_ca_certs": str(client_ca) if client_ca is not None else None,
            "ssl_cert_reqs": ssl.CERT_REQUIRED if listener.require_client_cert else ssl.CERT_NONE,
            "ssl_version": ssl.PROTOCOL_TLS_SERVER,
            "ssl_context_factory": harden_tls_context,
        }
    return uvicorn.Config(
        app,
        host=listener.host,
        port=listener.port,
        log_config=None,
        log_level=log_level,
        access_log=True,
        # h11 bounds a request line plus headers at 16 KiB (spec 2.3 invariant 8); the hook
        # records each connection's certificate role for PeerRoleMiddleware (ADR 0031).
        http=peer_role_h11_protocol(),
        h11_max_incomplete_event_size=16 * 1024,
        limit_concurrency=256,
        server_header=False,
        proxy_headers=False,
        lifespan="off",
        **tls,  # type: ignore[arg-type]
    )


def run_api(
    settings: CoreSettings,
    *,
    ports_factory: Callable[[Engine, CoreSettings], DataPorts] = data_ports,
) -> int:
    """Connect to PostgreSQL, build the app and serve until the process is signalled."""
    configure_logging("api", settings.log_level, settings.log_json)
    listener = settings.api
    check_listener_files(listener)  # refuse before any store connection is attempted
    engine = create_engine_from_settings(settings.postgres)
    ports = build_ports(engine, ports_factory(engine, settings))
    app = create_app(settings, ports)
    config = build_uvicorn_config(app, listener, log_level=settings.log_level)
    if listener.insecure_http:
        log.warning("api serves plain HTTP; this is not a production setting")
    elif not listener.require_client_cert:
        log.warning("client certificates are not required; this is not a production setting")
    if not listener.oidc.issuer:
        log.warning("api.oidc.issuer is not set; nobody can sign in")
    log.info(
        "api listening",
        host=listener.host,
        port=listener.port,
        public_url=listener.public_url,
        tenant_id=settings.tenant_id,
    )
    uvicorn.Server(config).run()
    return 0
