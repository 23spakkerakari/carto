"""carto_edge.connectors.sftp against an in-process asyncssh server that records every SFTP
operation (spec 18.3 "SFTP mock server records operations (only list/stat allowed)"): pinned
host key accepted, wrong fingerprint refused, arrived/removed diffing, the local:// variant."""

from __future__ import annotations

import os
import stat
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncssh
import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorContext,
    ConnectorError,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ResolvedHost,
)
from carto_edge.connectors.sftp import SftpConnector
from carto_edge.pipeline.model import RawRecord
from carto_schema.event import EventKind

SIM_OUTBOUND = Path(__file__).resolve().parents[2] / "sim-out" / "shop" / "shipping" / "outbound"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
USERNAME = "carto"
PASSWORD = "readonly-pw-0123456789"  # noqa: S105
ALLOWED_OPS = {"scandir", "stat", "lstat", "realpath", "readlink", "fstat"}
FORBIDDEN_OPS = {
    "open",
    "read",
    "write",
    "remove",
    "rename",
    "posix_rename",
    "mkdir",
    "rmdir",
    "setstat",
    "fsetstat",
    "symlink",
    "link",
}

CALLS: list[str] = []
CLIENT_KEY = asyncssh.generate_private_key("ssh-ed25519")


class RecordingSftpServer(asyncssh.SFTPServer):
    """Every operation is recorded; mutations are refused (and must never be attempted)."""

    root: bytes = b"."

    def __init__(self, chan: Any) -> None:
        super().__init__(chan, chroot=self.root)

    async def scandir(self, path: bytes) -> AsyncIterator[asyncssh.SFTPName]:
        CALLS.append("scandir")
        async for entry in super().scandir(path):
            yield entry

    def stat(self, path: bytes) -> Any:
        CALLS.append("stat")
        return super().stat(path)

    def lstat(self, path: bytes) -> Any:
        CALLS.append("lstat")
        return super().lstat(path)

    def realpath(self, path: bytes) -> Any:
        CALLS.append("realpath")
        return super().realpath(path)

    def readlink(self, path: bytes) -> Any:
        CALLS.append("readlink")
        return super().readlink(path)

    def fstat(self, file_obj: object) -> Any:
        CALLS.append("fstat")
        return super().fstat(file_obj)

    def open(self, path: bytes, pflags: int, attrs: asyncssh.SFTPAttrs) -> Any:
        CALLS.append("open")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def read(self, file_obj: object, offset: int, size: int) -> Any:
        CALLS.append("read")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def write(self, file_obj: object, offset: int, data: bytes) -> Any:
        CALLS.append("write")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def remove(self, path: bytes) -> Any:
        CALLS.append("remove")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def rename(self, oldpath: bytes, newpath: bytes) -> Any:
        CALLS.append("rename")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def posix_rename(self, oldpath: bytes, newpath: bytes) -> Any:
        CALLS.append("posix_rename")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def mkdir(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> Any:
        CALLS.append("mkdir")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def rmdir(self, path: bytes) -> Any:
        CALLS.append("rmdir")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def setstat(self, path: bytes, attrs: asyncssh.SFTPAttrs) -> Any:
        CALLS.append("setstat")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def fsetstat(self, file_obj: object, attrs: asyncssh.SFTPAttrs) -> Any:
        CALLS.append("fsetstat")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def symlink(self, oldpath: bytes, newpath: bytes) -> Any:
        CALLS.append("symlink")
        raise asyncssh.SFTPPermissionDenied("read-only test server")

    def link(self, oldpath: bytes, newpath: bytes) -> Any:
        CALLS.append("link")
        raise asyncssh.SFTPPermissionDenied("read-only test server")


class Server(asyncssh.SSHServer):
    def begin_auth(self, username: str) -> bool:
        return True

    def password_auth_supported(self) -> bool:
        return True

    def validate_password(self, username: str, password: str) -> bool:
        return username == USERNAME and password == PASSWORD

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        return username == USERNAME and key == CLIENT_KEY.convert_to_public()


class SftpHost:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.fingerprint = self.host_key.get_fingerprint("sha256")
        self.port = 0
        self._listener: asyncssh.SSHAcceptor | None = None

    async def __aenter__(self) -> SftpHost:
        RecordingSftpServer.root = os.fsencode(str(self.root))
        self._listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[self.host_key],
            server_factory=Server,
            sftp_factory=RecordingSftpServer,
        )
        self.port = self._listener.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._listener is not None
        self._listener.close()
        await self._listener.wait_closed()


