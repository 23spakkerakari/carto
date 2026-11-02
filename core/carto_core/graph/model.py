"""Flow-graph data model (spec 7.3 ``nodes``, ``hops``, ``terminal_nodes``; spec 9.8; ADR 0061,
0066).

A node is one template (``id = system_id/template_id``) with a reviewer's display name. A hop
is the rollup row the API reads for one ``(entity, from_node, to_node)``; its id is a hash of
that triple so the M5 manual-hop routes can address it. ``entity_stats`` holds the per-entity
quiet span of spec 9.8 that the settling sweep uses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

__all__ = ["EntityStatsRow", "HopRow", "NodeRow", "TerminalNodeRow", "hop_id_for", "node_id_for"]


def node_id_for(system_id: str, template_id: str) -> str:
    """ADR 0066: ``system_id/template_id``."""
    return f"{system_id}/{template_id}"


def hop_id_for(entity_id: str | None, from_node: str, to_node: str) -> str:
    """``hop_`` plus 16 hex digits of the SHA-256 of the triple; stable across passes."""
    digest = hashlib.sha256(f"{entity_id or ''}\n{from_node}\n{to_node}".encode()).hexdigest()
    return f"hop_{digest[:16]}"


@dataclass(frozen=True, slots=True)
class NodeRow:
    """One ``nodes`` row. ``display_name`` is the reviewer's; ``None`` means the console shows
    the template id and kind (never ``template_text``)."""

    id: str
    system_id: str
    template_id: str
    kind: str = ""
    display_name: str | None = None
    event_type_label: str | None = None
    version: int = 1
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HopRow:
    """One ``hops`` row: the rollup over the window (ADR 0061)."""

    id: str
    entity_id: str | None
    from_node: str
    to_node: str
    support: int
    p_next: float
    latency_p50: float | None
    latency_p95: float | None
    latency_p99: float | None
    error_count: int
    human_actor_share: float | None
    link_types: tuple[str, ...]
    window_start: datetime
    window_end: datetime
    computed_at: datetime
    is_manual_score: float | None = None
    is_manual_confirmed: bool | None = None


@dataclass(frozen=True, slots=True)
class TerminalNodeRow:
    """Spec 9.8: a node after which at least the terminal share of the entity's settled
    transactions have no further event."""

    entity_id: str
    node_id: str
    share: float
    computed_at: datetime


@dataclass(frozen=True, slots=True)
class EntityStatsRow:
    """Per entity: the p99 inter-event gap over settled transactions and the quiet span derived
    from it (spec 9.8), plus how many settled transactions the figures rest on."""

    entity_id: str
    p99_gap_seconds: float | None
    quiet_span_seconds: float
    settled_count: int
    computed_at: datetime
