"""carto-ctl pki init: a CA per listener, per-service directories and service roles (spec 14.4;
ADR 0031)."""

from __future__ import annotations

import datetime as dt
import os
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from carto_common.pki import certificate_fingerprint, load_certificate, load_private_key
from carto_ctl.cli import main
from carto_ctl.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from carto_ctl.pki import LISTENER_CLIENTS, initialize_pki

STACK = "ingest-api,edge-gateway,otel-collector"


def _signed_by(leaf: x509.Certificate, issuer: x509.Certificate) -> None:
    public_key = issuer.public_key()
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    public_key.verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))


def _not_signed_by(leaf: x509.Certificate, issuer: x509.Certificate) -> None:
    with pytest.raises(InvalidSignature):
        _signed_by(leaf, issuer)


def _files(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()}


def _sans(leaf: x509.Certificate) -> x509.SubjectAlternativeName:
    return leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value


def _role_uris(leaf: x509.Certificate) -> list[str]:
    return _sans(leaf).get_values_for_type(x509.UniformResourceIdentifier)


def _ekus(leaf: x509.Certificate) -> x509.ExtendedKeyUsage:
    return leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value


def _init(out: Path, ca_dir: Path, *extra: str) -> int:
    return main(["pki", "init", "--out", str(out), "--ca-key-dir", str(ca_dir), *extra])


def test_each_listener_gets_its_own_ca_and_each_service_only_what_it_needs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, ca_dir = tmp_path / "pki", tmp_path / "pki-ca"
    argv = ["--services", STACK, "--dns", "core.internal", "--ip", "10.0.0.5"]
    assert _init(out, ca_dir, *argv) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    assert _files(ca_dir) == {
        "ingest-api-ca.crt",
        "ingest-api-ca.key",
        "edge-gateway-ca.crt",
        "edge-gateway-ca.key",
    }
    ingest_ca = load_certificate(ca_dir / "ingest-api-ca.crt")
    gateway_ca = load_certificate(ca_dir / "edge-gateway-ca.crt")
    assert ingest_ca.subject != gateway_ca.subject
    assert f"ingest-api CA  {certificate_fingerprint(ingest_ca)}" in captured.out
    assert _files(out) == {"ingest-api", "edge-gateway", "otel-collector"}
    assert _files(out / "ingest-api") == {"listener.crt", "listener.key", "listener-ca.crt"}
    assert _files(out / "edge-gateway") == {
        "listener.crt",
        "listener.key",
        "listener-ca.crt",
        "to-ingest-api.crt",
        "to-ingest-api.key",
        "to-ingest-api-ca.crt",
    }
    assert _files(out / "otel-collector") == {
        "to-edge-gateway.crt",
        "to-edge-gateway.key",
        "to-edge-gateway-ca.crt",
    }
    for listener, ca in (("ingest-api", ingest_ca), ("edge-gateway", gateway_ca)):
        server = load_certificate(out / listener / "listener.crt")
        load_private_key(out / listener / "listener.key")
        _signed_by(server, ca)
        assert (out / listener / "listener-ca.crt").read_bytes() == (
            ca_dir / f"{listener}-ca.crt"
        ).read_bytes()
        assert _sans(server).get_values_for_type(x509.DNSName) == [
            listener,
            "localhost",
            "core.internal",
        ]
        assert [str(ip) for ip in _sans(server).get_values_for_type(x509.IPAddress)] == ["10.0.0.5"]
        assert _role_uris(server) == [f"urn:carto:role:{listener}"]
        assert ExtendedKeyUsageOID.SERVER_AUTH in _ekus(server)
        assert ExtendedKeyUsageOID.CLIENT_AUTH in _ekus(server)  # its own health probe
        assert server.not_valid_after_utc - server.not_valid_before_utc == dt.timedelta(days=90)
        assert f"{listener}/listener.crt  {certificate_fingerprint(server)}" in captured.out
    # The gateway calls ingest-api with a certificate from the ingest CA only.
    to_ingest = load_certificate(out / "edge-gateway" / "to-ingest-api.crt")
    _signed_by(to_ingest, ingest_ca)
    _not_signed_by(to_ingest, gateway_ca)
    assert _role_uris(to_ingest) == ["urn:carto:role:edge-gateway"]
    assert ExtendedKeyUsageOID.SERVER_AUTH not in _ekus(to_ingest)
    # The collector holds a gateway-CA certificate only: it can never reach ingest-api.
    to_gateway = load_certificate(out / "otel-collector" / "to-edge-gateway.crt")
    _signed_by(to_gateway, gateway_ca)
    _not_signed_by(to_gateway, ingest_ca)
    assert _role_uris(to_gateway) == ["urn:carto:role:otel-collector"]
    assert "-----BEGIN" not in captured.out
    assert "PRIVATE" not in captured.out


def test_the_listener_table_matches_the_adr() -> None:
    assert LISTENER_CLIENTS == {
        "ingest-api": ("edge-gateway",),
        "edge-gateway": ("otel-collector", "collector", "proxy", "api"),
        "api": ("web",),
    }


