"""The reveal vault: ``token -> AES-256-GCM(raw value)`` at the edge (spec 8.4, 14.4, 14.10).

Only ``raw`` forms are stored. Each value is encrypted under the KMS-wrapped vault data key
(:meth:`carto_edge.keys.KeyManager.vault_data_key`) with a random 96-bit nonce and
``AAD = tenant_id || 0x00 || token`` (spec 14.4), so a ciphertext moved to another token, another
tenant or another data key does not decrypt. ``expires_at`` follows event retention (spec
14.10): every sighting of a value keeps the latest expiry of its token (a backfilled record
followed by a live one must not leave the entry expired), expired entries are never returned,
and :meth:`RevealVault.purge_expired` removes them; the gateway's housekeeping runs it hourly
(:meth:`carto_edge.runtime.EdgeRuntime.housekeeping`).

Storage is SQLite in WAL mode (ADR 0014), one connection shared across threads behind a lock.
Nothing here logs a value or a token.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from carto_common.crypto import AesGcmBox, CryptoError, Keyring
from carto_common.logging import get_logger
from carto_edge.pipeline.model import VaultEntry
from carto_schema.forms import TOKEN_PATTERN

__all__ = ["AAD_SEPARATOR", "RevealVault"]

AAD_SEPARATOR: Final = b"\x00"
QUERY_CHUNK: Final = 500
"""Tokens per ``IN (...)`` clause: well under SQLite's default variable limit."""

_SCHEMA: Final = (
    (
        "CREATE TABLE IF NOT EXISTS vault ("
        "token TEXT PRIMARY KEY, "
        "key_version INTEGER NOT NULL, "
        "ciphertext BLOB NOT NULL, "
        "expires_at INTEGER NOT NULL)"
    ),
    "CREATE INDEX IF NOT EXISTS vault_expires_at ON vault (expires_at)",
)

log = get_logger(component="vault")


def _epoch_seconds(moment: datetime) -> int:
    if moment.tzinfo is None or moment.utcoffset() is None:
        msg = "vault timestamps must be timezone-aware"
        raise ValueError(msg)
    return int(moment.timestamp())


def _valid_tokens(tokens: Iterable[str]) -> list[str]:
    """Well-formed tokens, de-duplicated, in order; anything else is silently a miss."""
    seen: set[str] = set()
    out: list[str] = []
    for token_text in tokens:
        if token_text in seen or not TOKEN_PATTERN.fullmatch(token_text):
            continue
        seen.add(token_text)
        out.append(token_text)
    return out


def _chunks(items: Sequence[str]) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), QUERY_CHUNK):
        yield items[start : start + QUERY_CHUNK]


class RevealVault:
    """Encrypted token-to-value store for one tenant. ``repr`` shows the path and tenant."""

    __slots__ = ("_box", "_conn", "_lock", "_path", "_tenant_id")

    def __init__(self, path: Path, data_key: bytes, tenant_id: str) -> None:
        self._path = path
        self._tenant_id = tenant_id
        self._box = AesGcmBox(data_key)
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        for statement in _SCHEMA:
            self._conn.execute(statement)

    def __repr__(self) -> str:
        return f"RevealVault(path={str(self._path)!r}, tenant_id={self._tenant_id!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _aad(self, token_text: str) -> bytes:
        return self._tenant_id.encode("utf-8") + AAD_SEPARATOR + token_text.encode("ascii")

    def put_many(self, entries: Iterable[VaultEntry]) -> int:
        """Store entries in one transaction. An existing token keeps its ciphertext (the same
        value under the same token) and takes the later of the two expiries. Returns the rows
        inserted or extended."""
        rows: list[tuple[str, int, bytes, int]] = []
        for entry in entries:
            if not TOKEN_PATTERN.fullmatch(entry.token):
                continue
            rows.append(
                (
                    entry.token,
                    Keyring.version_of(entry.token),
                    self._box.encrypt(entry.raw_value.encode("utf-8"), self._aad(entry.token)),
                    _epoch_seconds(entry.expires_at),
                )
            )
        if not rows:
            return 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cursor = self._conn.executemany(
                    "INSERT INTO vault (token, key_version, ciphertext, expires_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(token) DO UPDATE SET expires_at = excluded.expires_at "
                    "WHERE excluded.expires_at > vault.expires_at",
                    rows,
                )
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
            return max(cursor.rowcount, 0)

    def reveal(self, tokens: Sequence[str], now: datetime | None = None) -> dict[str, str]:
        """Values of the given tokens; misses, expired entries and undecryptable rows are
        left out."""
        wanted = _valid_tokens(tokens)
        if not wanted:
            return {}
        cutoff = _epoch_seconds(now if now is not None else datetime.now(UTC))
        found: list[tuple[str, bytes]] = []
        with self._lock:
            for chunk in _chunks(wanted):
                placeholders = ",".join("?" * len(chunk))
                found.extend(
                    self._conn.execute(
                        f"SELECT token, ciphertext FROM vault "  # noqa: S608  # nosec B608 - placeholders only
                        f"WHERE expires_at > ? AND token IN ({placeholders})",
                        (cutoff, *chunk),
                    ).fetchall()
                )
        values: dict[str, str] = {}
        failures = 0
        for token_text, blob in found:
            try:
                values[token_text] = self._box.decrypt(blob, self._aad(token_text)).decode("utf-8")
            except (CryptoError, UnicodeDecodeError):
                failures += 1
        if failures:
            log.warning("vault.undecryptable_rows", count=failures)
        return values

    def delete(self, tokens: Iterable[str]) -> int:
        """Remove entries (spec 14.10 targeted deletion). Returns how many rows went."""
        wanted = _valid_tokens(tokens)
        if not wanted:
            return 0
        deleted = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for chunk in _chunks(wanted):
                    placeholders = ",".join("?" * len(chunk))
                    cursor = self._conn.execute(
                        f"DELETE FROM vault WHERE token IN ({placeholders})",  # noqa: S608  # nosec B608
                        tuple(chunk),
                    )
                    deleted += max(cursor.rowcount, 0)
                self._conn.execute("COMMIT")
            except sqlite3.Error:
                self._conn.execute("ROLLBACK")
                raise
        return deleted

    def purge_expired(self, now: datetime | None = None) -> int:
        """Remove every entry whose ``expires_at`` is at or before ``now``."""
        cutoff = _epoch_seconds(now if now is not None else datetime.now(UTC))
        with self._lock:
            cursor = self._conn.execute("DELETE FROM vault WHERE expires_at <= ?", (cutoff,))
            return max(cursor.rowcount, 0)

    def count(self) -> int:
        with self._lock:
            (count,) = self._conn.execute("SELECT COUNT(*) FROM vault").fetchone()
            return int(count)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
