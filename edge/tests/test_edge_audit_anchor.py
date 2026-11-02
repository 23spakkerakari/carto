"""ADR 0033: the edge audit anchor (log line, RFC 5424/5425 syslog target, ``audit verify
--anchor``) and recovery of a torn final line."""

from __future__ import annotations

import io
import ipaddress
import json
import socket
import ssl
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from carto_edge.audit import (
    GENESIS_HASH,
    RECOVERED_ACTION,
    AuditAnchor,
    AuditChainError,
    EdgeAudit,
    parse_anchor,
    verify,
)
from carto_edge.audit_anchor import (
    SYSLOG_PRI,
    AnchorPublisher,
    SyslogAnchorSender,
    anchor_log_line,
    frame_rfc5425,
    syslog_message,
)
from carto_edge.cli.main import main
from carto_edge.config import AnchorSyslogSettings, EdgeSettings

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


def write_rows(path: Path, count: int) -> list[str]:
    with EdgeAudit(path) as audit:
        for index in range(count):
            audit.record("reveal", f"user:{index}", "vault", {"revealed": 1})
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def anchor_of(path: Path) -> AuditAnchor:
    with EdgeAudit(path) as audit:
        return audit.anchor()


# ---------------------------------------------------------------------------------------------
# Anchors catch what the chain alone cannot
# ---------------------------------------------------------------------------------------------


