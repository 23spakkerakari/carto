"""File upload connector: exported files and the offline analyzer's inputs (spec 8.1.1).

Reads local files only, never the network, never writes. Three kinds:

- ``log``: one :class:`RawRecord` per line with ``text``, locator ``<file name>:line:<n>``
  (1-based physical line numbers, the file's base name, ADR 0006 ``line_key``), kind ``log``.
- ``rows``: a CSV with a header, one record per data row with ``fields`` = the row, locator
  ``<table>:row:<primary key>`` (ADR 0006 ``row_key``), kind ``row_change``, template hint
  ``row_change <table>`` (ADR 0016).
- ``files``: one ``file_arrived`` record per file with ``fields`` name, size, mtime and
  directory, ``received_at`` = mtime, locator ``file:<name>`` (ADR 0006 ``file_key``),
  template hint ``file_arrived <generalized name>``.

Spec 8.1.1 limits: accepted extensions ``.log .txt .json .ndjson .csv .xml .gz .zip``; 2 GB per
file, streamed; 20 GB per upload; zip archives with at most 10,000 entries, a compression ratio
of at most 100:1 per entry and in total, at most 50 GB uncompressed, no absolute paths, no
``..``, no symlink entries. Entries are streamed straight out of the archive, never extracted
to disk. The cursor is ``{"file", "line"}`` so a resumed run skips what was already handed over.

Confinement (M1 review, package C): symlinks and Windows junctions are never followed, neither
as a configured path nor inside a walked or globbed tree, and every file is checked by its real
path. With ``base_dir`` set (the offline analyzer passes ``--input``) every configured path and
every file found must lie under it; without it, a directory walk or glob may not leave the
directory it started from. CSV rows are read with a 16 MiB field limit, and a row the reader
cannot parse is skipped and counted, never fatal.
"""

from __future__ import annotations

import asyncio
import codecs
import csv
import fnmatch
import glob
import gzip
import io
import logging
import os
import re
import stat
import zipfile
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, ClassVar, Final, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from carto_edge.config import GIB, MIB, SourceConfig
from carto_edge.connectors.base import (
    ConnectorConfig,
    ConnectorContext,
    ConnectorError,
    Cursor,
    ReadOnlyStatus,
    TestCheck,
    TestResult,
)
from carto_edge.connectors.registry import generalize_name, register, validate_config_model
from carto_edge.pipeline.model import RawRecord
from carto_schema.event import EventKind

__all__ = [
    "ACCEPTED_SUFFIXES",
    "MAX_FILE_BYTES",
    "MAX_UPLOAD_BYTES",
    "MAX_ZIP_ENTRIES",
    "MAX_ZIP_RATIO",
    "MAX_ZIP_TOTAL_BYTES",
    "UploadConfig",
    "UploadConnector",
    "UploadLimitError",
    "check_zip",
]

logger = logging.getLogger(__name__)

ACCEPTED_SUFFIXES: Final = frozenset({".log", ".txt", ".json", ".ndjson", ".csv", ".xml"})
ARCHIVE_SUFFIXES: Final = frozenset({".gz", ".zip"})
MAX_FILE_BYTES: Final = 2 * GIB
MAX_UPLOAD_BYTES: Final = 20 * GIB
MAX_ZIP_ENTRIES: Final = 10_000
MAX_ZIP_RATIO: Final = 100
MAX_ZIP_TOTAL_BYTES: Final = 50 * GIB
MAX_LINE_BYTES: Final = 16 * MIB
"""Lines longer than this are cut (``size_bytes`` keeps the real length) to bound memory."""
MAX_CSV_FIELD_BYTES: Final = 16 * MIB
"""``csv.field_size_limit`` while rows are read: one cell may not hold more than a line."""

YIELD_EVERY: Final = 1000
_GLOB_MAGIC: Final = re.compile(r"[*?\[]")
_WINDOWS_DRIVE: Final = re.compile(r"^[A-Za-z]:")

