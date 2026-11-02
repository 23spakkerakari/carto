"""carto_common.pki: private CA and leaf certificates for internal mTLS (spec 14.4)."""

from __future__ import annotations

import datetime as dt
import os
import ssl
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from carto_common.pki import (
    CA_VALID_DAYS,
    LEAF_VALID_DAYS,
    PkiError,
    certificate_fingerprint,
    create_ca,
    issue_certificate,
    load_certificate,
    load_private_key,
    write_pem,
)


def _verify_signature(certificate: x509.Certificate, issuer: x509.Certificate) -> None:
    """Raise if ``certificate`` was not signed by ``issuer``'s EC key."""
    public_key = issuer.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    public_key.verify(
        certificate.signature, certificate.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256())
    )


def test_ca_is_self_signed_with_ca_constraints() -> None:
    ca = create_ca("carto test CA")
    certificate = ca.certificate
    assert certificate.issuer == certificate.subject
    basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic.critical and basic.value.ca and basic.value.path_length == 0
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    assert usage.key_cert_sign and usage.crl_sign
    lifetime = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert lifetime == dt.timedelta(days=CA_VALID_DAYS)
    _verify_signature(certificate, certificate)


def test_leaf_is_signed_by_the_ca_with_sans_and_ekus() -> None:
    ca = create_ca()
    leaf = issue_certificate(
        ca, "ingest-api", dns_names=("ingest-api", "localhost"), ip_addresses=("127.0.0.1",)
    )
    certificate = leaf.certificate
    assert certificate.issuer == ca.certificate.subject
    _verify_signature(certificate, ca.certificate)
    sans = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["ingest-api", "localhost"]
    assert [str(ip) for ip in sans.get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]
    ekus = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert ExtendedKeyUsageOID.SERVER_AUTH in ekus and ExtendedKeyUsageOID.CLIENT_AUTH in ekus
    basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert not basic.ca
    lifetime = certificate.not_valid_after_utc - certificate.not_valid_before_utc
    assert lifetime == dt.timedelta(days=LEAF_VALID_DAYS)


def test_leaf_defaults_san_to_common_name_and_requires_a_purpose() -> None:
    ca = create_ca()
    leaf = issue_certificate(ca, "edge-gateway", server=False, client=True)
    sans = leaf.certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert sans.get_values_for_type(x509.DNSName) == ["edge-gateway"]
    ekus = leaf.certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(ekus) == [ExtendedKeyUsageOID.CLIENT_AUTH]
    with pytest.raises(PkiError, match="server or client"):
        issue_certificate(ca, "x", server=False, client=False)
    with pytest.raises(PkiError, match="not an IP address"):
        issue_certificate(ca, "x", ip_addresses=("not-an-ip",))


def test_write_and_load_pem_refuses_to_overwrite_keys(tmp_path: Path) -> None:
    ca = create_ca()
    write_pem(ca, tmp_path / "pki" / "ca.crt", tmp_path / "pki" / "ca.key")
    assert load_certificate(tmp_path / "pki" / "ca.crt").subject == ca.certificate.subject
    assert load_private_key(tmp_path / "pki" / "ca.key").public_key().public_numbers() == (
        ca.private_key.public_key().public_numbers()
    )
    with pytest.raises(PkiError, match="refusing to overwrite"):
        write_pem(ca, tmp_path / "pki" / "ca.crt", tmp_path / "pki" / "ca.key")
    with pytest.raises(PkiError, match="cannot load"):
        load_certificate(tmp_path / "missing.crt")
    with pytest.raises(PkiError, match="cannot load"):
        load_private_key(tmp_path / "missing.key")


