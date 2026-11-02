"""``carto-core``: migrations, the services, bundle verification and loading, discovery.

    carto-core migrate [--clickhouse-only | --postgres-only]
    carto-core ingest-api
    carto-core api
    carto-core worker [--once]
    carto-core verify-bundle --bundle PATH
    carto-core load-bundle --bundle PATH [--chunk-size N]
    carto-core discover --out DIR [--bundle PATH] [--as-of TIME]
    carto-core gate-report --predictions DIR --confirmed links.csv [--out PATH]
    carto-core openapi [--out PATH]
    carto-core audit-verify
    carto-core healthcheck --url https://localhost:8443/healthz [--timeout SECONDS]

``api`` serves the console API (spec 12), ``worker`` leases the profiler and linker jobs
(spec 5.3), ``discover`` runs one profile and link pass over the store (loading ``--bundle``
first when given) and writes the eval prediction files, ``gate-report`` compares the partner's
hand-confirmed links with the engine's top proposals (spec 21 gate), ``openapi`` prints the
committed OpenAPI document and ``audit-verify`` walks the audit log's hash chain (spec 14.9).

Settings come from the environment (``CARTO_...``, :class:`carto_core.settings.CoreSettings`);
``verify-bundle`` needs none. ``migrate`` runs with the configured database users, so point
``CARTO_CLICKHOUSE__USER`` and ``CARTO_POSTGRES__USER`` at the users that own the schema for that
one run (spec 14.4: per-service users with least privilege). Exit codes: 0 success, 1 the
operation failed (a store unreachable, a migration refused, a bundle rejected), 2 a usage or
configuration error. Handlers return codes; only ``__main__`` exits.

``healthcheck`` is the container healthcheck of ``ingest-api`` (Compose ``healthcheck.test``): it
GETs an ``https://`` URL presenting the service's own certificate as the client certificate
(``ingest.tls_cert_file``/``tls_key_file``; ``carto-ctl pki init`` issues every leaf for client
and server use) and verifying the server against ``ingest.client_ca_file``, so it works against a
listener that requires mutual TLS. Exit 0 on a 2xx, 1 otherwise; the response body is not read.
"""

from __future__ import annotations

import argparse
import contextlib
import ssl
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import httpx
from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from carto_common.logging import configure_logging
from carto_core.bundle import (
    DEFAULT_CHUNK_SIZE,
    BundleError,
    load_bundle,
    record_bundle_metadata,
    verify_bundle,
)
from carto_core.db.clickhouse import ClickHouseWriter, WriterError, create_client
from carto_core.db.postgres import create_engine_from_settings
from carto_core.ingest.ledger import LedgerError, PostgresBatchLedger
from carto_core.ingest.server import run_ingest_api
from carto_core.metadata import MetadataError, PostgresMetadataStore
from carto_core.migrations import (
    MigrationError,
    apply_clickhouse_migrations,
    apply_postgres_migrations,
    apply_retention,
    migration_lock,
)
from carto_core.settings import (
    ApiSettings,
    CoreConfigError,
    CoreSettings,
    IngestListenerSettings,
)
from carto_schema.ingest import MAX_EVENTS_PER_BATCH

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client

    from carto_core.db.clickhouse import EventWriter
    from carto_core.ingest.ledger import BatchLedger
    from carto_core.metadata import MetadataStore

__all__ = ["EXIT_FAILURE", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]

PROG: Final = "carto-core"
EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
EXIT_USAGE: Final = 2
_MAX_MESSAGE: Final = 300
HEALTHCHECK_TIMEOUT: Final = 5.0


def _emit(text: str) -> None:
    """Print without failing on characters the console encoding cannot show (ADR 0007)."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding))


def _describe(exc: BaseException) -> str:
    """Class name plus the first line of the message, bounded; never a statement's parameters."""
    first_line = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {first_line[:_MAX_MESSAGE]}"


def _fail(message: str) -> int:
    print(f"{PROG}: {message}", file=sys.stderr)
    return EXIT_FAILURE


