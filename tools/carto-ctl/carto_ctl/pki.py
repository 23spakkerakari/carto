"""``carto-ctl pki init`` and ``pki issue``: a CA per listener and the certificates it signs
(spec 14.4; ADR 0031, 0032).

Each listener named in ``--services`` (:data:`LISTENER_CLIENTS`: ``ingest-api``, ``edge-gateway``,
``api``)
gets its own CA. That CA signs the listener's server certificate and the client certificates of
the services allowed to call it, and the listener trusts nothing else, so a caller meant for
another listener is refused during the TLS handshake. Every certificate also names its service
role (``urn:carto:role:<service>`` when the name is one of :data:`carto_common.mtls.ROLES`), which
each listener checks per route. A service whose name is not a role (a scraper) gets a client
certificate without one for every listener: enough for health and metrics.

Each service gets a directory ``<out>/<service>/`` holding only what it needs, so a deployment can
give each service only its own keys (Compose mounts one volume per directory):

- ``listener.crt``, ``listener.key``, ``listener-ca.crt``: a listener's server certificate (also a
  client certificate for its own health route) and the CA it trusts for its callers;
- ``to-<listener>.crt``, ``to-<listener>.key``, ``to-<listener>-ca.crt``: the client certificate
  for calling ``<listener>`` and that listener's CA, to verify the server.

The CA keys and certificates go to ``--ca-key-dir`` (default ``--out``) as ``<listener>-ca.key``
and ``<listener>-ca.crt``; keeping them where no running service mounts means a compromised
service cannot mint certificates (spec 14.4, 15). Server certificates carry SANs for the service
name, ``localhost`` and any ``--dns``/``--ip`` given. Keys are written with owner-only permissions
and never overwritten: a directory holding any key this run would write is refused before
anything is touched. With ``--if-missing`` a complete earlier run (every file this run would write
is present) is a success that writes nothing, which is what a Compose one-shot job needs on its
second start; a partial one is still refused. The output lists certificate fingerprints only.
Kubernetes installs use cert-manager with one issuer per listener instead (M6).

``carto-ctl pki issue`` signs one client certificate from a listener's CA for a host outside the
stack (a customer's collector, ADR 0032): only a role that listener accepts, written as
``to-<listener>.crt``, ``.key`` and ``-ca.crt``, never over an existing key.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from carto_common.mtls import ROLES
from carto_common.pki import (
    LEAF_VALID_DAYS,
    CertificatePair,
    PkiError,
    certificate_fingerprint,
    create_ca,
    issue_certificate,
    load_certificate,
    load_private_key,
    write_pem,
)
from carto_ctl.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE

if TYPE_CHECKING:
    import datetime as dt

    from carto_ctl.registry import Invocation

__all__ = [
    "DEFAULT_OUT",
    "DEFAULT_SERVICES",
    "LISTENER_CLIENTS",
    "MAX_DAYS",
    "IssuedFile",
    "PkiInitResult",
    "PkiIssueResult",
    "configure_pki_init",
    "configure_pki_issue",
    "initialize_pki",
    "issue_client",
    "pki_complete",
    "pki_init",
    "pki_issue",
]

DEFAULT_SERVICES: Final[tuple[str, ...]] = (
    "edge-gateway",
    "ingest-api",
    "otel-collector",
    "api",
    "web",
)
DEFAULT_OUT: Final = Path("pki")
MAX_DAYS: Final = 3650
LISTENER_CLIENTS: Final[dict[str, tuple[str, ...]]] = {
    "ingest-api": ("edge-gateway",),
    "edge-gateway": ("otel-collector", "collector", "proxy", "api"),
    "api": ("web",),
}
"""Each listener and the roles its CA signs client certificates for (ADR 0031)."""
_SERVICE_PATTERN: Final = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_DNS_PATTERN: Final = re.compile(
    r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*$"
)


@dataclass(frozen=True, slots=True)
class IssuedFile:
    """One certificate written: its path under ``--out``, fingerprint and role."""

    path: str
    fingerprint: str
    role: str | None


@dataclass(frozen=True, slots=True)
class PkiInitResult:
    out: Path
    cas: tuple[tuple[str, str], ...]
    """``(listener, CA certificate fingerprint)`` in the order written."""
    certificates: tuple[IssuedFile, ...]
    san_names: tuple[str, ...]
    san_addresses: tuple[str, ...]
    days: int


def _service_list(text: str) -> tuple[str, ...]:
    names: list[str] = []
    for raw in text.split(","):
        name = raw.strip()
        if not _SERVICE_PATTERN.match(name):
            msg = f"service name must match {_SERVICE_PATTERN.pattern}"
            raise argparse.ArgumentTypeError(msg)
        if name not in names:
            names.append(name)
    return tuple(names)


def _dns_name(text: str) -> str:
    if len(text) > 253 or not _DNS_PATTERN.match(text):
        msg = "not a DNS name"
        raise argparse.ArgumentTypeError(msg)
    return text


def _ip_address(text: str) -> str:
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        msg = "not an IP address"
        raise argparse.ArgumentTypeError(msg) from None


def _days(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        msg = "must be a whole number of days"
        raise argparse.ArgumentTypeError(msg) from None
    if not 1 <= value <= MAX_DAYS:
        msg = f"must be between 1 and {MAX_DAYS}"
        raise argparse.ArgumentTypeError(msg)
    return value


def configure_pki_init(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Directory to write the per-service directories into (default {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--ca-key-dir",
        type=Path,
        default=None,
        help="Directory for the listener CAs' keys, kept away from the services (default: --out)",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Succeed without writing when a complete earlier run is present (Compose jobs)",
    )
    parser.add_argument(
        "--services",
        type=_service_list,
        default=DEFAULT_SERVICES,
        metavar="NAME[,NAME...]",
        help=f"Services to issue for (default {','.join(DEFAULT_SERVICES)})",
    )
    parser.add_argument(
        "--dns",
        action="append",
        type=_dns_name,
        default=[],
        metavar="NAME",
        help="Extra DNS SAN on every listener certificate (repeatable)",
    )
    parser.add_argument(
        "--ip",
        action="append",
        type=_ip_address,
        default=[],
        metavar="ADDR",
        help="Extra IP SAN on every listener certificate (repeatable)",
    )
    parser.add_argument(
        "--days",
        type=_days,
        default=LEAF_VALID_DAYS,
        help=f"Service certificate lifetime in days (default {LEAF_VALID_DAYS}, spec 14.4)",
    )


@dataclass(frozen=True, slots=True)
class _Plan:
    """What one run writes: the listener CAs and, per service, the listener it serves (if
    any) and the listeners it calls."""

    listeners: tuple[str, ...]
    calls: dict[str, tuple[str, ...]]


def _plan(services: tuple[str, ...]) -> _Plan:
    if not services:
        msg = "at least one service is required"
        raise PkiError(msg)
    listeners = tuple(service for service in services if service in LISTENER_CLIENTS)
    calls: dict[str, tuple[str, ...]] = {}
    for service in services:
        if service in ROLES:
            targets = tuple(
                listener
                for listener in listeners
                if listener != service and service in LISTENER_CLIENTS[listener]
            )
        else:
            targets = tuple(listener for listener in listeners if listener != service)
        if not targets and service not in listeners:
            msg = (
                f"{service} serves no listener and calls none of the listeners in --services "
                f"({', '.join(listeners) or 'none'})"
            )
            raise PkiError(msg)
        calls[service] = targets
    return _Plan(listeners=listeners, calls=calls)


def _planned(out: Path, ca_key_dir: Path, plan: _Plan) -> tuple[list[Path], list[Path]]:
    """Every file a run writes, and the private keys among them."""
    files: list[Path] = []
    keys: list[Path] = []
    for listener in plan.listeners:
        files.append(ca_key_dir / f"{listener}-ca.crt")
        keys.append(ca_key_dir / f"{listener}-ca.key")
        folder = out / listener
        files.extend((folder / "listener.crt", folder / "listener-ca.crt"))
        keys.append(folder / "listener.key")
    for service, targets in plan.calls.items():
        folder = out / service
        for target in targets:
            files.extend((folder / f"to-{target}.crt", folder / f"to-{target}-ca.crt"))
            keys.append(folder / f"to-{target}.key")
    return files + keys, keys


def pki_complete(out: Path, services: tuple[str, ...], ca_key_dir: Path | None = None) -> bool:
    """True when every file :func:`initialize_pki` would write for ``services`` exists."""
    try:
        plan = _plan(services)
    except PkiError:
        return False
    files, _keys = _planned(out, ca_key_dir if ca_key_dir is not None else out, plan)
    return all(path.is_file() for path in files)


def _record(
    issued: list[IssuedFile], out: Path, path: Path, pair: CertificatePair, role: str | None
) -> None:
    issued.append(
        IssuedFile(
            path=path.relative_to(out).as_posix(),
            fingerprint=certificate_fingerprint(pair.certificate),
            role=role,
        )
    )


def initialize_pki(
    out: Path,
    services: tuple[str, ...],
    dns_names: tuple[str, ...],
    ip_addresses: tuple[str, ...],
    days: int,
    *,
    ca_key_dir: Path | None = None,
    now: dt.datetime | None = None,
) -> PkiInitResult:
    """Create a CA per listener and every service's certificates; refuse to overwrite any key."""
    plan = _plan(services)
    key_dir = ca_key_dir if ca_key_dir is not None else out
    _files, keys = _planned(out, key_dir, plan)
    existing = [str(path) for path in keys if path.exists()]
    if existing:
        msg = f"refusing to overwrite existing private key(s): {', '.join(existing)}"
        raise PkiError(msg)
    key_dir.mkdir(parents=True, exist_ok=True)
    authorities: dict[str, CertificatePair] = {}
    cas: list[tuple[str, str]] = []
    issued: list[IssuedFile] = []
    for listener in plan.listeners:
        ca = create_ca(f"carto {listener} CA", now=now)
        write_pem(ca, key_dir / f"{listener}-ca.crt", key_dir / f"{listener}-ca.key")
        authorities[listener] = ca
        cas.append((listener, certificate_fingerprint(ca.certificate)))
        names: list[str] = []
        for name in (listener, "localhost", *dns_names):
            if name not in names:
                names.append(name)
        server = issue_certificate(
            ca,
            listener,
            dns_names=tuple(names),
            ip_addresses=ip_addresses,
            role=listener if listener in ROLES else None,
            valid_days=days,
            now=now,
        )
        folder = out / listener
        write_pem(server, folder / "listener.crt", folder / "listener.key")
        (folder / "listener-ca.crt").write_bytes(ca.certificate_pem)
        _record(issued, out, folder / "listener.crt", server, listener)
    for service, targets in plan.calls.items():
        folder = out / service
        role = service if service in ROLES else None
        for target in targets:
            ca = authorities[target]
            client = issue_certificate(
                ca, service, server=False, role=role, valid_days=days, now=now
            )
            write_pem(client, folder / f"to-{target}.crt", folder / f"to-{target}.key")
            (folder / f"to-{target}-ca.crt").write_bytes(ca.certificate_pem)
            _record(issued, out, folder / f"to-{target}.crt", client, role)
    return PkiInitResult(
        out=out,
        cas=tuple(cas),
        certificates=tuple(issued),
        san_names=("<listener>", "localhost", *dns_names),
        san_addresses=tuple(ip_addresses),
        days=days,
    )


