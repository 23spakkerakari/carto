"""Ground truth contract shared by the simulator (writer) and the eval harness (reader).

ADR 0006: every emitted record is identified by a locator key ``<source_id>:<locator>``. Native
files carry nothing beyond what the real system would log; everything the eval needs to score the
engine lives in ``ground_truth/``:

| File | Model | One record per |
| --- | --- | --- |
| ``manifest.json`` | :class:`Manifest` | generation run |
| ``sources.json`` | ``list[SourceDef]`` | configured source (spec 3: Source) |
| ``event_txn.ndjson`` | :class:`EventTruth` | emitted record (noise included, ``txn_id`` null) |
| ``links.json`` | ``list[LinkTruth]`` | true key link (spec 3: Key link, Bridge, Composite link) |
| ``entities.json`` | ``list[EntityTruth]`` | entity family (spec 3: Entity) |
| ``batches.json`` | ``list[BatchTruth]`` | batch (spec 3: Batch) |
| ``faults.json`` | ``list[FaultTruth]`` | injected fault (spec 19) |
| ``manual_hops.json`` | ``list[ManualHopTruth]`` | hop with its manual share (spec 11) |
| ``markers.json`` | :class:`MarkerSet` | generation run (spec 18.3 leak test input) |

Times are UTC and tz-aware everywhere in this module. The simulator applies per-source clock skew
only when rendering native files; ground truth keeps true time.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

GROUND_TRUTH_VERSION = "1"
GROUND_TRUTH_DIR = "ground_truth"

EVENTS_FILE = "event_txn.ndjson"
MANIFEST_FILE = "manifest.json"
SOURCES_FILE = "sources.json"
LINKS_FILE = "links.json"
ENTITIES_FILE = "entities.json"
BATCHES_FILE = "batches.json"
FAULTS_FILE = "faults.json"
MANUAL_HOPS_FILE = "manual_hops.json"
MARKERS_FILE = "markers.json"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "ground truth timestamps must be timezone-aware UTC"
        raise ValueError(msg)
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------------------------
# Locator keys (ADR 0006)
# ---------------------------------------------------------------------------------------------


def line_key(source_id: str, file_name: str, line_no: int) -> str:
    """Key for line ``line_no`` (1-based) of ``file_name`` within a log-file source."""
    if line_no < 1:
        msg = "line numbers are 1-based"
        raise ValueError(msg)
    return f"{source_id}:{file_name}:line:{line_no}"


def row_key(source_id: str, table: str, primary_key: int | str) -> str:
    """Key for a database row by primary key."""
    return f"{source_id}:{table}:row:{primary_key}"


def file_key(source_id: str, file_name: str) -> str:
    """Key for a file arrival in a file-drop source."""
    return f"{source_id}:file:{file_name}"


# ---------------------------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------------------------


class SourceKind(StrEnum):
    """How the edge will read the source (spec 8.1). Determines the locator shape."""

    LOG_FILE = "log_file"  # one record per line (collector filelog or upload)
    SQL_TABLE = "sql_table"  # one record per row (read-only polling)
    FILE_DROP = "file_drop"  # one record per file arrival (SFTP or directory watch)


class SourceFormat(StrEnum):
    """Wire format of the records (spec 8.2 parser order)."""

    NDJSON = "ndjson"
    LOGFMT = "logfmt"
    XML_LINES = "xml_lines"  # one complete XML document per line
    TEXT = "text"  # unstructured lines for template mining
    CSV = "csv"
    SQL_ROWS = "sql_rows"  # SQL seed file plus CSV export of the same rows
    FILES = "files"  # file arrivals; the files themselves may be CSV


class SourceDef(_Model):
    """A configured connector instance as the customer would set it up (spec 3: Source)."""

    source_id: str = Field(pattern=r"^src_[a-z0-9_]{1,60}$")
    system_id: str = Field(pattern=r"^sys_[a-z0-9_]{1,60}$")
    system_name: str
    kind: SourceKind
    format: SourceFormat
    path: str = Field(description="Relative to the scenario output directory.")
    timezone: str = Field(description="IANA zone of timestamps inside the data, e.g. UTC.")
    timestamp_format: str = Field(description="Hint for the parser config, e.g. iso8601.")
    clock_skew_seconds: int = Field(
        default=0, description="How far this source's clock runs ahead of true time."
    )
    notes: str = ""


# ---------------------------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------------------------


class ActorKind(StrEnum):
    HUMAN = "human"
    SERVICE = "service"
    UNKNOWN = "unknown"


class EventTruth(_Model):
    """One emitted record. ``txn_id`` is null for noise and for records of no transaction."""

    key: str
    source_id: str
    system_id: str
    node: str = Field(description="True node label, '<system>:<event_type>'.")
    observed_at: datetime = Field(description="True time, UTC, before clock skew.")
    txn_id: str | None
    batch_id: str | None = None
    actor_kind: ActorKind | None = None
    is_error: bool = False

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


# ---------------------------------------------------------------------------------------------
# Links, entities, batches
# ---------------------------------------------------------------------------------------------


class FieldRef(_Model):
    """A field as the engine will see it: system, source, and the name in the data."""

    system_id: str
    source_id: str
    field: str = Field(description="Column, key, or parameter name as it appears in the data.")

    def __str__(self) -> str:
        return f"{self.system_id}/{self.source_id}/{self.field}"


LinkType = Literal["exact", "bridge", "composite", "batch", "association"]
LinkRole = Literal["transaction", "association", "batch"]
Direction = Literal["a_to_b", "b_to_a", "undirected"]


class CompositeComponent(_Model):
    """One weak field pair inside a composite link (spec 9.5): amount, date, phonetic name."""

    a: FieldRef
    form_a: str
    b: FieldRef
    form_b: str
    agreement_rate: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Share of true pairs where the component agrees."
    )


class LinkTruth(_Model):
    """A true key link between two fields (spec 3, 9.3 to 9.5).

    For ``link_type == "composite"`` the primary ``a``/``b`` pair is the strongest component and
    ``components`` lists every component, the primary one included.
    """

    link_id: str
    a: FieldRef
    form_a: str
    b: FieldRef
    form_b: str
    link_type: LinkType
    role: LinkRole
    direction: Direction
    entity_id: str
    manual: bool = Field(default=False, description="True when a person moves the data.")
    components: list[CompositeComponent] = Field(default_factory=list)
    notes: str = ""


class EntityField(_Model):
    ref: FieldRef
    form: str


class EntityTruth(_Model):
    entity_id: str
    name: str
    fields: list[EntityField]


class BatchTruth(_Model):
    """A grouping identifier spanning many transactions (spec 3: Batch, 9.7)."""

    batch_id: str
    key_field: FieldRef
    key_value: str = Field(description="The batch key as it appears in the data, e.g. a file name.")
    txn_ids: list[str]


# ---------------------------------------------------------------------------------------------
# Faults and manual hops
# ---------------------------------------------------------------------------------------------


class FaultKind(StrEnum):
    MISSING_FILE = "missing_file"
    ERROR_SPIKE = "error_spike"
    SCHEMA_DRIFT = "schema_drift"
    VISIBILITY_GAP = "visibility_gap"
    PARTIAL_STALL = "partial_stall"
    CLOCK_SKEW = "clock_skew"


class FaultTruth(_Model):
    """An injected fault and what the detector is expected to do about it (spec 18.4, 19)."""

    fault_id: str
    kind: FaultKind
    start: datetime
    end: datetime | None
    system_id: str
    source_id: str | None = None
    affected_txn_ids: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(
        default_factory=dict, description="e.g. {'warehouse_code': 'DC-03'} for a partial stall."
    )
    cause_evidence: str = Field(
        default="", description="The error text a cause ranker should find."
    )
    expected_alert: bool = Field(
        default=True, description="False for faults that must NOT raise a business alert."
    )
    expected_alert_kind: str | None = Field(
        default=None,
        description="Expectation kind the alert should carry: hop_deadline, schedule, volume, "
        "error_rate, freshness, schema_drift.",
    )
    expected_cause_kind: str | None = Field(
        default=None, description="Top-1 likely cause kind per spec 10.4, when applicable."
    )
    notes: str = ""

    @field_validator("start", "end")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class ManualHopTruth(_Model):
    """A hop and whether a person performs it (spec 11.1)."""

    hop_id: str
    from_node: str
    to_node: str
    entity_id: str
    manual: bool
    share_manual: float = Field(ge=0.0, le=1.0)
    actors: list[str] = Field(default_factory=list, description="Human actor names when manual.")
    typo_rate: float = Field(default=0.0, ge=0.0, le=1.0)


# ---------------------------------------------------------------------------------------------
# Markers (spec 18.3 leak test input) and manifest
# ---------------------------------------------------------------------------------------------


class MarkerSet(_Model):
    """Values that must never appear beyond the edge in clear."""

    marker_tokens: list[str] = Field(
        description="Unique substrings embedded in synthetic PII (e.g. 'mk3f9a2c1e')."
    )
    pii_values: list[str] = Field(description="Exact synthetic PII strings as emitted.")
    identifier_values: dict[str, list[str]] = Field(
        description="Field ref string -> every identifier value emitted for it."
    )
    actor_values: list[str] = Field(
        default_factory=list,
        description="Human actor logins as emitted (clerks); the edge tokenizes actors (7.1).",
    )
    amount_values: list[str] = Field(
        default_factory=list,
        description="Order amounts as emitted (two decimals, and JSON's float rendering); "
        "spec 8.3 rule 4: never in clear.",
    )
    secret_values: list[str] = Field(
        default_factory=list,
        description="Secret-shaped values planted into noise lines (an AWS access key id, a "
        "bearer token, a password assigned in prose, a PEM header and key body, a DSN with "
        "userinfo and its password); spec 8.3 rule 1: never in clear (ADR 0068).",
    )


class Manifest(_Model):
    ground_truth_version: str = GROUND_TRUTH_VERSION
    generator_version: str
    scenario: str
    seed: int
    days: int
    start_date: date
    daily_volume: int
    faults_enabled: bool
    noise_rate: float
    pii_density: float
    counts: dict[str, int] = Field(description="Record counts per source and totals.")
    sha256: dict[str, str] = Field(
        default_factory=dict,
        description="Digest of every native file, relative path -> hex, for determinism checks.",
    )


# ---------------------------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------------------------


class GroundTruth(_Model):
    """Everything in ``ground_truth/`` except the event stream, which is streamed separately."""

    manifest: Manifest
    sources: list[SourceDef]
    links: list[LinkTruth]
    entities: list[EntityTruth]
    batches: list[BatchTruth]
    faults: list[FaultTruth]
    manual_hops: list[ManualHopTruth]
    markers: MarkerSet


def _dump_json(path: Path, payload: object) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def _model_list(items: Iterable[BaseModel]) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in items]


def write_ground_truth(out_dir: Path, truth: GroundTruth, events: Iterable[EventTruth]) -> Path:
    """Write ``ground_truth/`` under ``out_dir``. Events are streamed to NDJSON in given order."""
    gt_dir = out_dir / GROUND_TRUTH_DIR
    gt_dir.mkdir(parents=True, exist_ok=True)
    _dump_json(gt_dir / MANIFEST_FILE, truth.manifest.model_dump(mode="json"))
    _dump_json(gt_dir / SOURCES_FILE, _model_list(truth.sources))
    _dump_json(gt_dir / LINKS_FILE, _model_list(truth.links))
    _dump_json(gt_dir / ENTITIES_FILE, _model_list(truth.entities))
    _dump_json(gt_dir / BATCHES_FILE, _model_list(truth.batches))
    _dump_json(gt_dir / FAULTS_FILE, _model_list(truth.faults))
    _dump_json(gt_dir / MANUAL_HOPS_FILE, _model_list(truth.manual_hops))
    _dump_json(gt_dir / MARKERS_FILE, truth.markers.model_dump(mode="json"))
    with (gt_dir / EVENTS_FILE).open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(event.model_dump_json())
            handle.write("\n")
    return gt_dir


def read_ground_truth(out_dir: Path) -> GroundTruth:
    """Read everything except the event stream; see :func:`iter_events`."""
    gt_dir = out_dir / GROUND_TRUTH_DIR

    def load(name: str) -> object:
        return json.loads((gt_dir / name).read_text(encoding="utf-8"))

    return GroundTruth(
        manifest=Manifest.model_validate(load(MANIFEST_FILE)),
        sources=[SourceDef.model_validate(x) for x in _as_list(load(SOURCES_FILE))],
        links=[LinkTruth.model_validate(x) for x in _as_list(load(LINKS_FILE))],
        entities=[EntityTruth.model_validate(x) for x in _as_list(load(ENTITIES_FILE))],
        batches=[BatchTruth.model_validate(x) for x in _as_list(load(BATCHES_FILE))],
        faults=[FaultTruth.model_validate(x) for x in _as_list(load(FAULTS_FILE))],
        manual_hops=[ManualHopTruth.model_validate(x) for x in _as_list(load(MANUAL_HOPS_FILE))],
        markers=MarkerSet.model_validate(load(MARKERS_FILE)),
    )


def iter_events(out_dir: Path) -> Iterator[EventTruth]:
    """Stream ``event_txn.ndjson`` without loading it all."""
    path = out_dir / GROUND_TRUTH_DIR / EVENTS_FILE
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield EventTruth.model_validate_json(line)


def _as_list(value: object) -> list[object]:
    if not isinstance(value, list):
        msg = "expected a JSON array"
        raise TypeError(msg)
    return value
