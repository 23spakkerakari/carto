"""Persistence of the flow graph (spec 7.3 ``nodes``, ``hops``, ``terminal_nodes``,
``entity_stats``; ADR 0061, 0066): a Protocol with an in-memory reference and a PostgreSQL
implementation over migration 0005, proven equal by ``core/tests/test_core_graph_repository.py``.

Semantics both share:

- :meth:`GraphRepository.upsert_nodes` records the nodes a pass met (kind, first and last
  seen) and never touches ``display_name``, ``event_type_label`` or ``version``, which are the
  reviewer's (ADR 0066).
- :meth:`GraphRepository.rename_node` is the only write of ``display_name``; it bumps
  ``version`` and refuses a stale ``expected_version`` (:class:`StaleVersionError`), which the
  API's ``If-Match`` relies on.
- :meth:`GraphRepository.replace_hops` makes the rows the tenant's whole set in one
  transaction, keeping ``is_manual_confirmed`` of a row that already existed (an M5 decision a
  rollup must not erase); the same for terminal nodes and entity statistics, which have no
  reviewer columns.

Nothing stored here is a token or a raw value. Errors raise :class:`GraphRepositoryError`
naming the driver error class only (spec 14.7).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

from carto_core.db.postgres import ping_engine
from carto_core.graph.model import EntityStatsRow, HopRow, NodeRow, TerminalNodeRow

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "GraphRepository",
    "GraphRepositoryError",
    "InMemoryGraphRepository",
    "NotFoundError",
    "PostgresGraphRepository",
    "StaleVersionError",
]


class GraphRepositoryError(Exception):
    """A read or write failed. The message names the cause, never a row."""


class StaleVersionError(GraphRepositoryError):
    """``expected_version`` did not match the stored row."""


class NotFoundError(GraphRepositoryError):
    """No row with that id for that tenant."""


@runtime_checkable
class GraphRepository(Protocol):
    def upsert_nodes(self, tenant_id: str, rows: Sequence[NodeRow], now: datetime) -> int:
        """Insert the nodes a pass met or refresh ``kind`` and the seen times; return how many
        rows were new. Reviewer columns are left alone."""
        ...

    def list_nodes(self, tenant_id: str) -> list[NodeRow]:
        """Every node of the tenant, ordered by id."""
        ...

    def get_node(self, tenant_id: str, node_id: str) -> NodeRow | None: ...

    def rename_node(
        self,
        tenant_id: str,
        node_id: str,
        *,
        display_name: str | None,
        expected_version: int,
        now: datetime,
    ) -> NodeRow:
        """Set the display name (``None`` clears it) and bump the version. Raises
        :class:`NotFoundError` or :class:`StaleVersionError`."""
        ...

    def replace_hops(self, tenant_id: str, rows: Sequence[HopRow]) -> None: ...

    def list_hops(self, tenant_id: str) -> list[HopRow]:
        """Every hop of the tenant, ordered by id."""
        ...

    def replace_terminal_nodes(self, tenant_id: str, rows: Sequence[TerminalNodeRow]) -> None: ...

    def terminal_nodes(self, tenant_id: str) -> list[TerminalNodeRow]:
        """Ordered by entity id then node id."""
        ...

    def upsert_entity_stats(self, tenant_id: str, rows: Sequence[EntityStatsRow]) -> None: ...

    def entity_stats(self, tenant_id: str) -> dict[str, EntityStatsRow]: ...

    def ping(self) -> bool: ...


# -- in-memory ---------------------------------------------------------------------------------


class InMemoryGraphRepository:
    """Reference semantics and test double. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nodes: dict[tuple[str, str], NodeRow] = {}
        self._hops: dict[tuple[str, str], HopRow] = {}
        self._terminals: dict[str, list[TerminalNodeRow]] = {}
        self._stats: dict[tuple[str, str], EntityStatsRow] = {}

    def upsert_nodes(self, tenant_id: str, rows: Sequence[NodeRow], now: datetime) -> int:
        new = 0
        with self._lock:
            for row in rows:
                current = self._nodes.get((tenant_id, row.id))
                if current is None:
                    self._nodes[(tenant_id, row.id)] = replace(
                        row,
                        display_name=None,
                        event_type_label=None,
                        version=1,
                        updated_at=now,
                    )
                    new += 1
                    continue
                first = _earliest(current.first_seen, row.first_seen)
                last = _latest(current.last_seen, row.last_seen)
                self._nodes[(tenant_id, row.id)] = replace(
                    current,
                    kind=row.kind or current.kind,
                    first_seen=first,
                    last_seen=last,
                    updated_at=now,
                )
        return new

    def list_nodes(self, tenant_id: str) -> list[NodeRow]:
        with self._lock:
            rows = [row for (tenant, _id), row in self._nodes.items() if tenant == tenant_id]
        return sorted(rows, key=lambda row: row.id)

    def get_node(self, tenant_id: str, node_id: str) -> NodeRow | None:
        with self._lock:
            return self._nodes.get((tenant_id, node_id))

    def rename_node(
        self,
        tenant_id: str,
        node_id: str,
        *,
        display_name: str | None,
        expected_version: int,
        now: datetime,
    ) -> NodeRow:
        with self._lock:
            current = self._nodes.get((tenant_id, node_id))
            if current is None:
                msg = "node not found"
                raise NotFoundError(msg)
            if current.version != expected_version:
                msg = "node version changed"
                raise StaleVersionError(msg)
            stored = replace(
                current, display_name=display_name, version=current.version + 1, updated_at=now
            )
            self._nodes[(tenant_id, node_id)] = stored
            return stored

    def replace_hops(self, tenant_id: str, rows: Sequence[HopRow]) -> None:
        with self._lock:
            previous = {key[1]: row for key, row in self._hops.items() if key[0] == tenant_id}
            for key in [key for key in self._hops if key[0] == tenant_id]:
                del self._hops[key]
            for row in rows:
                old = previous.get(row.id)
                kept = row
                if old is not None and row.is_manual_confirmed is None:
                    kept = replace(row, is_manual_confirmed=old.is_manual_confirmed)
                self._hops[(tenant_id, row.id)] = kept

    def list_hops(self, tenant_id: str) -> list[HopRow]:
        with self._lock:
            rows = [row for (tenant, _id), row in self._hops.items() if tenant == tenant_id]
        return sorted(rows, key=lambda row: row.id)

    def replace_terminal_nodes(self, tenant_id: str, rows: Sequence[TerminalNodeRow]) -> None:
        with self._lock:
            self._terminals[tenant_id] = sorted(rows, key=lambda r: (r.entity_id, r.node_id))

    def terminal_nodes(self, tenant_id: str) -> list[TerminalNodeRow]:
        with self._lock:
            return list(self._terminals.get(tenant_id, []))

    def upsert_entity_stats(self, tenant_id: str, rows: Sequence[EntityStatsRow]) -> None:
        with self._lock:
            for row in rows:
                self._stats[(tenant_id, row.entity_id)] = row

    def entity_stats(self, tenant_id: str) -> dict[str, EntityStatsRow]:
        with self._lock:
            return {key[1]: row for key, row in self._stats.items() if key[0] == tenant_id}

    def ping(self) -> bool:
        return True


