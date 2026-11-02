"""Core service settings (spec Section 20, 14.4, 14.10).

``CARTO_`` environment variables with ``__`` between levels, as for every service
(:class:`carto_common.settings.ProductSettings`). Three sections are core's own:

- ``clickhouse``: the HTTP(S) endpoint ``ingest-api`` and the workers write to and read from.
  TLS is the default (spec 14.4 "PostgreSQL and ClickHouse connections require TLS"); plain
  ``http://`` needs an explicit ``secure=false`` and is for local development only.
- ``postgres``: the metadata store. ``sslmode`` defaults to ``verify-full``: the server certificate
  and its name are checked (``ca_file`` names the CA, else libpq's default root file). ``prefer``
  and ``allow`` are not offered because they silently fall back to clear text; ``disable`` is
  for the Compose stack's internal network and local containers only.
- ``ingest``: the mutual-TLS listener of ``ingest-api`` (spec 12 "Internal", 14.4) and its body
  caps (spec 8.5: 5 MB compressed batches; spec 2.3 invariant 8: size limits on every input).

Passwords come from files only (``password_file``), read at use by :meth:`read_password`, never
from an environment value, never stored on the settings object and never part of its ``repr``
(spec 8.4 "No key material in environment variables, config files, images or logs", 14.3).
Per-service database users (spec 14.4) are the operator's choice; the defaults name the ingest
user. Retention (the ClickHouse TTLs) is ``ProductSettings.retention`` (spec 14.10).
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from carto_common.settings import ProductSettings

__all__ = [
    "MAX_PASSWORD_FILE_BYTES",
    "MIB",
    "ClickHouseSettings",
    "CoreConfigError",
    "CoreSettings",
    "IngestListenerSettings",
    "LogLevel",
    "PostgresSettings",
    "SslMode",
]

MIB: Final = 1024**2
MAX_PASSWORD_FILE_BYTES: Final = 4096
"""A password file longer than this is a mistake (a certificate, a dump), not a password."""

_IDENTIFIER_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
"""Database and user names: what both databases accept unquoted, so no quoting is ever built."""

LogLevel = Literal["critical", "error", "warning", "info", "debug"]
SslMode = Literal["disable", "require", "verify-ca", "verify-full"]


class CoreConfigError(Exception):
    """A configuration file could not be used. The message never carries the file's content."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


def _read_secret_file(path: Path | None, what: str) -> str:
    """Return the trimmed content of a secret file, or ``""`` when no file is configured."""
    if path is None:
        return ""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_PASSWORD_FILE_BYTES + 1)
    except OSError as exc:
        msg = f"cannot read {what} file {path}"
        raise CoreConfigError(msg) from exc
    if len(data) > MAX_PASSWORD_FILE_BYTES:
        msg = f"{what} file {path} is larger than {MAX_PASSWORD_FILE_BYTES} bytes"
        raise CoreConfigError(msg)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{what} file {path} is not UTF-8 text"
        raise CoreConfigError(msg) from exc
    return text.strip("\r\n")