def test_a_service_that_is_not_a_role_gets_roleless_certificates_for_every_listener(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, ca_dir = tmp_path / "pki", tmp_path / "pki-ca"
    assert _init(out, ca_dir, "--services", f"{STACK},scraper") == EXIT_OK
    assert _files(out / "scraper") == {
        "to-ingest-api.crt",
        "to-ingest-api.key",
        "to-ingest-api-ca.crt",
        "to-edge-gateway.crt",
        "to-edge-gateway.key",
        "to-edge-gateway-ca.crt",
    }
    assert _role_uris(load_certificate(out / "scraper" / "to-ingest-api.crt")) == []
    assert "scraper/to-ingest-api.crt" in capsys.readouterr().out


def test_a_service_with_nothing_to_call_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, ca_dir = tmp_path / "pki", tmp_path / "pki-ca"
    # otel-collector calls only edge-gateway, which is not in --services.
    assert _init(out, ca_dir, "--services", "ingest-api,otel-collector") == EXIT_FAILURE
    assert "otel-collector" in capsys.readouterr().err
    assert not out.exists()


def test_ca_key_dir_defaults_to_out(tmp_path: Path) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out), "--services", STACK]) == EXIT_OK
    assert {"ingest-api-ca.key", "edge-gateway-ca.key"} <= _files(out)


def test_if_missing_succeeds_on_a_complete_run_and_refuses_a_partial_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, ca_dir = tmp_path / "pki", tmp_path / "pki-ca"
    argv = ("--services", STACK, "--if-missing")
    assert _init(out, ca_dir, *argv) == EXIT_OK
    first = {path: path.read_bytes() for path in out.rglob("*") if path.is_file()}
    capsys.readouterr()
    assert _init(out, ca_dir, *argv) == EXIT_OK  # the Compose job on its second start
    assert "nothing written" in capsys.readouterr().out
    assert {path: path.read_bytes() for path in out.rglob("*") if path.is_file()} == first
    (out / "otel-collector" / "to-edge-gateway-ca.crt").unlink()  # never papered over
    assert _init(out, ca_dir, *argv) == EXIT_FAILURE
    assert "refusing to overwrite" in capsys.readouterr().err
    assert _init(out, ca_dir, "--services", STACK) == EXIT_FAILURE


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_private_keys_are_owner_only(tmp_path: Path) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out)]) == EXIT_OK
    keys = list(out.rglob("*.key"))
    assert keys
    for key in keys:
        assert stat.S_IMODE(key.stat().st_mode) == 0o600, key


def test_pki_init_refuses_to_overwrite_any_existing_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out)]) == EXIT_OK
    before = {path: path.read_bytes() for path in out.rglob("*") if path.is_file()}
    capsys.readouterr()
    assert main(["pki", "init", "--out", str(out)]) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to overwrite" in captured.err
    assert {path: path.read_bytes() for path in out.rglob("*") if path.is_file()} == before
    # One stale service key is enough, and nothing is written before the check.
    other = tmp_path / "other"
    (other / "api").mkdir(parents=True)
    (other / "api" / "to-edge-gateway.key").write_bytes(b"stale")
    assert main(["pki", "init", "--out", str(other)]) == EXIT_FAILURE
    assert "to-edge-gateway.key" in capsys.readouterr().err
    assert {path.name for path in other.rglob("*")} == {"api", "to-edge-gateway.key"}


def test_custom_lifetime(tmp_path: Path) -> None:
    out = tmp_path / "pki"
    argv = ["pki", "init", "--out", str(out), "--services", STACK, "--days", "30"]
    assert main(argv) == EXIT_OK
    leaf = load_certificate(out / "otel-collector" / "to-edge-gateway.crt")
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == dt.timedelta(days=30)
    ca = load_certificate(out / "edge-gateway-ca.crt")
    assert ca.not_valid_after_utc - ca.not_valid_before_utc == dt.timedelta(days=3650)


@pytest.mark.parametrize(
    "argv",
    [
        ["--services", "Edge"],
        ["--services", "a,,b"],
        ["--services", "-edge"],
        ["--ip", "nope"],
        ["--days", "0"],
        ["--days", "4000"],
        ["--days", "ninety"],
        ["--dns", "bad name"],
        ["--dns", "-core.internal"],
        ["--layout", "flat"],  # removed with the per-listener CAs (ADR 0031 amendment)
    ],
)
def test_bad_options_are_usage_errors_and_write_nothing(
    tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "pki"
    assert main(["pki", "init", "--out", str(out), *argv]) == EXIT_USAGE
    assert not out.exists()
    assert "usage:" in capsys.readouterr().err


def test_default_out_is_pki_in_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["pki", "init"]) == EXIT_OK
    assert (tmp_path / "pki" / "edge-gateway" / "listener.crt").is_file()
    assert (tmp_path / "pki" / "api" / "to-edge-gateway.crt").is_file()
    assert "pki" in capsys.readouterr().out