Kind = Literal["log", "rows", "files"]


class UploadLimitError(ConnectorError):
    """A spec 8.1.1 limit was exceeded or an archive entry is unsafe."""


class UploadConfig(ConnectorConfig):
    paths: list[str] = Field(min_length=1, max_length=4096)
    base_dir: str | None = Field(default=None, max_length=4096)
    kind: Kind = "log"
    encoding: str = Field(default="utf-8", min_length=1, max_length=64)
    table: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
    primary_key: str | None = Field(default=None, min_length=1, max_length=256)
    timestamp_column: str | None = Field(default=None, max_length=256)
    actor_column: str | None = Field(default=None, max_length=256)
    include: list[str] = Field(default_factory=list, max_length=256)
    max_file_bytes: int = Field(default=MAX_FILE_BYTES, ge=1, le=MAX_FILE_BYTES)
    max_upload_bytes: int = Field(default=MAX_UPLOAD_BYTES, ge=1, le=MAX_UPLOAD_BYTES)

    @field_validator("encoding")
    @classmethod
    def _known_encoding(cls, value: str) -> str:
        try:
            codecs.lookup(value)
        except LookupError as exc:
            msg = "unknown encoding"
            raise ValueError(msg) from exc
        return value

    @field_validator("paths")
    @classmethod
    def _no_nul(cls, value: list[str]) -> list[str]:
        if any(not entry or "\x00" in entry for entry in value):
            msg = "paths must be non-empty and free of NUL"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _rows_need_table(self) -> UploadConfig:
        if self.kind == "rows" and (self.table is None or self.primary_key is None):
            msg = "kind 'rows' needs 'table' and 'primary_key'"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class _Input:
    """One file to read; ``path`` is the file system path, ``name`` the logical name used in
    locators and the cursor (the base name, with ``.gz`` stripped; zip entries are
    ``<archive>!<entry>`` in the cursor and the entry's base name in locators)."""

    path: Path
    size: int
    mtime: float

    @property
    def key(self) -> str:
        return str(self.path)

    @property
    def suffix(self) -> str:
        return self.path.suffix.lower()


def _is_link(path: Path) -> bool:
    """A symlink or a Windows junction: neither is followed, wherever it appears."""
    return path.is_symlink() or os.path.isjunction(path)


def _within(path: str, root: str) -> bool:
    """``path`` is ``root`` or below it; both are real paths already."""
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == (
            os.path.normcase(root)
        )
    except ValueError:  # different drives or one relative, one absolute
        return False


def _static_prefix(target: Path) -> Path:
    """The part of a pattern before its first glob segment: what its matches may not leave."""
    parts: list[str] = []
    for part in target.parts:
        if _GLOB_MAGIC.search(part):
            break
        parts.append(part)
    return Path(*parts) if parts else target


def _is_safe_entry_name(name: str) -> bool:
    if not name or "\x00" in name:
        return False
    if name.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(name):
        return False
    segments = re.split(r"[/\\]", name)
    return all(segment not in {"..", ""} for segment in segments[:-1]) and segments[-1] != ".."


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == stat.S_IFLNK


