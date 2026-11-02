"""The offline bundle writer (spec 4.1, 8.1.1; ADR 0006, 0022, 0024; plan M1 "Bundle format").

:class:`BundleWriter` turns the analyzer's pass-2 results into ``<name>.carto/``:

- ``events.ndjson.zst``: one :class:`~carto_schema.event.CanonicalEvent` JSON line per emitted
  event, streamed through one zstd frame (level 3) so memory stays flat for any input size;
- ``fields.json`` and ``templates.json``: ``list[BundleField]`` and ``list[BundleTemplate]``;
- ``MANIFEST.md``: the customer's review copy (spec 4.1 step 3): run metadata, a sources table,
  the fields kept in clear with their sample values, the fields tokenized (forms and shapes
  only), the fields dropped (class and reason) and the templates;
- ``manifest.json``: :class:`~carto_schema.bundle.BundleManifest` with the sha256 and size of
  exactly :data:`~carto_schema.bundle.DATA_FILES`;
- ``signature.json``: Ed25519 over the exact bytes of ``manifest.json`` with a key generated
  for this run and discarded afterwards (ADR 0022: integrity, not origin).

Nothing else is ever written into the directory, so ``carto_core.bundle.verify_bundle`` accepts
it. The eval-only locator map (ADR 0006) goes to ``<out>.locator_map.ndjson`` in the parent of
the bundle directory (ADR 0024, :func:`default_locator_map_path`); a path inside the bundle is
refused.

What may appear in clear (spec 2.3 invariant 2, 8.4): event content is whatever the pipeline
emitted (attributes passed the hygiene check, identifiers are tokens); ``sample_values`` of
``keep`` fields, which the classifier already hygiene-checked, are the only other values. A
field whose policy is not ``keep`` never shows a sample, even if one was handed in.
``templates.json`` describes the events of this bundle: one entry per template id the events
use, with the (redacted) text the events carry, and counts and first/last seen taken from those
events, not the template store's lifetime totals (which persist across runs and count pass 1
as well).

The writer logs counts and ids only, never a value (spec 2.3 invariant 7). A run that fails
before :meth:`BundleWriter.finish` can :meth:`BundleWriter.abort`, which closes every handle and
removes the files this writer created, so a re-run is not refused for a half-written bundle.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Final, Self

import zstandard
from pydantic import TypeAdapter

from carto_common.crypto import SigningKey, b64url_encode
from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_edge.pipeline.model import PipelineResult
from carto_edge.pipeline.parser import (
    REASON_CSV_HEADER,
    REASON_EMPTY,
    REASON_INTERNAL,
    REASON_TOO_LARGE,
    REASON_UNPARSEABLE,
)
from carto_edge.pipeline.pipeline import OUT_OF_RANGE_FUTURE, REASON_TOO_OLD
from carto_edge.pipeline.templates import TemplateRecord
from carto_schema.bundle import (
    BUNDLE_VERSION,
    DATA_FILES,
    EVENTS_FILE,
    FIELDS_FILE,
    LOCATOR_MAP_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
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
from carto_schema.event import SCHEMA_VERSION, EventKind

__all__ = [
    "BUNDLE_FILES",
    "PARSE_ERROR_REASONS",
    "ZSTD_LEVEL",
    "BundleWriteError",
    "BundleWriter",
    "SourceTally",
    "default_locator_map_path",
    "render_manifest_md",
]

ZSTD_LEVEL: Final = 3
PARSE_ERROR_REASONS: Final = frozenset(
    {REASON_UNPARSEABLE, REASON_CSV_HEADER, REASON_INTERNAL, REASON_TOO_LARGE, REASON_EMPTY}
)
"""Drop reasons that come from the parser; ``parse_errors`` in the manifest counts these."""
BUNDLE_FILES: Final = frozenset({MANIFEST_FILE, SIGNATURE_FILE, *DATA_FILES})
"""The only entries a bundle directory may hold."""
_UNKNOWN_REASON: Final = "unknown"

_FIELDS_ADAPTER: Final = TypeAdapter(list[BundleField])
_TEMPLATES_ADAPTER: Final = TypeAdapter(list[BundleTemplate])

log = get_logger(component="carto_edge.bundle")


class BundleWriteError(Exception):
    """The bundle cannot be written as asked; the message names paths and ids, never values."""


def default_locator_map_path(out_dir: Path) -> Path:
    """``<out>.locator_map.ndjson`` in the parent of the bundle directory (ADR 0024)."""
    resolved = out_dir.resolve()
    return resolved.parent / f"{resolved.name}.{LOCATOR_MAP_FILE}"


@dataclass(slots=True)
class SourceTally:
    """Per-source counts of one run; ``dropped`` maps reason codes to counts."""

    records_read: int = 0
    events_written: int = 0
    records_dropped: int = 0
    parse_errors: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None

    def observe(self, at: datetime) -> None:
        if self.first_observed_at is None or at < self.first_observed_at:
            self.first_observed_at = at
        if self.last_observed_at is None or at > self.last_observed_at:
            self.last_observed_at = at


@dataclass(slots=True)
class _TemplateTally:
    system_id: str
    kind: EventKind
    text: str
    count: int
    first_seen: datetime
    last_seen: datetime


def _is_inside(path: Path, directory: Path) -> bool:
    return path == directory or path.is_relative_to(directory)


class BundleWriter:
    """Stream pass-2 results into a bundle directory; :meth:`finish` seals it."""

    def __init__(
        self,
        out_dir: Path,
        *,
        tenant_id: str,
        producer: str,
        key_versions: Sequence[int],
        policy_version: str,
        created_at: datetime,
        locator_map: Path | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if created_at.tzinfo is None:
            msg = "created_at must be timezone-aware"
            raise BundleWriteError(msg)
        resolved = out_dir.resolve()
        if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
            msg = f"output {resolved} exists and is not an empty directory"
            raise BundleWriteError(msg)
        locator_path = locator_map.resolve() if locator_map is not None else None
        if locator_path is not None and _is_inside(locator_path, resolved):
            msg = "the locator map must not be inside the bundle directory (ADR 0024)"
            raise BundleWriteError(msg)
        if locator_path is not None and locator_path.is_dir():
            msg = f"locator map path {locator_path} is a directory"
            raise BundleWriteError(msg)
        self._out_dir = resolved
        self._tenant_id = tenant_id
        self._producer = producer
        self._key_versions = list(key_versions)
        self._policy_version = policy_version
        self._created_at = created_at.astimezone(UTC)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._locator_path = locator_path
        self._sources: dict[str, SourceTally] = {}
        self._templates: dict[str, _TemplateTally] = {}
        self._identifiers = 0
        self._out_of_range: Counter[str] = Counter()
        self._first: datetime | None = None
        self._last: datetime | None = None
        self._finished = False
        self._closed = False
        self._created_dir = not resolved.exists()
        resolved.mkdir(parents=True, exist_ok=True)
        self._written: list[Path] = []
        self._raw: IO[bytes] | None = None
        self._events: zstandard.ZstdCompressionWriter | None = None
        self._locator: IO[str] | None = None
        try:
            events_path = resolved / EVENTS_FILE
            self._raw = events_path.open("xb")
            self._written.append(events_path)
            self._events = zstandard.ZstdCompressor(level=ZSTD_LEVEL).stream_writer(
                self._raw, closefd=False
            )
            if locator_path is not None:
                locator_path.parent.mkdir(parents=True, exist_ok=True)
                self._locator = locator_path.open("w", encoding="utf-8", newline="\n")
        except OSError as exc:
            self.abort()
            msg = f"cannot create the bundle files in {resolved}"
            raise BundleWriteError(msg) from exc

    def __repr__(self) -> str:
        return f"BundleWriter(out_dir={str(self._out_dir)!r}, finished={self._finished})"

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None and not self._finished:
            self.abort()
        else:
            self.close()

    # -- properties -----------------------------------------------------------------------------

    @property
    def out_dir(self) -> Path:
        return self._out_dir

    @property
    def locator_map(self) -> Path | None:
        return self._locator_path

    @property
    def events_written(self) -> int:
        return sum(tally.events_written for tally in self._sources.values())

    @property
    def records_read(self) -> int:
        return sum(tally.records_read for tally in self._sources.values())

    @property
    def identifiers(self) -> int:
        return self._identifiers

    def dropped_by_reason(self) -> dict[str, int]:
        """Drops of every source by reason code, most frequent first."""
        total: Counter[str] = Counter()
        for tally in self._sources.values():
            total.update(tally.dropped)
        return dict(total.most_common())

    def tally(self, source_id: str) -> SourceTally:
        """The counts of one source (created empty on first use)."""
        tally = self._sources.get(source_id)
        if tally is None:
            tally = SourceTally()
            self._sources[source_id] = tally
        return tally

    # -- streaming ------------------------------------------------------------------------------

    def add(self, result: PipelineResult, source_id: str) -> None:
        """Count one pass-2 result; write its event (and its locator line in eval mode)."""
        if self._finished or self._events is None:
            msg = "the bundle is already finished or closed"
            raise BundleWriteError(msg)
        event = result.event
        if event is not None and (
            event.tenant_id != self._tenant_id or event.source_id != source_id
        ):
            msg = f"an event of source {source_id} does not belong to this bundle's tenant/source"
            raise BundleWriteError(msg)
        tally = self.tally(source_id)
        tally.records_read += 1
        if result.out_of_range is not None:
            self._out_of_range[result.out_of_range] += 1
        if event is None:
            reason = result.dropped_reason or _UNKNOWN_REASON
            tally.records_dropped += 1
            tally.dropped[reason] += 1
            if reason in PARSE_ERROR_REASONS:
                tally.parse_errors += 1
            return
        self._events.write(event.model_dump_json().encode("utf-8") + b"\n")
        tally.events_written += 1
        tally.observe(event.observed_at)
        if self._first is None or event.observed_at < self._first:
            self._first = event.observed_at
        if self._last is None or event.observed_at > self._last:
            self._last = event.observed_at
        self._identifiers += len(event.identifiers)
        self._count_template(
            event.template_id, event.system_id, event.kind, event.template_text, event.observed_at
        )
        if self._locator is not None:
            line = {"key": f"{source_id}:{result.locator}", "event_id": event.event_id}
            self._locator.write(json.dumps(line, ensure_ascii=False) + "\n")

    def _count_template(
        self, template_id: str, system_id: str, kind: EventKind, text: str, at: datetime
    ) -> None:
        entry = self._templates.get(template_id)
        if entry is None:
            self._templates[template_id] = _TemplateTally(system_id, kind, text, 1, at, at)
            return
        entry.count += 1
        entry.first_seen = min(entry.first_seen, at)
        entry.last_seen = max(entry.last_seen, at)

    def source_summary(
        self, source_id: str, *, system_id: str, connector_type: str
    ) -> BundleSourceSummary:
        tally = self.tally(source_id)
        return BundleSourceSummary(
            source_id=source_id,
            system_id=system_id,
            connector_type=connector_type,
            records_read=tally.records_read,
            events_written=tally.events_written,
            records_dropped=tally.records_dropped,
            parse_errors=tally.parse_errors,
            first_observed_at=tally.first_observed_at,
            last_observed_at=tally.last_observed_at,
        )

    # -- sealing --------------------------------------------------------------------------------

    def finish(
        self,
        fields: Iterable[BundleField],
        templates: Iterable[TemplateRecord],
        *,
        connector_types: Mapping[str, str],
        system_of: Mapping[str, str],
        notes: Sequence[str] | None = None,
    ) -> BundleManifest:
        """Close the events stream and write the field, template, manifest and signature files.

        ``connector_types`` and ``system_of`` map every source id to its connector type and
        system; the manifest lists the sources in ``connector_types`` order (sources with no
        record still appear), then any other source that produced records.
        """
        if self._finished or self._events is None:
            msg = "the bundle is already finished or closed"
            raise BundleWriteError(msg)
        self._close_streams()
        source_ids = list(connector_types)
        source_ids.extend(sorted(set(self._sources) - set(connector_types)))
        missing = [source_id for source_id in source_ids if source_id not in system_of]
        if missing:
            msg = f"no system id for source(s) {', '.join(missing)}"
            raise BundleWriteError(msg)
        summaries = [
            self.source_summary(
                source_id,
                system_id=system_of[source_id],
                connector_type=connector_types.get(source_id, "upload"),
            )
            for source_id in source_ids
        ]
        field_list = sorted((_reviewable(item) for item in fields), key=lambda f: f.field_ref)
        template_list = self._bundle_templates(templates)
        bundle_id = new_ulid()
        counts = BundleCounts(
            records_read=sum(s.records_read for s in summaries),
            events=sum(s.events_written for s in summaries),
            records_dropped=sum(s.records_dropped for s in summaries),
            parse_errors=sum(s.parse_errors for s in summaries),
            identifiers=self._identifiers,
            fields_kept=sum(1 for f in field_list if f.policy == "keep"),
            fields_tokenized=sum(1 for f in field_list if f.policy == "tokenize"),
            fields_dropped=sum(1 for f in field_list if f.policy == "drop"),
            events_too_old=self._out_of_range[REASON_TOO_OLD],
            events_future=self._out_of_range[OUT_OF_RANGE_FUTURE],
        )
        draft = BundleManifest(
            bundle_version=BUNDLE_VERSION,
            schema_version=SCHEMA_VERSION,
            bundle_id=bundle_id,
            tenant_id=self._tenant_id,
            created_at=self._created_at,
            producer=self._producer,
            key_versions=self._key_versions,
            policy_version=self._policy_version,
            sources=summaries,
            counts=counts,
            first_observed_at=self._first,
            last_observed_at=self._last,
            files={},
            notes=list(notes or []),
        )
        self._write(FIELDS_FILE, _FIELDS_ADAPTER.dump_json(field_list, indent=2) + b"\n")
        self._write(TEMPLATES_FILE, _TEMPLATES_ADAPTER.dump_json(template_list, indent=2) + b"\n")
        markdown = render_manifest_md(draft, field_list, template_list)
        self._write(MANIFEST_MD_FILE, markdown.encode("utf-8"))
        files = {name: self._digest(name) for name in DATA_FILES}
        manifest = BundleManifest.model_validate(draft.model_dump() | {"files": files})
        manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
        self._write(MANIFEST_FILE, manifest_bytes)
        key = SigningKey.generate()
        signature = BundleSignature(
            algorithm="ed25519",
            key_id=key.verify_key.key_id,
            public_key=key.verify_key.to_text(),
            signature=b64url_encode(key.sign(manifest_bytes)),
            signed_at=self._clock(),
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        )
        self._write(SIGNATURE_FILE, signature.model_dump_json(indent=2).encode("utf-8") + b"\n")
        self._finished = True
        self._closed = True
        log.info(
            "bundle.written",
            bundle_id=bundle_id,
            events=counts.events,
            sources=len(summaries),
            fields=len(field_list),
            templates=len(template_list),
            signing_key_id=signature.key_id,  # "signature" names a secret (ADR 0010)
        )
        return manifest

    def _bundle_templates(self, templates: Iterable[TemplateRecord]) -> list[BundleTemplate]:
        """One entry per template the events use, with the text the events carry (redacted by
        the pipeline, so no constant reaches the bundle unredacted). Store templates without an
        event in this bundle (mined in pass 1 and generalized later, or from earlier runs of
        the same state directory) describe nothing in it and are counted, not listed."""
        known = {record.template_id for record in templates}
        out = [
            BundleTemplate(
                template_id=template_id,
                system_id=tally.system_id,
                template_text=tally.text,
                kind=tally.kind,
                count=tally.count,
                first_seen=tally.first_seen,
                last_seen=tally.last_seen,
            )
            for template_id, tally in self._templates.items()
        ]
        out.sort(key=lambda item: (item.system_id, item.template_id))
        log.info(
            "bundle.templates",
            in_bundle=len(out),
            store_only=len(known - set(self._templates)),
            not_in_store=len(set(self._templates) - known),
        )
        return out

    def _write(self, name: str, data: bytes) -> None:
        path = self._out_dir / name
        with path.open("xb") as handle:
            self._written.append(path)
            handle.write(data)

    def _digest(self, name: str) -> FileDigest:
        path = self._out_dir / name
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        return FileDigest(sha256=digest, bytes=path.stat().st_size)

    # -- lifecycle ------------------------------------------------------------------------------

    def _close_streams(self) -> None:
        events, raw, locator = self._events, self._raw, self._locator
        self._events = None
        self._raw = None
        self._locator = None
        try:
            if events is not None:
                events.flush(zstandard.FLUSH_FRAME)  # ends the frame; the raw file closes below
        finally:
            try:
                if raw is not None:
                    raw.close()
            finally:
                if locator is not None:
                    locator.close()

    def close(self) -> None:
        """Release every handle; an unfinished bundle stays incomplete (no manifest)."""
        if self._closed:
            return
        self._closed = True
        self._close_streams()

    def abort(self) -> None:
        """Close and remove what this writer created (never anything else)."""
        with contextlib.suppress(Exception):
            self.close()
        self._closed = True
        if self._finished:
            return
        for path in reversed(self._written):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        self._written.clear()
        if self._locator_path is not None:
            with contextlib.suppress(OSError):
                self._locator_path.unlink(missing_ok=True)
        if self._created_dir:
            with contextlib.suppress(OSError):
                self._out_dir.rmdir()


def _reviewable(item: BundleField) -> BundleField:
    """Only ``keep`` fields may show sample values (spec 4.1 step 2, 8.4)."""
    if item.policy != "keep" and item.sample_values:
        return item.model_copy(update={"sample_values": []})
    return item


# -- MANIFEST.md ------------------------------------------------------------------------------


def _code(text: str) -> str:
    """A Markdown code span that survives backticks, newlines and table pipes."""
    flat = " ".join(text.split())
    if not flat:
        return "(empty)"
    longest = 0
    run = 0
    for char in flat:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}".replace("|", "\\|")


def _n(value: int) -> str:
    return f"{value:,}"


def _ts(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join(" --- " for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return lines


def _shapes(item: BundleField) -> str:
    if not item.top_shapes:
        return "-"
    return ", ".join(f"{_code(s.shape)} {s.share:.0%}" for s in item.top_shapes)


def render_manifest_md(
    manifest: BundleManifest, fields: Sequence[BundleField], templates: Sequence[BundleTemplate]
) -> str:
    """The human-readable review copy of a bundle (spec 4.1 step 3, 8.1.1)."""
    counts = manifest.counts
    kept = [item for item in fields if item.policy == "keep"]
    tokenized = [item for item in fields if item.policy == "tokenize"]
    dropped = [item for item in fields if item.policy == "drop"]
    lines: list[str] = [
        f"# Carto bundle {manifest.bundle_id}",
        "",
        (
            f"Tenant {_code(manifest.tenant_id)}, written by {_code(manifest.producer)} at "
            f"{_ts(manifest.created_at)}."
        ),
        "",
        (
            "Review this file before you send the bundle. Everything that leaves your machine is "
            "listed here: the events carry the fields kept in clear (with the sample values shown "
            "below), tokens (keyed hashes) for the fields tokenized, and template text with "
            "parameters removed. The tokenization key and the reveal vault stay in the analyzer's "
            "state directory on this machine and are never part of a bundle."
        ),
        "",
        "## Run",
        "",
        *_table(
            ("Item", "Value"),
            [
                ("Bundle id", _code(manifest.bundle_id)),
                ("Tenant", _code(manifest.tenant_id)),
                ("Created", _ts(manifest.created_at)),
                ("Producer", _code(manifest.producer)),
                ("Key versions", ", ".join(str(v) for v in manifest.key_versions)),
                ("Policy version", _code(manifest.policy_version)),
                ("Records read", _n(counts.records_read)),
                ("Events", _n(counts.events)),
                ("Records dropped", _n(counts.records_dropped)),
                ("Parse errors", _n(counts.parse_errors)),
                ("Dropped as older than retention (ADR 0036)", _n(counts.events_too_old)),
                ("Future times replaced by ingest time", _n(counts.events_future)),
                ("Identifier tokens", _n(counts.identifiers)),
                ("Fields kept / tokenized / dropped", _fields_line(counts)),
                ("First observed", _ts(manifest.first_observed_at)),
                ("Last observed", _ts(manifest.last_observed_at)),
            ],
        ),
        "",
        "## Signature",
        "",
        (
            f"`{SIGNATURE_FILE}` holds an Ed25519 signature over `{MANIFEST_FILE}`, which records "
            "the size and sha256 of every other file. The signing key was generated for this run "
            "and discarded. The signature proves the bundle was not changed after it was written; "
            "it does not prove who wrote it."
        ),
        "",
        "## Sources",
        "",
        *_table(
            (
                "Source",
                "System",
                "Connector",
                "Records read",
                "Events",
                "Dropped",
                "Parse errors",
                "First observed",
                "Last observed",
            ),
            [
                (
                    _code(s.source_id),
                    _code(s.system_id),
                    s.connector_type,
                    _n(s.records_read),
                    _n(s.events_written),
                    _n(s.records_dropped),
                    _n(s.parse_errors),
                    _ts(s.first_observed_at),
                    _ts(s.last_observed_at),
                )
                for s in manifest.sources
            ],
        ),
        "",
        "## Fields kept in clear",
        "",
        (
            "These values travel in clear in the events (low-cardinality attributes such as status "
            "codes). Up to five sample values each; check that none of them is sensitive."
        ),
        "",
        *_table(
            ("Field", "Class", "Count", "Distinct (est.)", "Sample values"),
            [
                (
                    _code(item.field_ref),
                    item.field_class,
                    _n(item.count),
                    _n(item.distinct_estimate),
                    ", ".join(_code(v) for v in item.sample_values) or "-",
                )
                for item in kept
            ],
        ),
        "",
        "## Fields tokenized",
        "",
        (
            "Values are replaced by keyed hashes of the forms listed; only value shapes are shown "
            "(digits as 9, letters as A)."
        ),
        "",
        *_table(
            ("Field", "Class", "Count", "Forms", "Top shapes"),
            [
                (
                    _code(item.field_ref),
                    item.field_class,
                    _n(item.count),
                    ", ".join(item.forms) or "-",
                    _shapes(item),
                )
                for item in tokenized
            ],
        ),
        "",
        "## Fields dropped",
        "",
        "Never sent; the events name the field only.",
        "",
        *_table(
            ("Field", "Class", "Count", "Reason"),
            [
                (
                    _code(item.field_ref),
                    item.field_class,
                    _n(item.count),
                    _code(item.reason) if item.reason else "-",
                )
                for item in dropped
            ],
        ),
        "",
        "## Templates",
        "",
        *_table(
            ("Template", "System", "Kind", "Events", "First seen", "Last seen", "Text"),
            [
                (
                    _code(t.template_id),
                    _code(t.system_id),
                    t.kind.value,
                    _n(t.count),
                    _ts(t.first_seen),
                    _ts(t.last_seen),
                    _code(t.template_text),
                )
                for t in templates
            ],
        ),
        "",
    ]
    if manifest.notes:
        lines.extend(["## Notes", ""])
        lines.extend(f"- {' '.join(note.split())}" for note in manifest.notes)
        lines.append("")
    return "\n".join(lines)


def _fields_line(counts: BundleCounts) -> str:
    return f"{_n(counts.fields_kept)} / {_n(counts.fields_tokenized)} / {_n(counts.fields_dropped)}"
