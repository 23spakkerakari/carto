"""The assembly index: the PostgreSQL side of transaction assembly (spec 7.3, 9.7; ADR 0060;
plan M3 decision 2).

:class:`AssemblyIndex` is what the assembler pass reads before resolving a micro-batch in
memory and writes after: the live token index (``txn_token_index``), the transactions, the
batch keys, the per-day fan-out counters, the merge history and the store watermark. Two
implementations share one contract suite (``core/tests/test_core_assembler_index.py``):
:class:`InMemoryAssemblyIndex`, the reference semantics and the unit-test double, and
:class:`PostgresAssemblyIndex` over SQLAlchemy Core text against migration 0005.

Semantics both share:

- :meth:`AssemblyIndex.apply` is one transaction: every part of an :class:`IndexWrite` lands or
  none does. Transactions are upserted by id; index entries by token; a merge inserts its
  history row and sets ``merged_into`` on the merged-away transaction; batch keys are inserted
  when absent (the first sighting wins); fan-out counts and refusals are added, never set.
- :meth:`AssemblyIndex.lookup` returns live entries only: an entry whose ``expires_at`` is at
  or before ``now`` is not returned (and :meth:`AssemblyIndex.expire` deletes it).
- The watermark is the latest ``stored_at`` the pass has consumed (``settings`` row
  ``assembler.watermark``); clearing it makes the next pass replay the read window over the
  existing index (ADR 0060, ``full``).
- :meth:`AssemblyIndex.settle` is the graph pass's sweep (spec 9.7 item 7, "settled"): the
  surviving ``in_progress`` transactions of an entity whose ``last_seen`` is before the cut
  become ``completed``.

Nothing stored here is a raw value: tokens, ids, counts and times only. Errors raise
:class:`AssemblyIndexError` naming the driver error class only (spec 14.7).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Collection
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.assembler.model import (
    BatchKeyRow,
    IndexEntry,
    IndexWrite,
    MergeRecord,
    TransactionRow,
    TransactionStatus,
)
from carto_core.db.postgres import ping_engine

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "WATERMARK_KEY",
    "AssemblyIndex",
    "AssemblyIndexError",
    "InMemoryAssemblyIndex",
    "PostgresAssemblyIndex",
]

WATERMARK_KEY: Final = "assembler.watermark"
"""The ``settings.key`` of the assembler's ``stored_at`` watermark."""


class AssemblyIndexError(Exception):
    """The index could not be read or written. The message names the cause, never a row."""


@runtime_checkable
class AssemblyIndex(Protocol):
    def lookup(
        self, tenant_id: str, tokens: Collection[str], now: datetime
    ) -> dict[str, IndexEntry]:
        """The live entries of ``tokens`` (expired ones excluded)."""
        ...

    def batch_keys(self, tenant_id: str, tokens: Collection[str]) -> dict[str, BatchKeyRow]: ...

    def fanout(self, tenant_id: str, tokens: Collection[str], day: date) -> dict[str, int]:
        """Distinct transactions seen per token on ``day`` (absent tokens: not returned)."""
        ...

    def transactions(
        self, tenant_id: str, txn_ids: Collection[str]
    ) -> dict[str, TransactionRow]: ...

    def apply(self, tenant_id: str, write: IndexWrite) -> None:
        """Apply the write atomically (module docstring)."""
        ...

    def settle(
        self, tenant_id: str, *, entity_id: str | None, quiet_before: datetime, now: datetime
    ) -> int:
        """Mark surviving ``in_progress`` transactions (of ``entity_id``, or of no entity when
        ``None``) whose ``last_seen`` is before ``quiet_before`` as ``completed``; return how
        many changed."""
        ...

    def expire(self, tenant_id: str, before: datetime) -> int:
        """Delete index entries whose ``expires_at`` is at or before ``before``; return how
        many."""
        ...

    def watermark(self, tenant_id: str) -> datetime | None: ...

    def set_watermark(self, tenant_id: str, at: datetime | None) -> None:
        """Move the watermark, or clear it with ``None`` (a full replay follows)."""
        ...

    def ping(self) -> bool: ...


# -- in-memory ---------------------------------------------------------------------------------