def check_zip(
    archive: zipfile.ZipFile, *, max_file_bytes: int = MAX_FILE_BYTES
) -> list[zipfile.ZipInfo]:
    """Validate an archive against spec 8.1.1 and return its readable entries."""
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        msg = f"zip has {len(infos)} entries; the limit is {MAX_ZIP_ENTRIES}"
        raise UploadLimitError(msg)
    total_uncompressed = 0
    total_compressed = 0
    entries: list[zipfile.ZipInfo] = []
    for info in infos:
        if not _is_safe_entry_name(info.filename):
            msg = "zip entry name is absolute or contains '..'"
            raise UploadLimitError(msg)
        if _is_symlink_entry(info):
            msg = "zip contains a symlink entry"
            raise UploadLimitError(msg)
        if info.is_dir():
            continue
        if info.file_size > max_file_bytes:
            msg = f"zip entry exceeds {max_file_bytes} bytes uncompressed"
            raise UploadLimitError(msg)
        if info.file_size > 0 and (
            info.compress_size == 0 or info.file_size > info.compress_size * MAX_ZIP_RATIO
        ):
            msg = f"zip entry compression ratio exceeds {MAX_ZIP_RATIO}:1"
            raise UploadLimitError(msg)
        total_uncompressed += info.file_size
        total_compressed += info.compress_size
        if total_uncompressed > MAX_ZIP_TOTAL_BYTES:
            msg = f"zip exceeds {MAX_ZIP_TOTAL_BYTES} bytes uncompressed in total"
            raise UploadLimitError(msg)
        entries.append(info)
    if total_uncompressed > 0 and total_uncompressed > max(total_compressed, 1) * MAX_ZIP_RATIO:
        msg = f"zip total compression ratio exceeds {MAX_ZIP_RATIO}:1"
        raise UploadLimitError(msg)
    return entries


