"""Console API contract (spec 12, 13, 9.3 to 9.8): the request and response models of every M2
and M3 endpoint, exported to JSON Schema like the other contracts and the source of the
committed OpenAPI document (``core/openapi/openapi.v1.json``).

The vocabularies of the linker (link types, roles, directions, statuses, reject reasons) live
here and nowhere else, so the engine (``carto_core.linker``), the API and the web client agree
on the same strings. Every string in a view that comes from customer data (field paths, template
text, names) is bounded in length and rendered by the console as text only (spec 13).

Nothing in a view is a raw value (spec 2.3 invariant 2): evidence carries counts, shares, lags
and shapes (spec 9.6 "features in plain language"), and search results carry no tokens. The one
place tokens appear is :class:`TraceEventView.identifiers` (spec 12 "lists each event's
identifiers so the Reveal button can name what to reveal"), which the console keeps behind
"Details" (spec 13).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from carto_schema.bundle import FieldClassName, PolicyName, ShapeShare
from carto_schema.event import ContractModel, SystemId, TemplateId, UtcTimestamp

__all__ = [
    "API_VERSION",
    "MAX_DESCRIPTION_LEN",
    "MAX_EVENTS_PER_TRACE",
    "MAX_NAME_LEN",
    "MAX_PAGE_SIZE",
    "MAX_QUERY_LEN",
    "MAX_REASON_LEN",
    "PROBLEM_TYPE_PREFIX",
    "AuditEntryView",
    "CompositeComponentView",
    "DecisionRequest",
    "Direction",
    "EntityFieldView",
    "EntityPatch",
    "EntityRef",
    "EntityView",
    "EvidenceView",
    "FieldView",
    "GraphView",
    "HopView",
    "IdentifierView",
    "LinkEnd",
    "LinkRole",
    "LinkStatus",
    "LinkType",
    "LinkView",
    "ManualSignalView",
    "MeView",
    "NodePatch",
    "NodeView",
    "Page",
    "Permission",
    "ProblemDetail",
    "ProblemType",
    "RejectReason",
    "Role",
    "SearchResponse",
    "SourceHealthView",
    "SourceView",
    "SystemPatch",
    "SystemView",
    "TraceEventView",
    "TraceSummary",
    "TraceView",
    "TransactionStatus",
]

API_VERSION: Final = "v1"
MAX_PAGE_SIZE: Final = 500
"""Spec 12: cursor pagination, ``limit`` at most 500."""
MAX_NAME_LEN: Final = 128
MAX_DESCRIPTION_LEN: Final = 1024
MAX_REASON_LEN: Final = 512
MAX_SENTENCE_LEN: Final = 1024
MAX_QUERY_LEN: Final = 256
"""Spec 12 search: the typed identifier after NFKC and trim, 1 to 256 characters."""
MAX_EVENTS_PER_TRACE: Final = 10_000
"""Spec 9.7 item 4: a transaction never exceeds 10,000 events."""
MAX_NODE_ID_LEN: Final = 256
MAX_TOKEN_LEN: Final = 64
PROBLEM_TYPE_PREFIX: Final = "urn:carto:problem:"
"""Plan M3 decision 19: machine-readable problem types, ``urn:carto:problem:<name>``."""


# ---------------------------------------------------------------------------------------------
# Vocabularies (spec 9.3 to 9.6, 14.2)
# ---------------------------------------------------------------------------------------------


class LinkType(StrEnum):
    """How two fields were found to identify the same record (spec 9.3 to 9.5)."""

    EXACT = "exact"
    BRIDGE = "bridge"
    COMPOSITE = "composite"


class LinkRole(StrEnum):
    """What a link may be used for (spec 9.3 "Link roles")."""

    TRANSACTION = "transaction"
    ASSOCIATION = "association"
    BATCH = "batch"


class Direction(StrEnum):
    """Spec 9.3 "Direction": which end's events come first."""

    A_TO_B = "a_to_b"
    B_TO_A = "b_to_a"
    UNDIRECTED = "undirected"


