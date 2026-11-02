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
- ``profiler`` and ``linker``: every spec 9.1 to 9.6 threshold with its spec default, so eval
  tuning never edits code (plan M2 decision 13).
- ``api``: the console API listener, sessions, OIDC and rate limits (spec 12, 14.1, 14.2, 14.7).
- ``worker``: how ``carto-core worker`` leases jobs (spec 5.3).

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
    "ROLE_NAMES",
    "SESSION_COOKIE_NAME",
    "ApiSettings",
    "ClickHouseSettings",
    "CoreConfigError",
    "CoreSettings",
    "IngestListenerSettings",
    "LinkerSettings",
    "LogLevel",
    "OidcSettings",
    "PostgresSettings",
    "ProfilerSettings",
    "SslMode",
    "WorkerSettings",
]

SESSION_COOKIE_NAME: Final = "__Host-carto_session"
"""Spec 14.1: the session cookie; the ``__Host-`` prefix needs Secure, path ``/`` and no domain."""
ROLE_NAMES: Final[frozenset[str]] = frozenset(
    {"admin", "integration_engineer", "business_viewer", "auditor"}
)
"""Spec 14.2 roles as settings spell them (:class:`carto_schema.api.Role`)."""

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


class ProfilerSettings(_Frozen):
    """Spec 9.1 field profiling, every number a setting (plan M2 decision 13)."""

    window_days: int = Field(default=7, ge=1, le=30, description="Rolling window (spec 9.1).")
    update_interval_seconds: int = Field(
        default=300, ge=30, le=3600, description="Incremental pass cadence (spec 9.1: 5 min)."
    )
    rebuild_hour_utc: int = Field(
        default=2, ge=0, le=23, description="Hour of the nightly full-window rebuild (UTC)."
    )
    minhash_permutations: int = Field(default=128, ge=16, le=512)
    bottom_k: int = Field(default=2048, ge=64, le=65536, description="Exact sample size.")
    quality_floor: float = Field(default=0.4, ge=0.0, le=1.0, description="q(field) floor.")
    min_distinct: int = Field(default=50, ge=1, description="Fewer distinct: not a candidate.")
    top_shapes: int = Field(default=8, ge=1, le=32)
    concentration_top_n: int = Field(default=10, ge=1, le=100)
    concentration_max_share: float = Field(
        default=0.2,
        gt=0.0,
        le=1.0,
        description="q(field) wants the top-n values under this share of occurrences (9.1).",
    )
    read_batch_rows: int = Field(
        default=200_000, ge=1000, le=5_000_000, description="Rows per store read chunk."
    )


class LinkerSettings(_Frozen):
    """Spec 9.2 to 9.6 thresholds with the spec defaults (plan M2 decision 13)."""

    containment_threshold: float = Field(default=0.2, gt=0.0, le=1.0, description="LSH (9.2).")
    max_candidates_per_run: int = Field(
        default=5000, ge=1, le=1_000_000, description="Highest estimated containment first."
    )
    min_match: int = Field(default=20, ge=1, description="Hard filter n_match (9.3).")
    max_concentration: float = Field(default=0.5, gt=0.0, le=1.0, description="Hard filter.")
    max_chance_ratio: float = Field(default=0.3, gt=0.0, description="Hard filter (9.3).")
    direction_share: float = Field(
        default=0.8,
        ge=0.5,
        le=1.0,
        description="lag_positive_share at or above: A to B; at or below 1 minus it: B to A.",
    )
    transaction_fanout_median_max: float = Field(default=3.0, ge=1.0)
    transaction_fanout_p99_max: float = Field(default=20.0, ge=1.0)
    batch_other_end_median_max: float = Field(
        default=1.5,
        ge=1.0,
        description=(
            "Spec 9.3 batch role: one end fans out past the p99 limit while the other end's "
            "median fan-out stays at or below this (one token to many transactions)."
        ),
    )
    bridge_consistency_min: float = Field(default=0.98, ge=0.0, le=1.0, description="9.4.")
    bridge_min_events: int = Field(default=20, ge=1, description="Co-occurring events needed.")
    proposal_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Score at or above enters the review queue."
    )
    auto_accept_enabled: bool = Field(default=False, description="Off by default (9.6).")
    auto_accept_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    repropose_score_delta: float = Field(
        default=0.2, ge=0.0, le=1.0, description="A rejected pair returns only above this rise."
    )
    stale_drop_share: float = Field(
        default=0.5, gt=0.0, le=1.0, description="Match-rate drop against the baseline (9.6)."
    )
    composite_enabled: bool = True
    composite_exact_ceiling: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Composite runs when no exact link scores above."
    )
    composite_coverage_floor: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "Composite matching also runs for a system pair whose best exact or bridge link "
            "covers less than this share of the later end (scenario C: an exact reference is "
            "present in 60% of HRIS rows and the rest is keyed by hand)."
        ),
    )
    composite_adjacency_min_correlation: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Hourly volume cross-correlation (9.5 trigger)."
    )
    composite_em_iterations: int = Field(default=10, ge=1, le=200)
    composite_window_business_days: int = Field(default=3, ge=1, le=30)
    composite_target_precision: float = Field(default=0.9, ge=0.5, le=1.0)
    composite_max_pairs: int = Field(default=2_000_000, ge=1000, description="Blocking cap.")
    weights_file: Path | None = Field(
        default=None,
        description="Versioned logistic weights (plan M2 decision 4); the shipped set when unset.",
    )


