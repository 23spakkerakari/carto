"""carto_common.settings: defaults, ranges, environment overrides and secret_ref.

Spec 14.10 and ADR 0004 (retention defaults), 9.9 and 2.3 invariant 5 (LLM off), 2.3 invariant 4
(telemetry off), 14.3 (secret_ref schemes) and Section 20 (pydantic-settings with explicit
schemas).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from carto_common.settings import (
    MAX_SECRET_REF_LENGTH,
    OBSERVED_AT_SLACK,
    SECRET_REF_SCHEMES,
    CartoBaseSettings,
    LlmSettings,
    ProductSettings,
    RetentionSettings,
    SecretRef,
    TelemetrySettings,
    validate_secret_ref,
)

secret_ref_adapter: TypeAdapter[str] = TypeAdapter(SecretRef)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove any CARTO_* variable from the developer's shell so defaults are really defaults."""
    for name in list(os.environ):
        if name.startswith("CARTO_"):
            monkeypatch.delenv(name)
    return monkeypatch


# ---------------------------------------------------------------------------------------------
# Defaults (ADR 0004, spec 14.10)
# ---------------------------------------------------------------------------------------------


def test_defaults_match_adr_0004_and_spec_14_10(clean_env: pytest.MonkeyPatch) -> None:
    settings = ProductSettings()
    assert settings.tenant_id == "default"
    assert settings.retention == RetentionSettings(
        events_days=30,
        txn_membership_days=90,
        aggregates_months=13,
        alerts_months=13,
        audit_days=365,
    )
    assert settings.llm == LlmSettings(enabled=False, provider=None, endpoint=None, model=None)
    assert settings.telemetry == TelemetrySettings(enabled=False)
    assert settings.llm.enabled is False
    assert settings.telemetry.enabled is False


def test_base_settings_configuration() -> None:
    config = CartoBaseSettings.model_config
    assert config["env_prefix"] == "CARTO_"
    assert config["extra"] == "forbid"
    assert config["frozen"] is True
    assert config["env_nested_delimiter"] == "__"
    assert config["hide_input_in_errors"] is True
    assert issubclass(ProductSettings, CartoBaseSettings)


# ---------------------------------------------------------------------------------------------
# Retention ranges (spec 14.10)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("events_days", 7, True),
        ("events_days", 6, False),
        ("events_days", 400, True),
        ("events_days", 401, False),
        ("txn_membership_days", 30, True),
        ("txn_membership_days", 29, False),
        ("txn_membership_days", 400, True),
        ("txn_membership_days", 401, False),
        ("aggregates_months", 3, True),
        ("aggregates_months", 2, False),
        ("aggregates_months", 36, True),
        ("aggregates_months", 37, False),
        ("alerts_months", 3, True),
        ("alerts_months", 2, False),
        ("alerts_months", 36, True),
        ("alerts_months", 37, False),
        ("audit_days", 90, True),
        ("audit_days", 89, False),
        ("audit_days", 36_500, True),
    ],
)
def test_retention_range_boundaries(field: str, value: int, ok: bool) -> None:
    fields: dict[str, Any] = {field: value}
    if field == "events_days":
        fields["txn_membership_days"] = max(value, 90)  # keep the cross-field rule satisfied
    if ok:
        assert getattr(RetentionSettings(**fields), field) == value
    else:
        with pytest.raises(ValidationError) as excinfo:
            RetentionSettings(**fields)
        assert excinfo.value.errors()[0]["loc"] == (field,)


def test_transaction_membership_must_outlive_events() -> None:
    assert RetentionSettings(events_days=90, txn_membership_days=90).events_days == 90
    with pytest.raises(ValidationError, match="txn_membership_days must be at least"):
        RetentionSettings(events_days=100, txn_membership_days=90)


def test_retention_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        unknown: dict[str, Any] = {"reveal_vault_days": 30}
        RetentionSettings(**unknown)


def test_the_plausible_observed_at_range_is_retention_plus_a_day_back_and_a_day_ahead() -> None:
    """ADR 0036: ``now - events_days - 1 day`` to ``now + 1 day``."""
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    lower, upper = RetentionSettings(events_days=45).observed_at_range(now)
    assert lower == datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    assert upper == datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert timedelta(days=1) == OBSERVED_AT_SLACK


# ---------------------------------------------------------------------------------------------
# LLM (spec 9.9, 2.3 invariant 5) and telemetry (2.3 invariant 4)
# ---------------------------------------------------------------------------------------------