class LinkStatus(StrEnum):
    """Review state of a link (spec 7.3 ``key_links.status``, 9.6)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    AUTO_ACCEPTED = "auto_accepted"
    STALE = "stale"


class TransactionStatus(StrEnum):
    """Spec 9.7 item 7; ``stalled`` is the detector's (M4)."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ERRORED = "errored"
    STALLED = "stalled"


class ProblemType(StrEnum):
    """The machine-readable problem types of RFC 7807 responses (plan M3 decision 19)."""

    BLANK = "about:blank"
    STEP_UP_REQUIRED = PROBLEM_TYPE_PREFIX + "step_up_required"
    SEARCH_UNAVAILABLE = PROBLEM_TYPE_PREFIX + "search_unavailable"
    REVEAL_UNAVAILABLE = PROBLEM_TYPE_PREFIX + "reveal_unavailable"
    SEARCH_RATE_LIMITED = PROBLEM_TYPE_PREFIX + "search_rate_limited"


class RejectReason(StrEnum):
    """Spec 9.6: "Reject (with reason: different meaning, coincidence, wrong direction)"."""

    DIFFERENT_MEANING = "different_meaning"
    COINCIDENCE = "coincidence"
    WRONG_DIRECTION = "wrong_direction"


class Role(StrEnum):
    """Spec 14.2 roles. ``reveal`` is a separate grant, never a role."""

    ADMIN = "admin"
    INTEGRATION_ENGINEER = "integration_engineer"
    BUSINESS_VIEWER = "business_viewer"
    AUDITOR = "auditor"


class Permission(StrEnum):
    """The rows of the spec 14.2 table. Handlers ask for exactly one of these."""

    VIEW = "view"
    """View status board, traces, alerts; in M2 systems, sources, fields, links, entities."""
    SEARCH = "search"
    """Search traces by identifier: the tokenize call to the edge (spec 8.4, 12), rate limited
    and audited at the edge (plan M3 decision 8). Every role that may view may search."""
    REVIEW_LINKS = "review_links"
    """Review links, name entities and nodes."""
    MANAGE_EXPECTATIONS = "manage_expectations"
    ACK_ALERTS = "ack_alerts"
    MANAGE_SOURCES = "manage_sources"
    """Manage sources, field policies, channels."""
    MANAGE_ADMIN = "manage_admin"
    """Manage roles, API keys, settings, retention."""
    VIEW_AUDIT = "view_audit"
    REVEAL = "reveal"
    """Only if granted; never implied by a role (spec 14.2)."""


# ---------------------------------------------------------------------------------------------
# Pagination and errors (spec 12 conventions)
# ---------------------------------------------------------------------------------------------


class Page[T](ContractModel):
    """One page of a list endpoint: ``items`` and the cursor of the next page, if any."""

    items: list[T]
    next_cursor: Annotated[str | None, Field(max_length=256)] = None


class ProblemDetail(ContractModel):
    """RFC 7807 problem document (spec 12). ``detail`` never echoes request content. ``type``
    is :data:`ProblemType.BLANK` or one of the ``urn:carto:problem:`` types."""

    type: Annotated[str, Field(max_length=256)] = ProblemType.BLANK.value
    title: Annotated[str, Field(max_length=256)]
    status: Annotated[int, Field(ge=400, le=599)]
    detail: Annotated[str, Field(max_length=2048)] = ""
    instance: Annotated[str | None, Field(max_length=256)] = None


# ---------------------------------------------------------------------------------------------
# Who am I (spec 12 ``GET /me``)
# ---------------------------------------------------------------------------------------------


class MeView(ContractModel):
    subject: Annotated[str, Field(min_length=1, max_length=256)]
    display_name: Annotated[str, Field(max_length=MAX_NAME_LEN)] = ""
    roles: list[Role]
    permissions: list[Permission]
    csrf_token: Annotated[str, Field(min_length=16, max_length=128)]
    """The synchronizer token the client sends in ``X-CSRF-Token`` on every mutation (14.1)."""
    session_expires_at: UtcTimestamp


# ---------------------------------------------------------------------------------------------
# Inventory views (plan M2 decision 8; spec 7.3 systems, sources, fields)
# ---------------------------------------------------------------------------------------------


