"""SFTP / file-drop watch connector (spec 8.1.5).

Lists the configured directories on each poll with a read-only account and emits
``file_arrived`` for files that are new or changed (size or mtime) since the cursor snapshot and
``file_removed`` for files that vanished. Only directory listings and ``stat`` calls are ever
made: the connector never opens, reads, writes, renames or deletes a file (spec 2.3 invariant
1; the read-only test in ``test_edge_conn_sftp.py`` records every server-side operation).

- Strict host key checking: the host key's SHA-256 fingerprint is pinned in config
  (``host_key_sha256: "SHA256:..."``). asyncssh is given an empty trusted set
  (``known_hosts=([], [], [])``) so that it always consults
  :meth:`_PinnedHostKeyClient.validate_host_public_key`, which accepts the pinned fingerprint
  only. ``known_hosts=None`` would make asyncssh trust any key without asking; it is never
  used here.
- SSRF (spec 14.7, ADR 0018): the connection is opened to the address the network policy
  pinned for the configured host. The user's SSH configuration (``~/.ssh/config``: proxy
  commands, identities, host aliases) and agent are never consulted (``config=None``,
  ``agent_path=None``); the source config and its secret are the whole connection.
- ``local://`` (spec 8.1.5 "Local and SMB/NFS-mounted directories"): the same connector lists
  a local directory with ``os.scandir`` and ``stat``; symlinks are not followed.
- Credentials: the secret is a private key (PEM text, or JSON ``{"private_key": ...,
  "passphrase": ...}``)
  or a password (JSON ``{"username", "password"}`` or ``user:password``).

The cursor is the snapshot ``{"snapshot": {directory: {name: [size, mtime]}}}``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hmac
import json
import logging
import os
import posixpath
import stat
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Final
from urllib.parse import urlsplit

import asyncssh
from pydantic import Field, model_validator

from carto_edge.config import SourceConfig
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorError,
    Cursor,
    InvalidConfigError,
    ReadOnlyStatus,
    TestCheck,
    TestResult,
)
from carto_edge.connectors.registry import generalize_name, register, validate_config_model
from carto_edge.pipeline.model import RawRecord
from carto_edge.secrets import SecretError, parse_credentials
from carto_schema.event import EventKind

__all__ = ["LOCAL_SCHEME", "SftpConfig", "SftpConnector", "Snapshot"]

logger = logging.getLogger(__name__)

LOCAL_SCHEME: Final = "local://"
HOST_KEY_PATTERN: Final = r"^SHA256:[A-Za-z0-9+/]{43}=?$"
NOT_VERIFIABLE_NOTE: Final = (
    "SFTP exposes no grant query; the install guide documents the read-only account and this "
    "connector only lists directories (no open, read, write, rename or delete)"
)

Snapshot = dict[str, dict[str, list[int]]]
"""``{directory: {file name: [size, mtime]}}``; lists so the cursor is JSON."""


class SftpConfig(ConnectorConfig):
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    host_key_sha256: str | None = Field(default=None, pattern=HOST_KEY_PATTERN)
    directories: list[str] = Field(default_factory=list, max_length=256)
    url: str | None = Field(default=None, max_length=4096, description="local:///absolute/dir")
    poll_seconds: int = Field(default=60, ge=5, le=86_400)
    filename_patterns: list[str] = Field(default_factory=lambda: ["*"], min_length=1, max_length=64)
    connect_timeout_seconds: float = Field(default=15.0, gt=0, le=300)
    max_entries_per_directory: int = Field(default=100_000, ge=1, le=10_000_000)

    @model_validator(mode="after")
    def _one_mode(self) -> SftpConfig:
        if any("\x00" in directory or not directory for directory in self.directories):
            msg = "directories must be non-empty and free of NUL"
            raise ValueError(msg)
        local_entries = [d for d in self.directories if d.startswith(LOCAL_SCHEME)]
        if self.url is not None:
            if not self.url.startswith(LOCAL_SCHEME):
                msg = "url must use the local:// scheme (remote sources use host)"
                raise ValueError(msg)
            if local_entries:
                msg = "give either url or local:// directories, not both"
                raise ValueError(msg)
            return self
        if local_entries:
            if len(local_entries) != len(self.directories):
                msg = "directories must be all local:// or all remote paths"
                raise ValueError(msg)
            return self
        if not self.directories:
            msg = "directories is required"
            raise ValueError(msg)
        if self.host is None or self.host_key_sha256 is None or self.username is None:
            msg = "a remote source needs host, username and host_key_sha256"
            raise ValueError(msg)
        return self

    @property
    def is_local(self) -> bool:
        return self.url is not None or any(d.startswith(LOCAL_SCHEME) for d in self.directories)


def _local_path(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "local" or parts.netloc:
        msg = "local URLs are local:///absolute/path"
        raise InvalidConfigError(msg)
    path = parts.path
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":  # local:///C:/dir on Windows
        path = path[1:]
    if not path:
        msg = "local URL has an empty path"
        raise InvalidConfigError(msg)
    return path


class _PinnedHostKeyClient(asyncssh.SSHClient):
    """Accepts exactly the pinned host key fingerprint (spec 8.1.5 strict host key checking)."""

    def __init__(self, expected: str) -> None:
        self._expected = expected

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        _ = (host, addr, port)
        return hmac.compare_digest(key.get_fingerprint("sha256"), self._expected)


@register("sftp")
class SftpConnector:
    """Spec 8.1.5. Lists; never touches file contents."""

    type: ClassVar[str] = "sftp"

    def __init__(
        self,
        source: SourceConfig,
        context: ConnectorContext,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.source = source
        self.context = context
        self.config = self.validate_config(source.config)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        if self.config.is_local:
            if self.config.url is not None:
                self._dirs = [_local_path(self.config.url)]
            else:
                self._dirs = [_local_path(d) for d in self.config.directories]
        else:
            if source.secret_ref is None:
                msg = f"source {source.id!r}: a remote sftp source needs a secret_ref"
                raise InvalidConfigError(msg)
            self._dirs = list(self.config.directories)
        self._sequence = 0

    def validate_config(self, cfg: Mapping[str, Any]) -> SftpConfig:
        return validate_config_model(SftpConfig, cfg, self.source.id)

    # -- listing --------------------------------------------------------------------------------

    def _matches(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.config.filename_patterns)

    def _list_local(self) -> Snapshot:
        snapshot: Snapshot = {}
        for directory in self._dirs:
            listing: dict[str, list[int]] = {}
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if len(listing) >= self.config.max_entries_per_directory:
                            logger.warning("sftp local listing capped source_id=%s", self.source.id)
                            break
                        if not entry.is_file(follow_symlinks=False) or not self._matches(
                            entry.name
                        ):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        listing[entry.name] = [int(info.st_size), int(info.st_mtime)]
            except OSError as exc:
                msg = f"source {self.source.id!r}: cannot list {directory}: {type(exc).__name__}"
                raise ConnectorError(msg) from exc
            snapshot[directory] = listing
        return snapshot

    def _auth_options(self) -> dict[str, Any]:
        value = self.context.secrets.resolve(self.source.secret_ref or "")
        options: dict[str, Any] = {"username": self.config.username}
        text = value.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError as exc:
                msg = "sftp secret is not valid JSON"
                raise SecretError(msg) from exc
            if isinstance(data, dict) and isinstance(data.get("private_key"), str):
                passphrase = data.get("passphrase")
                options["client_keys"] = [
                    asyncssh.import_private_key(
                        data["private_key"], passphrase if isinstance(passphrase, str) else None
                    )
                ]
                return options
        if "-----BEGIN" in text:
            options["client_keys"] = [asyncssh.import_private_key(text)]
            return options
        credentials = parse_credentials(text)
        options["password"] = credentials.password
        options["client_keys"] = None
        if self.config.username is None:
            options["username"] = credentials.username
        return options

    async def _list_remote(self) -> Snapshot:
        if self.config.host is None or self.config.host_key_sha256 is None:
            msg = "a remote source needs host and host_key_sha256"  # the config model enforces it
            raise RuntimeError(msg)
        resolved = self.context.network.resolve(self.config.host, self.config.port)
        expected = self.config.host_key_sha256
        try:
            auth = self._auth_options()
        except (SecretError, asyncssh.KeyImportError, ValueError) as exc:
            msg = f"source {self.source.id!r}: sftp credential is unusable: {type(exc).__name__}"
            raise ConnectorError(msg) from exc
        snapshot: Snapshot = {}
        try:
            # known_hosts=([], [], []) is an EMPTY trusted set, not "no checking": asyncssh then
            # asks _PinnedHostKeyClient.validate_host_public_key for every key, which accepts
            # the pinned fingerprint only. known_hosts=None would trust everything.
            async with (
                asyncssh.connect(
                    resolved.address,
                    port=resolved.port,
                    known_hosts=([], [], []),
                    client_factory=lambda: _PinnedHostKeyClient(expected),
                    config=None,  # never ~/.ssh/config: no ProxyCommand, identities or aliases
                    agent_path=None,
                    connect_timeout=self.config.connect_timeout_seconds,
                    login_timeout=self.config.connect_timeout_seconds,
                    **auth,
                ) as connection,
                connection.start_sftp_client() as sftp,
            ):
                for directory in self._dirs:
                    snapshot[directory] = await self._list_directory(sftp, directory)
        except (asyncssh.Error, OSError, TimeoutError) as exc:
            msg = f"source {self.source.id!r}: sftp listing failed: {type(exc).__name__}: {exc}"
            raise ConnectorError(msg) from exc
        return snapshot

    async def _list_directory(
        self, sftp: asyncssh.SFTPClient, directory: str
    ) -> dict[str, list[int]]:
        listing: dict[str, list[int]] = {}
        for entry in await sftp.readdir(directory):
            name = (
                entry.filename
                if isinstance(entry.filename, str)
                else entry.filename.decode("utf-8", "replace")
            )
            if name in {".", ".."} or not self._matches(name):
                continue
            if len(listing) >= self.config.max_entries_per_directory:
                logger.warning("sftp listing capped source_id=%s", self.source.id)
                break
            attrs = entry.attrs
            kind = attrs.type
            permissions = attrs.permissions
            is_regular = kind == asyncssh.FILEXFER_TYPE_REGULAR or (
                kind in {None, 0} and permissions is not None and stat.S_ISREG(permissions)
            )
            if not is_regular:
                continue
            if attrs.size is None or attrs.mtime is None:
                attrs = await sftp.stat(posixpath.join(directory, name))
            listing[name] = [int(attrs.size or 0), int(attrs.mtime or 0)]
        return listing

    async def _snapshot(self) -> Snapshot:
        if self.config.is_local:
            return await asyncio.to_thread(self._list_local)
        return await self._list_remote()

    # -- records --------------------------------------------------------------------------------

    def _record(
        self, kind: EventKind, directory: str, name: str, size: int, mtime: int, now: datetime
    ) -> RawRecord:
        moment = datetime.fromtimestamp(mtime, tz=UTC)
        verb = "file_arrived" if kind is EventKind.FILE_ARRIVED else "file_removed"
        self._sequence += 1
        return RawRecord(
            source_id=self.source.id,
            system_id=self.source.system,
            kind=kind,
            locator=f"file:{name}",
            received_at=moment if kind is EventKind.FILE_ARRIVED else now,
            fields={
                "name": name,
                "size": size,
                "mtime": moment.isoformat(),
                "directory": directory,
            },
            sequence=self._sequence,
            size_bytes=size,
            template_hint=f"{verb} {generalize_name(name)}",
        )

    def _diff(self, previous: Snapshot, current: Snapshot, now: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        for directory in self._dirs:
            before = previous.get(directory, {})
            after = current.get(directory, {})
            for name in sorted(after):
                size, mtime = after[name]
                if before.get(name) != [size, mtime]:
                    records.append(
                        self._record(EventKind.FILE_ARRIVED, directory, name, size, mtime, now)
                    )
            for name in sorted(before):
                if name not in after:
                    size, mtime = before[name]
                    records.append(
                        self._record(EventKind.FILE_REMOVED, directory, name, size, mtime, now)
                    )
        return records

    @staticmethod
    def _previous(cursor: Cursor | None) -> Snapshot:
        if not cursor:
            return {}
        raw = cursor.get("snapshot")
        if not isinstance(raw, Mapping):
            return {}
        snapshot: Snapshot = {}
        for directory, listing in raw.items():
            if not isinstance(listing, Mapping):
                continue
            entries: dict[str, list[int]] = {}
            for name, pair in listing.items():
                if isinstance(pair, list | tuple) and len(pair) == 2:
                    entries[str(name)] = [int(pair[0]), int(pair[1])]
            snapshot[str(directory)] = entries
        return snapshot

    async def read(self, cursor: Cursor | None) -> AsyncIterator[RawRecord]:
        now = self._clock()
        current = await self._snapshot()
        records = self._diff(self._previous(cursor), current, now)
        logger.info(
            "sftp poll source_id=%s directories=%d changes=%d",
            self.source.id,
            len(self._dirs),
            len(records),
        )
        for index, record in enumerate(records):
            if index == len(records) - 1:
                yield RawRecord(
                    source_id=record.source_id,
                    system_id=record.system_id,
                    kind=record.kind,
                    locator=record.locator,
                    received_at=record.received_at,
                    fields=record.fields,
                    sequence=record.sequence,
                    commit_cursor={"snapshot": current},
                    size_bytes=record.size_bytes,
                    template_hint=record.template_hint,
                )
            else:
                yield record

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        now = self._clock()
        current = await self._snapshot()
        for record in self._diff({}, current, now):
            if start <= record.received_at <= end:
                yield record

    async def close(self) -> None:
        return None

    async def test(self) -> TestResult:
        checks: list[TestCheck] = []
        problems: list[str] = []
        try:
            snapshot = await self._snapshot()
        except ConnectorError as exc:
            checks.append(TestCheck("connect", False, str(exc)))
            problems.append(str(exc))
            snapshot = {}
        else:
            checks.append(
                TestCheck(
                    "connect", True, "host key pinned" if not self.config.is_local else "local"
                )
            )
            checks.extend(
                TestCheck(
                    f"list {directory}",
                    True,
                    f"{len(snapshot.get(directory, {}))} matching files",
                )
                for directory in self._dirs
            )
        checks.append(TestCheck("read_only", True, NOT_VERIFIABLE_NOTE))
        return TestResult(
            ok=not problems,
            read_only=ReadOnlyStatus.NOT_VERIFIABLE,
            checks=tuple(checks),
            problems=tuple(problems),
            visible=tuple(self._dirs),
        )
