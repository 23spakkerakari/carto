"""The parse stage: a :class:`~carto_edge.pipeline.model.RawRecord` becomes a
:class:`~carto_edge.pipeline.model.ParsedRecord` or a :class:`ParseFailure` (spec 8.2, 5.4
step 3, ADR 0016).

One :class:`RecordParser` per source. For a record that arrives with ``fields`` (a row, an
OTLP body, a webhook payload, file metadata) the mapping is flattened; a ``text`` beside it is
the message. For a text record the format is the configured one, or auto-detected in spec
8.2 order (JSON, XML, logfmt, CSV, access log, unstructured text), first match wins, with the
winning format remembered per source. The template is, in order: the connector's
``template_hint`` (``row_change <query>``, ``file_arrived <generalized name>``), the Drain3
template of the message field (``message_field`` or the first of
:data:`~carto_edge.config.DEFAULT_MESSAGE_FIELDS`; its parameters become ``msg.param_0..n``
and the field itself is consumed), the root element name for XML, ``<METHOD> <route> <status
class>`` for access logs, the mined template of an unstructured line (parameters
``param_0..n``), else ``keys:<sorted top-level keys>``. The source timestamp comes from the
configured or default fields or the start of the line and is consumed; without one
``observed_at`` is the receive time with quality ``ingest``. Severity follows
:mod:`carto_edge.pipeline.severity`; ``actor`` is the raw value of the actor field (the record's
``actor_field`` override from a row connector's ``actor_column``, else the source's hint), left in
the fields and named in ``actor_path`` so the pipeline can skip it. A record's
``timestamp_field`` override (a query's ``timestamp_column``) is tried before the source's
timestamp hint.

Defensive by construction (spec 2.3 invariant 8): ``max_record_bytes`` is checked before any
parser runs, every parser is bounded and returns ``None`` on bad input, a configured format
is strict (a line it rejects is ``unparseable``), and :meth:`RecordParser.parse` never raises:
an unexpected exception is counted in :attr:`RecordParser.internal_errors`, logged by type and
source (never with a value, invariant 7), and reported as ``internal_error``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from carto_common.logging import get_logger
from carto_edge.config import (
    DEFAULT_MESSAGE_FIELDS,
    DEFAULT_TIMESTAMP_FIELDS,
    ParseConfig,
    RecordFormat,
    SourceConfig,
)
from carto_edge.pipeline.model import FieldClass, ParsedRecord, RawRecord
from carto_edge.pipeline.parse.access_log import AccessLogParser
from carto_edge.pipeline.parse.common import (
    SegmentPath,
    clean_text,
    flatten,
    key_signature,
    safe_path,
    utf8_len,
)
from carto_edge.pipeline.parse.csv import CsvHeader, CsvParser, looks_like_csv_header
from carto_edge.pipeline.parse.detect import FormatDetector
from carto_edge.pipeline.parse.json import parse_json
from carto_edge.pipeline.parse.logfmt import parse_logfmt
from carto_edge.pipeline.parse.names import FieldNameStore
from carto_edge.pipeline.parse.text import parse_text
from carto_edge.pipeline.parse.xml import parse_xml
from carto_edge.pipeline.severity import (
    LEVEL_FIELDS,
    OTEL_NUMBER_FIELDS,
    STATUS_FIELDS,
    detect_severity,
    map_level,
)
from carto_edge.pipeline.templates import TemplateStore
from carto_edge.pipeline.timestamps import find_timestamp, resolve_zone
from carto_schema.event import (
    MAX_FIELD_NAME_LEN,
    MAX_TEMPLATE_TEXT_LEN,
    EventKind,
    ObservedAtQuality,
    Severity,
)

__all__ = ["ParseFailure", "RecordParser"]

REASON_EMPTY: Final = "empty"
REASON_TOO_LARGE: Final = "too_large"
REASON_UNPARSEABLE: Final = "unparseable"
REASON_CSV_HEADER: Final = "csv_header"
REASON_INTERNAL: Final = "internal_error"
FORMAT_FIELDS: Final = "fields"
_LINE_MARKER: Final = ":line:"
_MAX_TIMESTAMP_OVERRIDES: Final = 64


@dataclass(frozen=True, slots=True)
class ParseFailure:
    """Why a record produced no event; a reason code, never a value (spec 2.3 invariant 7)."""

    reason: str


@dataclass(slots=True)
class _Draft:
    """What a format parser extracted, before the common finishing steps."""

    fields: dict[str, str]
    template_text: str | None
    top_keys: Sequence[str]
    parse_format: str
    notes: tuple[str, ...] = ()
    kind: EventKind | None = None
    timestamp: datetime | None = None
    level: str | None = None
    mine_message: str | None = None
    param_prefix: str = "msg."
    structure: dict[str, SegmentPath] | None = None


class RecordParser:
    """Parse records of one source (spec 8.2); see the module docstring for the rules."""

    def __init__(
        self,
        source: SourceConfig,
        templates: TemplateStore,
        names: FieldNameStore | None = None,
    ) -> None:
        self._source = source
        self._config: ParseConfig = source.parse
        self._templates = templates
        # The pipeline always passes the name gate (ADR 0030); without one, names are only
        # masked when they look like values (ADR 0027), which parser unit tests rely on.
        self._names = names
        self._trusted: frozenset[str] = frozenset()
        self._record_trusted: dict[tuple[str | None, str | None], frozenset[str]] = {}
        if names is not None:
            config = self._config
            self._trusted = names.trusted_set(
                (
                    config.timestamp_field,
                    config.message_field,
                    config.severity_field,
                    config.actor_field,
                    *DEFAULT_TIMESTAMP_FIELDS,
                    *DEFAULT_MESSAGE_FIELDS,
                    *LEVEL_FIELDS,
                    *OTEL_NUMBER_FIELDS,
                    *STATUS_FIELDS,
                )
            )
        self._zone = resolve_zone(self._config.timezone)
        self._format = self._config.format
        self._access = AccessLogParser(self._config.access_log_pattern)
        self._csv = CsvParser(self._config)
        self._detector = FormatDetector()
        self._current_file: str | None = None
        self._timestamp_configs: dict[str, ParseConfig] = {}
        self.internal_errors = 0
        """Unexpected exceptions swallowed by :meth:`parse`; a non-zero count is a bug."""

    @property
    def detected_format(self) -> RecordFormat | None:
        """The format an ``auto`` source is locked to, once records agreed on one."""
        return self._detector.locked

    # -- entry point --------------------------------------------------------------------------

    def parse(self, raw: RawRecord) -> ParsedRecord | ParseFailure:
        """Parse one record; never raises."""
        try:
            return self._parse(raw)
        except Exception as exc:  # the parse stage must not take the pipeline down
            self.internal_errors += 1
            get_logger(component="parser").warning(
                "parse_internal_error",
                source_id=raw.source_id,
                # The record kind, a fixed set: an upload locator starts with the customer's
                # file name, which product logs never carry (spec 2.3 invariant 7).
                record_kind=raw.kind.value,
                error=type(exc).__name__,
            )
            return ParseFailure(REASON_INTERNAL)

    def _parse(self, raw: RawRecord) -> ParsedRecord | ParseFailure:
        text = raw.text
        size = raw.size_bytes if raw.size_bytes > 0 else (utf8_len(text) if text else 0)
        if size > self._config.max_record_bytes:
            return ParseFailure(REASON_TOO_LARGE)
        if raw.fields is not None:
            return self._parse_fields(raw)
        if text is None or not text.strip():
            return ParseFailure(REASON_EMPTY)
        return self._parse_text(raw, clean_text(text.rstrip("\r\n")))

    # -- records that arrive as fields --------------------------------------------------------

    def _parse_fields(self, raw: RawRecord) -> ParsedRecord | ParseFailure:
        if raw.fields is None:  # checked by the caller
            return ParseFailure(REASON_EMPTY)
        flat = flatten(raw.fields)
        fields = flat.fields
        message = clean_text(raw.text.strip()) if raw.text and raw.text.strip() else None
        if message is None:
            message = self._take_message(fields)
        if not fields and message is None:
            return ParseFailure(REASON_EMPTY)
        hint = clean_text(raw.template_hint.strip()) if raw.template_hint else ""
        draft = _Draft(
            fields=fields,
            template_text=hint or None,
            top_keys=flat.top_keys,
            parse_format=FORMAT_FIELDS,
            notes=flat.notes,
            mine_message=message,
            structure=flat.structure,
        )
        return self._finish(raw, draft)

    # -- text records -------------------------------------------------------------------------

    def _parse_text(self, raw: RawRecord, line: str) -> ParsedRecord | ParseFailure:
        self._track_file(raw.locator)
        if self._format == RecordFormat.AUTO:
            csv_ready = self._csv.ready or self._csv_header_candidate(raw.locator, line)
            candidates = self._detector.order(line, csv_ready=csv_ready)
        else:
            candidates = (self._format,)
        for fmt in candidates:
            outcome = self._try(fmt, raw, line)
            if outcome is None:
                continue
            if isinstance(outcome, ParseFailure):
                return outcome
            if self._format == RecordFormat.AUTO:
                self._detector.observe(fmt)
            return self._finish(raw, outcome)
        return ParseFailure(REASON_UNPARSEABLE)

    def _track_file(self, locator: str) -> None:
        """A new file (locator prefix before ``:line:``) forgets a learned CSV header."""
        file_key = locator.rpartition(_LINE_MARKER)[0] if _LINE_MARKER in locator else locator
        if file_key != self._current_file:
            self._current_file = file_key
            self._csv.reset()

    def _csv_header_candidate(self, locator: str, line: str) -> bool:
        return (
            not self._csv.ready
            and locator.endswith(f"{_LINE_MARKER}1")
            and looks_like_csv_header(line, self._config.csv_delimiter)
        )

    def _try(self, fmt: RecordFormat, raw: RawRecord, line: str) -> _Draft | ParseFailure | None:
        if fmt in (RecordFormat.JSON, RecordFormat.NDJSON):
            flat = parse_json(line)
            if flat is None:
                return None
            fields = flat.fields
            return _Draft(
                fields=fields,
                template_text=None,
                top_keys=flat.top_keys,
                parse_format=fmt.value,
                notes=flat.notes,
                mine_message=self._take_message(fields),
                structure=flat.structure,
            )
        if fmt == RecordFormat.XML:
            xml = parse_xml(line)
            if xml is None:
                return None
            return _Draft(
                fields=xml.fields,
                template_text=xml.root,
                top_keys=(),
                parse_format=fmt.value,
                notes=xml.notes,
                structure=xml.structure,
            )
        if fmt == RecordFormat.LOGFMT:
            pairs = parse_logfmt(line)
            if pairs is None:
                return None
            top_keys = tuple(pairs)
            return _Draft(
                fields=pairs,
                template_text=None,
                top_keys=top_keys,
                parse_format=fmt.value,
                mine_message=self._take_message(pairs),
            )
        if fmt == RecordFormat.CSV:
            strict = self._format == RecordFormat.AUTO
            row = self._csv.parse(line, strict=strict)
            if row is None:
                return None
            if isinstance(row, CsvHeader):
                return ParseFailure(REASON_CSV_HEADER)
            columns = self._csv.columns or tuple(row)
            return _Draft(
                fields=row,
                template_text=None,
                top_keys=columns,
                parse_format=fmt.value,
                mine_message=self._take_message(row),
            )
        if fmt == RecordFormat.ACCESS_LOG:
            access = self._access.parse(line)
            if access is None:
                return None
            return _Draft(
                fields=access.fields,
                template_text=access.template,
                top_keys=(),
                parse_format=fmt.value,
                kind=EventKind.HTTP_ACCESS,
            )
        if fmt == RecordFormat.TEXT:
            text = parse_text(line, self._zone, reference=raw.received_at)
            if text is None:
                return ParseFailure(REASON_EMPTY)
            return _Draft(
                fields={},
                template_text=None,
                top_keys=(),
                parse_format=fmt.value,
                timestamp=text.timestamp,
                level=text.level,
                mine_message=text.message,
                param_prefix="",
            )
        return None

    # -- common finishing ---------------------------------------------------------------------

    def _take_message(self, fields: dict[str, str]) -> str | None:
        """Pop the message field (configured, else the first default) when it has text."""
        names: tuple[str, ...] = (
            (self._config.message_field,) if self._config.message_field else DEFAULT_MESSAGE_FIELDS
        )
        for name in names:
            value = fields.get(name)
            if value is None:
                continue
            del fields[name]
            return value if value.strip() else None
        return None

    def _finish(self, raw: RawRecord, draft: _Draft) -> ParsedRecord:
        config = self._config
        system_id = raw.system_id
        top_keys: Sequence[str]
        name_classes: dict[str, FieldClass] = {}
        if self._names is None:
            fields = _safe_fields(draft.fields)
            top_keys = tuple(safe_path(key) for key in draft.top_keys)
        else:
            gated = self._names.apply(
                system_id,
                draft.fields,
                draft.top_keys,
                self._trusted_for(raw),
                structure=draft.structure,
                record_id=f"{raw.source_id}:{raw.locator}",
            )
            fields, top_keys, name_classes = gated.fields, gated.top_keys, gated.name_classes
        template_text = draft.template_text
        if draft.mine_message is not None:
            mined, params = self._templates.mine(system_id, draft.mine_message)
            for index, value in enumerate(params):
                fields[f"{draft.param_prefix}param_{index}"] = value
            if template_text is None:
                template_text = mined
        if template_text is None:
            template_text = key_signature(top_keys)
        if len(template_text) > MAX_TEMPLATE_TEXT_LEN:
            template_text = template_text[:MAX_TEMPLATE_TEXT_LEN]

        observed = draft.timestamp
        quality = ObservedAtQuality.SOURCE
        if observed is None:
            found = find_timestamp(
                fields,
                self._timestamp_config(raw.timestamp_field),
                zone=self._zone,
                reference=raw.received_at,
            )
            if found is not None:
                observed, consumed = found
                del fields[consumed]
            else:
                observed = _utc(raw.received_at)
                quality = ObservedAtQuality.INGEST

        severity: Severity | None = map_level(draft.level) if draft.level else None
        if severity is None:
            severity = detect_severity(fields, template_text, severity_field=config.severity_field)
        actor_path = raw.actor_field or config.actor_field
        actor = fields.get(actor_path) if actor_path else None

        template_id = self._templates.template_id(system_id, template_text)
        kind = draft.kind or raw.kind
        self._templates.register(system_id, template_id, template_text, kind, observed)
        return ParsedRecord(
            source_id=raw.source_id,
            system_id=system_id,
            kind=kind,
            locator=raw.locator,
            sequence=raw.sequence,
            received_at=_utc(raw.received_at),
            observed_at=observed,
            observed_at_quality=quality,
            template_id=template_id,
            template_text=template_text,
            fields=fields,
            severity=severity,
            actor=actor,
            actor_path=actor_path,
            parse_format=draft.parse_format,
            parse_notes=draft.notes,
            name_classes=name_classes,
        )

    def _trusted_for(self, raw: RawRecord) -> frozenset[str]:
        """The parser's trusted paths plus a record's own actor and timestamp columns (a row
        connector's ``actor_column`` and ``timestamp_column``), cached per pair."""
        if self._names is None or (raw.actor_field is None and raw.timestamp_field is None):
            return self._trusted
        key = (raw.actor_field, raw.timestamp_field)
        trusted = self._record_trusted.get(key)
        if trusted is None:
            if len(self._record_trusted) >= _MAX_TIMESTAMP_OVERRIDES:
                self._record_trusted.clear()
            trusted = self._trusted | self._names.trusted_set(key)
            self._record_trusted[key] = trusted
        return trusted

    def _timestamp_config(self, override: str | None) -> ParseConfig:
        """The parse config with a record's timestamp column in front (cached per column)."""
        if not override or override == self._config.timestamp_field:
            return self._config
        cached = self._timestamp_configs.get(override)
        if cached is None:
            if len(self._timestamp_configs) >= _MAX_TIMESTAMP_OVERRIDES:
                self._timestamp_configs.clear()
            cached = self._config.model_copy(update={"timestamp_field": override})
            self._timestamp_configs[override] = cached
        return cached


def _safe_fields(fields: dict[str, str]) -> dict[str, str]:
    """Field names never carry values (spec 2.3 invariant 2): value-like path segments become
    ``*``; when two paths mask to the same name the first value is kept. Longer than the
    canonical event allows, a path is dropped."""
    out: dict[str, str] = {}
    for path, value in fields.items():
        safe = safe_path(path)
        if safe not in out and len(safe) <= MAX_FIELD_NAME_LEN:
            out[safe] = value
    return out


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
