"""The edge pipeline: raw record in, canonical event out (spec 5.4 steps 2 to 6).

:class:`EdgePipeline` wires the finished stages together, one parser per source:

1. :class:`~carto_edge.pipeline.parser.RecordParser` parses and mines the template (spec 8.2).
2. Every parsed field is observed by the :class:`~carto_edge.pipeline.classify.Classifier` and
   decided (spec 8.3): ``keep`` fields become ``attributes`` only when
   :func:`~carto_edge.pipeline.redact.attribute_is_clean` passes, truncated to 256 characters
   and capped at ``MAX_ATTRIBUTES``; ``tokenize`` fields go to the
   :class:`~carto_edge.pipeline.tokenize.Tokenizer` with the decided forms; ``drop`` fields are
   listed by name in ``dropped_fields``. A kept value the hygiene check refuses is dropped too.
3. The template text goes through :func:`~carto_edge.pipeline.redact.redact_template_text`;
   the number of masked entities is the event's ``redaction.entities_masked``.
4. The actor field (``ParsedRecord.actor_path``: a row connector's ``actor_column``, else the
   source's ``actor_field`` hint) is never an attribute or an identifier: its value becomes
   ``actor.token`` (spec 7.1), ``kind`` ``service`` when the value looks like a service account
   (``svc_`` prefix or an ``api``, ``integration``, ``system``, ``bot``, ``daemon`` or
   ``service`` segment), else ``human``.
5. ``observed_at`` must lie in the plausible range of ADR 0036, read on the pipeline's
   clock: no older than event retention plus one day (the record is dropped with reason
   ``too_old``) and no more than one day ahead (the time becomes the ingest time, with
   ``observed_at_quality=ingest``: a future time is almost always a parsing error, and the
   event is still worth keeping). Both are counted in ``PipelineCounters.out_of_range``.
   ``event_id = derive_ulid(observed_at_ms, source_id, locator)`` (spec 8.1, ADR 0006), so a
   record read twice gets the same id. ``ingested_at`` is the connector's ``received_at``.
6. The :class:`~carto_schema.event.CanonicalEvent` is validated by the contract model. A
   validation failure is a bug in this pipeline: it is logged by error type and field
   locations only and the record is dropped with reason ``contract``.

Vault entries for the ``raw`` form tokens come back in the :class:`PipelineResult` for the
caller to write (spec 8.4 reveal vault); ``expires_at`` is ``observed_at`` plus the event
retention (spec 14.10).

Two modes. The gateway streams: the classifier applies its quarantine rule as records arrive.
The offline analyzer runs two passes (ADR 0017): :meth:`EdgePipeline.observe_only` for pass 1
(parse, mine, feed statistics, decide nothing) and :meth:`EdgePipeline.process` for pass 2 with
``observe_on_process=False`` so the complete statistics are not counted twice, and a classifier
whose ``quarantine_samples`` the runtime sets to 1 for the analyzer, which is ADR 0017's "no
quarantine, because every field has all the samples it will ever have". A template that Drain3
generalized late in pass 1 gives its early records a different ``template_id`` in pass 2; such a
field has thin or no statistics and lands in quarantine, the safe direction.

Counters (:class:`PipelineCounters`) hold records, events, drops by reason, identifiers,
truncated identifier sets, vault entries and refused attributes. Nothing here logs a value
(spec 2.3 invariant 7): log lines carry source ids, field refs, counts and reasons. The
pipeline is guarded by a lock because the template store is not thread-safe.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic import ValidationError

from carto_common.ids import derive_ulid
from carto_common.logging import get_logger
from carto_common.settings import observed_at_range
from carto_edge.config import SourceConfig, SourcesFile
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.forms import normalize
from carto_edge.pipeline.model import (
    FieldDecision,
    ParsedRecord,
    PipelineResult,
    Policy,
    RawRecord,
    VaultEntry,
    field_ref,
)
from carto_edge.pipeline.parse.names import FieldNameStore
from carto_edge.pipeline.parser import ParseFailure, RecordParser
from carto_edge.pipeline.pii import PiiDetector
from carto_edge.pipeline.redact import (
    attribute_is_clean,
    redact_template_text,
    truncate_attribute,
)
from carto_edge.pipeline.templates import TemplateStore
from carto_edge.pipeline.tokenize import FieldInput, Tokenizer
from carto_schema.event import (
    MAX_ATTRIBUTE_KEY_LEN,
    MAX_ATTRIBUTES,
    MAX_DROPPED_FIELDS,
    MAX_FIELD_NAME_LEN,
    SCHEMA_VERSION,
    Actor,
    ActorKind,
    CanonicalEvent,
    ObservedAtQuality,
    Redaction,
)

__all__ = [
    "OUT_OF_RANGE_FUTURE",
    "REASON_CONTRACT",
    "REASON_TOO_OLD",
    "REASON_UNKNOWN_SOURCE",
    "SERVICE_ACCOUNT_SEGMENTS",
    "EdgePipeline",
    "PipelineCounters",
    "actor_kind",
    "wall_clock",
]

REASON_CONTRACT: Final = "contract"
REASON_UNKNOWN_SOURCE: Final = "unknown_source"
REASON_ATTRIBUTE_UNCLEAN: Final = "attribute_unclean"
REASON_TOO_OLD: Final = "too_old"
"""Drop reason, and ``out_of_range`` value, of a record older than the plausible range."""
OUT_OF_RANGE_FUTURE: Final = "future"
"""``out_of_range`` value of an event whose future time was replaced by the ingest time."""
SERVICE_ACCOUNT_SEGMENTS: Final = frozenset(
    {"svc", "api", "integration", "system", "bot", "daemon", "service"}
)
_SEGMENT_SEPARATORS: Final = frozenset("_-.:@/ ")

log = get_logger(component="carto_edge.pipeline")


def wall_clock() -> datetime:
    """The gateway's reference time for the plausible range (ADR 0036). Looked up at each call,
    so the edge tests can pin it to a fixed day for their fixed-date data."""
    return datetime.now(UTC)


def _segments(value: str) -> list[str]:
    out: list[str] = []
    current: list[str] = []
    for char in value.lower():
        if char in _SEGMENT_SEPARATORS:
            if current:
                out.append("".join(current))
                current = []
        else:
            current.append(char)
    if current:
        out.append("".join(current))
    return out


def actor_kind(value: str) -> ActorKind:
    """``service`` for service-account-looking values, ``human`` otherwise (spec 7.1)."""
    lowered = value.strip().lower()
    if not lowered:
        return ActorKind.UNKNOWN
    if lowered.startswith(("svc_", "svc-", "svc.")):
        return ActorKind.SERVICE
    segments = _segments(lowered)
    if any(segment in SERVICE_ACCOUNT_SEGMENTS for segment in segments):
        return ActorKind.SERVICE
    if any(word in lowered for word in ("integration", "daemon", "system")):
        return ActorKind.SERVICE
    return ActorKind.HUMAN


@dataclass(slots=True)
class PipelineCounters:
    """Operational counts; never values."""

    records: int = 0
    events: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    identifiers: int = 0
    truncated_identifier_sets: int = 0
    vault_entries: int = 0
    attributes_refused: int = 0
    observed: int = 0
    out_of_range: Counter[str] = field(default_factory=Counter)

    @property
    def dropped_total(self) -> int:
        return sum(self.dropped.values())

    def snapshot(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "events": self.events,
            "dropped": dict(self.dropped),
            "dropped_total": self.dropped_total,
            "identifiers": self.identifiers,
            "truncated_identifier_sets": self.truncated_identifier_sets,
            "vault_entries": self.vault_entries,
            "attributes_refused": self.attributes_refused,
            "observed": self.observed,
            "out_of_range": dict(self.out_of_range),
        }


def pin_paths(sources: SourcesFile) -> list[str]:
    """The field paths named by field policy pins (``system/template/path``): an admin named
    them, so the name gate trusts them from the first record (ADR 0030)."""
    return [
        pin.field.split("/", 2)[2] for pin in sources.field_policies if pin.field.count("/") >= 2
    ]


class EdgePipeline:
    """Parser, classifier, redaction and tokenizer for every source of one edge."""

    def __init__(
        self,
        *,
        sources: SourcesFile,
        templates: TemplateStore,
        classifier: Classifier,
        detector: PiiDetector,
        tokenizer: Tokenizer,
        tenant_id: str,
        retention_days: int,
        clock: Callable[[], datetime] | None = None,
        observe_on_process: bool = True,
        names: FieldNameStore | None = None,
    ) -> None:
        """``clock`` is the time the plausible ``observed_at`` range is read against (ADR 0036):
        the wall clock in the gateway, the run's reference time in the analyzer."""
        self._sources = {source.id: source for source in sources.sources}
        self._observe_on_process = observe_on_process
        self._templates = templates
        # The name gate (ADR 0030); in memory unless the runtime passes a persisted one.
        self._names = (
            names
            if names is not None
            else FieldNameStore(detector, trusted_paths=pin_paths(sources))
        )
        self._classifier = classifier
        self._detector = detector
        self._tokenizer = tokenizer
        self._tenant_id = tenant_id
        self._retention_days = retention_days
        self._retention = timedelta(days=retention_days)
        # A lambda, not the function: it is looked up per call so tests can pin it.
        self._clock = clock if clock is not None else lambda: wall_clock()  # noqa: PLW0108
        self._parsers: dict[str, RecordParser] = {}
        self._lock = threading.RLock()
        self.counters = PipelineCounters()

    def __repr__(self) -> str:
        return f"EdgePipeline(tenant_id={self._tenant_id!r}, sources={len(self._sources)})"

    @property
    def classifier(self) -> Classifier:
        return self._classifier

    @property
    def templates(self) -> TemplateStore:
        return self._templates

    @property
    def names(self) -> FieldNameStore:
        return self._names

    @property
    def tokenizer(self) -> Tokenizer:
        return self._tokenizer

    @property
    def policy_version(self) -> str:
        return self._classifier.policy_version

    def source(self, source_id: str) -> SourceConfig | None:
        return self._sources.get(source_id)

    def reset_parsers(self) -> None:
        """Forget per-source parser state (a learned CSV header, a locked format). The analyzer
        calls it between its passes so pass 2 reads each file from its first line again."""
        with self._lock:
            self._parsers.clear()

    def parser_for(self, source_id: str) -> RecordParser | None:
        """The parser of a configured source, created on first use."""
        parser = self._parsers.get(source_id)
        if parser is None:
            source = self._sources.get(source_id)
            if source is None:
                return None
            parser = RecordParser(source, self._templates, self._names)
            self._parsers[source_id] = parser
        return parser

    # -- pass 1 of the analyzer ---------------------------------------------------------------

    def observe_only(self, raw: RawRecord) -> bool:
        """Parse, mine the template and feed field statistics; emit nothing (ADR 0017 pass 1).
        Returns whether the record parsed."""
        with self._lock:
            parser = self.parser_for(raw.source_id)
            if parser is None:
                return False
            parsed = parser.parse(raw)
            if isinstance(parsed, ParseFailure):
                return False
            for path, value in parsed.fields.items():
                if path != parsed.actor_path:
                    ref = field_ref(parsed.system_id, parsed.template_id, path)
                    self._classifier.observe(ref, path, value)
            self.counters.observed += 1
            return True

    def _decide_fields(self, parsed: ParsedRecord) -> dict[str, FieldDecision]:
        """Observe (unless the statistics are complete, ADR 0017 pass 2) and decide each field.
        Decisions are made only here, never in pass 1, so the classifier's decision cache
        never holds a verdict made on partial statistics."""
        decisions: dict[str, FieldDecision] = {}
        for path, value in parsed.fields.items():
            if path == parsed.actor_path:
                continue
            ref = field_ref(parsed.system_id, parsed.template_id, path)
            if self._observe_on_process:
                self._classifier.observe(ref, path, value)
            decisions[path] = self._classifier.decide(
                ref, path, name_class=parsed.name_classes.get(path)
            )
        return decisions

    # -- streaming and pass 2 -------------------------------------------------------------------

    def process(self, raw: RawRecord) -> PipelineResult:
        """One raw record to one canonical event (or a dropped result with a reason)."""
        with self._lock:
            self.counters.records += 1
            parser = self.parser_for(raw.source_id)
            if parser is None:
                return self._drop(raw.locator, REASON_UNKNOWN_SOURCE)
            parsed = parser.parse(raw)
            if isinstance(parsed, ParseFailure):
                return self._drop(raw.locator, parsed.reason)
            lower, upper = observed_at_range(self._clock(), self._retention_days)
            out_of_range: str | None = None
            if parsed.observed_at < lower:
                self.counters.out_of_range[REASON_TOO_OLD] += 1
                result = self._drop(raw.locator, REASON_TOO_OLD)
                result.out_of_range = REASON_TOO_OLD
                return result
            if parsed.observed_at > upper:
                self.counters.out_of_range[OUT_OF_RANGE_FUTURE] += 1
                out_of_range = OUT_OF_RANGE_FUTURE
                parsed = replace(
                    parsed,
                    observed_at=parsed.received_at,
                    observed_at_quality=ObservedAtQuality.INGEST,
                )
            decisions = self._decide_fields(parsed)
            emitted = self._emit(parsed, decisions)
            emitted.out_of_range = out_of_range
            return emitted

    def _drop(self, locator: str, reason: str) -> PipelineResult:
        self.counters.dropped[reason] += 1
        return PipelineResult(locator=locator, event=None, dropped_reason=reason)

    def _emit(self, parsed: ParsedRecord, decisions: dict[str, FieldDecision]) -> PipelineResult:
        attributes: dict[str, str] = {}
        dropped: list[str] = []
        inputs: list[FieldInput] = []
        for path, value in parsed.fields.items():
            if path == parsed.actor_path:
                continue
            decision = decisions[path]
            name = path[:MAX_FIELD_NAME_LEN]
            if decision.policy is Policy.KEEP:
                ref = field_ref(parsed.system_id, parsed.template_id, path)
                if (
                    len(attributes) < MAX_ATTRIBUTES
                    and self._classifier.keeps(ref, value, pinned=decision.pinned)
                    and attribute_is_clean(value, self._detector)
                ):
                    attributes[path[:MAX_ATTRIBUTE_KEY_LEN]] = truncate_attribute(value)
                else:
                    self.counters.attributes_refused += 1
                    dropped.append(name)
            elif decision.policy is Policy.TOKENIZE:
                inputs.append((path, decision.field_class, decision.forms, value))
            else:
                dropped.append(name)

        expires_at = parsed.observed_at + self._retention
        tokenized = self._tokenizer.build_event_identifiers(inputs, expires_at=expires_at)
        template_text, masked = redact_template_text(parsed.template_text, self._detector)
        actor = self._actor(parsed.actor)
        observed_ms = int(parsed.observed_at.timestamp() * 1000)
        event_id = derive_ulid(observed_ms, parsed.source_id, parsed.locator)
        unique_dropped = list(dict.fromkeys(dropped))[:MAX_DROPPED_FIELDS]
        try:
            event = CanonicalEvent(
                schema_version=SCHEMA_VERSION,
                event_id=event_id,
                tenant_id=self._tenant_id,
                source_id=parsed.source_id,
                system_id=parsed.system_id,
                kind=parsed.kind,
                observed_at=parsed.observed_at,
                ingested_at=parsed.received_at,
                observed_at_quality=parsed.observed_at_quality,
                template_id=parsed.template_id,
                template_text=template_text,
                severity=parsed.severity,
                attributes=attributes,
                identifiers=tokenized.identifiers,
                actor=actor,
                dropped_fields=unique_dropped,
                redaction=Redaction(
                    policy_version=self._classifier.policy_version or "0",
                    entities_masked=masked,
                ),
            )
        except ValidationError as exc:
            locations = sorted(
                {
                    ".".join(str(piece) for piece in error.get("loc", ()))
                    for error in exc.errors(include_input=False, include_url=False)
                }
            )
            log.error(
                "event_contract_violation",
                source_id=parsed.source_id,
                template_id=parsed.template_id,
                error="ValidationError",
                locations=locations[:10],
            )
            return self._drop(parsed.locator, REASON_CONTRACT)
        self.counters.events += 1
        self.counters.identifiers += len(tokenized.identifiers)
        if tokenized.truncated:
            self.counters.truncated_identifier_sets += 1
        self.counters.vault_entries += len(tokenized.vault_entries)
        return PipelineResult(
            locator=parsed.locator, event=event, vault_entries=list(tokenized.vault_entries)
        )

    def _actor(self, value: str | None) -> Actor | None:
        if value is None or not normalize(value):
            return None
        return Actor(token=self._tokenizer.actor_token(value), kind=actor_kind(value))

    # -- maintenance --------------------------------------------------------------------------

    def flush(self) -> None:
        """Persist templates and field names; the caller flushes the statistics store on its
        own clock."""
        with self._lock:
            try:
                self._templates.flush()
            finally:
                self._names.flush()

    def parser_internal_errors(self) -> int:
        return sum(parser.internal_errors for parser in self._parsers.values())

    @staticmethod
    def vault_entries_of(results: list[PipelineResult]) -> list[VaultEntry]:
        return [entry for result in results for entry in result.vault_entries]