def _usage(message: str) -> int:
    print(f"{PROG}: {message}", file=sys.stderr)
    return EXIT_USAGE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="carto core: migrations, ingest-api, bundle verification and loading.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="<command>")
    migrate = commands.add_parser(
        "migrate", help="Apply ClickHouse and PostgreSQL migrations and retention TTLs"
    )
    scope = migrate.add_mutually_exclusive_group()
    scope.add_argument("--clickhouse-only", action="store_true")
    scope.add_argument("--postgres-only", action="store_true")
    commands.add_parser("ingest-api", help="Serve the ingest API over mutual TLS")
    commands.add_parser("api", help="Serve the console API (spec 12)")
    worker = commands.add_parser("worker", help="Lease and run profiler and linker jobs")
    worker.add_argument("--once", action="store_true", help="Run the runnable jobs once and exit")
    discover = commands.add_parser(
        "discover", help="One profile and link pass; writes eval prediction files"
    )
    discover.add_argument("--out", type=Path, required=True, help="Prediction directory")
    discover.add_argument("--bundle", type=Path, help="Verify and load this bundle first")
    discover.add_argument(
        "--as-of", help="Window end as ISO 8601 UTC (default: the latest event in the store)"
    )
    gate = commands.add_parser(
        "gate-report", help="Top-3 proposals per confirmed partner link (spec 21 gate)"
    )
    gate.add_argument("--predictions", type=Path, required=True, help="discover --out directory")
    gate.add_argument(
        "--confirmed", type=Path, required=True, help="CSV: system_a,field_a,system_b,field_b"
    )
    gate.add_argument("--out", type=Path, help="Write the Markdown report here (else stdout)")
    openapi = commands.add_parser("openapi", help="Print the OpenAPI document of the console API")
    openapi.add_argument("--out", type=Path, help="Write the document here instead of stdout")
    commands.add_parser("audit-verify", help="Verify the audit log hash chain (spec 14.9)")
    verify = commands.add_parser("verify-bundle", help="Verify a bundle's signature and digests")
    verify.add_argument("--bundle", type=Path, required=True, help="Path of the .carto directory")
    load = commands.add_parser("load-bundle", help="Verify a bundle and load it into ClickHouse")
    load.add_argument("--bundle", type=Path, required=True, help="Path of the .carto directory")
    load.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Events per write, 1 to {MAX_EVENTS_PER_BATCH} (default {DEFAULT_CHUNK_SIZE})",
    )
    health = commands.add_parser(
        "healthcheck", help="GET a health URL over mutual TLS (container healthcheck)"
    )
    health.add_argument("--url", required=True, help="https:// URL, e.g. the service's /healthz")
    health.add_argument(
        "--timeout",
        type=float,
        default=HEALTHCHECK_TIMEOUT,
        help=f"Seconds before giving up (default {HEALTHCHECK_TIMEOUT:g})",
    )
    health.add_argument(
        "--listener",
        choices=("ingest", "api"),
        default="ingest",
        help="Whose certificate files to present and trust (default ingest)",
    )
    return parser


def _settings() -> CoreSettings | int:
    try:
        return CoreSettings()
    except ValidationError as exc:
        return _usage(f"invalid settings: {_describe(exc)}")


def _migrate_clickhouse(client: Client, settings: CoreSettings) -> None:
    applied = apply_clickhouse_migrations(client, settings.retention)
    for name in applied:
        _emit(f"clickhouse: applied {name}")
    changed = apply_retention(client, settings.retention)
    for table in changed:
        _emit(f"clickhouse: retention of {table} set to {settings.retention.events_days} days")
    if not applied and not changed:
        _emit("clickhouse: up to date")


def _migrate(args: argparse.Namespace, settings: CoreSettings) -> int:
    """Both stores under the PostgreSQL advisory lock (``--clickhouse-only`` too, since
    ClickHouse has no lock of its own), so concurrent migrate runs never interleave."""
    configure_logging(PROG, settings.log_level, settings.log_json)
    try:
        with contextlib.ExitStack() as stack:
            client = None
            if not args.postgres_only:
                client = create_client(settings.clickhouse)
                stack.callback(client.close)
            engine = create_engine_from_settings(settings.postgres)
            stack.callback(engine.dispose)
            with migration_lock(engine):
                if client is not None:
                    _migrate_clickhouse(client, settings)
                if not args.clickhouse_only:
                    apply_postgres_migrations(engine)
                    _emit("postgres: at head")
    except (WriterError, MigrationError, SQLAlchemyError, CommandError, CoreConfigError) as exc:
        return _fail(f"migration failed: {_describe(exc)}")
    return EXIT_OK