class SystemView(ContractModel):
    id: SystemId
    name: Annotated[str, Field(max_length=256)]
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION_LEN)] = ""
    owner_group: Annotated[str | None, Field(max_length=256)] = None
    criticality: Annotated[str, Field(max_length=16)] = "medium"
    source_count: Annotated[int, Field(ge=0)] = 0
    updated_at: UtcTimestamp
    etag: Annotated[str, Field(min_length=1, max_length=64)]


class SystemPatch(ContractModel):
    """``PATCH /systems/{id}``: only what a reviewer may rename (spec 12)."""

    name: Annotated[str | None, Field(min_length=1, max_length=256)] = None
    description: Annotated[str | None, Field(max_length=MAX_DESCRIPTION_LEN)] = None


class SourceHealthView(ContractModel):
    """The latest heartbeat of a source (spec 8.1 common requirements, 12 ``/health``)."""

    status: Annotated[str, Field(max_length=16)]
    last_success_at: UtcTimestamp | None = None
    lag_seconds: Annotated[float, Field(ge=0)] = 0.0
    error_count: Annotated[int, Field(ge=0)] = 0
    buffer_depth: Annotated[int, Field(ge=0)] = 0
    oldest_buffered_at: UtcTimestamp | None = None
    message: Annotated[str, Field(max_length=1024)] = ""
    received_at: UtcTimestamp | None = None


class SourceView(ContractModel):
    """A connector as the edge reported it: configuration without secrets (spec 14.3)."""

    id: Annotated[str, Field(max_length=64)]
    system_id: SystemId
    type: Annotated[str, Field(max_length=32)]
    enabled: bool = True
    config: dict[str, object] = Field(
        default_factory=dict,
        description="Connector configuration without secrets; the secret is named by secret_ref.",
    )
    secret_ref: Annotated[str | None, Field(max_length=1024)] = None
    backfill_days: Annotated[int | None, Field(ge=0, le=400)] = None
    schedule: Annotated[str | None, Field(max_length=128)] = None
    health: SourceHealthView | None = None
    last_test: Annotated[str | None, Field(max_length=1024)] = None
    """The last read-only verification result the edge reported, as text."""
    updated_at: UtcTimestamp


class FieldView(ContractModel):
    """One field as the profiler knows it (spec 7.3 ``fields``). Shapes, never values."""

    id: Annotated[str, Field(min_length=1, max_length=512)]
    """The ``field_ref``: ``system_id/template_id/path``."""
    system_id: SystemId
    template_id: TemplateId
    path: Annotated[str, Field(min_length=1, max_length=256)]
    field_class: FieldClassName
    policy: PolicyName
    pinned: bool = False
    count: Annotated[int, Field(ge=0)] = 0
    distinct_estimate: Annotated[int, Field(ge=0)] = 0
    null_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    shapes: Annotated[list[ShapeShare], Field(max_length=8)] = Field(default_factory=list)
    forms: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    quality: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    """Identifier quality ``q(field)`` of spec 9.1; null for fields that are not identifiers."""
    label: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    """A confirmed business label; suggestions stay ``suggested_label`` until confirmed."""
    suggested_label: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    first_seen: UtcTimestamp | None = None
    last_seen: UtcTimestamp | None = None


# ---------------------------------------------------------------------------------------------
# Links (spec 9.3 to 9.6, 12 ``/links``)
# ---------------------------------------------------------------------------------------------


class LinkEnd(ContractModel):
    """One end of a link: a field and the form of its tokens."""

    field_id: Annotated[str, Field(min_length=1, max_length=512)]
    system_id: SystemId
    template_id: TemplateId
    path: Annotated[str, Field(min_length=1, max_length=256)]
    form: Annotated[str, Field(min_length=1, max_length=32)]
    label: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    top_shape: Annotated[str | None, Field(max_length=64)] = None


class CompositeComponentView(ContractModel):
    a: LinkEnd
    b: LinkEnd
    weight: float
    """Fellegi-Sunter log-likelihood ratio of agreement (spec 9.5)."""
    m: Annotated[float, Field(ge=0.0, le=1.0)]
    u: Annotated[float, Field(ge=0.0, le=1.0)]