def pki_init(invocation: Invocation) -> int:
    args = invocation.args
    services = tuple(args.services)
    if args.if_missing and pki_complete(args.out, services, args.ca_key_dir):
        invocation.out.write(
            f"PKI already present in {args.out} for {', '.join(services)}; nothing written.\n"
        )
        return EXIT_OK
    try:
        result = initialize_pki(
            args.out,
            services,
            tuple(args.dns),
            tuple(args.ip),
            args.days,
            ca_key_dir=args.ca_key_dir,
        )
    except (PkiError, OSError) as exc:
        invocation.err.write(f"carto-ctl pki init: {exc}\n")
        return EXIT_FAILURE
    out = invocation.out
    for listener, fingerprint in result.cas:
        out.write(f"{listener} CA  {fingerprint}\n")
    for issued in result.certificates:
        role = f"role {issued.role}" if issued.role else "no role"
        out.write(f"{issued.path}  {issued.fingerprint}  {role}\n")
    sans = ", ".join((*result.san_names, *result.san_addresses))
    out.write(
        f"wrote {len(result.cas)} listener CAs and {len(result.certificates)} certificates valid "
        f"{result.days} days (listener SANs: {sans}) to {result.out}; keep every *.key private.\n"
    )
    return EXIT_OK


# ---------------------------------------------------------------------------------------------
# pki issue: a client certificate for a host outside the stack (ADR 0032)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PkiIssueResult:
    certificate: Path
    fingerprint: str
    role: str
    listener: str
    days: int


