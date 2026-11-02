"""carto-core llm_gateway (spec 5.3, 9.9; ADR 0054; plan M2 package G).

The optional LLM assist: fixed payload schemas built from metadata only, fenced prompts, a
strict output schema, an egress allowlist, retries and an audit row per call. Off by default
(spec 2.3 invariant 5). :mod:`.schemas` is the seam other packages import; :class:`LlmGateway`
is the one entry point to a provider.
"""

from __future__ import annotations

from carto_core.llm_gateway.audit import (
    AuditSinkError,
    CallAuditSink,
    CallRecord,
    CallStatus,
    InMemoryCallAuditSink,
    PostgresCallAuditSink,
)
from carto_core.llm_gateway.egress import build_client
from carto_core.llm_gateway.gateway import (
    GatewayDisabledError,
    GatewayMetrics,
    InvalidOutputError,
    LlmGateway,
    PayloadRefusedError,
)
from carto_core.llm_gateway.payload import (
    InMemoryMetadataReader,
    MetadataReader,
    PayloadError,
    build_label_field_payload,
    build_name_entity_payload,
    forbidden_content_check,
)
from carto_core.llm_gateway.prompts import PROMPT_VERSION
from carto_core.llm_gateway.registry import provider_for
from carto_core.llm_gateway.schemas import (
    LABEL_DICTIONARY,
    AttributeSummary,
    FieldSummary,
    LabelFieldRequest,
    LabelType,
    NameEntityRequest,
    NameNodeRequest,
    Provider,
    ProviderError,
    Suggestion,
    SuggestionList,
    TaskKind,
)

__all__ = [
    "LABEL_DICTIONARY",
    "PROMPT_VERSION",
    "AttributeSummary",
    "AuditSinkError",
    "CallAuditSink",
    "CallRecord",
    "CallStatus",
    "FieldSummary",
    "GatewayDisabledError",
    "GatewayMetrics",
    "InMemoryCallAuditSink",
    "InMemoryMetadataReader",
    "InvalidOutputError",
    "LabelFieldRequest",
    "LabelType",
    "LlmGateway",
    "MetadataReader",
    "NameEntityRequest",
    "NameNodeRequest",
    "PayloadError",
    "PayloadRefusedError",
    "PostgresCallAuditSink",
    "Provider",
    "ProviderError",
    "Suggestion",
    "SuggestionList",
    "TaskKind",
    "build_client",
    "build_label_field_payload",
    "build_name_entity_payload",
    "forbidden_content_check",
    "provider_for",
]
