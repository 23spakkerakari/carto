"""Edge configuration: the sources file and the service settings (spec Section 20, Appendix A).

Two inputs:

- **The sources file** (``sources.yaml`` for the gateway, ``analyze.yaml`` for the offline
  analyzer; same schema, :class:`SourcesFile`): systems, sources with their connector
  ``config`` block and ``secret_ref``, parse hints, admin field-policy pins and the network
  allow-list. Loaded with ``yaml.safe_load`` under a size limit (spec 14.7). The offline
  analyzer uses sources of type ``upload`` whose config names local files or globs.
- **Settings** (:class:`EdgeSettings`, environment ``CARTO_...``): the state directory, the
  core link (mTLS), the KMS, the classifier thresholds, buffer and forwarding limits, the
  reveal rate limit and the gateway listener. No secrets and no key material live here (spec
  8.4, 14.3): every credential is a ``secret_ref`` and every key is a KMS-wrapped file under
  the state directory.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carto_common.settings import ProductSettings, SecretRef
from carto_schema.event import SOURCE_ID_PATTERN

__all__ = [
    "DEFAULT_MESSAGE_FIELDS",
    "DEFAULT_TIMESTAMP_FIELDS",
    "MAX_SOURCES_FILE_BYTES",
    "AnchorSyslogSettings",
    "AuditSettings",
    "BufferSettings",
    "ClassifySettings",
    "ConfigError",
    "CoreLinkSettings",
    "EdgeSettings",
    "FieldPolicyPin",
    "GatewaySettings",
    "KmsSettings",
    "NetworkConfig",
    "ParseConfig",
    "PiiSettings",
    "RecordFormat",
    "RevealSettings",
    "SourceConfig",
    "SourceType",
    "SourcesFile",
    "SystemConfig",
    "load_sources_file",
    "parse_sources_text",
]

MAX_SOURCES_FILE_BYTES: Final = 1024 * 1024
MAX_RECORD_BYTES_DEFAULT: Final = 1024 * 1024
GIB: Final = 1024**3
MIB: Final = 1024**2

DEFAULT_TIMESTAMP_FIELDS: Final[tuple[str, ...]] = (
    "ts",
    "timestamp",
    "@timestamp",
    "time",
    "_time",
    "datetime",
    "date",
    "eventTime",
    "event_time",
    "created_at",
    "updated_at",
)
"""Field paths the parser tries, in order, when no ``timestamp_field`` is configured."""

DEFAULT_MESSAGE_FIELDS: Final[tuple[str, ...]] = ("msg", "message", "log", "body", "text")
"""Field paths that hold a free-text message to mine for a template when none is configured."""


_CREDENTIAL_WORDS: Final = ("password", "passwd", "token", "secret", "api_key")


class ConfigError(ValueError):
    """The sources file is invalid; the message names the place, never a secret."""


class SourceType(StrEnum):
    """Connector types (spec 2.1, 8.1)."""

    UPLOAD = "upload"
    OTLP = "otlp"
    SPLUNK = "splunk"
    SQL = "sql"
    SFTP = "sftp"
    WEBHOOK = "webhook"


class RecordFormat(StrEnum):
    """Parser selection (spec 8.2); ``auto`` tries the spec order, first match wins."""

    AUTO = "auto"
    NDJSON = "ndjson"
    JSON = "json"
    XML = "xml"
    LOGFMT = "logfmt"
    CSV = "csv"
    ACCESS_LOG = "access_log"
    TEXT = "text"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ParseConfig(_Frozen):
    """Per-source parse hints (spec 8.2 "with per-source hints")."""

    format: RecordFormat = RecordFormat.AUTO
    timestamp_field: str | None = Field(default=None, max_length=256)
    timestamp_format: str | None = Field(
        default=None,
        max_length=128,
        description="iso8601, epoch_s, epoch_ms, or a strftime pattern; None auto-detects.",
    )
    timezone: str = Field(
        default="UTC", max_length=64, description="IANA zone for timestamps without an offset."
    )
    message_field: str | None = Field(default=None, max_length=256)
    severity_field: str | None = Field(default=None, max_length=256)
    actor_field: str | None = Field(default=None, max_length=256)
    csv_columns: list[str] | None = Field(default=None, max_length=1024)
    csv_has_header: bool = True
    csv_delimiter: str = Field(default=",", min_length=1, max_length=1)
    access_log_pattern: str | None = Field(
        default=None, max_length=2048, description="RE2 pattern with named groups (spec 8.2)."
    )
    max_record_bytes: int = Field(default=MAX_RECORD_BYTES_DEFAULT, ge=256, le=16 * MIB)


class SystemConfig(_Frozen):
    id: str = Field(pattern=SOURCE_ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=128)
    owner_group: str = Field(default="", max_length=128)
    criticality: Literal["low", "medium", "high"] = "medium"


class SourceConfig(_Frozen):
    """One configured connector instance (spec 3 "Source", Appendix A)."""

    id: str = Field(pattern=SOURCE_ID_PATTERN.pattern)
    system: str = Field(pattern=SOURCE_ID_PATTERN.pattern)
    type: SourceType
    enabled: bool = True
    config: dict[str, Any] = Field(
        default_factory=dict, description="Connector-specific; validated by the connector."
    )
    secret_ref: SecretRef | None = None
    parse: ParseConfig = Field(default_factory=ParseConfig)
    backfill_days: int | None = Field(default=None, ge=0, le=400)

    @field_validator("config")
    @classmethod
    def _no_inline_secrets(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Spec 8.1: credentials are referenced, never stored in config."""
        for key in config:
            lowered = str(key).lower()
            if any(word in lowered for word in _CREDENTIAL_WORDS):
                msg = f"config key {key!r} looks like a credential; use secret_ref instead"
                raise ValueError(msg)
        return config


