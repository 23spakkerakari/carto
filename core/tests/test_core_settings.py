"""CoreSettings (spec Section 20, 14.4, 14.10): TLS by default, passwords only from files, the
listener caps, and ``CARTO_`` environment nesting."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from carto_core.settings import (
    MIB,
    ClickHouseSettings,
    CoreConfigError,
    CoreSettings,
    IngestListenerSettings,
    PostgresSettings,
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own ``CARTO_`` variables must not leak into these assertions."""
    for name in list(os.environ):
        if name.startswith("CARTO_"):
            monkeypatch.delenv(name)


def test_defaults_require_tls_and_listen_on_loopback() -> None:
    settings = CoreSettings()
    assert settings.tenant_id == "default"
    assert settings.clickhouse.url == "https://127.0.0.1:8443"
    assert settings.clickhouse.secure is True
    assert settings.clickhouse.database == "carto"
    assert settings.clickhouse.user == "carto_ingest"
    assert settings.clickhouse.password_file is None
    assert settings.postgres.host == "127.0.0.1"
    assert settings.postgres.port == 5432
    # verify-full: encryption without checking the server name is no defence against a
    # man in the middle; only the Compose stack sets "disable" on its internal network.
    assert settings.postgres.sslmode == "verify-full"
    assert settings.postgres.user == "carto_ingest"
    assert settings.ingest.host == "127.0.0.1"
    assert settings.ingest.port == 8443
    assert settings.ingest.require_client_cert is True
    assert settings.ingest.max_body_bytes == 5 * MIB
    assert settings.ingest.max_decompressed_bytes == 16 * MIB
    assert settings.ingest.max_inflight_batches == 8
    assert settings.retention.events_days == 30
    assert settings.log_level == "info"
    assert settings.log_json is True


def test_environment_nesting_with_the_carto_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARTO_CLICKHOUSE__URL", "http://clickhouse:8123")
    monkeypatch.setenv("CARTO_CLICKHOUSE__SECURE", "false")
    monkeypatch.setenv("CARTO_CLICKHOUSE__DATABASE", "carto_test")
    monkeypatch.setenv("CARTO_POSTGRES__HOST", "postgres")
    monkeypatch.setenv("CARTO_POSTGRES__SSLMODE", "verify-full")
    monkeypatch.setenv("CARTO_RETENTION__EVENTS_DAYS", "45")
    monkeypatch.setenv("CARTO_INGEST__PORT", "9443")
    monkeypatch.setenv("CARTO_LOG_LEVEL", "DEBUG")
    settings = CoreSettings()
    assert settings.clickhouse.url == "http://clickhouse:8123"
    assert settings.clickhouse.host == "clickhouse"
    assert settings.clickhouse.port == 8123
    assert settings.clickhouse.database == "carto_test"
    assert settings.postgres.host == "postgres"
    assert settings.postgres.sslmode == "verify-full"
    assert settings.retention.events_days == 45
    assert settings.ingest.port == 9443
    assert settings.log_level == "debug"