def test_llm_enabled_requires_a_provider() -> None:
    with pytest.raises(ValidationError, match=r"llm\.provider is required"):
        LlmSettings(enabled=True)
    enabled = LlmSettings(enabled=True, provider="anthropic")
    assert enabled.provider == "anthropic"
    assert LlmSettings(enabled=False, provider=None).enabled is False


@pytest.mark.parametrize("provider", ["anthropic", "openai", "bedrock", "vertex"])
def test_llm_providers_are_the_adr_0004_set_plus_openai_compatible(provider: str) -> None:
    """ADR 0004 names Anthropic, Bedrock and Vertex; plan M2 decision 12 adds any
    OpenAI-compatible endpoint as the adapter most self-hosted deployments speak."""
    assert LlmSettings(enabled=True, provider=provider).provider == provider  # type: ignore[arg-type]


def test_llm_rejects_other_providers() -> None:
    with pytest.raises(ValidationError):
        LlmSettings(enabled=True, provider="cohere")  # type: ignore[arg-type]


def test_llm_credential_comes_from_a_file_not_a_value() -> None:
    settings = LlmSettings(api_key_file=Path("/run/secrets/llm_key"))
    assert settings.api_key_file == Path("/run/secrets/llm_key")
    assert "api_key" not in {name for name in LlmSettings.model_fields if name != "api_key_file"}


def test_llm_endpoint_must_be_https() -> None:
    assert LlmSettings(endpoint="https://llm.internal/v1").endpoint == "https://llm.internal/v1"
    assert LlmSettings(endpoint=None).endpoint is None
    with pytest.raises(ValidationError, match="must start with https://"):
        LlmSettings(endpoint="http://llm.internal/v1")


def test_telemetry_is_off_unless_enabled() -> None:
    assert TelemetrySettings().enabled is False
    assert TelemetrySettings(enabled=True).enabled is True


# ---------------------------------------------------------------------------------------------
# Environment overrides (CARTO_ prefix, __ nesting)
# ---------------------------------------------------------------------------------------------