class FieldPolicyPin(_Frozen):
    """An admin decision for a field (spec 8.3 "Admins can pin a field's class and policy").

    ``field`` is a ``field_ref`` (``system/template/path``) or a pattern with ``*`` for the
    template (``sys_orders/*/order_id``). ``forms`` restricts the forms when tokenizing and is
    how phonetic forms are enabled for a name field (spec 8.3 rule 2, 8.4 ``phonetic.k``).
    """

    field: str = Field(min_length=3, max_length=512)
    field_class: Literal[
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
    ]
    policy: Literal["keep", "tokenize", "drop"]
    forms: list[str] | None = Field(default=None, max_length=16)
    reason: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def _secret_never_kept(self) -> Self:
        if self.field_class == "secret_like" and self.policy != "drop":
            msg = "secret_like fields are always dropped (spec 8.3 rule 1)"
            raise ValueError(msg)
        if self.policy == "keep" and self.field_class in {
            "person_name",
            "contact",
            "government_id",
            "financial",
            "health",
        }:
            msg = "PII classes cannot be kept in clear (spec 2.3 invariant 2)"
            raise ValueError(msg)
        return self


class NetworkConfig(_Frozen):
    allowed_source_cidrs: list[str] = Field(
        default_factory=list,
        max_length=256,
        description="Private ranges a connector may reach (spec 14.7); public ranges always may.",
    )


class SourcesFile(_Frozen):
    """The whole sources file (Appendix A; expectations, calendars and channels are core's)."""

    systems: list[SystemConfig] = Field(max_length=1024)
    sources: list[SourceConfig] = Field(max_length=4096)
    field_policies: list[FieldPolicyPin] = Field(default_factory=list, max_length=4096)
    network: NetworkConfig = Field(default_factory=NetworkConfig)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        system_ids = [system.id for system in self.systems]
        if len(set(system_ids)) != len(system_ids):
            msg = "system ids must be unique"
            raise ValueError(msg)
        source_ids = [source.id for source in self.sources]
        if len(set(source_ids)) != len(source_ids):
            msg = "source ids must be unique"
            raise ValueError(msg)
        known = set(system_ids)
        for source in self.sources:
            if source.system not in known:
                msg = f"source {source.id!r} names unknown system {source.system!r}"
                raise ValueError(msg)
        return self

    def system(self, system_id: str) -> SystemConfig:
        for system in self.systems:
            if system.id == system_id:
                return system
        msg = f"unknown system {system_id!r}"
        raise KeyError(msg)

    @property
    def policy_version(self) -> str:
        """Spec 8.3: a version that changes whenever the pins change (``policy_version`` in
        events). The first 12 hex characters of the sha256 of the canonical pins."""
        import hashlib  # noqa: PLC0415
        import json  # noqa: PLC0415

        pins = [pin.model_dump(mode="json") for pin in self.field_policies]
        digest = hashlib.sha256(json.dumps(pins, sort_keys=True).encode("utf-8")).hexdigest()
        return digest[:12]


