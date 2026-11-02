"""carto_edge.connectors.registry: every spec 8.1 type builds from its Appendix A config, unknown
or non-pollable types and invalid config blocks raise InvalidConfigError without echoing input."""

from __future__ import annotations

from typing import Any

import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors.base import (
    ConnectorContext,
    InvalidConfigError,
    ReadConnector,
    ResolvedHost,
)
from carto_edge.connectors.registry import (
    CONNECTOR_TYPES,
    build_connector,
    generalize_name,
    register,
)
from carto_edge.connectors.sftp import SftpConnector
from carto_edge.connectors.splunk import SplunkConnector
from carto_edge.connectors.sql import SqlConnector
from carto_edge.connectors.upload import UploadConnector
from carto_edge.connectors.webhook import WebhookConnector


class Secrets:
    def resolve(self, secret_ref: str) -> str:
        return "value"


class Network:
    def resolve(self, host: str, port: int) -> ResolvedHost:
        return ResolvedHost(host=host, address="10.20.0.1", port=port)


CONTEXT = ConnectorContext(secrets=Secrets(), network=Network(), tenant_id="acme")

APPENDIX_A: dict[str, tuple[dict[str, Any], str | None, type[Any]]] = {
    "splunk": (
        {
            "base_url": "https://splunk.internal.example:8089",
            "search": "search index=orders sourcetype=order_svc",
            "window_minutes": 5,
            "overlap_minutes": 2,
            "max_concurrency": 2,
        },
        "vault://kv/carto/splunk-orders-token",
        SplunkConnector,
    ),
    "sql": (
        {
            "dialect": "postgresql",
            "host": "wms-replica.internal.example",
            "port": 5432,
            "database": "wms",
            "sslmode": "verify-full",
            "poll_seconds": 60,
            "queries": [
                {
                    "name": "purchase_orders",
                    "sql": (
                        "SELECT id, po_num, order_ref, status, warehouse_code, created_by, "
                        "updated_at FROM purchase_orders WHERE updated_at > :watermark "
                        "AND updated_at <= :upper ORDER BY updated_at LIMIT :batch"
                    ),
                    "watermark_column": "updated_at",
                    "timestamp_column": "updated_at",
                    "actor_column": "created_by",
                }
            ],
        },
        "aws-sm://carto/wms-readonly",
        SqlConnector,
    ),
    "sftp": (
        {
            "host": "sftp.internal.example",
            "port": 22,
            "username": "carto",
            "host_key_sha256": "SHA256:" + "A" * 43,
            "directories": ["/outbound/shipping"],
            "poll_seconds": 60,
            "filename_patterns": ["SHIP_*.csv"],
        },
        "vault://kv/carto/sftp-readonly",
        SftpConnector,
    ),
    "upload": ({"paths": ["orders/*.log"], "base_dir": "/srv/export"}, None, UploadConnector),
    "webhook": ({"event_type_field": "type"}, "local://hook-secret", WebhookConnector),
}


@pytest.mark.parametrize("type_name", sorted(APPENDIX_A))
def test_builds_every_connector_type(type_name: str) -> None:
    config, secret_ref, expected = APPENDIX_A[type_name]
    source = SourceConfig(
        id=f"src_{type_name}",
        system="sys_orders",
        type=SourceType(type_name),
        config=config,
        secret_ref=secret_ref,
    )
    connector = build_connector(source, CONTEXT)
    assert isinstance(connector, expected)
    assert isinstance(connector, ReadConnector)
    assert connector.type == type_name
    assert connector.source is source
    assert not hasattr(connector, "write")


def test_registry_lists_the_five_pollable_types() -> None:
    build_connector(
        SourceConfig(id="s", system="x", type=SourceType.UPLOAD, config={"paths": ["a.log"]}),
        CONTEXT,
    )
    assert set(CONNECTOR_TYPES) == {"upload", "splunk", "sql", "sftp", "webhook"}


def test_otlp_has_no_connector() -> None:
    source = SourceConfig(id="src_otel", system="sys_orders", type=SourceType.OTLP)
    with pytest.raises(InvalidConfigError, match="no connector"):
        build_connector(source, CONTEXT)


def test_invalid_config_names_the_field_but_not_the_input() -> None:
    source = SourceConfig(
        id="src_up",
        system="sys_orders",
        type=SourceType.UPLOAD,
        config={"paths": ["x.log"], "kind": "video-SENSITIVE-INPUT-9f8e"},
    )
    with pytest.raises(InvalidConfigError) as info:
        build_connector(source, CONTEXT)
    message = str(info.value)
    assert "src_up" in message
    assert "kind" in message
    assert "SENSITIVE-INPUT" not in message


def test_register_rejects_unknown_type_and_duplicates() -> None:
    with pytest.raises(ValueError, match="unknown connector type"):
        register("bogus")
    with pytest.raises(ValueError, match="already registered"):
        register("upload")(WebhookConnector)
    assert register("upload")(UploadConnector) is UploadConnector  # same factory is idempotent


def test_generalize_name() -> None:
    assert generalize_name("SHIP_20261006_2130.csv") == "SHIP_*_*.csv"
    assert generalize_name("CLAIMS_20261006_2130.edi") == "CLAIMS_*_*.edi"
    assert generalize_name("report.txt") == "report.txt"