def _ingest_api(settings: CoreSettings) -> int:
    try:
        return run_ingest_api(settings)
    except (CoreConfigError, WriterError, SQLAlchemyError, OSError) as exc:
        return _fail(f"ingest-api cannot start: {_describe(exc)}")


def _verify(args: argparse.Namespace) -> int:
    try:
        verified = verify_bundle(args.bundle)
    except BundleError as exc:
        return _fail(f"bundle rejected: {exc}")
    manifest = verified.manifest
    _emit(
        f"bundle {manifest.bundle_id} verified: tenant {manifest.tenant_id}, "
        f"{manifest.counts.events} events from {len(manifest.sources)} sources, "
        f"{len(manifest.files)} data files, signature key {verified.signature.key_id}, "
        f"produced by {manifest.producer}"
    )
    return EXIT_OK


@contextmanager
def _open_stores(
    settings: CoreSettings,
) -> Iterator[tuple[EventWriter, BatchLedger, MetadataStore]]:
    """The writer, ledger and metadata store of the configured stores, closed on exit (tests
    substitute in-memory ones)."""
    client = create_client(settings.clickhouse)
    try:
        engine = create_engine_from_settings(settings.postgres)
        try:
            yield (
                ClickHouseWriter(client),
                PostgresBatchLedger(engine),
                PostgresMetadataStore(engine),
            )
        finally:
            engine.dispose()
    finally:
        client.close()


def _load(args: argparse.Namespace, settings: CoreSettings) -> int:
    if not 1 <= args.chunk_size <= MAX_EVENTS_PER_BATCH:
        return _usage(f"--chunk-size must be between 1 and {MAX_EVENTS_PER_BATCH}")
    configure_logging(PROG, settings.log_level, settings.log_json)
    try:
        verified = verify_bundle(args.bundle)
    except BundleError as exc:
        return _fail(f"bundle rejected: {exc}")
    try:
        with _open_stores(settings) as (writer, ledger, metadata):
            result = load_bundle(
                verified,
                writer,
                ledger,
                chunk_size=args.chunk_size,
                retention=settings.retention,
                claim_timeout=timedelta(seconds=settings.ingest.claim_timeout_seconds),
            )
            # Template counts add up (ADR 0056): a re-run that wrote nothing records none.
            recorded = record_bundle_metadata(
                verified, metadata, templates=result.events_written > 0
            )
    except BundleError as exc:
        return _fail(f"bundle rejected while loading: {exc}")
    except (WriterError, LedgerError, MetadataError, SQLAlchemyError, CoreConfigError) as exc:
        return _fail(f"load failed, re-run to resume: {_describe(exc)}")
    _emit(
        f"bundle {verified.manifest.bundle_id} loaded: {result.events_written} events written "
        f"in {result.chunks} chunks, {result.duplicates} chunks already present; "
        f"{recorded.templates} templates and {recorded.fields} fields recorded"
    )
    if result.too_old or result.future:
        _emit(
            f"skipped outside the plausible range (ADR 0036): {result.too_old} events older "
            f"than retention, {result.future} in the future"
        )
    return EXIT_OK