def test_truncating_the_last_rows_fails_an_anchor_but_not_the_chain(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    lines = write_rows(path, 5)
    anchor = anchor_of(path)
    assert anchor.seq == 5
    path.write_text("".join(lines[:2]), encoding="utf-8")
    plain = verify(path)
    assert plain.ok  # the chain alone cannot tell
    anchored = verify(path, [(anchor.seq, anchor.row_hash)])
    assert not anchored.ok
    assert [check.status for check in anchored.anchors] == ["missing"]


def test_rewriting_a_row_and_every_later_hash_fails_an_anchor(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    write_rows(path, 3)
    anchor = anchor_of(path)
    path.unlink()
    with EdgeAudit(path) as audit:  # a forged chain: same length, other content
        for index in range(3):
            audit.record("reveal", f"someone-else:{index}", "vault", {"revealed": 0})
    assert verify(path).ok
    anchored = verify(path, [(anchor.seq, anchor.row_hash)])
    assert not anchored.ok
    assert anchored.anchors[0].status == "mismatch"


def test_anchors_that_hold_and_the_empty_anchor(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    write_rows(path, 2)
    first = anchor_of(path)
    write_rows(path, 1)
    second = anchor_of(path)
    result = verify(path, [(first.seq, first.row_hash), (second.seq, second.row_hash)])
    assert result.ok
    assert [check.status for check in result.anchors] == ["ok", "ok"]
    empty = tmp_path / "empty.ndjson"
    assert anchor_of(empty) == AuditAnchor(0, GENESIS_HASH, "")
    assert verify(empty, [(0, GENESIS_HASH)]).ok


def test_parse_anchor() -> None:
    digest = "ab" * 32
    assert parse_anchor(f"12:{digest}") == (12, digest)
    assert parse_anchor(f" 12:{digest.upper()} ") == (12, digest)
    for bad in ("12", f"x:{digest}", "12:abc", f"-1:{digest}", f"12:{digest}0"):
        with pytest.raises(ValueError, match="anchor"):
            parse_anchor(bad)


# ---------------------------------------------------------------------------------------------
# Torn final line
# ---------------------------------------------------------------------------------------------


def test_a_torn_final_line_is_moved_aside_and_recorded(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    lines = write_rows(path, 3)
    torn = lines[0][:20]
    path.write_text("".join(lines) + torn, encoding="utf-8")
    assert verify(path).torn_tail_bytes == len(torn)

    with EdgeAudit(path) as audit:
        assert audit.seq == 4  # three rows plus audit.recovered
        audit.record("reveal", "user:9", "vault", {"revealed": 1})

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert rows[3]["action"] == RECOVERED_ACTION
    assert rows[3]["details"] == {"torn_bytes": len(torn)}
    assert verify(path).ok and verify(path).rows == 5
    kept = list(tmp_path.glob("audit.ndjson.torn-*"))
    assert len(kept) == 1
    assert kept[0].read_text(encoding="utf-8") == torn


def test_a_torn_only_line_in_a_new_file_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    path.write_text('{"seq":1,"torn', encoding="utf-8")
    with EdgeAudit(path) as audit:
        assert audit.seq == 1
    assert verify(path).ok


def test_a_final_row_that_lost_only_its_newline_gets_it_back(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    lines = write_rows(path, 2)
    path.write_text("".join(lines).rstrip("\n"), encoding="utf-8")
    with EdgeAudit(path) as audit:
        audit.record("reveal", "user:3", "vault", {})
    assert verify(path).ok and verify(path).rows == 3
    assert not list(tmp_path.glob("audit.ndjson.torn-*"))


@pytest.mark.parametrize("where", ["middle", "unterminated_json"])
def test_any_other_mismatch_still_refuses_to_open(tmp_path: Path, where: str) -> None:
    path = tmp_path / "audit.ndjson"
    lines = write_rows(path, 3)
    if where == "middle":
        lines[1] = lines[1][:20] + "\n"
        path.write_text("".join(lines), encoding="utf-8")
        line = 2
    else:  # parses as JSON but does not chain: tampering, not a torn write
        path.write_text("".join(lines) + '{"seq": 4}', encoding="utf-8")
        line = 4
    with pytest.raises(AuditChainError, match=f"line {line}"):
        EdgeAudit(path)
    assert not list(tmp_path.glob("audit.ndjson.torn-*"))


# ---------------------------------------------------------------------------------------------
# Publishing: the log line and the syslog target
# ---------------------------------------------------------------------------------------------


def test_anchor_log_line_is_exact_and_unmasked(tmp_path: Path) -> None:
    path = tmp_path / "audit.ndjson"
    write_rows(path, 2)
    stream = io.StringIO()
    with EdgeAudit(path) as audit:
        anchor = AnchorPublisher(audit, stream=stream, clock=lambda: NOW).publish()
    assert anchor is not None
    line = json.loads(stream.getvalue())
    assert line["event"] == "audit.anchor"
    assert line["seq"] == 2
    assert line["row_hash"] == anchor.row_hash  # the processor would have shaped this hex
    assert line["ts"] == anchor.ts
    assert line["timestamp"] == "2026-10-12T09:00:00Z"
    assert verify(path, [parse_anchor(f"{line['seq']}:{line['row_hash']}")]).ok


def test_the_bypassing_line_refuses_anything_but_an_anchor() -> None:
    with pytest.raises(ValueError, match="row_hash"):
        anchor_log_line(AuditAnchor(1, "CUST-88210", ""), "edge-gateway", NOW)
    with pytest.raises(ValueError, match="ts"):
        anchor_log_line(AuditAnchor(1, "a" * 64, "jane@example.com"), "edge-gateway", NOW)
    with pytest.raises(ValueError, match="seq"):
        anchor_log_line(AuditAnchor(-1, "a" * 64, ""), "edge-gateway", NOW)


def test_syslog_message_is_rfc5424_with_octet_counting() -> None:
    anchor = AuditAnchor(7, "c" * 64, "2026-10-12T08:59:59.123+00:00")
    message = syslog_message(anchor, NOW, hostname="edge-1")
    text = message.decode("ascii")
    assert text.startswith(f"<{SYSLOG_PRI}>1 2026-10-12T09:00:00.000000Z edge-1 carto-edge ")
    assert " audit.anchor - event=audit.anchor seq=7 row_hash=" + "c" * 64 in text
    assert text.endswith("ts=2026-10-12T08:59:59.123+00:00")
    framed = frame_rfc5425(message)
    length, _, rest = framed.partition(b" ")
    assert int(length) == len(rest) == len(message)


class _Collector:
    """A one-connection-at-a-time TCP (optionally TLS) syslog receiver."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        self._context = context
        self.frames: list[bytes] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            try:
                if self._context is not None:
                    connection = self._context.wrap_socket(connection, server_side=True)
                chunks = []
                while chunk := connection.recv(65536):
                    chunks.append(chunk)
                self.frames.append(b"".join(chunks))
            except (OSError, ssl.SSLError):
                pass
            finally:
                connection.close()

    def close(self) -> None:
        self._server.close()


def _self_signed(tmp_path: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file = tmp_path / "syslog.crt"
    key_file = tmp_path / "syslog.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


@pytest.fixture
def tls_collector(tmp_path: Path) -> Iterator[tuple[_Collector, Path]]:
    cert_file, key_file = _self_signed(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    collector = _Collector(context)
    yield collector, cert_file
    collector.close()


def _wait_for(collector: _Collector, count: int) -> None:
    for _ in range(200):
        if len(collector.frames) >= count:
            return
        threading.Event().wait(0.01)
    raise AssertionError("the collector received nothing")


def test_syslog_target_receives_the_anchor_over_tls(
    tmp_path: Path, tls_collector: tuple[_Collector, Path]
) -> None:
    collector, ca_file = tls_collector
    settings = AnchorSyslogSettings(host="127.0.0.1", port=collector.port, ca_file=ca_file)
    path = tmp_path / "audit.ndjson"
    write_rows(path, 1)
    with EdgeAudit(path) as audit:
        sender = SyslogAnchorSender(settings, clock=lambda: NOW, hostname="edge-1")
        anchor = AnchorPublisher(audit, sender=sender, stream=io.StringIO()).publish()
    assert anchor is not None
    _wait_for(collector, 1)
    length, _, message = collector.frames[0].partition(b" ")
    assert int(length) == len(message)
    assert f"seq=1 row_hash={anchor.row_hash}".encode("ascii") in message


def test_a_failed_syslog_send_is_logged_and_the_next_anchor_is_sent(
    tmp_path: Path, tls_collector: tuple[_Collector, Path]
) -> None:
    collector, ca_file = tls_collector
    path = tmp_path / "audit.ndjson"
    write_rows(path, 1)
    unreachable = socket.create_server(("127.0.0.1", 0))
    dead_port = unreachable.getsockname()[1]
    unreachable.close()
    stream = io.StringIO()
    with EdgeAudit(path) as audit:
        dead = SyslogAnchorSender(
            AnchorSyslogSettings(host="127.0.0.1", port=dead_port, tls=False, timeout_seconds=1)
        )
        assert AnchorPublisher(audit, sender=dead, stream=stream).publish() is not None
        audit.record("reveal", "user:2", "vault", {})
        live = SyslogAnchorSender(
            AnchorSyslogSettings(host="127.0.0.1", port=collector.port, ca_file=ca_file)
        )
        AnchorPublisher(audit, sender=live, stream=stream).publish()
    assert len(stream.getvalue().splitlines()) == 2  # both anchors reached the product log
    _wait_for(collector, 1)
    assert b"seq=2 " in collector.frames[0]


def test_tls_is_verified_against_the_ca(
    tmp_path: Path, tls_collector: tuple[_Collector, Path]
) -> None:
    collector, _ca_file = tls_collector
    sender = SyslogAnchorSender(AnchorSyslogSettings(host="127.0.0.1", port=collector.port))
    with pytest.raises(ssl.SSLError):
        sender.send(AuditAnchor(1, "a" * 64, ""))


def test_anchor_syslog_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARTO_AUDIT__ANCHOR_SYSLOG__HOST", "siem.example.internal")
    monkeypatch.setenv("CARTO_AUDIT__ANCHOR_SYSLOG__PORT", "6514")
    target = EdgeSettings().audit.anchor_syslog
    assert target is not None
    assert (target.host, target.port, target.tls) == ("siem.example.internal", 6514, True)
    assert EdgeSettings(_env_file=None).audit.anchor_syslog is not None
    monkeypatch.delenv("CARTO_AUDIT__ANCHOR_SYSLOG__HOST")
    monkeypatch.delenv("CARTO_AUDIT__ANCHOR_SYSLOG__PORT")
    assert EdgeSettings().audit.anchor_syslog is None


# ---------------------------------------------------------------------------------------------
# carto-edge audit verify --anchor
# ---------------------------------------------------------------------------------------------


def test_cli_verify_with_anchors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "audit.ndjson"
    lines = write_rows(path, 4)
    anchor = anchor_of(path)
    assert main(["audit", "verify", "--file", str(path), "--anchor", anchor.text]) == 0
    assert f"anchor {anchor.seq}: holds" in capsys.readouterr().out
    path.write_text("".join(lines[:3]), encoding="utf-8")
    assert main(["audit", "verify", "--file", str(path), "--anchor", anchor.text]) == 1
    out = capsys.readouterr().out
    assert "3 rows, intact" in out
    assert f"anchor {anchor.seq}: MISSING" in out


def test_cli_verify_reports_a_torn_tail_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "audit.ndjson"
    lines = write_rows(path, 2)
    path.write_text("".join(lines) + '{"seq":3,', encoding="utf-8")
    assert main(["audit", "verify", "--file", str(path)]) == 0
    assert "torn final line of 9 bytes" in capsys.readouterr().out


def test_cli_verify_rejects_a_malformed_anchor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "audit.ndjson"
    write_rows(path, 1)
    assert main(["audit", "verify", "--file", str(path), "--anchor", "1:nothex"]) == 2
    assert "anchor" in capsys.readouterr().err
