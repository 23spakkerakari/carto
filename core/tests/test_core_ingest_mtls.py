"""ingest-api over mutual TLS (spec 12 "Internal", 14.4): a throwaway CA from carto_common.pki,
uvicorn in a thread on a free port; a valid client certificate succeeds, no certificate or a
certificate from another CA is refused at the handshake, plain HTTP and TLS below 1.2 never
connect; a body that trickles past the deadline is answered 408 and the connection is closed
by the server (ADR 0073), which only a real uvicorn and a raw socket can show."""

from __future__ import annotations

import contextlib
import json
import socket
import ssl
import threading
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from carto_common.ids import new_ulid
from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_core.db.clickhouse import WriteResult
from carto_core.ingest.app import create_app
from carto_core.ingest.health import InMemorySourceHealthStore
from carto_core.ingest.ledger import InMemoryBatchLedger
from carto_core.ingest.server import build_uvicorn_config, harden_tls_context
from carto_core.settings import CoreConfigError, CoreSettings, IngestListenerSettings
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch

JSON = {"content-type": "application/json"}


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[list[CanonicalEvent]] = []

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        self.batches.append(list(events))
        return WriteResult(len(events), 0)

    def ping(self) -> bool:
        return True


@dataclass(frozen=True)
class Pki:
    ca_cert: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    stranger_cert: Path
    stranger_key: Path
    collector_cert: Path
    collector_key: Path
    roleless_cert: Path
    roleless_key: Path


@dataclass
class Served:
    url: str
    host: str
    port: int
    writer: FakeWriter


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    root = tmp_path_factory.mktemp("pki")
    ca = create_ca("carto test CA")
    write_pem(ca, root / "ca.crt", root / "ca.key")
    server = issue_certificate(
        ca, "ingest-api", dns_names=("localhost",), ip_addresses=("127.0.0.1",), client=False
    )
    write_pem(server, root / "ingest-api.crt", root / "ingest-api.key")
    client = issue_certificate(ca, "edge-gateway", server=False, role="edge-gateway")
    write_pem(client, root / "edge-gateway.crt", root / "edge-gateway.key")
    collector = issue_certificate(ca, "otel-collector", server=False, role="otel-collector")
    write_pem(collector, root / "otel-collector.crt", root / "otel-collector.key")
    roleless = issue_certificate(ca, "edge-gateway-old", server=False)
    write_pem(roleless, root / "roleless.crt", root / "roleless.key")
    stranger_ca = create_ca("someone else's CA")
    stranger = issue_certificate(stranger_ca, "stranger", server=False)
    write_pem(stranger, root / "stranger.crt", root / "stranger.key")
    return Pki(
        ca_cert=root / "ca.crt",
        server_cert=root / "ingest-api.crt",
        server_key=root / "ingest-api.key",
        client_cert=root / "edge-gateway.crt",
        client_key=root / "edge-gateway.key",
        stranger_cert=root / "stranger.crt",
        stranger_key=root / "stranger.key",
        collector_cert=root / "otel-collector.crt",
        collector_key=root / "otel-collector.key",
        roleless_cert=root / "roleless.crt",
        roleless_key=root / "roleless.key",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _listener(pki: Pki, port: int, **overrides: object) -> IngestListenerSettings:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": port,
        "tls_cert_file": pki.server_cert,
        "tls_key_file": pki.server_key,
        "client_ca_file": pki.ca_cert,
    }
    return IngestListenerSettings.model_validate(values | overrides)


