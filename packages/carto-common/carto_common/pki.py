"""Private CA and certificates for internal mutual TLS (spec 14.4).

``carto-ctl pki init`` creates the CA and the per-service certificates for Compose
(cert-manager does the same on Kubernetes, M6). Tests use the same functions to build throwaway
CAs for the edge-to-core mTLS checks. ECDSA P-256 keys, SHA-256 signatures, 90-day leaf
certificates by default (spec 14.4 "90-day certificates, automated rotation"). A leaf can name
its service role in a ``urn:carto:role:<role>`` URI SAN, which the listeners check per route
(:mod:`carto_common.mtls`, ADR 0031).
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from carto_common.crypto import _create_private
from carto_common.mtls import role_uri

__all__ = [
    "CA_VALID_DAYS",
    "LEAF_VALID_DAYS",
    "CertificatePair",
    "PkiError",
    "certificate_fingerprint",
    "create_ca",
    "issue_certificate",
    "load_certificate",
    "load_private_key",
    "write_pem",
]

CA_VALID_DAYS: Final = 3650
LEAF_VALID_DAYS: Final = 90
ORGANIZATION: Final = "carto internal"
_BACKDATE: Final = dt.timedelta(minutes=5)


class PkiError(Exception):
    """A certificate operation failed."""


@dataclass(frozen=True, slots=True)
class CertificatePair:
    """A certificate with its private key, both PEM encoded."""

    certificate_pem: bytes
    private_key_pem: bytes

    @property
    def certificate(self) -> x509.Certificate:
        return x509.load_pem_x509_certificate(self.certificate_pem)

    @property
    def private_key(self) -> ec.EllipticCurvePrivateKey:
        key = serialization.load_pem_private_key(self.private_key_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            msg = "expected an EC private key"
            raise PkiError(msg)
        return key


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZATION),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _key_usage(*, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def create_ca(
    common_name: str = "carto internal CA",
    *,
    valid_days: int = CA_VALID_DAYS,
    now: dt.datetime | None = None,
) -> CertificatePair:
    """A self-signed CA certificate (path length 0) with an ECDSA P-256 key."""
    start = (now or dt.datetime.now(dt.UTC)) - _BACKDATE
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name(common_name)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return CertificatePair(certificate.public_bytes(serialization.Encoding.PEM), _key_pem(key))


def issue_certificate(
    ca: CertificatePair,
    common_name: str,
    *,
    dns_names: tuple[str, ...] = (),
    ip_addresses: tuple[str, ...] = (),
    server: bool = True,
    client: bool = True,
    role: str | None = None,
    valid_days: int = LEAF_VALID_DAYS,
    now: dt.datetime | None = None,
) -> CertificatePair:
    """A leaf certificate signed by ``ca`` for server and/or client authentication, naming
    ``role`` (one of :data:`carto_common.mtls.ROLES`) when given."""
    if not server and not client:
        msg = "a certificate must allow server or client authentication"
        raise PkiError(msg)
    try:
        role_name = role_uri(role) if role is not None else None
    except ValueError as exc:
        raise PkiError(str(exc)) from exc
    start = (now or dt.datetime.now(dt.UTC)) - _BACKDATE
    ca_certificate = ca.certificate
    ca_key = ca.private_key
    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(name) for name in dns_names]
    for address in ip_addresses:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(address)))
        except ValueError as exc:
            msg = f"not an IP address: {address!r}"
            raise PkiError(msg) from exc
    if not sans:
        sans.append(x509.DNSName(common_name))
    if role_name is not None:
        sans.append(x509.UniformResourceIdentifier(role_name))
    usages: list[x509.ObjectIdentifier] = []
    if server:
        usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
    if client:
        usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + dt.timedelta(days=valid_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        .add_extension(_key_usage(ca=False), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return CertificatePair(certificate.public_bytes(serialization.Encoding.PEM), _key_pem(key))


def write_pem(pair: CertificatePair, certificate_path: Path, key_path: Path) -> None:
    """Write both files. The key file is created exclusively with owner-only permissions from
    its first instant (``crypto._create_private``: ``O_EXCL`` and mode 0600 on the ``open``
    itself, so no umask or chmod window ever leaves it readable) and is never overwritten."""
    certificate_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        msg = f"refusing to overwrite existing private key {key_path}"
        raise PkiError(msg)
    certificate_path.write_bytes(pair.certificate_pem)
    try:
        _create_private(key_path, pair.private_key_pem)
    except FileExistsError as exc:  # created between the check and the open
        msg = f"refusing to overwrite existing private key {key_path}"
        raise PkiError(msg) from exc


def load_certificate(path: Path) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(path.read_bytes())
    except (OSError, ValueError) as exc:
        msg = f"cannot load certificate {path}"
        raise PkiError(msg) from exc


def load_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        msg = f"cannot load private key {path}"
        raise PkiError(msg) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        msg = f"{path} is not an EC private key"
        raise PkiError(msg)
    return key


def certificate_fingerprint(certificate: x509.Certificate) -> str:
    """``sha256:<hex>`` of the DER encoding."""
    return "sha256:" + certificate.fingerprint(hashes.SHA256()).hex()