def test_env_overrides_nested_retention(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CARTO_RETENTION__EVENTS_DAYS", "45")
    settings = ProductSettings()
    assert settings.retention.events_days == 45
    assert settings.retention.txn_membership_days == 90


def test_env_llm_enabled_without_provider_fails(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CARTO_LLM__ENABLED", "true")
    with pytest.raises(ValidationError, match=r"llm\.provider is required"):
        ProductSettings()


def test_env_llm_enabled_with_provider(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CARTO_LLM__ENABLED", "true")
    clean_env.setenv("CARTO_LLM__PROVIDER", "bedrock")
    clean_env.setenv("CARTO_LLM__ENDPOINT", "https://bedrock-runtime.us-east-1.amazonaws.com")
    settings = ProductSettings()
    assert settings.llm.enabled is True
    assert settings.llm.provider == "bedrock"


def test_env_range_violation_fails(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CARTO_RETENTION__EVENTS_DAYS", "6")
    with pytest.raises(ValidationError) as excinfo:
        ProductSettings()
    assert excinfo.value.errors()[0]["loc"] == ("retention", "events_days")


def test_env_tenant_and_telemetry(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CARTO_TENANT_ID", "acme-prod_1")
    clean_env.setenv("CARTO_TELEMETRY__ENABLED", "1")
    settings = ProductSettings()
    assert settings.tenant_id == "acme-prod_1"
    assert settings.telemetry.enabled is True


def test_unprefixed_variables_are_ignored(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("RETENTION__EVENTS_DAYS", "45")
    clean_env.setenv("TENANT_ID", "other")
    settings = ProductSettings()
    assert settings.retention.events_days == 30
    assert settings.tenant_id == "default"


def test_unknown_nested_env_keys_are_rejected(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("CARTO_RETENTION__BOGUS", "1")
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        ProductSettings()


def test_unknown_top_level_env_variables_are_ignored(clean_env: pytest.MonkeyPatch) -> None:
    """pydantic-settings reads only declared fields: a misspelt or single-underscore top-level
    variable is ignored rather than rejected (documented in the module and the README, because
    one Compose ``.env`` is shared by every service). This test pins the behaviour the docs
    describe; a service logs its effective settings at startup so a typo is visible."""
    clean_env.setenv("CARTO_TENNANT_ID", "typo")
    clean_env.setenv("CARTO_RETENTION_EVENTS_DAYS", "7")
    clean_env.setenv("CARTO_TELEMETRY_ENABLED", "true")
    settings = ProductSettings()
    assert settings.tenant_id == "default"
    assert settings.retention.events_days == 30
    assert settings.telemetry.enabled is False


@pytest.mark.parametrize("tenant_id", ["Acme", "-leading", "has space", "a" * 65, ""])
def test_bad_tenant_ids_are_rejected_without_echoing_them(
    clean_env: pytest.MonkeyPatch, tenant_id: str
) -> None:
    clean_env.setenv("CARTO_TENANT_ID", tenant_id)
    with pytest.raises(ValidationError) as excinfo:
        ProductSettings()
    assert excinfo.value.errors()[0]["loc"] == ("tenant_id",)
    if tenant_id:
        assert tenant_id not in str(excinfo.value)


@pytest.mark.parametrize("tenant_id", ["default", "acme", "acme_1-prod", "0", "a" * 64])
def test_good_tenant_ids(clean_env: pytest.MonkeyPatch, tenant_id: str) -> None:
    assert ProductSettings(tenant_id=tenant_id).tenant_id == tenant_id


def test_unknown_init_field_is_rejected(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        unknown: dict[str, Any] = {"bogus": 1}
        ProductSettings(**unknown)


def test_settings_are_frozen(clean_env: pytest.MonkeyPatch) -> None:
    settings = ProductSettings()
    field = "tenant_id"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(settings, field, "other")
    nested_field = "events_days"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(settings.retention, nested_field, 10)
    assert settings.tenant_id == "default"


# ---------------------------------------------------------------------------------------------
# secret_ref (spec 14.3)
# ---------------------------------------------------------------------------------------------


class _ConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    secret_ref: SecretRef


GOOD_REFS = [
    "vault://carto/edge/splunk",
    "aws-sm://arn:aws:secretsmanager:us-east-1:123456789012:secret:carto/splunk",
    "azure-kv://carto-kv/splunk-token",
    "gcp-sm://projects/carto/secrets/splunk/versions/latest",
    "local://edge/splunk",
    "vault://x",
]

BAD_REFS = [
    "",
    "http://vault.internal/carto",
    "https://vault.internal/carto",
    "vault://",
    "aws-sm://",
    "vault:// carto/edge",
    "vault://carto edge",
    " vault://carto",
    "vault://carto\n",
    "vault://carto\t",
    "vault://a\x00b",  # control characters are not a path (spec 0.1 item 6)
    "vault://a\x7fb",
    "vault://a\x1bb",
    "vault://a\u200bb",  # zero-width space
    "vault://a\u00a0b",  # no-break space
    "VAULT://carto",
    "vault:/carto",
    "vault:carto",
    "carto/edge/splunk",
    "s3://bucket/key",
    "file:///etc/passwd",
    "local://" + "a" * MAX_SECRET_REF_LENGTH,
]


def test_schemes_are_the_spec_14_3_set() -> None:
    assert SECRET_REF_SCHEMES == ("vault", "aws-sm", "azure-kv", "gcp-sm", "local")


@pytest.mark.parametrize("ref", GOOD_REFS)
def test_good_secret_refs_are_accepted_unchanged(ref: str) -> None:
    assert validate_secret_ref(ref) == ref
    assert secret_ref_adapter.validate_python(ref) == ref
    assert _ConnectorConfig(secret_ref=ref).secret_ref == ref


@pytest.mark.parametrize("ref", BAD_REFS)
def test_bad_secret_refs_are_rejected(ref: str) -> None:
    with pytest.raises(ValueError, match="secret_ref"):
        validate_secret_ref(ref)
    with pytest.raises(ValidationError):
        secret_ref_adapter.validate_python(ref)
    with pytest.raises(ValidationError) as excinfo:
        _ConnectorConfig(secret_ref=ref)
    assert excinfo.value.errors()[0]["loc"] == ("secret_ref",)


def test_secret_ref_error_lists_the_allowed_schemes_and_not_the_value() -> None:
    with pytest.raises(ValueError, match="vault://, aws-sm://, azure-kv://, gcp-sm://, local://"):
        validate_secret_ref("hunter2-is-not-a-reference")
    pasted: dict[str, Any] = {"secret_ref": "hunter2-is-not-a-reference"}
    with pytest.raises(ValidationError) as excinfo:
        _ConnectorConfig(**pasted)
    assert "hunter2" not in str(excinfo.value)


def test_secret_ref_rejects_non_strings() -> None:
    with pytest.raises(ValidationError):
        secret_ref_adapter.validate_python(42)
    with pytest.raises(ValidationError):
        secret_ref_adapter.validate_python(None)