def configure_pki_issue(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--listener",
        choices=sorted(LISTENER_CLIENTS),
        help="The listener the certificate calls; its CA signs it",
    )
    parser.add_argument(
        "--role",
        default="collector",
        help="The role the certificate names; the listener must accept it (default collector)",
    )
    parser.add_argument(
        "--name",
        type=_host_name,
        metavar="NAME",
        help="The certificate's common name, such as the collector host's name",
    )
    parser.add_argument(
        "--ca-key-dir",
        type=Path,
        help="Directory holding <listener>-ca.crt and <listener>-ca.key (pki init --ca-key-dir)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Directory to write to-<listener>.crt, .key and -ca.crt into",
    )
    parser.add_argument(
        "--days",
        type=_days,
        default=LEAF_VALID_DAYS,
        help=f"Certificate lifetime in days (default {LEAF_VALID_DAYS}, spec 14.4)",
    )


def _host_name(text: str) -> str:
    if len(text) > 64 or not _DNS_PATTERN.match(text) or text.startswith("*"):
        msg = "not a host name"
        raise argparse.ArgumentTypeError(msg)
    return text


def _load_authority(ca_key_dir: Path, listener: str) -> CertificatePair:
    certificate_path = ca_key_dir / f"{listener}-ca.crt"
    key_path = ca_key_dir / f"{listener}-ca.key"
    certificate = load_certificate(certificate_path)
    key = load_private_key(key_path)
    if certificate.public_key().public_numbers() != key.public_key().public_numbers():  # type: ignore[union-attr]
        msg = f"{key_path} is not the key of {certificate_path}"
        raise PkiError(msg)
    return CertificatePair(certificate_path.read_bytes(), key_path.read_bytes())


