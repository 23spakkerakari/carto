"""carto_schema.bundle: the offline bundle contract (spec 8.1.1, plan M1 "Bundle format")."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from carto_schema.bundle import (
    BUNDLE_VERSION,
    DATA_FILES,
    EVENTS_FILE,
    FIELDS_FILE,
    LOCATOR_MAP_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
    MAX_SAMPLE_VALUES,
    SIGNATURE_FILE,
    TEMPLATES_FILE,
    BundleCounts,
    BundleField,
    BundleManifest,
    BundleSignature,
    BundleSourceSummary,
    BundleTemplate,
    FileDigest,
)

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
ULID = "01J9ZK8X5Q8V3N6M2T4R7W1Y0A"
SHA = "a" * 64


def manifest_dict(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "bundle_version": "1",
        "schema_version": "1",
        "bundle_id": ULID,
        "tenant_id": "default",
        "created_at": "2026-10-08T09:00:00.000Z",
        "producer": "carto-edge analyze 0.1.0",
        "key_versions": [1],
        "policy_version": "abc123",
        "sources": [
            {
                "source_id": "src_orders_log",
                "system_id": "sys_orders",
                "connector_type": "upload",
                "records_read": 10,
                "events_written": 9,
                "records_dropped": 1,
                "parse_errors": 0,
            }
        ],
        "counts": {
            "records_read": 10,
            "events": 9,
            "records_dropped": 1,
            "parse_errors": 0,
            "identifiers": 20,
            "fields_kept": 2,
            "fields_tokenized": 3,
            "fields_dropped": 4,
        },
        "files": {name: {"sha256": SHA, "bytes": 1} for name in DATA_FILES},
    }
    data.update(overrides)
    return data


def test_file_names_and_data_files() -> None:
    assert MANIFEST_FILE == "manifest.json"
    assert SIGNATURE_FILE == "signature.json"
    assert set(DATA_FILES) == {EVENTS_FILE, FIELDS_FILE, TEMPLATES_FILE, MANIFEST_MD_FILE}
    assert MANIFEST_FILE not in DATA_FILES and SIGNATURE_FILE not in DATA_FILES
    assert LOCATOR_MAP_FILE == "locator_map.ndjson"
    assert BUNDLE_VERSION == "1"


def test_manifest_round_trip_preserves_field_order() -> None:
    manifest = BundleManifest.model_validate(manifest_dict())
    dumped = json.loads(manifest.model_dump_json())
    assert list(dumped)[:5] == [
        "bundle_version",
        "schema_version",
        "bundle_id",
        "tenant_id",
        "created_at",
    ]
    assert BundleManifest.model_validate(dumped) == manifest
    assert dumped["created_at"] == "2026-10-08T09:00:00.000Z"


def test_out_of_range_counts_default_to_zero_so_older_manifests_still_load() -> None:
    """ADR 0036 added the counts after bundles of version 1 were written."""
    data = manifest_dict()
    assert "events_too_old" not in data["counts"]
    counts = BundleManifest.model_validate(data).counts
    assert (counts.events_too_old, counts.events_future) == (0, 0)
    data["counts"] |= {"events_too_old": 3, "events_future": 1}
    counts = BundleManifest.model_validate(data).counts
    assert (counts.events_too_old, counts.events_future) == (3, 1)
    data["counts"]["events_future"] = -1
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(data)


@pytest.mark.parametrize(
    "overrides",
    [
        {"bundle_version": "2"},
        {"bundle_id": "not-a-ulid"},
        {"key_versions": []},
        {"key_versions": [0]},
        {"files": {EVENTS_FILE: {"sha256": "xyz", "bytes": 1}}},
        {"files": {EVENTS_FILE: {"sha256": SHA, "bytes": -1}}},
        {"extra": "no"},
    ],
)
def test_manifest_rejects_invalid_input(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BundleManifest.model_validate(manifest_dict(**overrides))


def test_bundle_field_sample_values_are_bounded() -> None:
    base: dict[str, Any] = {
        "field_ref": "sys_orders/tpl_abc/status",
        "system_id": "sys_orders",
        "template_id": "tpl_abc",
        "path": "status",
        "field_class": "low_card_attribute",
        "policy": "keep",
        "count": 100,
        "distinct_estimate": 3,
        "null_rate": 0.0,
        "top_shapes": [{"shape": "AAAAAAA", "share": 1.0}],
        "sample_values": ["CREATED", "RELEASED"],
    }
    field = BundleField.model_validate(base)
    assert field.forms == [] and field.pinned is False
    with pytest.raises(ValidationError):
        BundleField.model_validate({**base, "sample_values": ["x"] * (MAX_SAMPLE_VALUES + 1)})
    with pytest.raises(ValidationError):
        BundleField.model_validate({**base, "field_class": "bogus"})
    with pytest.raises(ValidationError):
        BundleField.model_validate({**base, "top_shapes": [{"shape": "9999", "share": 1.5}]})


def test_bundle_template_and_source_summary() -> None:
    template = BundleTemplate.model_validate(
        {
            "template_id": "tpl_4f1c9a",
            "system_id": "sys_warehouse",
            "template_text": "PO export finished: <*> POs written to <*>",
            "kind": "log",
            "count": 14,
            "first_seen": NOW,
            "last_seen": NOW,
        }
    )
    assert template.kind == "log"
    summary = BundleSourceSummary.model_validate(manifest_dict()["sources"][0])
    assert summary.first_observed_at is None
    with pytest.raises(ValidationError):
        BundleCounts.model_validate({"records_read": "10"})
    with pytest.raises(ValidationError):
        FileDigest.model_validate({"sha256": SHA, "bytes": True})


def test_signature_shape() -> None:
    signature = BundleSignature.model_validate(
        {
            "algorithm": "ed25519",
            "key_id": "0123456789abcdef",
            "public_key": "A" * 43,
            "signature": "B" * 86,
            "signed_at": NOW,
            "manifest_sha256": SHA,
        }
    )
    assert signature.algorithm == "ed25519"
    with pytest.raises(ValidationError):
        BundleSignature.model_validate(
            {
                "algorithm": "rsa",
                "key_id": "0123456789abcdef",
                "public_key": "A" * 43,
                "signature": "B" * 86,
                "signed_at": NOW,
                "manifest_sha256": SHA,
            }
        )