def parse_sources_text(text: str) -> SourcesFile:
    """Validate the YAML text of a sources file. Never ``yaml.load``; always ``safe_load``."""
    if len(text.encode("utf-8")) > MAX_SOURCES_FILE_BYTES:
        msg = f"sources file exceeds {MAX_SOURCES_FILE_BYTES} bytes"
        raise ConfigError(msg)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        msg = f"sources file is not valid YAML{where}"
        raise ConfigError(msg) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        msg = "sources file must be a mapping at the top level"
        raise ConfigError(msg)
    try:
        return SourcesFile.model_validate(data)
    except ValueError as exc:
        msg = f"sources file is invalid: {_summarize(exc)}"
        raise ConfigError(msg) from exc


def _summarize(exc: ValueError) -> str:
    """Locations and messages of a validation error without the inputs (spec 14.3)."""
    errors = getattr(exc, "errors", None)
    if errors is None:
        return str(exc)
    parts: list[str] = []
    for error in errors(include_input=False, include_url=False):
        location = ".".join(str(piece) for piece in error.get("loc", ()))
        parts.append(f"{location}: {error.get('msg', 'invalid')}")
    return "; ".join(parts[:10])


def load_sources_file(path: Path) -> SourcesFile:
    try:
        if path.stat().st_size > MAX_SOURCES_FILE_BYTES:
            msg = f"sources file {path} exceeds {MAX_SOURCES_FILE_BYTES} bytes"
            raise ConfigError(msg)
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"cannot read sources file {path}"
        raise ConfigError(msg) from exc
    return parse_sources_text(text)


# ---------------------------------------------------------------------------------------------
# Service settings
# ---------------------------------------------------------------------------------------------


class CoreLinkSettings(_Frozen):
    """Where and how the edge forwards to ``ingest-api`` (spec 8.5, 14.4 mTLS)."""

    url: str | None = Field(default=None, description="https://ingest.internal:8443")
    ca_file: Path | None = None
    cert_file: Path | None = None
    key_file: Path | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    batch_events: int = Field(default=5000, ge=1, le=5000)
    batch_bytes: int = Field(default=5 * MIB, ge=64 * 1024, le=5 * MIB)
    batch_flush_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=60,
        description="Oldest event age at which a partial batch is sealed and buffered.",
    )
    heartbeat_seconds: float = Field(default=30.0, ge=5, le=3600)
    retry_max_seconds: float = Field(
        default=60.0, ge=1, le=3600, description="Cap of the forwarder's exponential backoff."
    )

    @field_validator("url")
    @classmethod
    def _https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            msg = "core.url must start with https:// (spec 14.4)"
            raise ValueError(msg)
        return value


class KmsSettings(_Frozen):
    """Which KMS wraps key material (spec 8.4; ADR 0012)."""

    provider: Literal["local", "vault"] = "local"
    local_key_file: Path | None = Field(
        default=None, description="Local KMS master key; default <state_dir>/keys/local-kms.key"
    )
    vault_url: str | None = None
    vault_transit_key: str = Field(default="carto", min_length=1, max_length=128)
    vault_mount: str = Field(default="transit", min_length=1, max_length=128)
    vault_token_file: Path | None = Field(
        default=None, description="File holding the Vault token (never an env var)."
    )

    @model_validator(mode="after")
    def _vault_needs_url(self) -> Self:
        if self.provider == "vault" and (self.vault_url is None or self.vault_token_file is None):
            msg = "kms.vault_url and kms.vault_token_file are required for the vault provider"
            raise ValueError(msg)
        return self


class ClassifySettings(_Frozen):
    """Spec 8.3 thresholds."""

    distinct_threshold: int = Field(default=1000, ge=10)
    distinct_ratio: float = Field(default=0.2, gt=0, le=1)
    quarantine_samples: int = Field(default=200, ge=1)
    identifier_min_len: int = Field(default=3, ge=1)
    identifier_max_len: int = Field(default=128, ge=8)
    sample_values: int = Field(default=64, ge=8, le=1024, description="Reservoir per field.")
    stats_flush_seconds: float = Field(default=30.0, ge=1)
    map_min_keys: int = Field(
        default=256,
        ge=16,
        le=65_536,
        description="Distinct child names that make a parent path a map (ADR 0030).",
    )


