"""Offline bundle contract: what ``carto-edge analyze`` writes and core loads (spec 4.1, 8.1.1).

A bundle is a directory ``<name>.carto/`` with:

- ``manifest.json``: :class:`BundleManifest`, run metadata, per-source counts and the sha256 of
  every other data file.
- ``MANIFEST.md``: the same rendered for the customer's review, with the field and template
  tables (what was kept, tokenized, dropped, with sample kept values).
- ``events.ndjson.zst``: one :class:`~carto_schema.event.CanonicalEvent` per line (JSON mode),
  zstd compressed.
- ``fields.json``: ``list[BundleField]``; ``templates.json``: ``list[BundleTemplate]``.
- ``signature.json``: :class:`BundleSignature` over the exact bytes of ``manifest.json``.

The tokenization key and the reveal vault are never part of a bundle (spec 8.1.1). Because the
manifest carries every file's digest and the signature covers the manifest, verifying the
signature and the digests proves the bundle is intact. The signing key is generated for the run
and its public key travels with the bundle, so the signature proves integrity, not origin
(ADR 0022); origin is the customer's own secure hand-over.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field

from carto_schema.event import (
    ContractModel,
    EventKind,
    SchemaVersion,
    SourceId,
    SystemId,
    TemplateId,
    TenantId,
    Ulid,
    UtcTimestamp,
)

BUNDLE_VERSION: Final = "1"
BundleVersion = Literal["1"]

MANIFEST_FILE: Final = "manifest.json"
MANIFEST_MD_FILE: Final = "MANIFEST.md"
EVENTS_FILE: Final = "events.ndjson.zst"
FIELDS_FILE: Final = "fields.json"
TEMPLATES_FILE: Final = "templates.json"
SIGNATURE_FILE: Final = "signature.json"
LOCATOR_MAP_FILE: Final = "locator_map.ndjson"
"""Eval-mode only (ADR 0006): written next to the bundle, never inside it."""

DATA_FILES: Final[tuple[str, ...]] = (EVENTS_FILE, FIELDS_FILE, TEMPLATES_FILE, MANIFEST_MD_FILE)
"""Files whose digests the manifest records (everything but itself and the signature)."""

MAX_SAMPLE_VALUES: Final = 5
MAX_TOP_SHAPES: Final = 8

FieldClassName = Literal[
    "identifier",
    "low_card_attribute",
    "timestamp",
    "amount",
    "date",
    "person_name",
    "contact",
    "government_id",
    "financial",
    "health",
    "free_text",
    "secret_like",
    "unknown",
]
"""Spec 8.3 field classes (the PII group of rule 2 is split by kind)."""

PolicyName = Literal["keep", "tokenize", "drop"]
"""Spec 5.4 step 5: keep in clear, tokenize as identifier forms, or drop."""


class FileDigest(ContractModel):
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    bytes: Annotated[int, Field(ge=0, strict=True)]


class ShapeShare(ContractModel):
    shape: Annotated[str, Field(min_length=1, max_length=64)]
    share: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class BundleField(ContractModel):
    """One field as the edge classified it (spec 8.3), with what the customer may review.

    ``sample_values`` is non-empty only for ``policy == "keep"``: those values travel in clear in
    the events anyway, and the customer reviews them before sending the bundle (spec 4.1 step
    2). Fields that are tokenized or dropped show shapes only.
    """

    field_ref: Annotated[str, Field(min_length=1, max_length=512)]
    system_id: SystemId
    template_id: TemplateId
    path: Annotated[str, Field(min_length=1, max_length=256)]
    field_class: FieldClassName
    policy: PolicyName
    pinned: bool = False
    reason: Annotated[str, Field(max_length=256)] = ""
    count: Annotated[int, Field(ge=0, strict=True)]
    distinct_estimate: Annotated[int, Field(ge=0, strict=True)]
    null_rate: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
    top_shapes: Annotated[list[ShapeShare], Field(max_length=MAX_TOP_SHAPES)]
    forms: Annotated[list[str], Field(max_length=16)] = Field(default_factory=list)
    sample_values: Annotated[list[str], Field(max_length=MAX_SAMPLE_VALUES)] = Field(
        default_factory=list
    )


class BundleTemplate(ContractModel):
    template_id: TemplateId
    system_id: SystemId
    template_text: Annotated[str, Field(max_length=4096)]
    kind: EventKind
    count: Annotated[int, Field(ge=0, strict=True)]
    first_seen: UtcTimestamp
    last_seen: UtcTimestamp


class BundleSourceSummary(ContractModel):
    source_id: SourceId
    system_id: SystemId
    connector_type: Annotated[str, Field(min_length=1, max_length=32)]
    records_read: Annotated[int, Field(ge=0, strict=True)]
    events_written: Annotated[int, Field(ge=0, strict=True)]
    records_dropped: Annotated[int, Field(ge=0, strict=True)]
    parse_errors: Annotated[int, Field(ge=0, strict=True)]
    first_observed_at: UtcTimestamp | None = None
    last_observed_at: UtcTimestamp | None = None


class BundleCounts(ContractModel):
    records_read: Annotated[int, Field(ge=0, strict=True)]
    events: Annotated[int, Field(ge=0, strict=True)]
    records_dropped: Annotated[int, Field(ge=0, strict=True)]
    parse_errors: Annotated[int, Field(ge=0, strict=True)]
    identifiers: Annotated[int, Field(ge=0, strict=True)]
    fields_kept: Annotated[int, Field(ge=0, strict=True)]
    fields_tokenized: Annotated[int, Field(ge=0, strict=True)]
    fields_dropped: Annotated[int, Field(ge=0, strict=True)]
    events_too_old: Annotated[
        int,
        Field(
            ge=0,
            strict=True,
            description="Records dropped because their time lay before the plausible range "
            "(ADR 0036); also counted in records_dropped.",
        ),
    ] = 0
    events_future: Annotated[
        int,
        Field(
            ge=0,
            strict=True,
            description="Events whose time lay after the plausible range and became the ingest "
            "time (ADR 0036); also counted in events.",
        ),
    ] = 0


class BundleManifest(ContractModel):
    """``manifest.json``. Field order is the file order."""

    bundle_version: BundleVersion
    schema_version: SchemaVersion
    bundle_id: Ulid
    tenant_id: TenantId
    created_at: UtcTimestamp
    producer: Annotated[str, Field(min_length=1, max_length=128)]
    key_versions: Annotated[
        list[Annotated[int, Field(ge=1, le=9999, strict=True)]], Field(min_length=1, max_length=8)
    ]
    policy_version: Annotated[str, Field(min_length=1, max_length=32)]
    sources: Annotated[list[BundleSourceSummary], Field(max_length=1024)]
    counts: BundleCounts
    first_observed_at: UtcTimestamp | None = None
    last_observed_at: UtcTimestamp | None = None
    files: dict[str, FileDigest]
    notes: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)


class BundleSignature(ContractModel):
    """``signature.json``: Ed25519 over the exact bytes of ``manifest.json``."""

    algorithm: Literal["ed25519"]
    key_id: Annotated[str, Field(pattern=r"^[0-9a-f]{16}$")]
    public_key: Annotated[str, Field(min_length=43, max_length=43)]
    signature: Annotated[str, Field(min_length=86, max_length=86)]
    signed_at: UtcTimestamp
    manifest_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