def _earliest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _latest(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


# -- PostgreSQL --------------------------------------------------------------------------------

_NODE_COLUMNS = (
    "id, system_id, template_id, kind, display_name, event_type_label, version, first_seen, "
    "last_seen, updated_at"
)
_SELECT_NODES = sqlalchemy.text(
    f"SELECT {_NODE_COLUMNS} FROM nodes WHERE tenant_id = :tenant_id ORDER BY id"  # noqa: S608  # nosec B608 - fixed column list
)
_SELECT_NODE = sqlalchemy.text(
    f"SELECT {_NODE_COLUMNS} FROM nodes WHERE tenant_id = :tenant_id AND id = :id"  # noqa: S608  # nosec B608 - fixed column list
)
_UPSERT_NODE = sqlalchemy.text(
    "INSERT INTO nodes (tenant_id, id, system_id, template_id, kind, first_seen, last_seen, "
    "created_at, updated_at) VALUES (:tenant_id, :id, :system_id, :template_id, :kind, "
    ":first_seen, :last_seen, :now, :now) ON CONFLICT (tenant_id, id) DO UPDATE SET "
    "kind = CASE WHEN EXCLUDED.kind = '' THEN nodes.kind ELSE EXCLUDED.kind END, "
    "first_seen = LEAST(nodes.first_seen, EXCLUDED.first_seen), "
    "last_seen = GREATEST(nodes.last_seen, EXCLUDED.last_seen), updated_at = EXCLUDED.updated_at "
    "RETURNING (xmax = 0) AS inserted"
)
_RENAME_NODE = sqlalchemy.text(
    f"UPDATE nodes SET display_name = :display_name, version = version + 1, updated_at = :now "  # noqa: S608  # nosec B608 - fixed column list
    f"WHERE tenant_id = :tenant_id AND id = :id AND version = :expected_version "
    f"RETURNING {_NODE_COLUMNS}"
)
_HOP_COLUMNS = (
    "id, entity_id, from_node, to_node, support, p_next, latency_p50, latency_p95, latency_p99, "
    "error_count, human_actor_share, link_types, is_manual_score, is_manual_confirmed, "
    "window_start, window_end, computed_at"
)
_SELECT_HOPS = sqlalchemy.text(
    f"SELECT {_HOP_COLUMNS} FROM hops WHERE tenant_id = :tenant_id ORDER BY id"  # noqa: S608  # nosec B608 - fixed column list
)
_DELETE_HOPS_EXCEPT = sqlalchemy.text(
    "DELETE FROM hops WHERE tenant_id = :tenant_id AND NOT (id = ANY(:keep))"
)
_UPSERT_HOP = sqlalchemy.text(
    "INSERT INTO hops (tenant_id, id, entity_id, from_node, to_node, support, p_next, "
    "latency_p50, latency_p95, latency_p99, error_count, human_actor_share, link_types, "
    "is_manual_score, is_manual_confirmed, window_start, window_end, computed_at, created_at, "
    "updated_at) VALUES (:tenant_id, :id, :entity_id, :from_node, :to_node, :support, :p_next, "
    ":latency_p50, :latency_p95, :latency_p99, :error_count, :human_actor_share, "
    "CAST(:link_types AS JSONB), :is_manual_score, :is_manual_confirmed, :window_start, "
    ":window_end, :computed_at, :computed_at, :computed_at) ON CONFLICT (tenant_id, id) DO UPDATE "
    "SET entity_id = EXCLUDED.entity_id, support = EXCLUDED.support, p_next = EXCLUDED.p_next, "
    "latency_p50 = EXCLUDED.latency_p50, latency_p95 = EXCLUDED.latency_p95, "
    "latency_p99 = EXCLUDED.latency_p99, error_count = EXCLUDED.error_count, "
    "human_actor_share = EXCLUDED.human_actor_share, link_types = EXCLUDED.link_types, "
    "is_manual_score = EXCLUDED.is_manual_score, "
    "is_manual_confirmed = COALESCE(EXCLUDED.is_manual_confirmed, hops.is_manual_confirmed), "
    "window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end, "
    "computed_at = EXCLUDED.computed_at, updated_at = EXCLUDED.computed_at"
)
_SELECT_TERMINALS = sqlalchemy.text(
    "SELECT entity_id, node_id, share, computed_at FROM terminal_nodes "
    "WHERE tenant_id = :tenant_id ORDER BY entity_id, node_id"
)
_DELETE_TERMINALS = sqlalchemy.text("DELETE FROM terminal_nodes WHERE tenant_id = :tenant_id")
_INSERT_TERMINAL = sqlalchemy.text(
    "INSERT INTO terminal_nodes (tenant_id, entity_id, node_id, share, computed_at, created_at, "
    "updated_at) VALUES (:tenant_id, :entity_id, :node_id, :share, :computed_at, :computed_at, "
    ":computed_at)"
)
_SELECT_STATS = sqlalchemy.text(
    "SELECT entity_id, p99_gap_seconds, quiet_span_seconds, settled_count, computed_at "
    "FROM entity_stats WHERE tenant_id = :tenant_id ORDER BY entity_id"
)
_UPSERT_STATS = sqlalchemy.text(
    "INSERT INTO entity_stats (tenant_id, entity_id, p99_gap_seconds, quiet_span_seconds, "
    "settled_count, computed_at, created_at, updated_at) VALUES (:tenant_id, :entity_id, "
    ":p99_gap_seconds, :quiet_span_seconds, :settled_count, :computed_at, :computed_at, "
    ":computed_at) ON CONFLICT (tenant_id, entity_id) DO UPDATE SET "
    "p99_gap_seconds = EXCLUDED.p99_gap_seconds, quiet_span_seconds = EXCLUDED.quiet_span_seconds, "
    "settled_count = EXCLUDED.settled_count, computed_at = EXCLUDED.computed_at, "
    "updated_at = EXCLUDED.updated_at"
)


def _wrap(what: str, exc: SQLAlchemyError) -> GraphRepositoryError:
    return GraphRepositoryError(f"{what} failed: {type(exc).__name__}")


def _aware(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        msg = "the graph store returned a non-datetime where a time was expected"
        raise GraphRepositoryError(msg)
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _required(value: Any) -> datetime:
    at = _aware(value)
    if at is None:
        msg = "the graph store returned a null where a time is required"
        raise GraphRepositoryError(msg)
    return at


def _node_from_row(row: Any) -> NodeRow:
    return NodeRow(
        id=str(row.id),
        system_id=str(row.system_id),
        template_id=str(row.template_id),
        kind=str(row.kind or ""),
        display_name=row.display_name,
        event_type_label=row.event_type_label,
        version=int(row.version),
        first_seen=_aware(row.first_seen),
        last_seen=_aware(row.last_seen),
        updated_at=_aware(row.updated_at),
    )


def _hop_from_row(row: Any) -> HopRow:
    link_types = row.link_types
    if isinstance(link_types, str | bytes):
        link_types = json.loads(link_types)
    return HopRow(
        id=str(row.id),
        entity_id=row.entity_id,
        from_node=str(row.from_node),
        to_node=str(row.to_node),
        support=int(row.support),
        p_next=float(row.p_next),
        latency_p50=None if row.latency_p50 is None else float(row.latency_p50),
        latency_p95=None if row.latency_p95 is None else float(row.latency_p95),
        latency_p99=None if row.latency_p99 is None else float(row.latency_p99),
        error_count=int(row.error_count),
        human_actor_share=None if row.human_actor_share is None else float(row.human_actor_share),
        link_types=tuple(str(item) for item in (link_types or [])),
        is_manual_score=None if row.is_manual_score is None else float(row.is_manual_score),
        is_manual_confirmed=row.is_manual_confirmed,
        window_start=_required(row.window_start),
        window_end=_required(row.window_end),
        computed_at=_required(row.computed_at),
    )


class PostgresGraphRepository:
    """See the module docstring (migration 0005)."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert_nodes(self, tenant_id: str, rows: Sequence[NodeRow], now: datetime) -> int:
        if not rows:
            return 0
        new = 0
        try:
            with self._engine.begin() as connection:
                for row in rows:
                    inserted = connection.execute(
                        _UPSERT_NODE,
                        {
                            "tenant_id": tenant_id,
                            "id": row.id,
                            "system_id": row.system_id,
                            "template_id": row.template_id,
                            "kind": row.kind,
                            "first_seen": row.first_seen,
                            "last_seen": row.last_seen,
                            "now": now,
                        },
                    ).scalar_one()
                    new += 1 if inserted else 0
        except SQLAlchemyError as exc:
            raise _wrap("node upsert", exc) from exc
        return new

    def list_nodes(self, tenant_id: str) -> list[NodeRow]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_NODES, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _wrap("node list", exc) from exc
        return [_node_from_row(row) for row in rows]

    def get_node(self, tenant_id: str, node_id: str) -> NodeRow | None:
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    _SELECT_NODE, {"tenant_id": tenant_id, "id": node_id}
                ).first()
        except SQLAlchemyError as exc:
            raise _wrap("node read", exc) from exc
        return None if row is None else _node_from_row(row)

    def rename_node(
        self,
        tenant_id: str,
        node_id: str,
        *,
        display_name: str | None,
        expected_version: int,
        now: datetime,
    ) -> NodeRow:
        try:
            with self._engine.begin() as connection:
                updated = connection.execute(
                    _RENAME_NODE,
                    {
                        "tenant_id": tenant_id,
                        "id": node_id,
                        "display_name": display_name,
                        "expected_version": expected_version,
                        "now": now,
                    },
                ).first()
                if updated is None:
                    exists = connection.execute(
                        _SELECT_NODE, {"tenant_id": tenant_id, "id": node_id}
                    ).first()
                    if exists is None:
                        msg = "node not found"
                        raise NotFoundError(msg)
                    msg = "node version changed"
                    raise StaleVersionError(msg)
        except SQLAlchemyError as exc:
            raise _wrap("node rename", exc) from exc
        return _node_from_row(updated)

    def replace_hops(self, tenant_id: str, rows: Sequence[HopRow]) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    _DELETE_HOPS_EXCEPT,
                    {"tenant_id": tenant_id, "keep": [row.id for row in rows]},
                )
                if rows:
                    connection.execute(
                        _UPSERT_HOP,
                        [
                            {
                                "tenant_id": tenant_id,
                                "id": row.id,
                                "entity_id": row.entity_id,
                                "from_node": row.from_node,
                                "to_node": row.to_node,
                                "support": row.support,
                                "p_next": row.p_next,
                                "latency_p50": row.latency_p50,
                                "latency_p95": row.latency_p95,
                                "latency_p99": row.latency_p99,
                                "error_count": row.error_count,
                                "human_actor_share": row.human_actor_share,
                                "link_types": json.dumps(list(row.link_types)),
                                "is_manual_score": row.is_manual_score,
                                "is_manual_confirmed": row.is_manual_confirmed,
                                "window_start": row.window_start,
                                "window_end": row.window_end,
                                "computed_at": row.computed_at,
                            }
                            for row in rows
                        ],
                    )
        except SQLAlchemyError as exc:
            raise _wrap("hop replace", exc) from exc

    def list_hops(self, tenant_id: str) -> list[HopRow]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_HOPS, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _wrap("hop list", exc) from exc
        return [_hop_from_row(row) for row in rows]

    def replace_terminal_nodes(self, tenant_id: str, rows: Sequence[TerminalNodeRow]) -> None:
        try:
            with self._engine.begin() as connection:
                connection.execute(_DELETE_TERMINALS, {"tenant_id": tenant_id})
                if rows:
                    connection.execute(
                        _INSERT_TERMINAL,
                        [
                            {
                                "tenant_id": tenant_id,
                                "entity_id": row.entity_id,
                                "node_id": row.node_id,
                                "share": row.share,
                                "computed_at": row.computed_at,
                            }
                            for row in rows
                        ],
                    )
        except SQLAlchemyError as exc:
            raise _wrap("terminal node replace", exc) from exc

    def terminal_nodes(self, tenant_id: str) -> list[TerminalNodeRow]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_TERMINALS, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _wrap("terminal node list", exc) from exc
        return [
            TerminalNodeRow(
                entity_id=str(row.entity_id),
                node_id=str(row.node_id),
                share=float(row.share),
                computed_at=_required(row.computed_at),
            )
            for row in rows
        ]

    def upsert_entity_stats(self, tenant_id: str, rows: Sequence[EntityStatsRow]) -> None:
        if not rows:
            return
        try:
            with self._engine.begin() as connection:
                connection.execute(
                    _UPSERT_STATS,
                    [
                        {
                            "tenant_id": tenant_id,
                            "entity_id": row.entity_id,
                            "p99_gap_seconds": row.p99_gap_seconds,
                            "quiet_span_seconds": row.quiet_span_seconds,
                            "settled_count": row.settled_count,
                            "computed_at": row.computed_at,
                        }
                        for row in rows
                    ],
                )
        except SQLAlchemyError as exc:
            raise _wrap("entity stats upsert", exc) from exc

    def entity_stats(self, tenant_id: str) -> dict[str, EntityStatsRow]:
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(_SELECT_STATS, {"tenant_id": tenant_id}).all()
        except SQLAlchemyError as exc:
            raise _wrap("entity stats list", exc) from exc
        return {
            str(row.entity_id): EntityStatsRow(
                entity_id=str(row.entity_id),
                p99_gap_seconds=None if row.p99_gap_seconds is None else float(row.p99_gap_seconds),
                quiet_span_seconds=float(row.quiet_span_seconds),
                settled_count=int(row.settled_count),
                computed_at=_required(row.computed_at),
            )
            for row in rows
        }

    def ping(self) -> bool:
        return ping_engine(self._engine)