class OidcSettings(_Frozen):
    """Spec 14.1: SSO through OIDC, authorization code flow with PKCE (founder decision 1)."""

    issuer: str | None = Field(default=None, description="https:// issuer URL (discovery).")
    client_id: str | None = Field(default=None, max_length=256)
    client_secret_file: Path | None = Field(
        default=None, description="File holding the client secret; never an environment value."
    )
    scopes: str = Field(default="openid profile email groups", max_length=256)
    groups_claim: str = Field(default="groups", max_length=64)
    step_up_max_age_seconds: int = Field(
        default=900, ge=60, le=3600, description="Fresh login for step-up actions (14.1)."
    )
    allow_http_issuer: bool = Field(
        default=False, description="Plain http:// issuer for the Compose dev profile only."
    )
    backchannel_url: str | None = Field(
        default=None,
        description=(
            "Base URL the api process uses for discovery, token and JWKS calls when the issuer "
            "URL is only reachable by browsers (the Compose stack serves Dex under the console "
            "origin): discovered endpoints that start with the issuer are rewritten to it. The "
            "ID token's iss claim must still equal the issuer."
        ),
    )
    ca_file: Path | None = Field(
        default=None, description="CA bundle for the provider's TLS certificate (private CA)."
    )

    @field_validator("issuer")
    @classmethod
    def _issuer_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            msg = "api.oidc.issuer must be an http(s):// URL"
            raise ValueError(msg)
        if parts.username is not None or parts.password is not None:
            msg = "api.oidc.issuer must not carry credentials"
            raise ValueError(msg)
        return value.rstrip("/")

    @field_validator("backchannel_url")
    @classmethod
    def _backchannel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
            msg = "api.oidc.backchannel_url must be an http(s):// URL without credentials"
            raise ValueError(msg)
        return value.rstrip("/")

    @model_validator(mode="after")
    def _https_unless_allowed(self) -> Self:
        for name, url in (("issuer", self.issuer), ("backchannel_url", self.backchannel_url)):
            if url is not None and url.startswith("http://") and not self.allow_http_issuer:
                msg = f"api.oidc.{name} must be https:// unless api.oidc.allow_http_issuer is set"
                raise ValueError(msg)
        return self

    def read_client_secret(self) -> str:
        return _read_secret_file(self.client_secret_file, "oidc client secret")