def issue_client(
    listener: str,
    role: str,
    name: str,
    ca_key_dir: Path,
    out: Path,
    days: int,
    *,
    now: dt.datetime | None = None,
) -> PkiIssueResult:
    """Sign a client certificate for ``listener`` with ``role``; refuse a role the listener
    does not accept and an existing key."""
    accepted = LISTENER_CLIENTS.get(listener)
    if accepted is None:
        msg = f"unknown listener {listener!r}"
        raise PkiError(msg)
    if role not in accepted:
        msg = f"{listener} does not accept the role {role!r} (it accepts {', '.join(accepted)})"
        raise PkiError(msg)
    certificate_path = out / f"to-{listener}.crt"
    key_path = out / f"to-{listener}.key"
    if key_path.exists():
        msg = f"refusing to overwrite existing private key {key_path}"
        raise PkiError(msg)
    authority = _load_authority(ca_key_dir, listener)
    client = issue_certificate(authority, name, server=False, role=role, valid_days=days, now=now)
    write_pem(client, certificate_path, key_path)
    (out / f"to-{listener}-ca.crt").write_bytes(authority.certificate_pem)
    return PkiIssueResult(
        certificate=certificate_path,
        fingerprint=certificate_fingerprint(client.certificate),
        role=role,
        listener=listener,
        days=days,
    )


def pki_issue(invocation: Invocation) -> int:
    args = invocation.args
    missing = [
        option
        for option, value in (
            ("--listener", args.listener),
            ("--name", args.name),
            ("--ca-key-dir", args.ca_key_dir),
            ("--out", args.out),
        )
        if value is None
    ]
    if missing:
        invocation.err.write(f"carto-ctl pki issue: required: {', '.join(missing)}\n")
        return EXIT_USAGE
    try:
        result = issue_client(
            args.listener, args.role, args.name, args.ca_key_dir, args.out, args.days
        )
    except (PkiError, OSError) as exc:
        invocation.err.write(f"carto-ctl pki issue: {exc}\n")
        return EXIT_FAILURE
    invocation.out.write(
        f"{result.certificate}  {result.fingerprint}  role {result.role}, calls "
        f"{result.listener}, valid {result.days} days; keep the .key private.\n"
    )
    return EXIT_OK