class EvidenceView(ContractModel):
    """The spec 9.3 features, every one named as in the table, plus the bridge consistency and
    the composite components. Counts and ratios only."""

    n_a: Annotated[int, Field(ge=0)]
    n_b: Annotated[int, Field(ge=0)]
    n_match: Annotated[int, Field(ge=0)]
    c_ab: Annotated[float, Field(ge=0.0, le=1.0)]
    c_ba: Annotated[float, Field(ge=0.0, le=1.0)]
    lag_median_seconds: float | None = None
    lag_iqr_seconds: Annotated[float | None, Field(ge=0.0)] = None
    lag_positive_share: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    fanout_ab_median: Annotated[float | None, Field(ge=0.0)] = None
    fanout_ab_p99: Annotated[float | None, Field(ge=0.0)] = None
    fanout_ba_median: Annotated[float | None, Field(ge=0.0)] = None
    fanout_ba_p99: Annotated[float | None, Field(ge=0.0)] = None
    concentration: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    chance_ratio: Annotated[float | None, Field(ge=0.0)] = None
    domain_size: Annotated[float | None, Field(ge=1.0)] = None
    pairing_consistency: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    """Bridges only (spec 9.4): the share of A values that always pair with one B value."""
    components: Annotated[list[CompositeComponentView], Field(max_length=32)] = Field(
        default_factory=list
    )
    """Composite links only (spec 9.5)."""
    window_start: UtcTimestamp | None = None
    window_end: UtcTimestamp | None = None


class LinkView(ContractModel):
    """One review-queue item (spec 9.6)."""

    id: Annotated[str, Field(min_length=1, max_length=64)]
    a: LinkEnd
    b: LinkEnd
    link_type: LinkType
    role: LinkRole
    direction: Direction
    score: Annotated[float, Field(ge=0.0, le=1.0)]
    status: LinkStatus
    affected_volume: Annotated[int, Field(ge=0)]
    """Matched tokens over the window; the queue sorts by ``score x affected_volume``."""
    sentence: Annotated[str, Field(max_length=MAX_SENTENCE_LEN)]
    """The plain-language evidence sentence (spec 9.6)."""
    evidence: EvidenceView
    entity_id: Annotated[str | None, Field(max_length=64)] = None
    reject_reason: RejectReason | None = None
    decided_by: Annotated[str | None, Field(max_length=256)] = None
    decided_at: UtcTimestamp | None = None
    ambiguous_merges: Annotated[int, Field(ge=0)] = 0
    version: Annotated[int, Field(ge=1)]
    """Decision version; every decision appends one (spec 9.6 "audited and versioned")."""
    first_proposed_at: UtcTimestamp
    updated_at: UtcTimestamp
    etag: Annotated[str, Field(min_length=1, max_length=64)]


class DecisionRequest(ContractModel):
    """Body of ``POST /links/{id}/accept``, ``/reject`` and ``/association`` (spec 9.6)."""

    reason: RejectReason | None = None
    """Required on ``/reject``; ignored elsewhere."""
    note: Annotated[str, Field(max_length=MAX_REASON_LEN)] = ""


# ---------------------------------------------------------------------------------------------
# Entities (spec 9.6 families, 12 ``/entities``)
# ---------------------------------------------------------------------------------------------


class EntityFieldView(ContractModel):
    field_id: Annotated[str, Field(min_length=1, max_length=512)]
    form: Annotated[str, Field(min_length=1, max_length=32)]
    system_id: SystemId
    path: Annotated[str, Field(min_length=1, max_length=256)]


class EntityView(ContractModel):
    id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(max_length=MAX_NAME_LEN)]
    suggested_name: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    name_confirmed: bool = False
    description: Annotated[str, Field(max_length=MAX_DESCRIPTION_LEN)] = ""
    fields: Annotated[list[EntityFieldView], Field(max_length=4096)]
    link_ids: Annotated[list[str], Field(max_length=4096)] = Field(default_factory=list)
    system_count: Annotated[int, Field(ge=0)] = 0
    updated_at: UtcTimestamp
    etag: Annotated[str, Field(min_length=1, max_length=64)]


