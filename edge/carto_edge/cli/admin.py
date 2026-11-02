"""Operator commands of ``carto-edge``: keys, local secrets and connector tests.

    carto-edge key status [--state-dir DIR]
    carto-edge key rotate [--overlap-days N] [--state-dir DIR]
    carto-edge key rewrap [--state-dir DIR]
    carto-edge secret set NAME [--from-file PATH] [--state-dir DIR]
    carto-edge secret list [--state-dir DIR]
    carto-edge secret delete NAME [--state-dir DIR]
    carto-edge source test [--config FILE] [--source ID ...] [--state-dir DIR]

Settings come from the environment like the gateway's (``CARTO_STATE_DIR``, ``CARTO_KMS__*``,
``CARTO_SOURCES_FILE``); ``--state-dir`` overrides the state directory for one run. Run these
inside the edge container (``docker compose exec edge-gateway carto-edge key status``) so they
see the same state volume and KMS.

- ``key`` (spec 8.4, ``docs/runbooks/key-rotation.md``): ``status`` prints versions, fingerprints
  and retire dates, never material; ``rotate`` creates version n+1 and keeps n for the overlap
  window (the gateway reads the keyring at startup, so restart it afterwards); ``rewrap`` moves
  every wrapped key to a new KMS key without changing a token: with the local KMS a new master
  key file is generated and the old one is kept as ``local-kms.key.retired`` until the operator
  has verified the backups and destroys it; with Vault Transit, rotate the transit key in Vault
  first, then ``rewrap`` wraps with its latest version.
- ``secret`` (ADR 0013, spec 14.3): the ``local://`` store for Compose pilots. A value is read
  from ``--from-file`` or standard input (a prompt without echo on a terminal), never from an
  argument, so it cannot land in shell history or a process listing; ``list`` prints names only.
- ``source test`` (spec 8.1, 8.1.4): runs each connector's ``test()``: connectivity and the
  read-only verification. A source that cannot be enabled (a write-capable credential, a failed
  check) makes the exit status 1; the gateway's scheduler applies the same gate before polling.

Every change (rotate, rewrap, secret set and delete) is written to the edge audit file (spec 2.3
invariant 6) with the operating-system user as the actor, before it is made: the audit file is
opened first (a broken chain or an unwritable file stops the command with nothing changed), an
``<action>.intent`` row is written, the change is applied, and ``<action>.done`` or
``<action>.failed`` (with the exception type) follows. Exit codes: 0 success, 1 failure, 2
usage. Nothing printed or logged carries key material, a secret value or a record value.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Final, TextIO

from carto_common.crypto import CryptoError, KeyWrapper, KmsProvider, LocalKms, WrappedKey
from carto_edge.audit import AuditChainError, EdgeAudit
from carto_edge.config import ConfigError, EdgeSettings, SourceType, load_sources_file
from carto_edge.connectors.base import ConnectorContext, ConnectorError, ReadConnector, TestResult
from carto_edge.connectors.registry import build_connector
from carto_edge.keys import KeyManagementError, KeyManager, build_kms
from carto_edge.net.ssrf import DefaultNetworkPolicy
from carto_edge.secrets import EdgeSecretResolver, LocalSecretStore, SecretError

__all__ = ["EXIT_FAILURE", "EXIT_OK", "EXIT_USAGE", "MAX_SECRET_BYTES", "add_commands", "run"]

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
EXIT_USAGE: Final = 2
MAX_SECRET_BYTES: Final = 64 * 1024
RETIRED_SUFFIX: Final = ".retired"
NEW_SUFFIX: Final = ".new"
_PUSH_ONLY: Final = frozenset({SourceType.OTLP})

Handler = Callable[[argparse.Namespace, TextIO, TextIO], int]
type _Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


# ---------------------------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------------------------


def _state_dir_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir", type=Path, default=None, help="Edge state directory (default: settings)"
    )


def _leaf(
    group: _Subparsers, name: str, help_text: str, handler: Handler
) -> argparse.ArgumentParser:
    parser = group.add_parser(name, help=help_text, description=f"{help_text}.", allow_abbrev=False)
    _state_dir_option(parser)
    parser.set_defaults(admin=handler)
    return parser


def add_commands(commands: _Subparsers) -> None:
    """Add the ``key``, ``secret`` and ``source`` command groups to ``carto-edge``."""
    key = commands.add_parser("key", help="Tokenization key status, rotation and rewrap (8.4)")
    key_commands = key.add_subparsers(dest="key_command", required=True, metavar="<command>")
    _leaf(key_commands, "status", "Print key versions, fingerprints and retire dates", _key_status)
    rotate = _leaf(key_commands, "rotate", "Create the next key version", _key_rotate)
    rotate.add_argument(
        "--overlap-days",
        type=int,
        default=None,
        help="Days the old version keeps tokenizing (default: max(30, event retention))",
    )
    _leaf(key_commands, "rewrap", "Re-wrap every key under a new KMS key", _key_rewrap)

    secret = commands.add_parser("secret", help="The local:// secret store for pilots (14.3)")
    secret_commands = secret.add_subparsers(
        dest="secret_command", required=True, metavar="<command>"
    )
    put = _leaf(secret_commands, "set", "Store a secret read from a file or stdin", _secret_set)
    put.add_argument("name", help="Secret name, referenced as local://<name>")
    put.add_argument("--from-file", type=Path, default=None, help="Read the value from a file")
    _leaf(secret_commands, "list", "List secret names", _secret_list)
    delete = _leaf(secret_commands, "delete", "Delete a secret", _secret_delete)
    delete.add_argument("name")

    source = commands.add_parser("source", help="Connector checks (8.1)")
    source_commands = source.add_subparsers(
        dest="source_command", required=True, metavar="<command>"
    )
    test = _leaf(
        source_commands, "test", "Test connectivity and read-only access of sources", _source_test
    )
    test.add_argument("--config", type=Path, default=None, help="Sources file (default: settings)")
    test.add_argument(
        "--source", action="append", default=[], metavar="ID", help="Only this source (repeatable)"
    )


def run(args: argparse.Namespace, out: TextIO | None = None, err: TextIO | None = None) -> int:
    """Dispatch a parsed admin command; returns the exit code."""
    handler: Handler = args.admin
    return handler(args, out or sys.stdout, err or sys.stderr)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _settings(args: argparse.Namespace) -> EdgeSettings:
    if args.state_dir is not None:
        return EdgeSettings(state_dir=args.state_dir)
    return EdgeSettings()


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except (KeyError, OSError):  # no passwd entry for the uid (minimal images)
        return "cli:unknown"


class AuditUnavailableError(Exception):
    """The audit file cannot be opened or written; the change was not made."""


@contextlib.contextmanager
def _audited(
    settings: EdgeSettings, action: str, target: str, details: Mapping[str, object]
) -> Iterator[dict[str, object]]:
    """Audit a change around the ``with`` body (module docstring). The body fills the yielded
    mapping with the outcome details. Raises :class:`AuditUnavailableError`, before the body
    runs, when the intent row cannot be written."""
    actor = _actor()
    try:
        audit = EdgeAudit(settings.audit_file)
    except (AuditChainError, OSError) as exc:
        msg = f"cannot open the audit file {settings.audit_file}: {exc}"
        raise AuditUnavailableError(msg) from exc
    with audit:
        try:
            audit.record(f"{action}.intent", actor, target, details)
        except (AuditChainError, OSError) as exc:
            msg = f"cannot write to the audit file {settings.audit_file}: {type(exc).__name__}"
            raise AuditUnavailableError(msg) from exc
        outcome: dict[str, object] = {}
        try:
            yield outcome
        except BaseException as exc:
            audit.record(f"{action}.failed", actor, target, {"error": type(exc).__name__})
            raise
        audit.record(f"{action}.done", actor, target, outcome)


def _fail(err: TextIO, message: str) -> int:
    err.write(f"carto-edge: {message}\n")
    return EXIT_FAILURE


def _usage(err: TextIO, message: str) -> int:
    err.write(f"carto-edge: {message}\n")
    return EXIT_USAGE


# ---------------------------------------------------------------------------------------------
# key
# ---------------------------------------------------------------------------------------------


def _key_status(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        status = KeyManager(_settings(args)).status()
    except (KeyManagementError, CryptoError) as exc:
        return _fail(err, f"key status failed: {exc}")
    out.write(json.dumps(status, indent=2, default=str) + "\n")
    return EXIT_OK


def _key_rotate(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.overlap_days is not None and args.overlap_days < 1:
        return _usage(err, "--overlap-days must be at least 1")
    settings = _settings(args)
    manager = KeyManager(settings)
    try:
        with _audited(
            settings, "key.rotate", "tenant-key", {"overlap_days": args.overlap_days}
        ) as outcome:
            keyring = manager.rotate(args.overlap_days)
            versions = [key.version for key in keyring.tokenization_keys()]
            outcome.update(active_version=keyring.active.version, versions=versions)
        status = manager.status()
    except AuditUnavailableError as exc:
        return _fail(err, f"key rotation refused: {exc}")
    except (KeyManagementError, CryptoError) as exc:
        return _fail(err, f"key rotation failed: {exc}")
    out.write(
        f"active version {keyring.active.version}; tokenizing under {versions}; "
        f"overlap {status['overlap_days']} days. Restart edge-gateway to load the new keyring.\n"
    )
    return EXIT_OK


def _key_rewrap(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    settings = _settings(args)
    manager = KeyManager(settings)
    try:
        old_key_id = manager.kms.key_id
        with _audited(settings, "key.rewrap", "keys", {"old": old_key_id}) as outcome:
            if settings.kms.provider == "local":
                rewritten, new_key_id = _rewrap_local(manager, settings.local_kms_key_file)
            else:
                rewritten = manager.rewrap(manager.kms)
                new_key_id = manager.kms.key_id
            outcome.update(files=[path.name for path in rewritten], new=new_key_id)
    except AuditUnavailableError as exc:
        return _fail(err, f"rewrap refused: {exc}")
    except (KeyManagementError, CryptoError, OSError) as exc:
        return _fail(err, f"rewrap failed: {exc}")
    out.write(f"re-wrapped {len(rewritten)} key files under {new_key_id}; tokens are unchanged.\n")
    if settings.kms.provider == "local":
        retired = settings.local_kms_key_file.with_name(
            settings.local_kms_key_file.name + RETIRED_SUFFIX
        )
        out.write(
            f"The previous master key is {retired}: destroy it once backups taken after this "
            "rewrap are verified.\n"
        )
    return EXIT_OK


def _rewrap_local(manager: KeyManager, master: Path) -> tuple[list[Path], str]:
    """New local master key, rewrap, then swap the master files (old one kept as retired)."""
    fresh_path = master.with_name(master.name + NEW_SUFFIX)
    retired = master.with_name(master.name + RETIRED_SUFFIX)
    if fresh_path.exists() or retired.exists():
        msg = f"{fresh_path.name} or {retired.name} exists from an earlier rewrap; remove it first"
        raise KeyManagementError(msg)
    fresh = LocalKms.create(fresh_path)
    try:
        rewritten = manager.rewrap(fresh)
    except KeyManagementError:
        fresh_path.unlink(missing_ok=True)
        raise
    master.replace(retired)
    fresh_path.replace(master)
    return rewritten, fresh.key_id


# ---------------------------------------------------------------------------------------------
# secret
# ---------------------------------------------------------------------------------------------


def _read_value(args: argparse.Namespace, stdin: TextIO) -> str:
    if args.from_file is not None:
        path: Path = args.from_file
        with path.open("rb") as handle:
            data = handle.read(MAX_SECRET_BYTES + 1)
        if len(data) > MAX_SECRET_BYTES:
            msg = f"the value file is larger than {MAX_SECRET_BYTES} bytes"
            raise SecretError(msg)
        return data.decode("utf-8").rstrip("\r\n")
    if stdin.isatty():
        return getpass.getpass("value (not echoed): ")
    text = stdin.read(MAX_SECRET_BYTES + 1)
    if len(text) > MAX_SECRET_BYTES:
        msg = f"the value on stdin is larger than {MAX_SECRET_BYTES} bytes"
        raise SecretError(msg)
    return text.rstrip("\r\n")


def _open_store(settings: EdgeSettings) -> LocalSecretStore:
    return LocalSecretStore.open(settings, build_kms(settings))


def _secret_set(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    settings = _settings(args)
    try:
        value = _read_value(args, sys.stdin)
    except (OSError, UnicodeDecodeError, SecretError) as exc:
        return _fail(err, f"cannot read the value: {type(exc).__name__}")
    if not value:
        return _usage(err, "the value is empty")
    target = f"local://{args.name}"
    try:
        with (
            _audited(settings, "secret.set", target, {"bytes": len(value.encode("utf-8"))}),
            _open_store(settings) as store,
        ):
            store.put(args.name, value)
    except AuditUnavailableError as exc:
        return _fail(err, f"secret set refused: {exc}")
    except (SecretError, KeyManagementError, CryptoError) as exc:
        return _fail(err, f"secret set failed: {exc}")
    out.write(f"stored local://{args.name}\n")
    return EXIT_OK


def _secret_list(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        with _open_store(_settings(args)) as store:
            names = store.names()
    except (SecretError, KeyManagementError, CryptoError) as exc:
        return _fail(err, f"secret list failed: {exc}")
    out.writelines(f"local://{name}\n" for name in names)
    return EXIT_OK


def _secret_delete(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    settings = _settings(args)
    try:
        with (
            _audited(settings, "secret.delete", f"local://{args.name}", {}) as outcome,
            _open_store(settings) as store,
        ):
            existed = store.delete(args.name)
            outcome["existed"] = existed
    except AuditUnavailableError as exc:
        return _fail(err, f"secret delete refused: {exc}")
    except (SecretError, KeyManagementError, CryptoError) as exc:
        return _fail(err, f"secret delete failed: {exc}")
    if not existed:
        return _fail(err, f"no local secret named {args.name!r}")
    out.write(f"deleted local://{args.name}\n")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------
# source test
# ---------------------------------------------------------------------------------------------


async def _test_one(connector: ReadConnector) -> TestResult:
    try:
        return await connector.test()
    finally:
        await connector.close()


def _source_test(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    settings = _settings(args)
    config_path = args.config if args.config is not None else settings.sources_file
    if config_path is None:
        return _usage(err, "give --config or set CARTO_SOURCES_FILE")
    try:
        sources = load_sources_file(config_path)
    except ConfigError as exc:
        return _usage(err, str(exc))
    known = {source.id for source in sources.sources}
    unknown = sorted(set(args.source) - known)
    if unknown:
        return _usage(err, f"unknown source id(s): {', '.join(unknown)}")
    wanted = [
        source
        for source in sources.sources
        if source.enabled and (not args.source or source.id in args.source)
    ]
    resolver: EdgeSecretResolver | None = None
    failures = 0
    try:
        for source in wanted:
            if source.type in _PUSH_ONLY:
                out.write(f"{source.id} ({source.type.value}): pushed by the collector; skipped\n")
                continue
            if resolver is None:
                resolver = EdgeSecretResolver(settings, _kms_or_none(settings))
            context = ConnectorContext(
                secrets=resolver,
                network=DefaultNetworkPolicy(sources.network.allowed_source_cidrs),
                tenant_id=settings.tenant_id,
            )
            try:
                result = asyncio.run(_test_one(build_connector(source, context)))
            except (ConnectorError, OSError) as exc:
                failures += 1
                out.write(f"{source.id} ({source.type.value}): cannot test: {exc}\n")
                continue
            failures += 0 if result.can_enable else 1
            _print_result(out, source.id, source.type.value, result)
    finally:
        if resolver is not None:
            resolver.close()
    return EXIT_OK if failures == 0 else EXIT_FAILURE


class _NoKms:
    """Stands in when no KMS is configured: only ``vault://`` references can resolve."""

    @property
    def provider(self) -> KmsProvider:
        return "local"

    @property
    def key_id(self) -> str:
        return "none"

    def wrap(self, material: bytes, context: Mapping[str, str]) -> WrappedKey:
        del material, context
        msg = "no KMS is configured on this edge; local:// secrets are unavailable"
        raise CryptoError(msg)

    def unwrap(self, wrapped: WrappedKey) -> bytes:
        del wrapped
        msg = "no KMS is configured on this edge; local:// secrets are unavailable"
        raise CryptoError(msg)


def _kms_or_none(settings: EdgeSettings) -> KeyWrapper:
    try:
        return build_kms(settings)
    except KeyManagementError:
        return _NoKms()


def _print_result(out: TextIO, source_id: str, kind: str, result: TestResult) -> None:
    verdict = "can be enabled" if result.can_enable else "can NOT be enabled"
    out.write(f"{source_id} ({kind}): {verdict}; read-only: {result.read_only.value}\n")
    for check in result.checks:
        mark = "ok  " if check.ok else "FAIL"
        detail = f": {check.detail}" if check.detail else ""
        out.write(f"  [{mark}] {check.name}{detail}\n")
    out.writelines(f"  problem: {problem}\n" for problem in result.problems)
    if result.visible:
        out.write(f"  visible: {len(result.visible)} ({', '.join(result.visible[:10])})\n")