@contextlib.contextmanager
def _serve(pki: Pki, **overrides: object) -> Iterator[Served]:
    """The app under a real uvicorn on a free port, with the listener settings overridden."""
    port = _free_port()
    settings = CoreSettings(ingest=_listener(pki, port, **overrides))
    writer = FakeWriter()
    app = create_app(settings, writer, InMemoryBatchLedger(), InMemorySourceHealthStore())
    server = uvicorn.Server(build_uvicorn_config(app, settings.ingest))
    thread = threading.Thread(target=server.run, name="ingest-api-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("uvicorn did not start")
        time.sleep(0.05)
    try:
        yield Served(f"https://127.0.0.1:{port}", "127.0.0.1", port, writer)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def served(pki: Pki) -> Iterator[Served]:
    with _serve(pki) as running:
        yield running


@pytest.fixture(scope="module")
def served_short_deadline(pki: Pki) -> Iterator[Served]:
    """A listener whose body deadline is one second (ADR 0073), for the trickling client."""
    with _serve(pki, body_timeout_seconds=1) as running:
        yield running


def _context(pki: Pki, certificate: tuple[Path, Path] | None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(pki.ca_cert))
    if certificate is not None:
        context.load_cert_chain(str(certificate[0]), str(certificate[1]))
    return context


def _batch_body() -> bytes:
    batch = IngestBatch(
        schema_version="1",
        tenant_id="default",
        source_id="src_wms_db",
        batch_id=new_ulid(),
        sent_at=datetime.now(UTC),
        events=[CanonicalEvent.example()],
    )
    return batch.model_dump_json().encode("utf-8")


def test_client_with_a_certificate_from_the_ca_is_served(served: Served, pki: Pki) -> None:
    with httpx.Client(verify=_context(pki, (pki.client_cert, pki.client_key))) as client:
        health = client.get(f"{served.url}/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert "server" not in health.headers, "no server banner"
        accepted = client.post(f"{served.url}/internal/ingest", content=_batch_body(), headers=JSON)
        assert accepted.status_code == 200, accepted.text
        assert accepted.json() == {"accepted": 1, "duplicate": False}
    assert len(served.writer.batches) == 1


@pytest.mark.parametrize("who", ["collector", "roleless"])
def test_a_certificate_from_the_ca_without_the_edge_role_cannot_send_batches(
    served: Served, pki: Pki, who: str
) -> None:
    """ADR 0031 over a real TLS connection: the install CA signed both certificates, but only
    the edge-gateway role may post batches; health stays open to any install certificate."""
    pair = (
        (pki.collector_cert, pki.collector_key)
        if who == "collector"
        else (pki.roleless_cert, pki.roleless_key)
    )
    before = len(served.writer.batches)
    with httpx.Client(verify=_context(pki, pair)) as client:
        assert client.get(f"{served.url}/healthz").status_code == 200
        refused = client.post(f"{served.url}/internal/ingest", content=_batch_body(), headers=JSON)
        assert refused.status_code == 403
        assert refused.json()["title"] == "forbidden"
    assert len(served.writer.batches) == before


def test_without_a_client_certificate_the_handshake_is_refused(served: Served, pki: Pki) -> None:
    with (
        httpx.Client(verify=_context(pki, None), timeout=5) as client,
        pytest.raises(httpx.HTTPError),
    ):
        client.get(f"{served.url}/healthz")


def test_a_certificate_from_another_ca_is_refused(served: Served, pki: Pki) -> None:
    with (
        httpx.Client(
            verify=_context(pki, (pki.stranger_cert, pki.stranger_key)), timeout=5
        ) as client,
        pytest.raises(httpx.HTTPError),
    ):
        client.get(f"{served.url}/healthz")


def test_plain_http_is_not_served(served: Served) -> None:
    with httpx.Client(timeout=5) as client, pytest.raises(httpx.HTTPError):
        client.get(f"http://{served.host}:{served.port}/healthz")


def test_tls_below_1_2_is_never_negotiated(served: Served, pki: Pki) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(str(pki.ca_cert))
    context.load_cert_chain(str(pki.client_cert), str(pki.client_key))
    try:
        with warnings.catch_warnings():
            # Offering TLS 1.1 is deprecated, which is the point of this test.
            warnings.simplefilter("ignore", DeprecationWarning)
            context.maximum_version = ssl.TLSVersion.TLSv1_1
    except (ValueError, ssl.SSLError):
        pytest.skip("this platform cannot offer TLS 1.1 at all")
    with (
        pytest.raises((ssl.SSLError, OSError)),
        socket.create_connection((served.host, served.port), timeout=5) as sock,
        context.wrap_socket(sock, server_hostname="127.0.0.1") as tls,
    ):
        tls.do_handshake()


def _read_response(tls: ssl.SSLSocket) -> tuple[str, dict[str, str], bytes]:
    """One HTTP/1.1 response off the socket: the status line, the headers (lower-cased names)
    and the body, read to its Content-Length."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = tls.recv(4096)
        assert chunk, "the server closed the connection before answering"
        raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {
        name.strip().lower(): value.strip()
        for name, value in (line.split(":", 1) for line in header_lines)
    }
    length = int(headers["content-length"])
    while len(body) < length:
        chunk = tls.recv(4096)
        assert chunk, "the server closed the connection mid-body"
        body += chunk
    return status_line, headers, body[:length]


def test_a_trickling_body_is_408_with_connection_close_and_the_server_closes_the_socket(
    served_short_deadline: Served, pki: Pki
) -> None:
    """The 408 of ADR 0073 frees the in-flight slot, but on its own it left the HTTP/1.1
    connection open: h11 keeps keep-alive and uvicorn restarts its idle timer on every byte,
    so a client trickling one byte at a time held one of the 256 connections for ever. The 408
    carries ``Connection: close`` and uvicorn ends the connection after it."""
    context = _context(pki, (pki.client_cert, pki.client_key))
    with (
        socket.create_connection(
            (served_short_deadline.host, served_short_deadline.port), 15
        ) as sock,
        context.wrap_socket(sock, server_hostname="127.0.0.1") as tls,
    ):
        tls.sendall(
            b"POST /internal/ingest HTTP/1.1\r\n"
            b"Host: ingest-api\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: 100000\r\n"
            b"\r\n"
            b"{"
        )
        started = time.monotonic()
        status_line, headers, body = _read_response(tls)
        assert status_line.startswith("HTTP/1.1 408")
        assert time.monotonic() - started < 10, "answered at the deadline, not at the cap"
        assert headers["connection"] == "close"
        assert headers["content-type"].startswith("application/problem+json")
        problem = json.loads(body)
        assert problem["status"] == 408
        assert problem["request_id"] == headers["x-request-id"]
        # The server closed the connection: the next read is end of stream, not a wait.
        try:
            trailing = tls.recv(1)
        except TimeoutError:
            pytest.fail("the connection stayed open after the 408")
        except (ssl.SSLError, ConnectionError):
            trailing = b""
        assert trailing == b""
    # The slot and the connection are both back: an honest batch is served at once.
    with httpx.Client(verify=context) as client:
        accepted = client.post(
            f"{served_short_deadline.url}/internal/ingest", content=_batch_body(), headers=JSON
        )
        assert accepted.status_code == 200, accepted.text
    assert len(served_short_deadline.writer.batches) == 1


def test_server_context_has_tls_1_2_as_the_floor_and_requires_client_certs(pki: Pki) -> None:
    settings = CoreSettings(ingest=_listener(pki, _free_port()))
    app = create_app(settings, FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore())
    config = build_uvicorn_config(app, settings.ingest)
    config.load()
    assert config.ssl is not None
    assert config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
    assert config.ssl.verify_mode == ssl.CERT_REQUIRED
    assert config.log_config is None
    assert config.server_header is False
    assert config.proxy_headers is False


def test_harden_tls_context_only_raises_the_floor() -> None:
    def default() -> ssl.SSLContext:
        return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    context = harden_tls_context(uvicorn.Config(app="x:y"), default)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.maximum_version == ssl.TLSVersion.MAXIMUM_SUPPORTED


def test_missing_tls_files_are_refused_before_listening(pki: Pki, tmp_path: Path) -> None:
    app = create_app(
        CoreSettings(), FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore()
    )
    with pytest.raises(CoreConfigError, match="tls_cert_file"):
        build_uvicorn_config(app, IngestListenerSettings())
    with pytest.raises(CoreConfigError, match="client_ca_file"):
        build_uvicorn_config(
            app,
            IngestListenerSettings(tls_cert_file=pki.server_cert, tls_key_file=pki.server_key),
        )
    with pytest.raises(CoreConfigError, match="tls_key_file"):
        build_uvicorn_config(
            app,
            IngestListenerSettings(
                tls_cert_file=pki.server_cert,
                tls_key_file=tmp_path / "absent.key",
                client_ca_file=pki.ca_cert,
            ),
        )


def test_client_certificate_can_only_be_relaxed_explicitly(pki: Pki) -> None:
    app = create_app(
        CoreSettings(), FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore()
    )
    config = build_uvicorn_config(
        app, _listener(pki, _free_port(), require_client_cert=False, client_ca_file=None)
    )
    assert config.ssl_cert_reqs == ssl.CERT_NONE


def test_uvicorn_config_bounds_headers_and_connections(pki: Pki) -> None:
    settings = CoreSettings(ingest=_listener(pki, _free_port()))
    app = create_app(settings, FakeWriter(), InMemoryBatchLedger(), InMemorySourceHealthStore())
    config = build_uvicorn_config(app, settings.ingest)
    assert isinstance(config.http, type)
    assert issubclass(config.http, H11Protocol)  # h11, recording the certificate role
    assert config.h11_max_incomplete_event_size == 16 * 1024
    assert config.limit_concurrency == 256