class EntityPatch(ContractModel):
    """``PATCH /entities/{id}``: confirm or change the name and description."""

    name: Annotated[str | None, Field(min_length=1, max_length=MAX_NAME_LEN)] = None
    description: Annotated[str | None, Field(max_length=MAX_DESCRIPTION_LEN)] = None


# ---------------------------------------------------------------------------------------------
# Audit (spec 14.9, 12 ``GET /audit``)
# ---------------------------------------------------------------------------------------------


class AuditEntryView(ContractModel):
    id: Annotated[int, Field(ge=1)]
    at: UtcTimestamp
    actor: Annotated[str, Field(max_length=256)]
    action: Annotated[str, Field(max_length=64)]
    target: Annotated[str, Field(max_length=512)]
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None
    reason: Annotated[str, Field(max_length=MAX_REASON_LEN)] = ""
    request_id: Annotated[str | None, Field(max_length=128)] = None
    row_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


# ---------------------------------------------------------------------------------------------
# Traces (spec 9.7, 12 ``/traces``, 13 "Trace"; ADR 0060)
# ---------------------------------------------------------------------------------------------


class EntityRef(ContractModel):
    """An entity named in a trace or graph view."""

    id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(max_length=MAX_NAME_LEN)] = ""


class IdentifierView(ContractModel):
    """One identifier an event carries: the field, the form, the shape and the token the Reveal
    button sends (spec 12). Never a value."""

    field_id: Annotated[str, Field(min_length=1, max_length=512)]
    path: Annotated[str, Field(min_length=1, max_length=256)]
    form: Annotated[str, Field(min_length=1, max_length=32)]
    shape: Annotated[str | None, Field(max_length=64)] = None
    token: Annotated[str, Field(min_length=1, max_length=MAX_TOKEN_LEN)]


class TraceEventView(ContractModel):
    """One hop of the timeline (spec 13 "Trace"): the node, when, how long after the previous
    node against the hop's normal, and the event's identifiers behind Details."""

    event_id: Annotated[str, Field(min_length=1, max_length=26)]
    node_id: Annotated[str, Field(min_length=1, max_length=MAX_NODE_ID_LEN)]
    node_name: Annotated[str, Field(max_length=MAX_NAME_LEN)]
    system_id: SystemId
    template_id: TemplateId
    observed_at: UtcTimestamp
    position: Annotated[int, Field(ge=0)]
    """The event's place in spec 9.7 order (``observed_at``, ``ingested_at``, ``event_id``)."""
    severity: Annotated[str | None, Field(max_length=16)] = None
    latency_seconds: Annotated[float | None, Field(ge=0.0)] = None
    """Seconds after the previous event of the transaction; null for the first."""
    hop_p50_seconds: Annotated[float | None, Field(ge=0.0)] = None
    hop_p95_seconds: Annotated[float | None, Field(ge=0.0)] = None
    """The hop's normal (spec 13: the shaded band), when the graph knows the hop."""
    identifiers: Annotated[list[IdentifierView], Field(max_length=64)] = Field(default_factory=list)
    expired: bool = False
    """Spec 13: an event past the event retention shows its membership only."""


class TraceSummary(ContractModel):
    """A transaction as a search result or a list row (spec 12: ids, entity, status, node path,
    times; never tokens)."""

    id: Annotated[str, Field(min_length=1, max_length=26)]
    entity: EntityRef | None = None
    status: TransactionStatus
    first_seen: UtcTimestamp
    last_seen: UtcTimestamp
    current_node: Annotated[str | None, Field(max_length=MAX_NODE_ID_LEN)] = None
    current_node_name: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    node_path: Annotated[list[str], Field(max_length=MAX_EVENTS_PER_TRACE)] = Field(
        default_factory=list
    )
    """Node ids in order, one per event."""
    event_count: Annotated[int, Field(ge=0)] = 0
    merged_into: Annotated[str | None, Field(max_length=26)] = None
    """Set on a merged-away id (the API answers 303 to the survivor; spec 12)."""


class TraceView(TraceSummary):
    """``GET /traces/{txn_id}``: the summary plus the timeline."""

    events: Annotated[list[TraceEventView], Field(max_length=MAX_EVENTS_PER_TRACE)] = Field(
        default_factory=list
    )