class Secrets:
    def __init__(self, value: str) -> None:
        self.value = value

    def resolve(self, secret_ref: str) -> str:
        return self.value


class Network:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> ResolvedHost:
        self.calls.append((host, port))
        return ResolvedHost(host=host, address="127.0.0.1", port=port)


@pytest.fixture
def outbound(tmp_path: Path) -> Path:
    directory = tmp_path / "outbound"
    directory.mkdir()
    for name, minute in (("SHIP_20261006_2112.csv", 12), ("SHIP_20261007_2115.csv", 15)):
        path = directory / name
        path.write_bytes(b"shipment_id,order_ref\n1,SO-1\n")
        when = datetime(2026, 10, 7, 21, minute, tzinfo=UTC).timestamp()
        os.utime(path, (when, when))
    (directory / "README.txt").write_bytes(b"not a drop\n")
    (tmp_path / "archive").mkdir()
    return directory


@pytest.fixture
async def host(tmp_path: Path, outbound: Path) -> AsyncIterator[SftpHost]:
    CALLS.clear()
    async with SftpHost(tmp_path) as server:
        yield server


def make(
    host: SftpHost,
    secret: str = f"{USERNAME}:{PASSWORD}",
    config: dict[str, object] | None = None,
    network: Network | None = None,
) -> SftpConnector:
    source = SourceConfig(
        id="src_ship_sftp",
        system="sys_shipping",
        type=SourceType.SFTP,
        config={
            "host": "sftp.internal.example",
            "port": host.port,
            "username": USERNAME,
            "host_key_sha256": host.fingerprint,
            "directories": ["/outbound"],
            "poll_seconds": 60,
            "filename_patterns": ["SHIP_*.csv"],
            **(config or {}),
        },
        secret_ref="vault://kv/carto/sftp-readonly",  # noqa: S106
    )
    return SftpConnector(
        source,
        ConnectorContext(secrets=Secrets(secret), network=network or Network()),
        clock=lambda: NOW,
    )


