"""``carto-schema``: export, check and print the JSON Schemas of the contract models (spec 7.1).

``export --out DIR`` writes one Draft 2020-12 schema per model, ``check --dir DIR`` fails when
the committed files drift from the models (CI runs it; see ``make schema-check``), ``example``
prints the spec 7.1 example event. ``main`` returns exit codes and never calls ``sys.exit``:
usage errors return 2 and ``--help`` returns 0, so it can be embedded in-process.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from carto_schema.bundle import BundleManifest, BundleSignature
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import IngestBatch, SourceHeartbeat
from carto_schema.inventory import EdgeInventory

JSON_SCHEMA_DIALECT: Final = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_TAG: Final = "v1"


@dataclass(frozen=True)
class SchemaSpec:
    """One exported schema: the model, its short name and its human title."""

    name: str
    model: type[BaseModel]
    title: str

    @property
    def file_name(self) -> str:
        """``<name>.v1.schema.json``."""
        return f"{self.name}.{SCHEMA_TAG}.schema.json"

    @property
    def schema_id(self) -> str:
        """``urn:carto:schema:<name>:v1``."""
        return f"urn:carto:schema:{self.name}:{SCHEMA_TAG}"


SCHEMAS: Final[tuple[SchemaSpec, ...]] = (
    SchemaSpec("bundle_manifest", BundleManifest, "carto offline bundle manifest (version 1)"),
    SchemaSpec("bundle_signature", BundleSignature, "carto offline bundle signature (version 1)"),
    SchemaSpec("canonical_event", CanonicalEvent, "carto canonical event (schema version 1)"),
    SchemaSpec("edge_inventory", EdgeInventory, "carto edge inventory (schema version 1)"),
    SchemaSpec("ingest_batch", IngestBatch, "carto ingest batch (schema version 1)"),
    SchemaSpec("source_heartbeat", SourceHeartbeat, "carto source heartbeat (schema version 1)"),
)


def build_schema(spec: SchemaSpec) -> dict[str, Any]:
    """Return the validation-mode JSON Schema of ``spec.model`` with dialect, id and title."""
    schema = spec.model.model_json_schema(mode="validation")
    schema["$schema"] = JSON_SCHEMA_DIALECT
    schema["$id"] = spec.schema_id
    schema["title"] = spec.title
    return schema


def render_schema(spec: SchemaSpec) -> str:
    """Return the schema file text: sorted keys, two-space indent, UTF-8 and a trailing LF."""
    return json.dumps(build_schema(spec), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_schemas(out_dir: Path) -> list[Path]:
    """Write every schema into ``out_dir`` (created if needed) and return the paths written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for spec in SCHEMAS:
        path = out_dir / spec.file_name
        path.write_text(render_schema(spec), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def check_schemas(schema_dir: Path) -> list[str]:
    """Compare the committed files with freshly rendered ones; return one line per problem."""
    problems: list[str] = []
    for spec in SCHEMAS:
        path = schema_dir / spec.file_name
        if not path.is_file():
            problems.append(f"missing: {path}")
        elif path.read_bytes() != render_schema(spec).encode("utf-8"):
            problems.append(f"differs: {path}")
    return problems


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carto-schema",
        description="Export and check the carto edge-to-core JSON Schemas (spec 7.1, 8.5, 12).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="write the JSON Schema files into a directory")
    export.add_argument("--out", type=Path, required=True, help="output directory")

    check = commands.add_parser("check", help="exit 1 when committed files differ or are missing")
    check.add_argument("--dir", type=Path, required=True, help="directory of committed schemas")

    commands.add_parser("example", help="print the spec 7.1 example event as JSON")
    return parser


def _emit(text: str) -> None:
    """Print to stdout without failing on characters its encoding cannot represent.

    On Windows a pipe (or a legacy console code page) may not cover a path with, say, CJK
    characters (ADR 0007); those print as backslash escapes instead of raising
    ``UnicodeEncodeError`` after the files were already written.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding))


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code instead of exiting."""
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        # argparse reports a usage error (2) or ``--help`` (0) by exiting; hand back the code.
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    if args.command == "export":
        try:
            written = export_schemas(args.out)
        except OSError as exc:
            print(f"carto-schema: cannot write {args.out}: {exc}", file=sys.stderr)
            return 1
        for path in written:
            _emit(f"wrote {path}")
        return 0
    if args.command == "check":
        try:
            problems = check_schemas(args.dir)
        except OSError as exc:
            print(f"carto-schema: cannot read {args.dir}: {exc}", file=sys.stderr)
            return 1
        if problems:
            for line in problems:
                print(line, file=sys.stderr)
            print("carto-schema: run `make schema` and commit the result", file=sys.stderr)
            return 1
        _emit(f"ok: {len(SCHEMAS)} schema files match the models in {args.dir}")
        return 0
    _emit(CanonicalEvent.example().model_dump_json(indent=2))
    return 0