class _Reads(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


class _BoundedReader(io.RawIOBase):
    """Counts bytes read from a stream and stops at a limit (a lying zip header or a gzip
    bomb cannot expand past the per-file limit)."""

    def __init__(self, inner: _Reads, limit: int, what: str) -> None:
        super().__init__()
        self._inner = inner
        self._limit = limit
        self._what = what
        self.consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._inner.read(len(buffer))
        self.consumed += len(chunk)
        if self.consumed > self._limit:
            msg = f"{self._what} exceeds {self._limit} bytes uncompressed"
            raise UploadLimitError(msg)
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _iter_lines(stream: IO[bytes], encoding: str) -> Iterator[tuple[int, str, int]]:
    """``(line number, text, size)`` per physical line, blank lines counted but not yielded."""
    number = 0
    while True:
        chunk = stream.readline(MAX_LINE_BYTES + 1)
        if not chunk:
            return
        number += 1
        size = len(chunk)
        if len(chunk) > MAX_LINE_BYTES and not chunk.endswith(b"\n"):
            while True:  # drain the rest of an overlong line
                more = stream.readline(MIB)
                size += len(more)
                if not more or more.endswith(b"\n"):
                    break
            chunk = chunk[:MAX_LINE_BYTES]
        text = chunk.rstrip(b"\r\n").decode(encoding, errors="replace")
        if text.strip():
            yield number, text, size


def _mtime(item: _Input) -> datetime:
    """The file's modification time: the receive time of its records. Deterministic, so a
    record without a source timestamp gets the same ``event_id`` on every run (ADR 0025)."""
    return datetime.fromtimestamp(item.mtime, tz=UTC)


@register("upload")
class UploadConnector:
    """Spec 8.1.1. Local files only; no network, no writes."""

    type: ClassVar[str] = "upload"

    def __init__(self, source: SourceConfig, context: ConnectorContext) -> None:
        self.source = source
        self.context = context
        self.config = self.validate_config(source.config)
        self._base = Path(self.config.base_dir) if self.config.base_dir else Path.cwd()

    def validate_config(self, cfg: Mapping[str, Any]) -> UploadConfig:
        return validate_config_model(UploadConfig, cfg, self.source.id)

    # -- file discovery -------------------------------------------------------------------------

    def _candidates(self) -> tuple[list[_Input], list[str]]:
        """Resolve the configured paths to regular files (sorted, deduplicated) plus problems.

        Every file is confined: under ``base_dir`` when one is configured, else under the
        directory its own entry starts from. Links and junctions are never followed, so a file
        reached through one resolves outside and is skipped, and the count is a problem."""
        found: dict[str, _Input] = {}
        problems: list[str] = []
        base = os.path.realpath(self._base) if self.config.base_dir else None
        for entry in self.config.paths:
            raw = Path(entry)
            target = raw if raw.is_absolute() else self._base / raw
            start = os.path.realpath(_static_prefix(target))
            if base is not None and not _within(start, base):
                problems.append(f"path {entry!r} is outside the base directory")
                continue
            confine = base if base is not None else start
            outside = 0
            if _GLOB_MAGIC.search(str(target)):
                # glob.glob: the pattern is absolute and may hold ** (Path.glob needs relative)
                matches = [Path(m) for m in glob.glob(str(target), recursive=True)]  # noqa: PTH207
                if not matches:
                    problems.append(f"pattern {entry!r} matched no file")
                for match in matches:
                    outside += self._add(found, match, confine)
            elif _is_link(target):
                kind = "junction" if os.path.isjunction(target) else "symlink"
                problems.append(f"path {entry!r} is a {kind}; links are not followed")
            elif target.is_dir():
                for root, directories, files in os.walk(target, followlinks=False):
                    directories[:] = sorted(
                        name for name in directories if not _is_link(Path(root) / name)
                    )
                    for file_name in sorted(files):
                        outside += self._add(found, Path(root) / file_name, confine)
            elif target.is_file():
                outside += self._add(found, target, confine)
            else:
                problems.append(f"path {entry!r} does not exist")
            if outside:
                problems.append(
                    f"path {entry!r}: {outside} file(s) resolve outside it and were skipped; "
                    "links are not followed"
                )
        inputs = [found[key] for key in sorted(found)]
        if self.config.kind != "files":
            inputs = [
                item for item in inputs if item.suffix in ACCEPTED_SUFFIXES | ARCHIVE_SUFFIXES
            ]
        return inputs, problems

    def _add(self, found: dict[str, _Input], path: Path, confine: str) -> int:
        """Record a regular file whose real path lies under ``confine``; returns 1 when the file
        was skipped for resolving outside it, else 0."""
        try:
            info = os.lstat(path)
        except OSError:
            return 0
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return 0
        real = os.path.realpath(path)
        if not _within(real, confine):
            return 1
        if self.config.include and not any(
            fnmatch.fnmatch(path.name, pattern) for pattern in self.config.include
        ):
            return 0
        # Keyed by real path, so two spellings of one file (``sub/../a.log``) read it once;
        # the spelled path stays the cursor's ``file`` value.
        found.setdefault(os.path.normcase(real), _Input(path, info.st_size, info.st_mtime))
        return 0

    def _check_limits(self, inputs: list[_Input]) -> list[str]:
        problems: list[str] = []
        total = 0
        for item in inputs:
            total += item.size
            if item.size > self.config.max_file_bytes:
                problems.append(f"{item.path.name} exceeds {self.config.max_file_bytes} bytes")
            if item.suffix == ".zip":
                try:
                    with zipfile.ZipFile(item.path) as archive:
                        check_zip(archive, max_file_bytes=self.config.max_file_bytes)
                except zipfile.BadZipFile:
                    problems.append(f"{item.path.name} is not a valid zip archive")
                except UploadLimitError as exc:
                    problems.append(f"{item.path.name}: {exc}")
                except OSError:
                    problems.append(f"{item.path.name} is not readable")
        if total > self.config.max_upload_bytes:
            problems.append(
                f"the upload totals {total} bytes; the limit is {self.config.max_upload_bytes}"
            )
        return problems

    # -- interface --------------------------------------------------------------------------------

    async def test(self) -> TestResult:
        inputs, problems = self._candidates()
        checks = [
            TestCheck(
                "paths", not problems, "; ".join(problems) if problems else f"{len(inputs)} files"
            )
        ]
        if not inputs:
            problems.append("no readable input files")
        limit_problems = self._check_limits(inputs)
        checks.append(
            TestCheck(
                "limits",
                not limit_problems,
                "; ".join(limit_problems) if limit_problems else "within spec 8.1.1",
            )
        )
        problems.extend(limit_problems)
        unreadable = [item.path.name for item in inputs if not os.access(item.path, os.R_OK)]
        checks.append(
            TestCheck(
                "readable", not unreadable, "; ".join(unreadable) if unreadable else "all readable"
            )
        )
        problems.extend(f"{name} is not readable" for name in unreadable)
        checks.append(TestCheck("read_only", True, "local files are opened for reading only"))
        return TestResult(
            ok=not problems,
            read_only=ReadOnlyStatus.VERIFIED,
            checks=tuple(checks),
            problems=tuple(problems),
            visible=tuple(item.path.name for item in inputs),
        )

    async def read(self, cursor: Cursor | None) -> AsyncIterator[RawRecord]:
        inputs, _problems = self._candidates()
        limit_problems = self._check_limits(inputs)
        if limit_problems:
            msg = f"source {self.source.id!r}: {'; '.join(limit_problems)}"
            raise UploadLimitError(msg)
        skip_file = str(cursor.get("file", "")) if cursor else ""
        skip_line = int(cursor.get("line", 0)) if cursor else 0
        keys = [self._unit_keys(item) for item in inputs]
        start_index = 0
        if skip_file:
            found_at = next((i for i, unit_keys in enumerate(keys) if skip_file in unit_keys), None)
            if found_at is None:  # the file is gone: start over, core dedupes by event_id
                skip_file, skip_line = "", 0
            else:
                start_index = found_at
        sequence = 0
        for index in range(start_index, len(inputs)):
            item = inputs[index]
            for record in self._records(item, skip_file, skip_line):
                sequence += 1
                yield record
                if sequence % YIELD_EVERY == 0:
                    await asyncio.sleep(0)
            skip_file, skip_line = "", 0
        logger.info(
            "upload read complete source_id=%s files=%d records=%d",
            self.source.id,
            len(inputs),
            sequence,
        )

    async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]:
        async for record in self.read(None):
            if self.config.kind == "files" and not start <= record.received_at <= end:
                continue
            yield record

    async def close(self) -> None:
        return None

    # -- record production ----------------------------------------------------------------------

    def _unit_keys(self, item: _Input) -> set[str]:
        """Cursor ``file`` values this input can produce: its path, or ``path!entry`` per zip
        entry."""
        if item.suffix == ".zip" and self.config.kind != "files":
            try:
                with zipfile.ZipFile(item.path) as archive:
                    return {f"{item.key}!{info.filename}" for info in check_zip(archive)}
            except (OSError, zipfile.BadZipFile, UploadLimitError):
                return {item.key}
        return {item.key}

    def _records(self, item: _Input, skip_file: str, skip_line: int) -> Iterator[RawRecord]:
        if self.config.kind == "files":
            if skip_file == item.key:
                return
            yield self._file_record(item)
            return
        if item.suffix == ".zip":
            with zipfile.ZipFile(item.path) as archive:
                reached = not skip_file  # entries before the cursor's entry are skipped
                for info in check_zip(archive, max_file_bytes=self.config.max_file_bytes):
                    key = f"{item.key}!{info.filename}"
                    entry_name = Path(info.filename.replace("\\", "/")).name
                    if Path(entry_name).suffix.lower() not in ACCEPTED_SUFFIXES:
                        continue
                    if not reached:
                        if key != skip_file:
                            continue
                        reached = True
                        after = skip_line
                    else:
                        after = 0
                    with archive.open(info) as raw:
                        bounded = io.BufferedReader(
                            _BoundedReader(
                                raw, min(info.file_size, self.config.max_file_bytes), entry_name
                            )
                        )
                        yield from self._content_records(
                            bounded, entry_name, key, after, _mtime(item)
                        )
            return
        after = skip_line if skip_file == item.key else 0
        if item.suffix == ".gz":
            name = item.path.name[:-3]
            with gzip.open(item.path, "rb") as raw:
                bounded = io.BufferedReader(_BoundedReader(raw, self.config.max_file_bytes, name))
                yield from self._content_records(bounded, name, item.key, after, _mtime(item))
            return
        with item.path.open("rb") as handle:
            yield from self._content_records(handle, item.path.name, item.key, after, _mtime(item))

    def _content_records(
        self, stream: IO[bytes], name: str, key: str, after: int, received_at: datetime
    ) -> Iterator[RawRecord]:
        if self.config.kind == "rows":
            yield from self._row_records(stream, name, key, after, received_at)
        else:
            yield from self._line_records(stream, name, key, after, received_at)

    def _line_records(
        self, stream: IO[bytes], name: str, key: str, after: int, now: datetime
    ) -> Iterator[RawRecord]:
        for number, text, size in _iter_lines(stream, self.config.encoding):
            if number <= after:
                continue
            yield RawRecord(
                source_id=self.source.id,
                system_id=self.source.system,
                kind=EventKind.LOG,
                locator=f"{name}:line:{number}",
                received_at=now,
                text=text,
                sequence=number,
                commit_cursor={"file": key, "line": number},
                size_bytes=size,
            )

    def _row_records(
        self, stream: IO[bytes], name: str, key: str, after: int, now: datetime
    ) -> Iterator[RawRecord]:
        table = self.config.table or name
        primary_key = self.config.primary_key or "id"
        text_stream = io.TextIOWrapper(
            stream, encoding=self.config.encoding, errors="replace", newline=""
        )
        csv.field_size_limit(MAX_CSV_FIELD_BYTES)  # process-wide in the csv module
        skipped = 0
        try:
            reader = csv.DictReader(text_stream)
            try:
                header = reader.fieldnames
            except csv.Error as exc:
                msg = f"{name}: the CSV header row could not be read: {type(exc).__name__}"
                raise ConnectorError(msg) from exc
            if not header:
                msg = f"{name}: a CSV header row is required for kind 'rows'"
                raise ConnectorError(msg)
            if primary_key not in header:
                msg = f"{name}: primary_key column {primary_key!r} is not in the CSV header"
                raise ConnectorError(msg)
            number = 0
            while True:
                try:
                    row = next(reader)
                except StopIteration:
                    break
                except csv.Error:
                    # One row the reader cannot parse (a cell past the field limit, broken
                    # quoting) is skipped; it keeps its row number so the cursor stays honest.
                    number += 1
                    skipped += 1
                    continue
                number += 1
                if number <= after:
                    continue
                fields = {
                    column: value
                    for column, value in row.items()
                    if column is not None and value is not None
                }
                pk = fields.get(primary_key, "")
                locator = f"{table}:row:{pk}" if pk else f"{table}:row:line:{number}"
                yield RawRecord(
                    source_id=self.source.id,
                    system_id=self.source.system,
                    kind=EventKind.ROW_CHANGE,
                    locator=locator,
                    received_at=now,
                    fields=fields,
                    sequence=number,
                    commit_cursor={"file": key, "line": number},
                    size_bytes=sum(len(value) for value in fields.values()),
                    template_hint=f"row_change {table}",
                    timestamp_field=self.config.timestamp_column,
                    actor_field=self.config.actor_column,
                )
        finally:
            # The caller owns the binary stream: detach so the wrapper neither closes it nor
            # warns about being collected while open.
            text_stream.detach()
            if skipped:
                logger.warning(
                    "upload csv rows skipped source_id=%s table=%s skipped=%d",
                    self.source.id,
                    table,
                    skipped,
                )

    def _file_record(self, item: _Input) -> RawRecord:
        mtime = datetime.fromtimestamp(item.mtime, tz=UTC)
        return RawRecord(
            source_id=self.source.id,
            system_id=self.source.system,
            kind=EventKind.FILE_ARRIVED,
            locator=f"file:{item.path.name}",
            received_at=mtime,
            fields={
                "name": item.path.name,
                "size": item.size,
                "mtime": mtime.isoformat(),
                # Only the directory's own name: the full path names the analyst's machine.
                "directory": item.path.parent.name,
            },
            commit_cursor={"file": item.key, "line": 0},
            size_bytes=item.size,
            template_hint=f"file_arrived {generalize_name(item.path.name)}",
        )
