"""Assembler data model (spec 7.3 ``transactions``, ``txn_token_index``, ``txn_token_fanout``,
``txn_merges``, ``batch_keys``; spec 9.7; ADR 0060).

:class:`TransactionRow` is one ``transactions`` row; :class:`IndexEntry` one live token of the
union-find index; :class:`IndexWrite` what one micro-batch applies to PostgreSQL in one
transaction (:meth:`carto_core.assembler.index.AssemblyIndex.apply`). Status values are the
spec 9.7 vocabulary; ``stalled`` is set by the detector (M4) and never by the assembler.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Final

from carto_schema.api import TransactionStatus

__all__ = [
    "STATUS_PRECEDENCE",
    "BatchKeyRow",
    "IndexEntry",
    "IndexWrite",
    "MergeRecord",
    "TransactionRow",
    "TransactionStatus",
]


STATUS_PRECEDENCE: Final[tuple[TransactionStatus, ...]] = (
    TransactionStatus.STALLED,
    TransactionStatus.ERRORED,
    TransactionStatus.COMPLETED,
    TransactionStatus.IN_PROGRESS,
)
"""Spec 9.7 item 7: "Precedence when several hold: stalled, errored, completed, in_progress"."""


@dataclass(frozen=True, slots=True)
class TransactionRow:
    """One ``transactions`` row."""

    id: str
    """A ULID (``carto_common.ids``); the oldest id survives a merge (M3 plan decision 2)."""
    entity_id: str | None
    first_seen: datetime
    last_seen: datetime
    current_node: str | None
    status: TransactionStatus
    event_count: int
    merged_into: str | None = None
    updated_at: datetime | None = None

    @property
    def survives(self) -> bool:
        return self.merged_into is None


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One ``txn_token_index`` row: the transaction a live token points at."""

    token: str
    txn_id: str
    last_seen: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class MergeRecord:
    """One ``txn_merges`` row (union-find history); ``token_end`` names the ``field_ref/form``
    whose token caused the merge, for the review screen, never the token."""

    from_txn: str
    to_txn: str
    merged_at: datetime
    token_end: str | None = None


@dataclass(frozen=True, slots=True)
class BatchKeyRow:
    """One ``batch_keys`` row: a token that never merges (spec 9.7 item 2)."""

    token: str
    field_ref: str
    fanout: int
    first_seen: datetime


@dataclass(frozen=True, slots=True)
class IndexWrite:
    """What one micro-batch writes to PostgreSQL, atomically (M3 plan decision 2):
    transactions to upsert, index entries to upsert by token, merges to record (which also sets
    ``merged_into`` on the merged-away row), batch keys to add, fan-out counts to add per
    ``(token, day)`` and refused merges to add to ``key_links.ambiguous_merges`` per link id."""

    transactions: tuple[TransactionRow, ...] = ()
    entries: tuple[IndexEntry, ...] = ()
    merges: tuple[MergeRecord, ...] = ()
    batch_keys: tuple[BatchKeyRow, ...] = ()
    fanout: Mapping[tuple[str, date], int] = field(default_factory=dict)
    refusals: Mapping[str, int] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (
            self.transactions
            or self.entries
            or self.merges
            or self.batch_keys
            or self.fanout
            or self.refusals
        )