class InMemoryAssemblyIndex:
    """Reference semantics and test double. Thread-safe. ``refusals`` keeps the refused merge
    counts per link id that the PostgreSQL implementation adds to ``key_links``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], IndexEntry] = {}
        self._transactions: dict[tuple[str, str], TransactionRow] = {}
        self._batch_keys: dict[tuple[str, str], BatchKeyRow] = {}
        self._fanout: dict[tuple[str, str, date], int] = {}
        self._merges: list[tuple[str, MergeRecord]] = []
        self._watermarks: dict[str, datetime] = {}
        self.refusals: dict[tuple[str, str], int] = {}

    def lookup(
        self, tenant_id: str, tokens: Collection[str], now: datetime
    ) -> dict[str, IndexEntry]:
        with self._lock:
            out: dict[str, IndexEntry] = {}
            for token in tokens:
                entry = self._entries.get((tenant_id, token))
                if entry is not None and entry.expires_at > now:
                    out[token] = entry
            return out

    def batch_keys(self, tenant_id: str, tokens: Collection[str]) -> dict[str, BatchKeyRow]:
        with self._lock:
            return {
                token: row
                for token in tokens
                if (row := self._batch_keys.get((tenant_id, token))) is not None
            }

    def fanout(self, tenant_id: str, tokens: Collection[str], day: date) -> dict[str, int]:
        with self._lock:
            return {
                token: count
                for token in tokens
                if (count := self._fanout.get((tenant_id, token, day))) is not None
            }

    def transactions(self, tenant_id: str, txn_ids: Collection[str]) -> dict[str, TransactionRow]:
        with self._lock:
            return {
                txn_id: row
                for txn_id in txn_ids
                if (row := self._transactions.get((tenant_id, txn_id))) is not None
            }

    def merges(self, tenant_id: str) -> list[MergeRecord]:
        """The merge history, oldest first (tests and the review screen)."""
        with self._lock:
            return [record for tenant, record in self._merges if tenant == tenant_id]

    def apply(self, tenant_id: str, write: IndexWrite) -> None:
        now = datetime.now(UTC)
        with self._lock:
            for row in write.transactions:
                current = self._transactions.get((tenant_id, row.id))
                stored = row if row.updated_at is not None else replace(row, updated_at=now)
                if (
                    current is not None
                    and current.merged_into is not None
                    and row.merged_into is None
                ):
                    stored = replace(stored, merged_into=current.merged_into)
                self._transactions[(tenant_id, row.id)] = stored
            for entry in write.entries:
                self._entries[(tenant_id, entry.token)] = entry
            for record in write.merges:
                self._merges.append((tenant_id, record))
                gone = self._transactions.get((tenant_id, record.from_txn))
                if gone is not None:
                    self._transactions[(tenant_id, record.from_txn)] = replace(
                        gone, merged_into=record.to_txn, updated_at=record.merged_at
                    )
            for key in write.batch_keys:
                self._batch_keys.setdefault((tenant_id, key.token), key)
            for (token, day), count in write.fanout.items():
                self._fanout[(tenant_id, token, day)] = (
                    self._fanout.get((tenant_id, token, day), 0) + count
                )
            for link_id, count in write.refusals.items():
                self.refusals[(tenant_id, link_id)] = (
                    self.refusals.get((tenant_id, link_id), 0) + count
                )

    def settle(
        self, tenant_id: str, *, entity_id: str | None, quiet_before: datetime, now: datetime
    ) -> int:
        changed = 0
        with self._lock:
            for key, row in list(self._transactions.items()):
                if key[0] != tenant_id or row.entity_id != entity_id:
                    continue
                if (
                    row.merged_into is None
                    and row.status is TransactionStatus.IN_PROGRESS
                    and row.last_seen < quiet_before
                ):
                    self._transactions[key] = replace(
                        row, status=TransactionStatus.COMPLETED, updated_at=now
                    )
                    changed += 1
        return changed

    def expire(self, tenant_id: str, before: datetime) -> int:
        with self._lock:
            gone = [
                key
                for key, entry in self._entries.items()
                if key[0] == tenant_id and entry.expires_at <= before
            ]
            for key in gone:
                del self._entries[key]
        return len(gone)

    def watermark(self, tenant_id: str) -> datetime | None:
        with self._lock:
            return self._watermarks.get(tenant_id)

    def set_watermark(self, tenant_id: str, at: datetime | None) -> None:
        with self._lock:
            if at is None:
                self._watermarks.pop(tenant_id, None)
            elif at.tzinfo is None:
                msg = "watermark must be timezone-aware"
                raise ValueError(msg)
            else:
                self._watermarks[tenant_id] = at

    def ping(self) -> bool:
        return True


# -- PostgreSQL --------------------------------------------------------------------------------

_SELECT_ENTRIES = sqlalchemy.text(
    "SELECT token, txn_id, last_seen, expires_at FROM txn_token_index "
    "WHERE tenant_id = :tenant_id AND token = ANY(:tokens) AND expires_at > :now"
)
_SELECT_BATCH_KEYS = sqlalchemy.text(
    "SELECT token, field_ref, fanout, first_seen FROM batch_keys "
    "WHERE tenant_id = :tenant_id AND token = ANY(:tokens)"
)
_SELECT_FANOUT = sqlalchemy.text(
    "SELECT token, txn_count FROM txn_token_fanout "
    "WHERE tenant_id = :tenant_id AND token = ANY(:tokens) AND day = :day"
)
_SELECT_TRANSACTIONS = sqlalchemy.text(
    "SELECT id, entity_id, first_seen, last_seen, current_node, status, event_count, "
    "merged_into, updated_at FROM transactions WHERE tenant_id = :tenant_id AND id = ANY(:ids)"
)
_UPSERT_TRANSACTION = sqlalchemy.text(
    "INSERT INTO transactions (tenant_id, id, entity_id, first_seen, last_seen, current_node, "
    "status, event_count, merged_into, created_at, updated_at) VALUES (:tenant_id, :id, "
    ":entity_id, :first_seen, :last_seen, :current_node, :status, :event_count, :merged_into, "
    ":now, :now) ON CONFLICT (tenant_id, id) DO UPDATE SET entity_id = EXCLUDED.entity_id, "
    "first_seen = EXCLUDED.first_seen, last_seen = EXCLUDED.last_seen, "
    "current_node = EXCLUDED.current_node, status = EXCLUDED.status, "
    "event_count = EXCLUDED.event_count, "
    "merged_into = COALESCE(transactions.merged_into, EXCLUDED.merged_into), "
    "updated_at = EXCLUDED.updated_at"
)
_UPSERT_ENTRY = sqlalchemy.text(
    "INSERT INTO txn_token_index (tenant_id, token, txn_id, last_seen, expires_at, created_at, "
    "updated_at) VALUES (:tenant_id, :token, :txn_id, :last_seen, :expires_at, :now, :now) "
    "ON CONFLICT (tenant_id, token) DO UPDATE SET txn_id = EXCLUDED.txn_id, "
    "last_seen = EXCLUDED.last_seen, expires_at = EXCLUDED.expires_at, "
    "updated_at = EXCLUDED.updated_at"
)
_INSERT_MERGE = sqlalchemy.text(
    "INSERT INTO txn_merges (tenant_id, from_txn, to_txn, merged_at, token_end, created_at, "
    "updated_at) VALUES (:tenant_id, :from_txn, :to_txn, :merged_at, :token_end, :merged_at, "
    ":merged_at)"
)
_MARK_MERGED = sqlalchemy.text(
    "UPDATE transactions SET merged_into = :to_txn, updated_at = :merged_at "
    "WHERE tenant_id = :tenant_id AND id = :from_txn"
)
_INSERT_BATCH_KEY = sqlalchemy.text(
    "INSERT INTO batch_keys (tenant_id, token, field_ref, fanout, first_seen, created_at, "
    "updated_at) VALUES (:tenant_id, :token, :field_ref, :fanout, :first_seen, :now, :now) "
    "ON CONFLICT (tenant_id, token) DO NOTHING"
)
_ADD_FANOUT = sqlalchemy.text(
    "INSERT INTO txn_token_fanout (tenant_id, token, day, txn_count, created_at, updated_at) "
    "VALUES (:tenant_id, :token, :day, :count, :now, :now) ON CONFLICT (tenant_id, token, day) "
    "DO UPDATE SET txn_count = txn_token_fanout.txn_count + EXCLUDED.txn_count, "
    "updated_at = EXCLUDED.updated_at"
)
_ADD_REFUSALS = sqlalchemy.text(
    "UPDATE key_links SET ambiguous_merges = ambiguous_merges + :count "
    "WHERE tenant_id = :tenant_id AND id = :link_id"
)
_SETTLE = sqlalchemy.text(
    "UPDATE transactions SET status = 'completed', updated_at = :now "
    "WHERE tenant_id = :tenant_id AND merged_into IS NULL AND status = 'in_progress' "
    "AND last_seen < :quiet_before AND entity_id IS NOT DISTINCT FROM :entity_id"
)
_EXPIRE = sqlalchemy.text(
    "DELETE FROM txn_token_index WHERE tenant_id = :tenant_id AND expires_at <= :before"
)
_SELECT_SETTING = sqlalchemy.text(
    "SELECT value_json FROM settings WHERE tenant_id = :tenant_id AND key = :key"
)
_UPSERT_SETTING = sqlalchemy.text(
    "INSERT INTO settings (tenant_id, key, value_json, created_at, updated_at) VALUES "
    "(:tenant_id, :key, CAST(:value_json AS JSONB), now(), now()) "
    "ON CONFLICT (tenant_id, key) DO UPDATE SET value_json = EXCLUDED.value_json, "
    "updated_at = now()"
)
_DELETE_SETTING = sqlalchemy.text(
    "DELETE FROM settings WHERE tenant_id = :tenant_id AND key = :key"
)


def _wrap(what: str, exc: SQLAlchemyError) -> AssemblyIndexError:
    return AssemblyIndexError(f"{what} failed: {type(exc).__name__}")


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime):
        msg = "the index returned a non-datetime where a time was expected"
        raise AssemblyIndexError(msg)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _transaction_from_row(row: Any) -> TransactionRow:
    return TransactionRow(
        id=str(row.id),
        entity_id=row.entity_id,
        first_seen=_aware(row.first_seen),
        last_seen=_aware(row.last_seen),
        current_node=row.current_node,
        status=TransactionStatus(str(row.status)),
        event_count=int(row.event_count),
        merged_into=row.merged_into,
        updated_at=_aware(row.updated_at) if row.updated_at is not None else None,
    )


class PostgresAssemblyIndex:
    """See the module docstring (migration 0005)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _read(
        self, what: str, statement: sqlalchemy.TextClause, parameters: dict[str, Any]
    ) -> list[Any]:
        try:
            with self._engine.connect() as connection:
                return list(connection.execute(statement, parameters).all())
        except SQLAlchemyError as exc:
            raise _wrap(what, exc) from exc

    def lookup(
        self, tenant_id: str, tokens: Collection[str], now: datetime
    ) -> dict[str, IndexEntry]:
        if not tokens:
            return {}
        rows = self._read(
            "index lookup",
            _SELECT_ENTRIES,
            {"tenant_id": tenant_id, "tokens": sorted(set(tokens)), "now": now},
        )
        return {
            str(row.token): IndexEntry(
                token=str(row.token),
                txn_id=str(row.txn_id),
                last_seen=_aware(row.last_seen),
                expires_at=_aware(row.expires_at),
            )
            for row in rows
        }

    def batch_keys(self, tenant_id: str, tokens: Collection[str]) -> dict[str, BatchKeyRow]:
        if not tokens:
            return {}
        rows = self._read(
            "batch key lookup",
            _SELECT_BATCH_KEYS,
            {"tenant_id": tenant_id, "tokens": sorted(set(tokens))},
        )
        return {
            str(row.token): BatchKeyRow(
                token=str(row.token),
                field_ref=str(row.field_ref),
                fanout=int(row.fanout),
                first_seen=_aware(row.first_seen),
            )
            for row in rows
        }

    def fanout(self, tenant_id: str, tokens: Collection[str], day: date) -> dict[str, int]:
        if not tokens:
            return {}
        rows = self._read(
            "fanout lookup",
            _SELECT_FANOUT,
            {"tenant_id": tenant_id, "tokens": sorted(set(tokens)), "day": day},
        )
        return {str(row.token): int(row.txn_count) for row in rows}

    def transactions(self, tenant_id: str, txn_ids: Collection[str]) -> dict[str, TransactionRow]:
        if not txn_ids:
            return {}
        rows = self._read(
            "transaction lookup",
            _SELECT_TRANSACTIONS,
            {"tenant_id": tenant_id, "ids": sorted(set(txn_ids))},
        )
        return {str(row.id): _transaction_from_row(row) for row in rows}

    def apply(self, tenant_id: str, write: IndexWrite) -> None:
        if write.is_empty:
            return
        now = datetime.now(UTC)
        try:
            with self._engine.begin() as connection:
                self._apply(connection, tenant_id, write, now)
        except SQLAlchemyError as exc:
            raise _wrap("index write", exc) from exc

    @staticmethod
    def _apply(connection: Connection, tenant_id: str, write: IndexWrite, now: datetime) -> None:
        if write.transactions:
            connection.execute(
                _UPSERT_TRANSACTION,
                [
                    {
                        "tenant_id": tenant_id,
                        "id": row.id,
                        "entity_id": row.entity_id,
                        "first_seen": row.first_seen,
                        "last_seen": row.last_seen,
                        "current_node": row.current_node,
                        "status": row.status.value,
                        "event_count": row.event_count,
                        "merged_into": row.merged_into,
                        "now": row.updated_at if row.updated_at is not None else now,
                    }
                    for row in write.transactions
                ],
            )
        if write.entries:
            connection.execute(
                _UPSERT_ENTRY,
                [
                    {
                        "tenant_id": tenant_id,
                        "token": entry.token,
                        "txn_id": entry.txn_id,
                        "last_seen": entry.last_seen,
                        "expires_at": entry.expires_at,
                        "now": now,
                    }
                    for entry in write.entries
                ],
            )
        for record in write.merges:
            parameters = {
                "tenant_id": tenant_id,
                "from_txn": record.from_txn,
                "to_txn": record.to_txn,
                "merged_at": record.merged_at,
                "token_end": record.token_end,
            }
            connection.execute(_INSERT_MERGE, parameters)
            connection.execute(_MARK_MERGED, parameters)
        if write.batch_keys:
            connection.execute(
                _INSERT_BATCH_KEY,
                [
                    {
                        "tenant_id": tenant_id,
                        "token": key.token,
                        "field_ref": key.field_ref,
                        "fanout": key.fanout,
                        "first_seen": key.first_seen,
                        "now": now,
                    }
                    for key in write.batch_keys
                ],
            )
        if write.fanout:
            connection.execute(
                _ADD_FANOUT,
                [
                    {"tenant_id": tenant_id, "token": token, "day": day, "count": count, "now": now}
                    for (token, day), count in write.fanout.items()
                ],
            )
        for link_id, count in write.refusals.items():
            connection.execute(
                _ADD_REFUSALS, {"tenant_id": tenant_id, "link_id": link_id, "count": count}
            )

    def settle(
        self, tenant_id: str, *, entity_id: str | None, quiet_before: datetime, now: datetime
    ) -> int:
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    _SETTLE,
                    {
                        "tenant_id": tenant_id,
                        "entity_id": entity_id,
                        "quiet_before": quiet_before,
                        "now": now,
                    },
                )
                return int(result.rowcount)
        except SQLAlchemyError as exc:
            raise _wrap("settle", exc) from exc

    def expire(self, tenant_id: str, before: datetime) -> int:
        try:
            with self._engine.begin() as connection:
                result = connection.execute(_EXPIRE, {"tenant_id": tenant_id, "before": before})
                return int(result.rowcount)
        except SQLAlchemyError as exc:
            raise _wrap("expire", exc) from exc

    def watermark(self, tenant_id: str) -> datetime | None:
        rows = self._read(
            "watermark lookup", _SELECT_SETTING, {"tenant_id": tenant_id, "key": WATERMARK_KEY}
        )
        if not rows:
            return None
        value = rows[0][0]
        if isinstance(value, str | bytes):
            value = json.loads(value)
        stored = value.get("stored_at") if isinstance(value, dict) else None
        if not isinstance(stored, str):
            msg = "watermark setting holds no stored_at"
            raise AssemblyIndexError(msg)
        try:
            parsed = datetime.fromisoformat(stored)
        except ValueError as exc:
            msg = "watermark setting is not an ISO 8601 timestamp"
            raise AssemblyIndexError(msg) from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    def set_watermark(self, tenant_id: str, at: datetime | None) -> None:
        try:
            with self._engine.begin() as connection:
                if at is None:
                    connection.execute(
                        _DELETE_SETTING, {"tenant_id": tenant_id, "key": WATERMARK_KEY}
                    )
                    return
                if at.tzinfo is None:
                    msg = "watermark must be timezone-aware"
                    raise ValueError(msg)
                connection.execute(
                    _UPSERT_SETTING,
                    {
                        "tenant_id": tenant_id,
                        "key": WATERMARK_KEY,
                        "value_json": json.dumps({"stored_at": at.astimezone(UTC).isoformat()}),
                    },
                )
        except SQLAlchemyError as exc:
            raise _wrap("watermark write", exc) from exc

    def ping(self) -> bool:
        return ping_engine(self._engine)