class SearchResponse(ContractModel):
    """``GET /traces/search``: summaries and the forms that matched (``raw``, ``digits.0``), so
    the console can say why a hit matched. Never tokens (spec 12)."""

    traces: Annotated[list[TraceSummary], Field(max_length=MAX_PAGE_SIZE)]
    matched_forms: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    truncated: bool = False
    """More transactions matched than the page holds."""


# ---------------------------------------------------------------------------------------------
# Flow graph (spec 9.8, 11.1 preview, 12 ``/graph`` and ``/nodes``, 13 "Map"; ADR 0061, 0063,
# 0066)
# ---------------------------------------------------------------------------------------------


class NodeView(ContractModel):
    """One template node. ``name`` is the display name or, when none is set, the template id
    and kind; never ``template_text``."""

    id: Annotated[str, Field(min_length=1, max_length=MAX_NODE_ID_LEN)]
    system_id: SystemId
    template_id: TemplateId
    kind: Annotated[str, Field(max_length=16)] = ""
    name: Annotated[str, Field(max_length=MAX_NAME_LEN)]
    display_name: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    event_type_label: Annotated[str | None, Field(max_length=MAX_NAME_LEN)] = None
    terminal_for: Annotated[list[str], Field(max_length=4096)] = Field(default_factory=list)
    """Entity ids for which the node is terminal (spec 9.8)."""
    first_seen: UtcTimestamp | None = None
    last_seen: UtcTimestamp | None = None
    version: Annotated[int, Field(ge=1)]
    updated_at: UtcTimestamp
    etag: Annotated[str, Field(min_length=1, max_length=64)]


class NodePatch(ContractModel):
    """``PATCH /nodes/{node_id}``: the display name; an empty string clears it."""

    display_name: Annotated[str, Field(max_length=MAX_NAME_LEN)]


class ManualSignalView(ContractModel):
    """One spec 11.1 signal behind the "Likely manual" preview badge (ADR 0063), in plain
    language."""

    kind: Annotated[str, Field(max_length=32)]
    """``link_type`` or ``actor`` in the demo cut; the other four signals arrive with M5."""
    text: Annotated[str, Field(max_length=256)]
    value: Annotated[float | None, Field(ge=0.0, le=1.0)] = None


class HopView(ContractModel):
    """One hop of the map (spec 13): volume, latency, the preview badge and its signals."""

    id: Annotated[str, Field(min_length=1, max_length=64)]
    entity: EntityRef | None = None
    from_node: Annotated[str, Field(min_length=1, max_length=MAX_NODE_ID_LEN)]
    to_node: Annotated[str, Field(min_length=1, max_length=MAX_NODE_ID_LEN)]
    support: Annotated[int, Field(ge=0)]
    p_next: Annotated[float, Field(ge=0.0, le=1.0)]
    latency_p50_seconds: Annotated[float | None, Field(ge=0.0)] = None
    latency_p95_seconds: Annotated[float | None, Field(ge=0.0)] = None
    latency_p99_seconds: Annotated[float | None, Field(ge=0.0)] = None
    error_count: Annotated[int, Field(ge=0)] = 0
    human_actor_share: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    link_types: Annotated[list[LinkType], Field(max_length=3)] = Field(default_factory=list)
    """The accepted link types between the two systems."""
    likely_manual: bool = False
    manual_signals: Annotated[list[ManualSignalView], Field(max_length=6)] = Field(
        default_factory=list
    )
    is_manual_score: Annotated[float | None, Field(ge=0.0, le=1.0)] = None
    """Reserved for M5's weighted score; null in the demo cut."""
    is_manual_confirmed: bool | None = None


class GraphView(ContractModel):
    """``GET /graph``: the nodes and hops of the window, with the entities they belong to."""

    nodes: Annotated[list[NodeView], Field(max_length=10_000)]
    hops: Annotated[list[HopView], Field(max_length=100_000)]
    entities: Annotated[list[EntityRef], Field(max_length=4096)] = Field(default_factory=list)
    window_start: UtcTimestamp | None = None
    window_end: UtcTimestamp | None = None
    computed_at: UtcTimestamp | None = None
