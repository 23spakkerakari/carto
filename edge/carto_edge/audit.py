"""The edge audit trail: an append-only, hash-chained NDJSON file (spec 14.9, ADR 0014).

Core's audit log is a PostgreSQL table (M6). The edge has no database and writes its side of
every reveal, tokenize, key operation and configuration change to ``<state_dir>/audit.ndjson``
with the same chain rule as spec 14.9: every row carries ``prev_hash`` and
``row_hash = SHA-256(prev_hash || canonical_json(row without row_hash))``. The first row chains
to :data:`GENESIS_HASH`. ``carto-ctl audit verify`` recomputes the chain with :func:`verify`.

A row is ``seq``, ``ts``, ``action``, ``actor``, ``target``, ``request_id``, ``details``,
``prev_hash``, ``row_hash``. ``details`` is operator data about the call (counts, reasons,
purposes), never values: every string in it passes through
:func:`carto_common.logging.mask_string` before it is written (spec 2.3 invariant 7), and sizes
are bounded. Opening the file verifies the existing chain; a broken chain refuses to open so
tampering never goes unnoticed. Writes are serialized with a lock and fsynced.

Two changes the chain alone cannot see are covered by anchors (ADR 0033): deleting the last
rows, and rewriting a row with every later hash recomputed. :meth:`EdgeAudit.anchor` reads the
last row's ``seq``, ``row_hash`` and ``ts``; the gateway publishes it hourly and at shutdown
(:mod:`carto_edge.audit_anchor`), outside the edge's control, and :func:`verify` with
``anchors`` checks that the chain still holds each anchored row with that hash.

A power cut during an append can leave a partial last line. On open, a final line that has no
newline and is not JSON at all is moved to ``audit.ndjson.torn-<UTC timestamp>`` (preserved,
never deleted), the file is cut back to its last complete row and an ``audit.recovered`` row
recording the byte count is appended (ADR 0033). A final row that verifies but lost only its
newline gets the newline back. Any other mismatch, anywhere in the file, still refuses to open.

Two processes append to the same file: the gateway (reveal, tokenize) and the operator CLI
(``carto-edge key rotate``, ``secret set``) run inside the same container. Every append therefore
takes an exclusive operating-system lock on ``audit.ndjson.lock`` and, when the file grew since
this writer's last append, re-reads the last row's ``seq`` and ``row_hash`` before chaining to
it, so the chain stays one chain whoever writes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self, TextIO

from pydantic import AwareDatetime, BaseModel, ConfigDict

from carto_common.logging import get_logger, mask_string

__all__ = [
    "GENESIS_HASH",
    "RECOVERED_ACTION",
    "TORN_SUFFIX",
    "AnchorCheck",
    "AuditAnchor",
    "AuditChainError",
    "AuditRow",
    "EdgeAudit",
    "VerifyResult",
    "canonical_json",
    "last_anchor",
    "parse_anchor",
    "row_hash",
    "verify",
]

GENESIS_HASH: Final = "0" * 64
MAX_ACTION_LEN: Final = 128
MAX_ACTOR_LEN: Final = 256
MAX_TARGET_LEN: Final = 1024
MAX_REQUEST_ID_LEN: Final = 128
MAX_DETAIL_KEY_LEN: Final = 128
MAX_DETAIL_VALUE_LEN: Final = 4096
MAX_DETAIL_ITEMS: Final = 64
MAX_DETAIL_DEPTH: Final = 4
HASH_KEY: Final = "row_hash"
TORN_SUFFIX: Final = ".torn-"
RECOVERED_ACTION: Final = "audit.recovered"
SYSTEM_ACTOR: Final = "system:edge"
_ROW_HASH_RE: Final = re.compile(r"[0-9a-f]{64}")


log = get_logger(component="carto_edge.audit")


class AuditChainError(Exception):
    """The audit file does not verify; the message names the line, never its content."""


LOCK_SUFFIX: Final = ".lock"
_TAIL_CHUNK: Final = 8192


@contextlib.contextmanager
def _exclusive(path: Path) -> Iterator[None]:
    """An exclusive lock between processes on ``path`` (created when missing)."""
    with path.open("a+b") as handle:
        if sys.platform == "win32":
            import msvcrt  # noqa: PLC0415 - platform specific

            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:  # LK_LOCK gives up after about ten seconds; keep waiting
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl  # noqa: PLC0415 - platform specific

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _last_row(path: Path) -> dict[str, Any] | None:
    """The last row, read from the end of the file; ``None`` for an empty file."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        data = b""
        line = b""
        while position > 0:
            step = min(_TAIL_CHUNK, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
            stripped = data.rstrip(b"\n")
            cut = stripped.rfind(b"\n")
            if cut >= 0:
                line = stripped[cut + 1 :]
                break
            line = stripped
    if not line:
        return None
    try:
        row = json.loads(line)
        if not isinstance(row, dict):
            raise TypeError
        int(row["seq"])
        str(row[HASH_KEY])
    except (ValueError, KeyError, TypeError) as exc:
        msg = f"the last row of {path} is not an audit row"
        raise AuditChainError(msg) from exc
    return row


def _tail(path: Path) -> tuple[int, str] | None:
    """``seq`` and ``row_hash`` of the last row, read from the end of the file."""
    row = _last_row(path)
    if row is None:
        return None
    return int(row["seq"]), str(row[HASH_KEY])


@dataclass(frozen=True, slots=True)
class AuditAnchor:
    """The last row of the chain at one moment (ADR 0033): ``seq``, ``row_hash`` and ``ts``.
    An empty file anchors as ``seq`` 0 with the genesis hash."""

    seq: int
    row_hash: str
    ts: str

    @property
    def text(self) -> str:
        """``<seq>:<row_hash>``, the form ``carto-edge audit verify --anchor`` takes."""
        return f"{self.seq}:{self.row_hash}"


def parse_anchor(text: str) -> tuple[int, str]:
    """``<seq>:<row_hash>`` to ``(seq, row_hash)``; raises ``ValueError`` on anything else."""
    seq_text, separator, digest = text.strip().partition(":")
    digest = digest.lower()
    if not separator or not seq_text.isdecimal() or not _ROW_HASH_RE.fullmatch(digest):
        msg = "an anchor is <seq>:<row_hash>, a row number and 64 hex characters"
        raise ValueError(msg)
    return int(seq_text), digest


def last_anchor(path: Path) -> AuditAnchor:
    """The anchor of the file as it is now (callers hold the write lock)."""
    if not path.exists():
        return AuditAnchor(0, GENESIS_HASH, "")
    row = _last_row(path)
    if row is None:
        return AuditAnchor(0, GENESIS_HASH, "")
    return AuditAnchor(int(row["seq"]), str(row[HASH_KEY]), str(row.get("ts", "")))


class AuditRow(BaseModel):
    """One written row, as returned by :meth:`EdgeAudit.record`."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    seq: int
    ts: AwareDatetime
    action: str
    actor: str
    target: str
    request_id: str
    details: dict[str, Any]
    prev_hash: str
    row_hash: str


@dataclass(frozen=True, slots=True)
class AnchorCheck:
    """One anchor checked by :func:`verify`: ``ok``, or ``missing`` (the chain no longer
    reaches that row: rows were removed) or ``mismatch`` (the row has another hash: the chain
    was rewritten from there or before)."""

    seq: int
    row_hash: str
    status: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """``ok`` when the chain verifies and every anchor holds. ``torn_tail_bytes`` is the size
    of a torn final line that opening the file would recover (not tampering)."""

    ok: bool
    rows: int
    first_bad_line: int | None
    anchors: tuple[AnchorCheck, ...] = ()
    torn_tail_bytes: int = 0


def canonical_json(data: object) -> str:
    """Sorted keys, no whitespace, ASCII only: the same bytes for the same row everywhere."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def row_hash(prev_hash: str, row: Mapping[str, object]) -> str:
    """``sha256(prev_hash || canonical_json(row))`` as hex; ``row`` excludes ``row_hash``."""
    return hashlib.sha256((prev_hash + canonical_json(row)).encode("utf-8")).hexdigest()


def _bound(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _clean_value(value: object, depth: int) -> object:
    """Mask strings, keep JSON scalars, recurse into containers, stringify the rest."""
    if isinstance(value, bool | int) or value is None:
        return value
    if isinstance(value, float):
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, str):
        return mask_string(_bound(value, MAX_DETAIL_VALUE_LEN))
    if isinstance(value, datetime | date):
        return mask_string(value.isoformat())
    if depth >= MAX_DETAIL_DEPTH:
        return mask_string(_bound(str(value), MAX_DETAIL_VALUE_LEN))
    if isinstance(value, Mapping):
        return _clean_details(value, depth + 1)
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)[:MAX_DETAIL_ITEMS]
        return [_clean_value(item, depth + 1) for item in items]
    return mask_string(_bound(str(value), MAX_DETAIL_VALUE_LEN))