def health_ssl_context(settings: CoreSettings, listener_name: str = "ingest") -> ssl.SSLContext:
    """Client context for :func:`_healthcheck`: the service's own pair as the client
    certificate, the install CA as the trust anchor, TLS 1.2 minimum (spec 14.4). ``api`` probes
    the console API listener with the api service's files."""
    listener: IngestListenerSettings | ApiSettings = (
        settings.api if listener_name == "api" else settings.ingest
    )
    if listener.tls_cert_file is None or listener.tls_key_file is None:
        msg = f"{listener_name}.tls_cert_file and tls_key_file are required for the healthcheck"
        raise CoreConfigError(msg)
    if listener.client_ca_file is None:
        msg = f"{listener_name}.client_ca_file is required for the healthcheck"
        raise CoreConfigError(msg)
    context = ssl.create_default_context(cafile=str(listener.client_ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(listener.tls_cert_file), str(listener.tls_key_file))
    return context


def _health_client(
    settings: CoreSettings, timeout: float, listener_name: str = "ingest"
) -> httpx.Client:
    """The probe's HTTP client (tests substitute one with a mock transport)."""
    return httpx.Client(
        verify=health_ssl_context(settings, listener_name),
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


def _healthcheck(args: argparse.Namespace, settings: CoreSettings) -> int:
    if not str(args.url).startswith("https://"):
        return _usage("--url must be an https:// URL (the listener serves mutual TLS only)")
    if not 0 < args.timeout <= 60:
        return _usage("--timeout must be between 0 and 60 seconds")
    try:
        with _health_client(settings, args.timeout, args.listener) as client:
            response = client.get(args.url)
    except (CoreConfigError, OSError, ssl.SSLError, httpx.HTTPError) as exc:
        return _fail(f"healthcheck failed: {_describe(exc)}")
    if response.is_success:
        return EXIT_OK
    return _fail(f"healthcheck failed: HTTP {response.status_code}")


def _openapi(args: argparse.Namespace) -> int:
    from carto_core.api.openapi import render_openapi  # noqa: PLC0415  (fastapi app import)

    text = render_openapi()
    if args.out is None:
        _emit(text.rstrip("\n"))
        return EXIT_OK
    try:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        return _fail(f"cannot write {args.out}: {_describe(exc)}")
    return EXIT_OK


def _gate_report(args: argparse.Namespace) -> int:
    from carto_core.gate import GateError, gate_report  # noqa: PLC0415

    try:
        text = gate_report(args.predictions, args.confirmed)
    except GateError as exc:
        return _fail(f"gate report failed: {exc}")
    if args.out is None:
        _emit(text.rstrip("\n"))
        return EXIT_OK
    try:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        return _fail(f"cannot write {args.out}: {_describe(exc)}")
    return EXIT_OK


def _api(settings: CoreSettings) -> int:
    from carto_core.api.server import run_api  # noqa: PLC0415

    try:
        return run_api(settings)
    except (CoreConfigError, WriterError, SQLAlchemyError, OSError) as exc:
        return _fail(f"api cannot start: {_describe(exc)}")


def _worker(args: argparse.Namespace, settings: CoreSettings) -> int:
    from carto_core.worker import run_worker  # noqa: PLC0415

    try:
        return run_worker(settings, once=args.once)
    except (CoreConfigError, WriterError, SQLAlchemyError, OSError) as exc:
        return _fail(f"worker cannot start: {_describe(exc)}")


def _discover(args: argparse.Namespace, settings: CoreSettings) -> int:
    from carto_core.discover import DiscoverError, run_discover  # noqa: PLC0415

    configure_logging(PROG, settings.log_level, settings.log_json)
    try:
        summary = run_discover(settings, out_dir=args.out, bundle=args.bundle, as_of=args.as_of)
    except DiscoverError as exc:
        return _fail(f"discover failed: {exc}")
    except (WriterError, LedgerError, SQLAlchemyError, CoreConfigError, BundleError) as exc:
        return _fail(f"discover failed: {_describe(exc)}")
    for line in summary:
        _emit(line)
    return EXIT_OK


def _audit_verify(settings: CoreSettings) -> int:
    from carto_core.audit_postgres import verify_audit_log  # noqa: PLC0415

    try:
        engine = create_engine_from_settings(settings.postgres)
        try:
            problem = verify_audit_log(engine, settings.tenant_id)
        finally:
            engine.dispose()
    except (SQLAlchemyError, CoreConfigError) as exc:
        return _fail(f"audit verify failed: {_describe(exc)}")
    if problem is None:
        _emit("audit log chain intact")
        return EXIT_OK
    return _fail(
        f"audit log chain broken at row {problem.row_id}: expected {problem.expected}, "
        f"found {problem.found}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the exit code instead of exiting."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return EXIT_OK
        return code if isinstance(code, int) else EXIT_USAGE
    if args.command == "verify-bundle":
        return _verify(args)
    if args.command == "openapi":
        return _openapi(args)
    if args.command == "gate-report":
        return _gate_report(args)
    settings = _settings()
    if isinstance(settings, int):
        return settings
    if args.command == "migrate":
        return _migrate(args, settings)
    if args.command == "ingest-api":
        return _ingest_api(settings)
    if args.command == "api":
        return _api(settings)
    if args.command == "worker":
        return _worker(args, settings)
    if args.command == "discover":
        return _discover(args, settings)
    if args.command == "audit-verify":
        return _audit_verify(settings)
    if args.command == "healthcheck":
        return _healthcheck(args, settings)
    return _load(args, settings)


if __name__ == "__main__":
    sys.exit(main())