@pytest.fixture
def open_umask() -> Iterator[None]:
    """A permissive umask: the key's mode must come from its creation, not from the process."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.usefixtures("open_umask")
def test_private_key_is_owner_only_from_creation(tmp_path: Path) -> None:
    ca = create_ca()
    write_pem(ca, tmp_path / "ca.crt", tmp_path / "ca.key")
    assert stat.S_IMODE((tmp_path / "ca.key").stat().st_mode) == 0o600
    # The certificate is public: created exclusively with mode 0644, whatever the umask.
    assert stat.S_IMODE((tmp_path / "ca.crt").stat().st_mode) == 0o644


def test_an_existing_key_is_never_overwritten(tmp_path: Path) -> None:
    key_path = tmp_path / "ca.key"
    key_path.write_bytes(b"operator key")
    certificate_path = tmp_path / "ca.crt"
    with pytest.raises(PkiError, match="refusing to overwrite"):
        write_pem(create_ca(), certificate_path, key_path)
    assert key_path.read_bytes() == b"operator key"
    assert not certificate_path.exists()


def _plant_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # no symlink privilege (Windows without developer mode)
        pytest.skip("symlinks are not available here")


def test_a_planted_symlink_at_the_certificate_path_is_refused(tmp_path: Path) -> None:
    """ADR 0070 section 7: the certificate is created exclusively like the key, so a symlink
    planted at its path is neither followed nor replaced, and no key is left behind."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"operator file")
    certificate_path = tmp_path / "ca.crt"
    _plant_symlink(certificate_path, elsewhere)
    key_path = tmp_path / "ca.key"
    with pytest.raises(PkiError, match="refusing to overwrite"):
        write_pem(create_ca(), certificate_path, key_path)
    assert elsewhere.read_bytes() == b"operator file"
    assert certificate_path.is_symlink()
    assert not key_path.exists(), "a key written before the refused certificate is withdrawn"


def test_an_existing_certificate_is_never_overwritten_and_no_key_is_left(tmp_path: Path) -> None:
    certificate_path = tmp_path / "ca.crt"
    certificate_path.write_bytes(b"operator certificate")
    key_path = tmp_path / "ca.key"
    with pytest.raises(PkiError, match="refusing to overwrite"):
        write_pem(create_ca(), certificate_path, key_path)
    assert certificate_path.read_bytes() == b"operator certificate"
    assert not key_path.exists()


def test_the_key_is_written_before_the_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure while the certificate is written leaves no half pair: the key written first
    is withdrawn and the error names the certificate."""
    import carto_common.crypto as crypto_module  # noqa: PLC0415
    import carto_common.pki as pki_module  # noqa: PLC0415

    created: list[str] = []
    real_public = crypto_module.create_public

    def failing_public(path: Path, data: bytes) -> None:
        created.append(path.name)
        msg = "disk gone"
        raise OSError(msg)

    monkeypatch.setattr(pki_module, "create_public", failing_public)
    with pytest.raises(PkiError, match=r"ca\.crt"):
        write_pem(create_ca(), tmp_path / "ca.crt", tmp_path / "ca.key")
    assert created == ["ca.crt"]
    assert not (tmp_path / "ca.key").exists() and not (tmp_path / "ca.crt").exists()
    monkeypatch.setattr(pki_module, "create_public", real_public)
    write_pem(create_ca(), tmp_path / "ca.crt", tmp_path / "ca.key")  # the path is free again


def test_fingerprint_format() -> None:
    ca = create_ca()
    fingerprint = certificate_fingerprint(ca.certificate)
    assert fingerprint == "sha256:" + ca.certificate.fingerprint(hashes.SHA256()).hex()


def test_ssl_contexts_accept_the_chain(tmp_path: Path) -> None:
    """The files load into stdlib ssl contexts the services use (spec 14.4 mutual TLS)."""
    ca = create_ca()
    server = issue_certificate(ca, "ingest-api", dns_names=("localhost",))
    client = issue_certificate(ca, "edge-gateway", server=False)
    write_pem(ca, tmp_path / "ca.crt", tmp_path / "ca.key")
    write_pem(server, tmp_path / "server.crt", tmp_path / "server.key")
    write_pem(client, tmp_path / "client.crt", tmp_path / "client.key")
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(tmp_path / "server.crt", tmp_path / "server.key")
    server_context.load_verify_locations(tmp_path / "ca.crt")
    server_context.verify_mode = ssl.CERT_REQUIRED
    client_context = ssl.create_default_context(cafile=str(tmp_path / "ca.crt"))
    client_context.load_cert_chain(tmp_path / "client.crt", tmp_path / "client.key")
    assert server_context.verify_mode == ssl.CERT_REQUIRED
    # The default floor is the platform's OpenSSL setting (TLS 1.2 on the Windows builds,
    # "minimum supported" on Ubuntu); every carto client and listener sets TLS 1.2 itself.
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    assert client_context.minimum_version == ssl.TLSVersion.TLSv1_2