class ApiSettings(_Frozen):
    """The console API (spec 12, 13, 14.1, 14.2, 14.7). One origin with the web app (plan M2
    decision 10): ``public_url`` is that origin and the OIDC redirect is ``/api/v1/auth/callback``
    under it."""

    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=8444, ge=1, le=65535)
    public_url: str = Field(
        default="https://localhost",
        description="The origin users reach the console at; cookies and redirects derive from it.",
    )
    tls_cert_file: Path | None = Field(default=None, description="Server certificate (PEM).")
    tls_key_file: Path | None = Field(default=None, description="Server private key (PEM).")
    insecure_http: bool = Field(
        default=False,
        description="Serve plain HTTP behind a TLS-terminating proxy on a private network; tests.",
    )
    client_ca_file: Path | None = Field(
        default=None,
        description=(
            "CA that signs the web proxy's client certificate (carto-ctl pki init, role web). "
            "Browsers never reach this listener; the web service does (ADR 0052)."
        ),
    )
    require_client_cert: bool = Field(
        default=False,
        description="Mutual TLS from the web proxy; needs client_ca_file. False for tests only.",
    )
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    session_idle_minutes: int = Field(default=30, ge=5, le=240, description="Spec 14.1.")
    session_absolute_hours: int = Field(default=12, ge=1, le=24, description="Spec 14.1.")
    cookie_secure: bool = Field(
        default=True, description="Secure flag; false only for a plain-HTTP test client."
    )
    bootstrap_group_roles: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Identity-provider group to role (admin, integration_engineer, business_viewer, "
            "auditor) applied to role_bindings at startup when the table has no bindings yet."
        ),
    )
    login_rate_per_minute_per_ip: int = Field(default=10, ge=1, le=1000)
    request_rate_per_minute_per_user: int = Field(default=600, ge=10, le=100_000)
    max_body_bytes: int = Field(default=MIB, ge=16 * 1024, le=16 * MIB)
    page_size_default: int = Field(default=50, ge=1, le=500)

    @field_validator("public_url")
    @classmethod
    def _origin_only(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            msg = "api.public_url must be an http(s):// origin"
            raise ValueError(msg)
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            msg = "api.public_url must be an origin: scheme, host and port only"
            raise ValueError(msg)
        return f"{parts.scheme}://{parts.netloc}"

    @model_validator(mode="after")
    def _tls_or_insecure(self) -> Self:
        if not self.insecure_http and (self.tls_cert_file is None or self.tls_key_file is None):
            msg = (
                "api.tls_cert_file and api.tls_key_file are required unless api.insecure_http is "
                "set (spec 14.4: TLS for every connection)"
            )
            raise ValueError(msg)
        if self.require_client_cert and self.client_ca_file is None:
            msg = "api.client_ca_file is required when api.require_client_cert is set"
            raise ValueError(msg)
        for group, role in self.bootstrap_group_roles.items():
            if role not in ROLE_NAMES:
                msg = f"api.bootstrap_group_roles maps group {group!r} to an unknown role"
                raise ValueError(msg)
        return self

    @property
    def redirect_url(self) -> str:
        return f"{self.public_url}/api/v1/auth/callback"


class WorkerSettings(_Frozen):
    """``carto-core worker`` (spec 5.3: PostgreSQL job leases, no broker)."""

    poll_seconds: float = Field(default=5.0, ge=0.5, le=300)
    lease_seconds: int = Field(
        default=600, ge=30, le=7200, description="A job leased longer than this is retaken."
    )
    max_attempts: int = Field(default=5, ge=1, le=100)
    backoff_base_seconds: float = Field(default=30.0, ge=1, le=3600)
    link_after_profile: bool = Field(
        default=True, description="Queue a link pass after each profile pass over new sketches."
    )
    full_pass_hour_utc: int = Field(default=3, ge=0, le=23, description="Nightly full link pass.")


class CoreSettings(ProductSettings):
    """``CARTO_`` settings of every core process: ``ingest-api``, ``api``, ``worker``,
    ``carto-core migrate``, the bundle loader and ``discover``. The ``api`` section defaults to
    plain HTTP so processes that never serve the console load without certificate files;
    ``carto-core api`` refuses to start without TLS unless ``api.insecure_http`` is set."""

    log_level: LogLevel = Field(default="info")
    log_json: bool = Field(default=True, description="JSON log lines; false renders for a console.")
    clickhouse: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    ingest: IngestListenerSettings = Field(default_factory=IngestListenerSettings)
    profiler: ProfilerSettings = Field(default_factory=ProfilerSettings)
    linker: LinkerSettings = Field(default_factory=LinkerSettings)
    api: ApiSettings = Field(default_factory=lambda: ApiSettings(insecure_http=True))
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower_level(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value
