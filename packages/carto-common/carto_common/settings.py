"""Configuration schemas shared by every carto service.

Spec Section 20: configuration goes through pydantic-settings with explicit schemas and secrets
only through ``secret_ref``. Spec 14.3: a ``secret_ref`` names where a secret lives
(``vault://``, ``aws-sm://``, ``azure-kv://``, ``gcp-sm://`` or, for Compose pilots,
``local://``); the value is fetched at use by the service that owns the connector, never by this
module, never stored, never returned by the API and never logged. Spec 14.10 and ADR 0004 fix
the retention defaults and ranges. Spec 9.9, 2.3 invariant 5 and ADR 0004: LLM assist is off by
default and never required. Spec 2.3 invariant 4: no telemetry leaves the deployment unless the
customer enables it.

Every model forbids unknown fields and is frozen (settings are read once at startup and
shared). Environment variables use the ``CARTO_`` prefix and ``__`` between nesting levels:
``CARTO_RETENTION__EVENTS_DAYS=45``, ``CARTO_LLM__ENABLED=true``. An unknown key inside a known
section (``CARTO_RETENTION__BOGUS``) and an unknown keyword passed to the constructor are
errors. A top-level variable that matches no field (``CARTO_TENNANT_ID``, or the
single-underscore ``CARTO_RETENTION_EVENTS_DAYS``) is ignored, as pydantic-settings reads only
declared fields from the environment and one Compose ``.env`` is shared by every service; a
service should therefore log its effective settings at startup so a typo is visible.
Validation errors never echo the offending input (spec 14.3), so a secret pasted where a
reference belongs does not end up in a log line.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = [
    "MAX_SECRET_REF_LENGTH",
    "OBSERVED_AT_SLACK",
    "SECRET_REF_SCHEMES",
    "TENANT_ID_REGEX",
    "CartoBaseSettings",
    "LlmProvider",
    "LlmSettings",
    "ProductSettings",
    "RetentionSettings",
    "SecretRef",
    "TelemetrySettings",
    "observed_at_range",
    "validate_secret_ref",
]

SECRET_REF_SCHEMES: Final[tuple[str, ...]] = ("vault", "aws-sm", "azure-kv", "gcp-sm", "local")
"""Allowed ``secret_ref`` schemes (spec 14.3), without the ``://``."""

MAX_SECRET_REF_LENGTH: Final = 1024
TENANT_ID_REGEX: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"

_SCHEME_SEPARATOR: Final = "://"
_ALLOWED_SCHEMES_TEXT: Final = ", ".join(f"{scheme}://" for scheme in SECRET_REF_SCHEMES)


# ---------------------------------------------------------------------------------------------
# secret_ref (spec 14.3, 8.1 "Credentials are referenced, never stored")
# ---------------------------------------------------------------------------------------------


def validate_secret_ref(value: str) -> str:
    """Accept ``<scheme>://<path>`` for the spec 14.3 schemes and reject everything else.

    Checks shape only: nothing is resolved, fetched or touched. The path must be non-empty and
    the whole reference made of printable, non-whitespace characters (no NUL, DEL, escape or
    zero-width characters: user-entered config is untrusted input, spec 0.1 item 6). Error
    messages list the allowed schemes and never repeat the input.
    """
    scheme, separator, path = value.partition(_SCHEME_SEPARATOR)
    if not separator or scheme not in SECRET_REF_SCHEMES:
        msg = f"secret_ref must be '<scheme>://<path>' with one of {_ALLOWED_SCHEMES_TEXT}"
        raise ValueError(msg)
    if not path:
        msg = f"secret_ref needs a non-empty path after '{scheme}://'"
        raise ValueError(msg)
    if not value.isprintable() or any(char.isspace() for char in value):
        msg = "secret_ref must contain only printable, non-whitespace characters"
        raise ValueError(msg)
    if len(value) > MAX_SECRET_REF_LENGTH:
        msg = f"secret_ref must be at most {MAX_SECRET_REF_LENGTH} characters"
        raise ValueError(msg)
    return value


SecretRef = Annotated[str, AfterValidator(validate_secret_ref)]
"""A reference to a secret in a customer store (spec 14.3). Never the secret itself."""


