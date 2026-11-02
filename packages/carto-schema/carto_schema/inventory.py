"""Edge inventory: what the edge is connected to, reported to core (plan M2 decision 8).

``POST /internal/inventory`` (mutual TLS, role ``edge-gateway``) carries one
:class:`EdgeInventory`: the systems and sources of the edge's sources file and its field policy
pins, so the console shows Systems, Setup > Sources and Setup > Field policies read-only from the
first login. The edge sends it on start and whenever the sources file changes; core replaces its
copy for the sending edge (``edge_id``) as a whole.

Secrets never travel: a source carries its ``secret_ref`` name only (spec 14.3), and
``config`` is the connector configuration with every secret-bearing key removed by the edge
before sending. Core still treats ``config`` as untrusted text when it renders it (spec 13).
Writing configuration from core to the edge is not part of this contract (the control plane is
decided at M6 planning).
"""

from __future__ import annotations

from typing import Annotated, Final

from pydantic import Field

from carto_schema.bundle import FieldClassName, PolicyName
from carto_schema.event import (
    ContractModel,
    SchemaVersion,
    SourceId,
    SystemId,
    TenantId,
    UtcTimestamp,
)

__all__ = [
    "MAX_INVENTORY_PINS",
    "MAX_INVENTORY_SOURCES",
    "MAX_INVENTORY_SYSTEMS",
    "EdgeInventory",
    "InventoryFieldPolicy",
    "InventorySource",
    "InventorySystem",
]

MAX_INVENTORY_SYSTEMS: Final = 1024
MAX_INVENTORY_SOURCES: Final = 4096
MAX_INVENTORY_PINS: Final = 4096
MAX_CONFIG_KEYS: Final = 64
MAX_CONFIG_VALUE_LEN: Final = 1024

ConfigValue = (
    Annotated[str, Field(max_length=MAX_CONFIG_VALUE_LEN)]
    | int
    | float
    | bool
    | None
    | list[Annotated[str, Field(max_length=MAX_CONFIG_VALUE_LEN)]]
)
"""A connector configuration value as shown in the console: scalars and lists of strings only,
no nesting, so a secret cannot hide inside a structure core would not look at."""


class InventorySystem(ContractModel):
    id: SystemId
    name: Annotated[str, Field(min_length=1, max_length=128)]
    owner_group: Annotated[str, Field(max_length=128)] = ""
    criticality: Annotated[str, Field(pattern=r"^(low|medium|high)$")] = "medium"


class InventorySource(ContractModel):
    id: SourceId
    system_id: SystemId
    type: Annotated[str, Field(min_length=1, max_length=32)]
    enabled: bool = True
    config: Annotated[
        dict[Annotated[str, Field(max_length=64)], ConfigValue], Field(max_length=MAX_CONFIG_KEYS)
    ] = Field(default_factory=dict)
    secret_ref: Annotated[str | None, Field(max_length=1024)] = None
    backfill_days: Annotated[int | None, Field(ge=0, le=400)] = None
    schedule: Annotated[str | None, Field(max_length=128)] = None
    """How the edge polls it, as text ("every 60 s", "push"); informational."""
    last_test: Annotated[str | None, Field(max_length=1024)] = None
    """The last read-only verification result (connector ``test()``), redacted by the edge."""
    last_test_at: UtcTimestamp | None = None


class InventoryFieldPolicy(ContractModel):
    """One admin pin of the sources file (spec 8.3; ``FieldPolicyPin`` on the edge)."""

    field: Annotated[str, Field(min_length=3, max_length=512)]
    field_class: FieldClassName
    policy: PolicyName
    forms: Annotated[list[Annotated[str, Field(max_length=32)]], Field(max_length=16)] = Field(
        default_factory=list
    )
    reason: Annotated[str, Field(max_length=256)] = ""


class EdgeInventory(ContractModel):
    """One ``POST /internal/inventory`` body."""

    schema_version: SchemaVersion
    tenant_id: TenantId
    edge_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
    """Which edge reports (its certificate subject or configured name); core keeps one inventory
    per edge and merges them for the console."""
    sent_at: UtcTimestamp
    sources_file_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    systems: Annotated[list[InventorySystem], Field(max_length=MAX_INVENTORY_SYSTEMS)]
    sources: Annotated[list[InventorySource], Field(max_length=MAX_INVENTORY_SOURCES)]
    field_policies: Annotated[list[InventoryFieldPolicy], Field(max_length=MAX_INVENTORY_PINS)] = (
        Field(default_factory=list)
    )