class ClickHouseSettings(_Frozen):
    """Where ClickHouse listens and which user writes (spec 6, 14.4)."""

    url: str = Field(
        default="https://127.0.0.1:8443",
        description="http(s)://host:port of the ClickHouse HTTP interface; no path, no userinfo.",
    )
    database: str = Field(default="carto", pattern=_IDENTIFIER_PATTERN)
    user: str = Field(default="carto_ingest", pattern=_IDENTIFIER_PATTERN)
    password_file: Path | None = Field(
        default=None, description="File holding the password; never an environment value."
    )
    secure: bool = Field(
        default=True,
        description="TLS to ClickHouse (spec 14.4). False only with an http:// url, for local use.",
    )
    ca_file: Path | None = Field(default=None, description="CA bundle for the server certificate.")
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)

    @field_validator("url")
    @classmethod
    def _scheme_host_port(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https"):
            msg = "clickhouse.url must start with http:// or https://"
            raise ValueError(msg)
        if parts.username is not None or parts.password is not None:
            msg = "clickhouse.url must not carry credentials; use password_file"
            raise ValueError(msg)
        try:
            port = parts.port
        except ValueError as exc:
            msg = "clickhouse.url port is not a number in range"
            raise ValueError(msg) from exc
        if not parts.hostname or port is None or not 1 <= port <= 65535:
            msg = "clickhouse.url must be http(s)://host:port with an explicit port (1 to 65535)"
            raise ValueError(msg)
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            msg = "clickhouse.url must not have a path, query or fragment"
            raise ValueError(msg)
        return f"{parts.scheme}://{parts.netloc}"

    @model_validator(mode="after")
    def _secure_matches_scheme(self) -> Self:
        is_https = self.url.startswith("https://")
        if is_https and not self.secure:
            msg = "clickhouse.secure must be true for an https:// url"
            raise ValueError(msg)
        if not is_https and self.secure:
            msg = (
                "clickhouse.secure is true but the url is http://; use https:// (spec 14.4) or "
                "set secure=false for local development"
            )
            raise ValueError(msg)
        return self

    @property
    def host(self) -> str:
        hostname = urlsplit(self.url).hostname
        return hostname if hostname is not None else ""

    @property
    def port(self) -> int:
        port = urlsplit(self.url).port
        return port if port is not None else 0

    def read_password(self) -> str:
        """The password from ``password_file`` (empty when none is configured)."""
        return _read_secret_file(self.password_file, "clickhouse password")


class PostgresSettings(_Frozen):
    """The metadata store connection (spec 7.3, 14.4)."""

    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(default="carto", pattern=_IDENTIFIER_PATTERN)
    user: str = Field(default="carto_ingest", pattern=_IDENTIFIER_PATTERN)
    password_file: Path | None = Field(
        default=None, description="File holding the password; never an environment value."
    )
    sslmode: SslMode = Field(
        default="verify-full",
        description=(
            "libpq sslmode; verify-full checks the certificate and the host name. 'disable' is "
            "for the Compose stack's internal network and local containers only."
        ),
    )
    ca_file: Path | None = Field(default=None, description="libpq sslrootcert.")
    connect_timeout_seconds: int = Field(default=10, ge=1, le=300)

    def read_password(self) -> str:
        """The password from ``password_file`` (empty when none is configured)."""
        return _read_secret_file(self.password_file, "postgres password")


class IngestListenerSettings(_Frozen):
    """The ``ingest-api`` listener: mutual TLS and request caps (spec 8.5, 12, 14.4)."""

    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=8443, ge=1, le=65535)
    tls_cert_file: Path | None = Field(default=None, description="Server certificate (PEM).")
    tls_key_file: Path | None = Field(default=None, description="Server private key (PEM).")
    client_ca_file: Path | None = Field(
        default=None, description="CA that signs the edge client certificates (carto-ctl pki)."
    )
    max_body_bytes: int = Field(
        default=5 * MIB,
        ge=64 * 1024,
        le=64 * MIB,
        description="Largest request body on the wire (spec 8.5: batches of at most 5 MB).",
    )
    max_decompressed_bytes: int = Field(
        default=16 * MIB,
        ge=64 * 1024,
        le=128 * MIB,
        description=(
            "Largest body after zstd decompression (spec 2.3 invariant 8: no zip bombs). A full "
            "batch of 5,000 events is a few MiB of JSON."
        ),
    )
    max_inflight_batches: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "Ingest and heartbeat requests decoded or written at once; more are answered 429 "
            "with Retry-After, which bounds the memory that decoded batches take."
        ),
    )
    claim_timeout_seconds: int = Field(
        default=120,
        ge=30,
        le=3600,
        description=(
            "How long a ledger claim protects a batch being written (ADR 0034). A retry within "
            "it is answered 503; after it the claim is taken over and the batch written again. "
            "Keep it above the edge's request timeout."
        ),
    )
    require_client_cert: bool = Field(
        default=True,
        description="Mutual TLS (spec 14.4). Only false for a local smoke test, never in service.",
    )

    @model_validator(mode="after")
    def _decompressed_not_below_wire(self) -> Self:
        if self.max_decompressed_bytes < self.max_body_bytes:
            msg = "ingest.max_decompressed_bytes must be at least ingest.max_body_bytes"
            raise ValueError(msg)
        return self


class CoreSettings(ProductSettings):
    """``CARTO_`` settings of ``ingest-api``, ``carto-core migrate`` and the bundle loader."""

    log_level: LogLevel = Field(default="info")
    log_json: bool = Field(default=True, description="JSON log lines; false renders for a console.")
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    ingest: IngestListenerSettings = Field(default_factory=IngestListenerSettings)

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower_level(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value