# ---------------------------------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------------------------------


class CartoBaseSettings(BaseSettings):
    """Base class for every service's settings (spec Section 20).

    ``CARTO_`` prefix, ``__`` nesting delimiter, unknown nested keys and constructor keywords
    rejected (top-level variables that match no field are ignored, see the module docstring),
    frozen after load, and inputs hidden from validation errors so a misplaced secret is never
    echoed (spec 14.3).
    """

    model_config = SettingsConfigDict(
        env_prefix="CARTO_",
        extra="forbid",
        frozen=True,
        env_nested_delimiter="__",
        hide_input_in_errors=True,
    )


OBSERVED_AT_SLACK: Final = timedelta(days=1)
"""How far past retention, and how far ahead of now, an ``observed_at`` may lie (ADR 0036)."""


def observed_at_range(now: datetime, events_days: int) -> tuple[datetime, datetime]:
    """The plausible ``observed_at`` range at ``now``, both ends included (ADR 0036): no older
    than ``events_days`` plus one day, no more than one day ahead."""
    return now - timedelta(days=events_days) - OBSERVED_AT_SLACK, now + OBSERVED_AT_SLACK


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class RetentionSettings(_FrozenModel):
    """Retention periods with the spec 14.10 defaults and configurable ranges (ADR 0004).

    The reveal vault has no setting of its own: its entries expire with the events they belong
    to (spec 8.4, 14.10 "Follows events"). Transaction membership outlives events, so
    ``txn_membership_days`` must be at least ``events_days``.
    """

    events_days: int = Field(default=30, ge=7, le=400, description="Events and identifiers.")
    txn_membership_days: int = Field(
        default=90, ge=30, le=400, description="Transaction membership (event to transaction)."
    )
    aggregates_months: int = Field(default=13, ge=3, le=36, description="Hourly aggregates.")
    alerts_months: int = Field(default=13, ge=3, le=36, description="Alerts and their history.")
    audit_days: int = Field(default=365, ge=90, description="Tamper-evident audit log.")

    @model_validator(mode="after")
    def _membership_outlives_events(self) -> Self:
        if self.txn_membership_days < self.events_days:
            msg = "retention.txn_membership_days must be at least retention.events_days"
            raise ValueError(msg)
        return self

    def observed_at_range(self, now: datetime) -> tuple[datetime, datetime]:
        """The plausible ``observed_at`` range at ``now``, both ends included (ADR 0036): no older
        than events retention plus one day, no more than one day ahead. The edge drops older
        events and stamps later ones with their ingest time; ingest-api refuses both."""
        return observed_at_range(now, self.events_days)


LlmProvider = Literal["anthropic", "bedrock", "vertex"]
"""Providers the customer can route ``llm-gateway`` through (spec 9.9, ADR 0004)."""


class LlmSettings(_FrozenModel):
    """Optional LLM assist, off by default and never required (spec 9.9, 2.3 invariant 5)."""

    enabled: bool = Field(default=False, description="Off by default (ADR 0004).")
    provider: LlmProvider | None = Field(
        default=None, description="Required when enabled; chosen by the customer."
    )
    endpoint: str | None = Field(
        default=None,
        description="Customer-approved endpoint; the only egress the gateway allows (spec 9.9).",
    )
    model: str | None = Field(default=None, description="Model name at the provider.")

    @field_validator("endpoint")
    @classmethod
    def _endpoint_is_https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            msg = "llm.endpoint must start with https://"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _enabled_needs_provider(self) -> Self:
        if self.enabled and self.provider is None:
            msg = "llm.provider is required when llm.enabled is true"
            raise ValueError(msg)
        return self


class TelemetrySettings(_FrozenModel):
    """Product health telemetry, off by default (spec 2.3 invariant 4: no phone-home)."""

    enabled: bool = Field(
        default=False,
        description="Off by default; even when on it carries health metrics, never event data.",
    )


class ProductSettings(CartoBaseSettings):
    """Settings every service shares; services extend it with their own fields."""

    tenant_id: str = Field(
        default="default",
        pattern=TENANT_ID_REGEX,
        description="Tenant key domain name (spec 8.4 K_tenant); single tenant in v1 (ADR 0002).",
    )
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