@pytest.mark.parametrize("section", ["CLICKHOUSE", "POSTGRES"])
def test_a_password_in_the_environment_is_rejected_and_never_echoed(
    section: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(f"CARTO_{section}__PASSWORD", "hunter2-pasted-by-mistake")
    with pytest.raises(ValidationError) as info:
        CoreSettings()
    assert "hunter2" not in str(info.value)
    assert "password" in str(info.value)


def test_passwords_are_read_from_files_and_never_appear_in_repr(tmp_path: Path) -> None:
    secret_file = tmp_path / "db-password"
    secret_file.write_text("s3cret-value\n", encoding="utf-8")
    clickhouse = ClickHouseSettings(
        url="http://clickhouse:8123", secure=False, password_file=secret_file
    )
    postgres = PostgresSettings(password_file=secret_file)
    assert clickhouse.read_password() == "s3cret-value"
    assert postgres.read_password() == "s3cret-value"
    assert "s3cret" not in repr(clickhouse)
    assert "s3cret" not in repr(postgres)
    assert ClickHouseSettings().read_password() == ""
    assert PostgresSettings().read_password() == ""


def test_password_file_problems_are_clear_errors(tmp_path: Path) -> None:
    missing = PostgresSettings(password_file=tmp_path / "absent")
    with pytest.raises(CoreConfigError, match="cannot read"):
        missing.read_password()
    huge = tmp_path / "huge"
    huge.write_bytes(b"x" * 5000)
    with pytest.raises(CoreConfigError, match="larger than"):
        PostgresSettings(password_file=huge).read_password()
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(CoreConfigError, match="not UTF-8"):
        PostgresSettings(password_file=binary).read_password()


@pytest.mark.parametrize(
    "url",
    [
        "clickhouse:8123",
        "ftp://clickhouse:8123",
        "http://user:pw@clickhouse:8123",
        "http://clickhouse:8123/db",
        "http://clickhouse",
        "https://clickhouse:8443?x=1",
        "https://clickhouse:8443#frag",
        "https://:8443",
        "https://clickhouse:0",
        "https://clickhouse:70000",
    ],
)
def test_clickhouse_url_must_be_scheme_host_port(url: str) -> None:
    with pytest.raises(ValidationError, match=r"clickhouse\.url"):
        ClickHouseSettings(url=url, secure=url.startswith("https"))


def test_clickhouse_url_trailing_slash_is_accepted_and_normalized() -> None:
    settings = ClickHouseSettings(url="https://clickhouse.internal:8443/")
    assert settings.url == "https://clickhouse.internal:8443"
    assert settings.host == "clickhouse.internal"
    assert settings.port == 8443


def test_clickhouse_secure_must_agree_with_the_scheme() -> None:
    with pytest.raises(ValidationError, match="secure"):
        ClickHouseSettings(url="http://clickhouse:8123")
    with pytest.raises(ValidationError, match="secure"):
        ClickHouseSettings(url="https://clickhouse:8443", secure=False)
    assert ClickHouseSettings(url="http://clickhouse:8123", secure=False).secure is False


def test_clickhouse_timeout_and_database_are_bounded() -> None:
    with pytest.raises(ValidationError):
        ClickHouseSettings(timeout_seconds=0)
    with pytest.raises(ValidationError):
        ClickHouseSettings(database="carto;DROP")


@pytest.mark.parametrize("sslmode", ["prefer", "allow", "", "REQUIRE"])
def test_postgres_sslmode_choices_exclude_the_opportunistic_modes(sslmode: str) -> None:
    with pytest.raises(ValidationError):
        PostgresSettings(sslmode=sslmode)  # type: ignore[arg-type]


def test_postgres_disable_is_allowed_for_local_development_only() -> None:
    assert PostgresSettings(sslmode="disable").sslmode == "disable"
    assert PostgresSettings(sslmode="verify-full", ca_file=Path("ca.pem")).ca_file == Path("ca.pem")


def test_listener_caps_are_bounded_and_consistent() -> None:
    with pytest.raises(ValidationError):
        IngestListenerSettings(max_body_bytes=128 * MIB)
    with pytest.raises(ValidationError):
        IngestListenerSettings(max_body_bytes=1024)
    with pytest.raises(ValidationError, match="max_decompressed_bytes"):
        IngestListenerSettings(max_body_bytes=5 * MIB, max_decompressed_bytes=MIB)
    with pytest.raises(ValidationError):
        IngestListenerSettings(port=0)


def test_log_level_is_case_insensitive_and_bounded() -> None:
    assert CoreSettings.model_validate({"log_level": "Warning"}).log_level == "warning"
    with pytest.raises(ValidationError):
        CoreSettings.model_validate({"log_level": "loud"})


def test_settings_are_frozen_and_reject_unknown_keys() -> None:
    settings = CoreSettings()
    with pytest.raises(ValidationError):
        settings.log_level = "debug"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        CoreSettings(bogus=1)  # type: ignore[call-arg]
