"""The carto-schema CLI, the committed schema files and the jsonschema cross-check (M0 plan)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import (  # type: ignore[import-untyped]
    ValidationError as SchemaValidationError,
)

from carto_schema.cli import (
    JSON_SCHEMA_DIALECT,
    SCHEMAS,
    build_schema,
    check_schemas,
    export_schemas,
    main,
    render_schema,
)
from carto_schema.event import CanonicalEvent

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "schemas"
FILE_NAMES = [
    "bundle_manifest.v1.schema.json",
    "bundle_signature.v1.schema.json",
    "canonical_event.v1.schema.json",
    "edge_inventory.v1.schema.json",
    "ingest_batch.v1.schema.json",
    "source_heartbeat.v1.schema.json",
]


def committed_schema(name: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(
        (SCHEMAS_DIR / f"{name}.v1.schema.json").read_text(encoding="utf-8")
    )
    return schema


def committed_validator(name: str) -> Any:
    # jsonschema checks ``format: date-time`` only when rfc3339-validator is installed, which
    # the dev group does not pin, so the timestamp declarations are asserted structurally below.
    schema = committed_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def example_json() -> dict[str, Any]:
    return CanonicalEvent.example().model_dump(mode="json")


# -- committed files ------------------------------------------------------------------------------


def test_committed_schemas_match_the_models() -> None:
    assert check_schemas(SCHEMAS_DIR) == []
    assert main(["check", "--dir", str(SCHEMAS_DIR)]) == 0


def test_committed_files_are_byte_identical_to_rendered_schemas() -> None:
    assert sorted(path.name for path in SCHEMAS_DIR.iterdir()) == FILE_NAMES
    for spec in SCHEMAS:
        committed = (SCHEMAS_DIR / spec.file_name).read_bytes()
        assert committed == render_schema(spec).encode("utf-8")
        assert b"\r" not in committed


def test_schema_names_ids_and_headers() -> None:
    assert [spec.file_name for spec in SCHEMAS] == FILE_NAMES
    assert [spec.schema_id for spec in SCHEMAS] == [
        "urn:carto:schema:bundle_manifest:v1",
        "urn:carto:schema:bundle_signature:v1",
        "urn:carto:schema:canonical_event:v1",
        "urn:carto:schema:edge_inventory:v1",
        "urn:carto:schema:ingest_batch:v1",
        "urn:carto:schema:source_heartbeat:v1",
    ]
    for spec in SCHEMAS:
        schema = build_schema(spec)
        assert (
            schema["$schema"]
            == JSON_SCHEMA_DIALECT
            == "https://json-schema.org/draft/2020-12/schema"
        )
        assert schema["$id"] == spec.schema_id
        assert schema["title"] == spec.title
        assert schema["additionalProperties"] is False
        Draft202012Validator.check_schema(schema)


def test_rendered_text_is_sorted_indented_lf_terminated() -> None:
    text = render_schema(SCHEMAS[0])
    assert text.endswith("}\n")
    assert "\r" not in text
    assert text.startswith('{\n  "$defs": {')
    loaded = json.loads(text)
    assert list(loaded) == sorted(loaded)
    assert loaded == build_schema(SCHEMAS[0])


# -- CLI exit codes -------------------------------------------------------------------------------


def test_export_then_check_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "nested" / "schemas"
    assert main(["export", "--out", str(out)]) == 0
    assert sorted(path.name for path in out.iterdir()) == FILE_NAMES
    assert capsys.readouterr().out.count("wrote ") == len(FILE_NAMES)
    for name in FILE_NAMES:
        assert b"\r\n" not in (out / name).read_bytes()
    assert main(["check", "--dir", str(out)]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith(f"ok: {len(FILE_NAMES)} schema files")
    assert captured.err == ""


def test_tampered_file_fails_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    export_schemas(tmp_path)
    target = tmp_path / FILE_NAMES[1]
    target.write_text(target.read_text(encoding="utf-8") + " ", encoding="utf-8", newline="\n")
    assert main(["check", "--dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"differs: {target}" in captured.err
    assert "missing:" not in captured.err
    assert captured.err.count("differs:") == 1


def test_crlf_file_fails_check(tmp_path: Path) -> None:
    export_schemas(tmp_path)
    target = tmp_path / FILE_NAMES[0]
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    assert check_schemas(tmp_path) == [f"differs: {target}"]


def test_missing_file_fails_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    export_schemas(tmp_path)
    (tmp_path / FILE_NAMES[2]).unlink()
    assert main(["check", "--dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"missing: {tmp_path / FILE_NAMES[2]}" in captured.err
    assert "differs:" not in captured.err


def test_missing_dir_fails_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "does-not-exist"
    assert main(["check", "--dir", str(missing)]) == 1
    captured = capsys.readouterr()
    assert captured.err.count("missing:") == len(FILE_NAMES)
    assert captured.out == ""


def test_export_onto_a_file_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    assert main(["export", "--out", str(blocker)]) == 1
    assert "cannot write" in capsys.readouterr().err


def test_example_command_prints_the_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["example"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == example_json()
    assert captured.out.endswith("}\n")


def test_usage_errors_return_2_without_exiting(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bogus"]) == 2
    assert "invalid choice" in capsys.readouterr().err
    assert main([]) == 2
    assert "required" in capsys.readouterr().err
    assert main(["export"]) == 2
    assert "--out" in capsys.readouterr().err
    assert main(["--help"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("usage: carto-schema")
    assert captured.err == ""


def test_paths_outside_the_stdout_encoding_do_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Windows pipe uses the ANSI code page (cp1252), which cannot encode this directory name.
    out = tmp_path / "\u65e5\u672c\u8a9e" / "schemas"
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252", newline="\n"))
    assert main(["export", "--out", str(out)]) == 0
    assert main(["check", "--dir", str(out)]) == 0
    sys.stdout.flush()
    text = raw.getvalue().decode("cp1252")
    assert text.count("wrote ") == len(FILE_NAMES)
    assert f"ok: {len(FILE_NAMES)} schema files" in text
    assert "\\u65e5\\u672c\\u8a9e" in text


# -- jsonschema cross-check -----------------------------------------------------------------------


def test_example_validates_against_the_committed_canonical_event_schema() -> None:
    committed_validator("canonical_event").validate(example_json())


def test_example_batch_and_heartbeat_validate_against_their_schemas() -> None:
    committed_validator("ingest_batch").validate(
        {
            "schema_version": "1",
            "tenant_id": "default",
            "source_id": "src_wms_db",
            "batch_id": "01J9ZK8X5Q8V3N6M2T4R7W1Y0B",
            "sent_at": "2026-10-06T21:12:10.000Z",
            "events": [example_json()],
        }
    )
    committed_validator("source_heartbeat").validate(
        {
            "schema_version": "1",
            "tenant_id": "default",
            "source_id": "src_wms_db",
            "sent_at": "2026-10-06T21:12:10.000Z",
            "status": "degraded",
            "last_success_at": None,
            "lag_seconds": 0,
            "error_count": 3,
            "buffer_depth": 0,
            "oldest_buffered_at": None,
            "message": "SFTP upload failed: Permission denied",
        }
    )


TIMESTAMP_PROPERTIES = [
    ("canonical_event", ("properties", "observed_at"), False),
    ("canonical_event", ("properties", "ingested_at"), False),
    ("ingest_batch", ("properties", "sent_at"), False),
    ("ingest_batch", ("$defs", "CanonicalEvent", "properties", "observed_at"), False),
    ("ingest_batch", ("$defs", "CanonicalEvent", "properties", "ingested_at"), False),
    ("source_heartbeat", ("properties", "sent_at"), False),
    ("source_heartbeat", ("properties", "last_success_at"), True),
    ("source_heartbeat", ("properties", "oldest_buffered_at"), True),
]


@pytest.mark.parametrize(("name", "path", "nullable"), TIMESTAMP_PROPERTIES)
def test_committed_schemas_declare_timestamps_as_date_time_strings(
    name: str, path: tuple[str, ...], nullable: bool
) -> None:
    node: Any = committed_schema(name)
    for key in path:
        node = node[key]
    declared = {key: value for key, value in node.items() if key != "title"}
    date_time = {"type": "string", "format": "date-time"}
    assert declared == ({"anyOf": [date_time, {"type": "null"}]} if nullable else date_time)


def bad_examples() -> list[tuple[str, dict[str, Any]]]:
    base = example_json()
    first = base["identifiers"][0]
    too_many = [
        {"field": f"f{i}", "form": "raw", "token": f"t1.{i:022d}", "shape": "9", "len": 1}
        for i in range(65)
    ]
    return [
        ("unknown key", {**base, "raw_message": "x"}),
        ("bad token", {**base, "actor": {"token": "t1.short", "kind": "human"}}),
        ("bad event id", {**base, "event_id": "not-a-ulid"}),
        ("bad form", {**base, "identifiers": [{**first, "form": "digits.3"}]}),
        ("len is a bool", {**base, "identifiers": [{**first, "len": True}]}),
        ("len is a string", {**base, "identifiers": [{**first, "len": "4"}]}),
        ("len is a float", {**base, "identifiers": [{**first, "len": 4.5}]}),
        (
            "entities_masked bool",
            {**base, "redaction": {**base["redaction"], "entities_masked": True}},
        ),
        ("65 identifiers", {**base, "identifiers": too_many}),
        ("long attribute", {**base, "attributes": {"status": "x" * 257}}),
        ("empty attribute key", {**base, "attributes": {"": "x"}}),
        ("bad kind", {**base, "kind": "insert"}),
        ("bad schema version", {**base, "schema_version": "2"}),
        ("missing redaction", {k: v for k, v in base.items() if k != "redaction"}),
    ]


@pytest.mark.parametrize(("label", "payload"), bad_examples(), ids=lambda item: str(item)[:24])
def test_committed_schema_rejects_what_the_model_rejects(
    label: str, payload: dict[str, Any]
) -> None:
    with pytest.raises(SchemaValidationError):
        committed_validator("canonical_event").validate(payload)
    with pytest.raises(ValueError, match=r"validation error"):
        CanonicalEvent.model_validate(payload)