def _clean_details(details: Mapping[Any, Any], depth: int = 0) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in list(details.items())[:MAX_DETAIL_ITEMS]:
        cleaned[_bound(str(key), MAX_DETAIL_KEY_LEN)] = _clean_value(value, depth)
    return cleaned


def _parses(line: str | None) -> bool:
    if line is None:
        return False
    try:
        json.loads(line)
    except ValueError:
        return False
    return True


def _check_line(line: str, prev_hash: str, expected_seq: int) -> str | None:
    """The row hash of a valid line that chains to ``prev_hash``, else ``None``."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    claimed = row.pop(HASH_KEY, None)
    if (
        not isinstance(claimed, str)
        or row.get("prev_hash") != prev_hash
        or row.get("seq") != expected_seq
        or row_hash(prev_hash, row) != claimed
    ):
        return None
    return claimed


@dataclass(slots=True)
class _Walk:
    last_hash: str = GENESIS_HASH
    rows: int = 0
    """Rows in the file; lines after the first bad one are counted, a torn tail is not."""
    first_bad: int | None = None
    good_bytes: int = 0
    """Bytes up to the end of the last row that verified."""
    torn_bytes: int = 0
    """Size of a final line with no newline that is not JSON (a torn append)."""
    unterminated: bool = False
    """The final row verifies but has no newline."""
    hashes: dict[int, str] = field(default_factory=dict)


def _walk(path: Path, wanted: Iterable[int] = ()) -> _Walk:
    """Walk the chain; a missing file is an empty chain. ``hashes`` holds the row hash of
    every verified row whose ``seq`` is in ``wanted``."""
    walk = _Walk()
    if not path.exists():
        return walk
    want = frozenset(wanted)
    with path.open("rb") as handle:
        for number, raw in enumerate(handle, start=1):
            walk.rows = number
            if walk.first_bad is not None:
                continue
            terminated = raw.endswith(b"\n")
            try:
                line: str | None = (raw[:-1] if terminated else raw).decode("utf-8")
            except UnicodeDecodeError:
                line = None
            next_hash = _check_line(line, walk.last_hash, number) if line is not None else None
            if next_hash is None:
                if not terminated and not _parses(line):
                    walk.torn_bytes = len(raw)  # only the last line can lack its newline
                    walk.rows = number - 1
                else:
                    walk.first_bad = number
                continue
            walk.last_hash = next_hash
            walk.good_bytes += len(raw)
            walk.unterminated = not terminated
            if number in want:
                walk.hashes[number] = next_hash
    return walk


def verify(path: Path, anchors: Iterable[tuple[int, str]] = ()) -> VerifyResult:
    """Recompute the chain of an audit file and check each ``(seq, row_hash)`` anchor
    (``carto-edge audit verify``, ADR 0033)."""
    wanted = list(anchors)
    walk = _walk(path, (seq for seq, _digest in wanted))
    checks: list[AnchorCheck] = []
    for seq, digest in wanted:
        held = walk.hashes.get(seq)
        if seq == 0 and digest == GENESIS_HASH:
            status = "ok"
        elif held is None:
            status = "missing" if walk.first_bad is None or seq < walk.first_bad else "broken"
        else:
            status = "ok" if held == digest.lower() else "mismatch"
        checks.append(AnchorCheck(seq, digest, status))
    ok = walk.first_bad is None and all(check.ok for check in checks)
    return VerifyResult(
        ok=ok,
        rows=walk.rows,
        first_bad_line=walk.first_bad,
        anchors=tuple(checks),
        torn_tail_bytes=walk.torn_bytes,
    )


def _fsync_directory(directory: Path) -> None:
    """Make a rename or truncation in ``directory`` durable; Windows has no directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_torn(path: Path, walk: _Walk) -> int:
    """Copy the torn tail to ``<name>.torn-<UTC timestamp>``, then cut the file back to its
    last complete row. Returns the bytes moved."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    keep = path.with_name(f"{path.name}{TORN_SUFFIX}{stamp}")
    with path.open("r+b") as handle:
        handle.seek(walk.good_bytes)
        torn = handle.read()
        with keep.open("xb") as copy:
            copy.write(torn)
            copy.flush()
            os.fsync(copy.fileno())
        handle.truncate(walk.good_bytes)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
    return len(torn)


def _terminate(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b"\n")
        handle.flush()
        os.fsync(handle.fileno())


class EdgeAudit:
    """Append rows to the chain. Open verifies the file; ``close`` releases it."""

    __slots__ = ("_handle", "_last_hash", "_lock", "_lock_path", "_path", "_seq", "_size")

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._lock_path = path.with_name(path.name + LOCK_SUFFIX)
        walk = _walk(path)
        recovered = 0
        if walk.first_bad is None and (walk.torn_bytes or walk.unterminated):
            # Another process may be half way through an append: decide under its lock.
            with _exclusive(self._lock_path):
                walk = _walk(path)
                if walk.first_bad is None and walk.torn_bytes:
                    recovered = _recover_torn(path, walk)
                elif walk.first_bad is None and walk.unterminated:
                    _terminate(path)
        if walk.first_bad is not None:
            msg = f"audit chain broken at line {walk.first_bad} of {path}"
            raise AuditChainError(msg)
        self._last_hash = walk.last_hash
        self._seq = walk.rows
        self._handle: TextIO = path.open("a", encoding="utf-8", newline="\n")
        self._size = os.fstat(self._handle.fileno()).st_size
        if recovered:
            log.warning("audit.torn_line_recovered", torn_bytes=recovered)
            self.record(RECOVERED_ACTION, SYSTEM_ACTOR, path.name, {"torn_bytes": recovered})

    def __repr__(self) -> str:
        return f"EdgeAudit(path={str(self._path)!r}, seq={self._seq})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def last_hash(self) -> str:
        return self._last_hash

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def path(self) -> Path:
        return self._path

    def record(
        self,
        action: str,
        actor: str,
        target: str,
        details: Mapping[str, object],
        request_id: str = "",
    ) -> AuditRow:
        """Append one row and return it. ``details`` strings are masked; never pass values."""
        cleaned = _clean_details(details)
        with self._lock, _exclusive(self._lock_path):
            if os.fstat(self._handle.fileno()).st_size != self._size:
                tail = _tail(self._path)  # another process appended since our last row
                if tail is not None:
                    self._seq, self._last_hash = tail
            moment = datetime.now(UTC)
            row: dict[str, object] = {
                "seq": self._seq + 1,
                "ts": moment.isoformat(timespec="milliseconds"),
                "action": _bound(action, MAX_ACTION_LEN),
                "actor": _bound(actor, MAX_ACTOR_LEN),
                "target": _bound(target, MAX_TARGET_LEN),
                "request_id": _bound(request_id, MAX_REQUEST_ID_LEN),
                "details": cleaned,
                "prev_hash": self._last_hash,
            }
            digest = row_hash(self._last_hash, row)
            row[HASH_KEY] = digest
            self._handle.write(canonical_json(row) + "\n")
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._seq += 1
            self._last_hash = digest
            self._size = os.fstat(self._handle.fileno()).st_size
        return AuditRow.model_validate({**row, "ts": moment})

    def anchor(self) -> AuditAnchor:
        """The last row of the file now, whoever wrote it (ADR 0033)."""
        with self._lock, _exclusive(self._lock_path):
            return last_anchor(self._path)

    @staticmethod
    def verify(path: Path, anchors: Iterable[tuple[int, str]] = ()) -> VerifyResult:
        return verify(path, anchors)

    def close(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.close()