async def collect(
    connector: SftpConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [record async for record in connector.read(cursor)]


# ---------------------------------------------------------------------------------------------
# remote
# ---------------------------------------------------------------------------------------------


async def test_first_poll_lists_arrivals_with_only_list_and_stat_operations(host: SftpHost) -> None:
    network = Network()
    connector = make(host, network=network)
    assert isinstance(connector, ReadConnector)
    records = await collect(connector)
    assert [record.locator for record in records] == [
        "file:SHIP_20261006_2112.csv",
        "file:SHIP_20261007_2115.csv",
    ]
    first = records[0]
    assert first.kind is EventKind.FILE_ARRIVED
    assert first.received_at == datetime(2026, 10, 7, 21, 12, tzinfo=UTC)
    assert first.template_hint == "file_arrived SHIP_*_*.csv"
    assert first.fields == {
        "name": "SHIP_20261006_2112.csv",
        "size": len(b"shipment_id,order_ref\n1,SO-1\n"),
        "mtime": "2026-10-07T21:12:00+00:00",
        "directory": "/outbound",
    }
    assert first.commit_cursor is None
    assert records[-1].commit_cursor == {
        "snapshot": {
            "/outbound": {
                "SHIP_20261006_2112.csv": [
                    first.fields["size"],
                    int(first.received_at.timestamp()),
                ],
                "SHIP_20261007_2115.csv": [
                    first.fields["size"],
                    int(datetime(2026, 10, 7, 21, 15, tzinfo=UTC).timestamp()),
                ],
            }
        }
    }
    assert network.calls == [("sftp.internal.example", host.port)]
    assert CALLS, "the server saw no operations"
    assert set(CALLS) <= ALLOWED_OPS
    assert not set(CALLS) & FORBIDDEN_OPS


async def test_second_poll_diffs_against_the_snapshot(host: SftpHost, outbound: Path) -> None:
    connector = make(host)
    first = await collect(connector)
    cursor = dict(first[-1].commit_cursor or {})
    assert await collect(connector, cursor) == []
    (outbound / "SHIP_20261008_2113.csv").write_bytes(b"shipment_id,order_ref\n2,SO-2\n")
    changed = outbound / "SHIP_20261006_2112.csv"
    changed.write_bytes(b"shipment_id,order_ref\n1,SO-1\n3,SO-3\n")
    (outbound / "SHIP_20261007_2115.csv").unlink()
    records = await collect(connector, cursor)
    assert [(record.kind.value, record.locator) for record in records] == [
        ("file_arrived", "file:SHIP_20261006_2112.csv"),
        ("file_arrived", "file:SHIP_20261008_2113.csv"),
        ("file_removed", "file:SHIP_20261007_2115.csv"),
    ]
    removed = records[-1]
    assert removed.received_at == NOW
    assert removed.template_hint == "file_removed SHIP_*_*.csv"
    assert removed.fields is not None and removed.fields["mtime"] == "2026-10-07T21:15:00+00:00"
    assert removed.commit_cursor is not None
    cursor_after = removed.commit_cursor
    assert cursor_after is not None
    assert "SHIP_20261007_2115.csv" not in cursor_after["snapshot"]["/outbound"]
    assert set(CALLS) <= ALLOWED_OPS


async def test_wrong_host_key_fingerprint_is_refused(host: SftpHost) -> None:
    wrong = "SHA256:" + "A" * 43
    connector = make(host, config={"host_key_sha256": wrong})
    with pytest.raises(ConnectorError, match="sftp listing failed"):
        await collect(connector)
    assert CALLS == []  # the session never got as far as SFTP


async def test_wrong_password_is_refused(host: SftpHost) -> None:
    with pytest.raises(ConnectorError, match="sftp listing failed"):
        await collect(make(host, secret=f"{USERNAME}:nope"))


async def test_the_users_ssh_configuration_is_ignored(
    host: SftpHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``~/.ssh/config`` on the edge host could route the session through a ProxyCommand or
    add identities; the connector connects with the source config and its secret only."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text(
        "Host *\n  ProxyCommand carto-must-never-run-this %h %p\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # The file is one asyncssh understands: given it, the session would go through the proxy.
    options = asyncssh.SSHClientConnectionOptions(
        host="sftp.internal.example", config=[str(home / ".ssh" / "config")]
    )
    assert options.proxy_command and options.proxy_command[0] == "carto-must-never-run-this"
    records = await collect(make(host))
    assert [record.locator for record in records] == [
        "file:SHIP_20261006_2112.csv",
        "file:SHIP_20261007_2115.csv",
    ]


async def test_private_key_authentication(host: SftpHost) -> None:
    pem = CLIENT_KEY.export_private_key().decode()
    records = await collect(make(host, secret=pem))
    assert len(records) == 2
    records = await collect(
        make(host, secret='{"private_key": ' + __import__("json").dumps(pem) + "}")
    )
    assert len(records) == 2


async def test_unusable_credential(host: SftpHost) -> None:
    with pytest.raises(ConnectorError, match="credential"):
        await collect(make(host, secret="not-a-credential-pair"))  # noqa: S106


async def test_missing_directory_is_an_error_not_a_removal_storm(host: SftpHost) -> None:
    connector = make(host, config={"directories": ["/outbound", "/nowhere"]})
    with pytest.raises(ConnectorError):
        await collect(connector)


async def test_test_reports_not_verifiable(host: SftpHost) -> None:
    result = await make(host).test()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.NOT_VERIFIABLE
    assert result.can_enable
    assert result.visible == ("/outbound",)
    assert [check.name for check in result.checks] == ["connect", "list /outbound", "read_only"]
    assert "2 matching files" in result.checks[1].detail
    assert "no grant query" in result.checks[2].detail
    bad = await make(host, secret=f"{USERNAME}:nope").test()
    assert not bad.ok and bad.read_only is ReadOnlyStatus.NOT_VERIFIABLE


async def test_backfill_window(host: SftpHost) -> None:
    connector = make(host)
    start = datetime(2026, 10, 7, 21, 14, tzinfo=UTC)
    records = [r async for r in connector.backfill(start, NOW)]
    assert [r.locator for r in records] == ["file:SHIP_20261007_2115.csv"]
    assert all(r.commit_cursor is None for r in records)


# ---------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------


def local_source(config: dict[str, object]) -> SftpConnector:
    source = SourceConfig(
        id="src_local", system="sys_shipping", type=SourceType.SFTP, config=config
    )
    return SftpConnector(
        source, ConnectorContext(secrets=Secrets(""), network=Network()), clock=lambda: NOW
    )


@pytest.mark.parametrize(
    "config",
    [
        {"host": "h", "username": "u", "directories": ["/x"]},  # no host key
        {"host": "h", "username": "u", "host_key_sha256": "md5:abc", "directories": ["/x"]},
        {"host": "h", "username": "u", "host_key_sha256": "SHA256:" + "A" * 43},  # no directories
        {"directories": ["local:///a", "/b"]},  # mixed modes
        {"url": "sftp://h/x"},
        {"url": "local:///a", "directories": ["local:///b"]},
        {"url": "local:///a", "bogus": 1},
    ],
)
def test_invalid_configs(config: dict[str, object]) -> None:
    with pytest.raises(InvalidConfigError):
        local_source(config)


def test_remote_needs_a_secret_ref() -> None:
    source = SourceConfig(
        id="s",
        system="sys_shipping",
        type=SourceType.SFTP,
        config={
            "host": "h",
            "username": "u",
            "host_key_sha256": "SHA256:" + "A" * 43,
            "directories": ["/x"],
        },
    )
    with pytest.raises(InvalidConfigError, match="secret_ref"):
        SftpConnector(source, ConnectorContext(secrets=Secrets(""), network=Network()))


# ---------------------------------------------------------------------------------------------
# local:// variant
# ---------------------------------------------------------------------------------------------


async def test_local_directory_variant(outbound: Path) -> None:
    connector = local_source(
        {"url": f"local:///{outbound.as_posix().lstrip('/')}", "filename_patterns": ["SHIP_*.csv"]}
    )
    records = await collect(connector)
    assert [r.locator for r in records] == [
        "file:SHIP_20261006_2112.csv",
        "file:SHIP_20261007_2115.csv",
    ]
    assert records[0].fields is not None
    assert Path(records[0].fields["directory"]) == outbound
    result = await connector.test()
    assert result.ok and result.read_only is ReadOnlyStatus.NOT_VERIFIABLE
    cursor = dict(records[-1].commit_cursor or {})
    (outbound / "SHIP_20261006_2112.csv").unlink()
    again = await collect(connector, cursor)
    assert [(r.kind.value, r.locator) for r in again] == [
        ("file_removed", "file:SHIP_20261006_2112.csv")
    ]


async def test_local_directories_form_and_symlinks_skipped(outbound: Path) -> None:
    link = outbound / "SHIP_99999999_0000.csv"
    try:
        link.symlink_to(outbound / "SHIP_20261006_2112.csv")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    assert stat.S_ISLNK(os.lstat(link).st_mode)
    connector = local_source({"directories": [f"local:///{outbound.as_posix().lstrip('/')}"]})
    records = await collect(connector)
    assert all("99999999" not in r.locator for r in records)


@pytest.mark.skipif(not SIM_OUTBOUND.is_dir(), reason="sim-out/shop not present; run make sim")
async def test_local_variant_against_scenario_a_file_drops() -> None:
    connector = local_source(
        {"url": f"local:///{SIM_OUTBOUND.as_posix()}", "filename_patterns": ["SHIP_*.csv"]}
    )
    records = await collect(connector)
    assert len(records) == len(list(SIM_OUTBOUND.glob("SHIP_*.csv")))
    assert records[0].locator == "file:SHIP_20260923_2112.csv"
    assert all(r.template_hint == "file_arrived SHIP_*_*.csv" for r in records)
    assert all(r.received_at.tzinfo is UTC for r in records)