class BufferSettings(_Frozen):
    """Spec 8.5 disk buffer."""

    max_bytes: int = Field(default=20 * GIB, ge=64 * MIB)
    backpressure_ratio: float = Field(default=0.8, gt=0, lt=1)


class RevealSettings(_Frozen):
    """Spec 8.4 reveal vault limits."""

    values_per_user_per_hour: int = Field(default=100, ge=1)
    max_tokens_per_request: int = Field(default=20, ge=1, le=100)
    assertion_public_key_file: Path | None = Field(
        default=None, description="Core's Ed25519 public key for internal assertions."
    )


class AnchorSyslogSettings(_Frozen):
    """The customer's syslog or SIEM that receives the hourly audit anchor (ADR 0033): RFC 5424
    over TCP with RFC 5425 framing, TLS on by default."""

    host: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9._:-]+$")
    port: int = Field(default=6514, ge=1, le=65535)
    tls: bool = Field(default=True, description="Plain TCP only when the customer turns it off.")
    ca_file: Path | None = Field(
        default=None, description="CA bundle for the syslog server; default the system store."
    )
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)


class AuditSettings(_Frozen):
    """Edge audit trail (ADR 0014, 0033)."""

    anchor_syslog: AnchorSyslogSettings | None = None


class PiiSettings(_Frozen):
    enabled: bool = True
    spacy_model: str = Field(default="en_core_web_sm", min_length=1, max_length=64)
    language: str = Field(default="en", min_length=2, max_length=8)
    score_threshold: float = Field(default=0.5, ge=0, le=1)


class GatewaySettings(_Frozen):
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=8443, ge=1, le=65535)
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    tls_client_ca_file: Path | None = Field(
        default=None, description="CA for client certificates (collector, core API)."
    )
    otlp_max_body_bytes: int = Field(default=4 * MIB, ge=64 * 1024, le=64 * MIB)
    webhook_max_body_bytes: int = Field(default=MIB, ge=1024, le=MIB)
    poll_concurrency: int = Field(default=4, ge=1, le=64)
    poll_seconds: float = Field(
        default=60.0,
        ge=1,
        le=86_400,
        description="Poll interval for pull sources whose connector config names none.",
    )
    stats_flush_seconds: float = Field(default=30.0, ge=1, le=3600)


class EdgeSettings(ProductSettings):
    """``CARTO_`` settings of edge-gateway and the offline analyzer."""

    state_dir: Path = Field(default=Path("/var/lib/carto-edge"))
    sources_file: Path | None = None
    core: CoreLinkSettings = Field(default_factory=CoreLinkSettings)
    kms: KmsSettings = Field(default_factory=KmsSettings)
    classify: ClassifySettings = Field(default_factory=ClassifySettings)
    buffer: BufferSettings = Field(default_factory=BufferSettings)
    reveal: RevealSettings = Field(default_factory=RevealSettings)
    pii: PiiSettings = Field(default_factory=PiiSettings)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)

    @property
    def keys_dir(self) -> Path:
        return self.state_dir / "keys"

    @property
    def local_kms_key_file(self) -> Path:
        return self.kms.local_key_file or (self.keys_dir / "local-kms.key")

    @property
    def tenant_key_file(self) -> Path:
        """The KMS-wrapped tenant key (``WrappedKey`` JSON)."""
        return self.keys_dir / "tenant-key.json"

    @property
    def vault_db_file(self) -> Path:
        return self.state_dir / "vault.sqlite"

    @property
    def buffer_db_file(self) -> Path:
        return self.state_dir / "buffer.sqlite"

    @property
    def cursors_db_file(self) -> Path:
        return self.state_dir / "cursors.sqlite"

    @property
    def stats_file(self) -> Path:
        return self.state_dir / "field_stats.json"

    @property
    def templates_dir(self) -> Path:
        return self.state_dir / "templates"

    @property
    def names_dir(self) -> Path:
        return self.state_dir / "names"

    @property
    def audit_file(self) -> Path:
        return self.state_dir / "audit.ndjson"