def test_initialize_pki_dedupes_san_names_and_keeps_order(tmp_path: Path) -> None:
    result = initialize_pki(
        tmp_path / "p",
        ("edge-gateway",),
        ("localhost", "edge-gateway", "x.internal"),
        ("127.0.0.1",),
        90,
    )
    assert [listener for listener, _fp in result.cas] == ["edge-gateway"]
    leaf = load_certificate(tmp_path / "p" / "edge-gateway" / "listener.crt")
    assert _sans(leaf).get_values_for_type(x509.DNSName) == [
        "edge-gateway",
        "localhost",
        "x.internal",
    ]
    assert [str(ip) for ip in _sans(leaf).get_values_for_type(x509.IPAddress)] == ["127.0.0.1"]


def test_help_lists_the_options_and_no_longer_says_it_is_a_stub(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COLUMNS", "200")
    assert main(["pki", "init", "--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for option in ("--out", "--services", "--dns", "--ip", "--days", "--ca-key-dir"):
        assert option in out
    assert "--layout" not in out
    assert "Arrives in" not in out
    assert "(spec 14.4)" in out


# ---------------------------------------------------------------------------------------------
# pki issue (ADR 0032)
# ---------------------------------------------------------------------------------------------


def test_pki_issue_signs_a_collector_certificate_from_the_gateway_ca(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, ca_dir, host = tmp_path / "pki", tmp_path / "pki-ca", tmp_path / "host-a"
    assert _init(out, ca_dir, "--services", STACK) == EXIT_OK
    capsys.readouterr()
    argv = ["pki", "issue", "--listener", "edge-gateway", "--name", "collector-a.example.internal"]
    argv += ["--ca-key-dir", str(ca_dir), "--out", str(host), "--days", "30"]
    assert main(argv) == EXIT_OK
    assert _files(host) == {"to-edge-gateway.crt", "to-edge-gateway.key", "to-edge-gateway-ca.crt"}
    leaf = load_certificate(host / "to-edge-gateway.crt")
    _signed_by(leaf, load_certificate(ca_dir / "edge-gateway-ca.crt"))
    _not_signed_by(leaf, load_certificate(ca_dir / "ingest-api-ca.crt"))
    assert _role_uris(leaf) == ["urn:carto:role:collector"]
    assert ExtendedKeyUsageOID.SERVER_AUTH not in _ekus(leaf)
    assert leaf.not_valid_after_utc - leaf.not_valid_before_utc == dt.timedelta(days=30)
    printed = capsys.readouterr().out
    assert certificate_fingerprint(leaf) in printed and "role collector" in printed
    assert "PRIVATE" not in printed
    assert main(argv) == EXIT_FAILURE  # never over an existing key
    assert "refusing to overwrite" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("listener", "role"),
    [("edge-gateway", "edge-gateway"), ("edge-gateway", "ingest-api"), ("ingest-api", "collector")],
)
def test_pki_issue_refuses_a_role_the_listener_does_not_accept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], listener: str, role: str
) -> None:
    out, ca_dir, host = tmp_path / "pki", tmp_path / "pki-ca", tmp_path / "host"
    assert _init(out, ca_dir, "--services", STACK) == EXIT_OK
    capsys.readouterr()
    argv = ["pki", "issue", "--listener", listener, "--role", role, "--name", "h"]
    assert main([*argv, "--ca-key-dir", str(ca_dir), "--out", str(host)]) == EXIT_FAILURE
    assert "does not accept" in capsys.readouterr().err
    assert not host.exists()


def test_pki_issue_refuses_a_ca_key_that_does_not_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, ca_dir = tmp_path / "pki", tmp_path / "pki-ca"
    assert _init(out, ca_dir, "--services", STACK) == EXIT_OK
    swapped = tmp_path / "swapped"
    swapped.mkdir()
    (swapped / "edge-gateway-ca.crt").write_bytes((ca_dir / "edge-gateway-ca.crt").read_bytes())
    (swapped / "edge-gateway-ca.key").write_bytes((ca_dir / "ingest-api-ca.key").read_bytes())
    capsys.readouterr()
    argv = ["pki", "issue", "--listener", "edge-gateway", "--name", "h", "--ca-key-dir"]
    assert main([*argv, str(swapped), "--out", str(tmp_path / "h")]) == EXIT_FAILURE
    assert "is not the key of" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--name", "bad name"], ["--name", "*.x"], ["--listener", "x"]])
def test_pki_issue_bad_options_are_usage_errors(
    tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    base = {"--listener": "edge-gateway", "--name": "h"}
    base.update(dict(zip(argv[::2], argv[1::2], strict=True)))
    flat = [item for pair in base.items() for item in pair]
    command = ["pki", "issue", *flat, "--ca-key-dir", str(tmp_path), "--out", str(tmp_path / "o")]
    assert main(command) == EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def test_pki_issue_names_the_missing_options(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pki", "issue", "--listener", "edge-gateway"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "--name" in err and "--ca-key-dir" in err and "--out" in err
    assert "--listener," not in err
