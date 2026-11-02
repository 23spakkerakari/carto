"""carto_schema.inventory as core's backstop for secrets in an inventory (ADR 0069, spec 14.3):
``secret_ref`` is a ``<scheme>://<path>`` reference and never a literal secret, a config key
that names a secret is refused, a config string value is under the value-shape rule, and the
hostnames, schedules, paths and lists the edge legitimately sends still pass. No message echoes
the refused text."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from carto_schema.inventory import (
    MAX_CONFIG_LIST_ITEMS,
    SECRET_REF_SCHEMES,
    InventorySource,
    config_key_is_secret,
    validate_secret_ref,
)

SECRET = "hunter2"  # noqa: S105 - the planted value that must never be echoed


def _source(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "src_orders_db",
        "system_id": "sys_orders",
        "type": "sql",
        "config": {"host": "db.internal", "port": 5432, "tables": ["orders"]},
        "secret_ref": "vault://db/orders",
        "schedule": "every 30 s",
        **overrides,
    }


def _refused(data: dict[str, Any]) -> pytest.ExceptionInfo[ValidationError]:
    with pytest.raises(ValidationError) as info:
        InventorySource.model_validate(data)
    rendered = str(info.value) + json.dumps(info.value.errors(include_input=False), default=str)
    assert SECRET not in rendered
    return info


# -- secret_ref ----------------------------------------------------------------------------------


@pytest.mark.parametrize("ref", [SECRET, "AKIAIOSFODNN7EXAMPLE", "http://x/y", "vault://", " "])
def test_a_literal_secret_or_an_unknown_scheme_in_secret_ref_is_refused(ref: str) -> None:
    info = _refused(_source(secret_ref=ref))
    assert [tuple(error["loc"]) for error in info.value.errors()] == [("secret_ref",)]


@pytest.mark.parametrize(
    "ref", ["vault://db/orders", "aws-sm://prod/orders", "local:///run/secrets/orders", None]
)
def test_a_reference_is_accepted(ref: str | None) -> None:
    assert InventorySource.model_validate(_source(secret_ref=ref)).secret_ref == ref


def test_validate_secret_ref_names_the_schemes_not_the_input() -> None:
    with pytest.raises(ValueError, match="vault://") as info:
        validate_secret_ref(SECRET)
    assert SECRET not in str(info.value)
    assert all(f"{scheme}://" in str(info.value) for scheme in SECRET_REF_SCHEMES)


# -- config keys ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["password", "Password", "api_key", "x-api-key", "clientSecret", "session_token", "auth"],
)
def test_a_config_key_that_names_a_secret_is_refused_without_the_key(key: str) -> None:
    info = _refused(_source(config={key: "x"}))
    rendered = str(info.value) + json.dumps(info.value.errors(include_input=False), default=str)
    assert key not in rendered and key.lower() not in rendered
    assert [tuple(error["loc"]) for error in info.value.errors()] == [("config",)]


def test_config_key_is_secret_follows_the_logging_rule() -> None:
    assert all(
        config_key_is_secret(key)
        for key in ["password", "db_pass", "AuthToken", "private_key", "access-keys", "sig"]
    )
    assert not any(
        config_key_is_secret(key)
        for key in ["host", "secret_ref", "token_count", "password_present", "ssh_key_path", ""]
    )


# -- config values -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        {"url": f"postgres://carto:{SECRET}@db.internal/orders"},
        {"dsn": f"mssql://sa:{SECRET}@wms.internal:1433"},
        {"contact": "ops@example.com"},
        {"note": f"password={SECRET}"},
        {"hosts": ["a.internal", f"https://u:{SECRET}@b.internal"]},
        {"trace": "0123456789abcdef0123456789abcdef"},
    ],
)
def test_a_config_value_with_a_value_shape_is_refused(config: dict[str, Any]) -> None:
    info = _refused(_source(config=config))
    assert "a value" in str(info.value)


def test_a_config_list_is_capped_in_length() -> None:
    """ADR 0073: a config list had no length cap, so one inventory could carry an unbounded
    number of strings into ``sources.config_json``; 64 items is more than any connector lists."""
    assert MAX_CONFIG_LIST_ITEMS == 64
    full = [f"table_{index}" for index in range(MAX_CONFIG_LIST_ITEMS)]
    assert InventorySource.model_validate(_source(config={"tables": full})).config == {
        "tables": full
    }
    info = _refused(_source(config={"tables": [*full, "one_more"]}))
    locations = [tuple(error["loc"]) for error in info.value.errors()]
    assert locations, "refused"
    # pydantic reports one entry per member of the ConfigValue union it tried; every one of
    # them sits under the key, none under an item.
    assert all(location[:2] == ("config", "tables") for location in locations)
    assert all(not isinstance(part, int) for location in locations for part in location[2:])
    assert "one_more" not in str(info.value)


def test_the_configuration_the_edge_sends_still_passes() -> None:
    config = {
        "host": "db.internal",
        "port": 5432,
        "poll_seconds": 30,
        "ratio": 0.5,
        "verify_tls": True,
        "nothing": None,
        "tables": ["orders", "order_lines"],
        "path": "local:///var/spool/carto/upload",
        "dsn": "postgres://carto@db.internal/orders",
        "ssh_key_path": "/etc/carto/edge.key",
        "index": "main",
        "search": "index=orders sourcetype=wms earliest=-24h",
        "token_count": 3,
    }
    source = InventorySource.model_validate(_source(config=config))
    assert source.config == config
