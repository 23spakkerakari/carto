"""Engine fields to truth fields (plan M2 decision 6; ADR 0049 item 3; ADR 0006).

The engine knows a field as ``system_id/template_id/path`` plus a form; the truth knows it as
``(system_id, source_id, field)`` where ``field`` is the name in the data. :class:`FieldMapping`
bridges the two:

- **Template to source.** The bundle's events carry both the template and the source; the
  analyzer's locator map (``<bundle>.locator_map.ndjson``, locator key to ``event_id``, ADR 0006)
  names the truth-side record of each event, and a locator key starts with its ``source_id``.
  The mapping streams the bundle once and counts, per ``(system_id, template_id)``, the source
  the locator map names for each event (the event's own ``source_id`` when the map is absent);
  the majority source carries the template. Events whose two sources disagree are counted and
  reported.
- **Path to name** (:mod:`carto_core.fieldpaths`, shared with the gate report): the first of
  the path's :func:`~carto_core.fieldpaths.name_candidates` that the truth names a field by, in
  that system and source, wins; a dotted path therefore flattens to its leaf unless the truth
  field itself is dotted, a map entry ``parent.0.key`` or a masked tail ``parent.*`` names
  ``parent`` (ADR 0030), CSV column names stand as emitted and case is preserved. A path the
  truth does not know keeps its leaf name, so a link on it is still scored (as the false positive
  it is). A path whose name is masked, or a template no source carries, cannot be mapped; a link
  with such an end is dropped and listed.

:meth:`FieldMapping.map_links` turns ``engine_links.json`` records into
:class:`~carto_eval.predictions.PredictedLink` rows under the eval acceptance policy of ADR 0049
item 2 (:func:`family_usable`) and two rules the truth's vocabulary forces:

- the truth knows no templates, so an engine link between two templates' copies of one source
  field (``order_id`` in three order-log templates) maps to a pair of one field with itself and
  is not a prediction; it is counted, not scored;
- the truth names one form pair per link, while the engine proposes every form pair that
  shares tokens (``cart_id`` raw, ``alnum`` and ``digits.0`` on both sides of L01). Per unordered
  pair of truth fields the one engine link with the most ``raw`` ends, then the highest score,
  stands as the prediction; the others are counted as form variants. The link metrics compare
  ``(field, form)`` pairs, so a link the engine found only in a derived form the truth does not
  use still scores as a miss plus a false positive.

The linker itself now proposes one link per field pair (its ``extra["form_variants"]``
lists the others), so the collapse is a no-op on current engine output and still counts what it
removed. :meth:`FieldMapping.map_entities` maps the engine's own families,
:func:`policy_families` builds the families the policy implies (over fields, transaction role
only, as ``KeyLink.is_usable_for_families``), and :class:`MappingReport` lists every engine
field that matched no truth field and every truth link field no engine field reached, rendered
as a Markdown section, so a naming mismatch is visible and never silently scored as a miss.

The ground truth is read here, in the eval harness; nothing of it reaches core.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from carto_core.bundle import BundleError, VerifiedBundle, iter_events, read_bundle_metadata
from carto_core.fieldpaths import leaf_name, name_candidates, split_field_ref
from carto_eval.metrics import link_pair
from carto_eval.predictions import PredictedEntity, PredictedLink
from carto_simulator.ground_truth import EntityField, FieldRef, GroundTruth, LinkType

__all__ = [
    "ENGINE_STATUSES",
    "MAX_LISTED",
    "FieldMapping",
    "MappedLink",
    "MappingError",
    "MappingReport",
    "collapse_forms",
    "family_usable",
    "policy_families",
    "policy_statement",
    "template_sources",
    "truth_field_names",
]

ENGINE_STATUSES: Final[frozenset[str]] = frozenset(
    {"proposed", "accepted", "auto_accepted", "stale"}
)
"""Engine statuses reported as predictions (a rejected link is not a proposal)."""
MAX_LISTED: Final = 60
"""Per list in the Markdown section; the JSON holds every entry."""
MAX_LOCATOR_LINE_BYTES: Final = 4096

TruthField = tuple[str, str, str]
"""``(system_id, source_id, field)``."""
TemplateKey = tuple[str, str]
"""``(system_id, template_id)``."""


class MappingError(Exception):
    """The mapping could not be built; the message names the file, never a value."""


# -- truth side ---------------------------------------------------------------------------------


def truth_field_names(truth: GroundTruth) -> frozenset[TruthField]:
    """Every field the truth names in a link (ends and composite components), an entity or a
    batch key."""
    out: set[TruthField] = set()

    def add(ref: FieldRef) -> None:
        out.add((ref.system_id, ref.source_id, ref.field))

    for link in truth.links:
        add(link.a)
        add(link.b)
        for component in link.components:
            add(component.a)
            add(component.b)
    for entity in truth.entities:
        for member in entity.fields:
            add(member.ref)
    for batch in truth.batches:
        add(batch.key_field)
    return frozenset(out)


def truth_link_fields(truth: GroundTruth) -> frozenset[TruthField]:
    """The fields the link metrics need: ends of exact, bridge and composite truth links."""
    out: set[TruthField] = set()
    for link in truth.links:
        if link.link_type in ("exact", "bridge", "composite"):
            out.add((link.a.system_id, link.a.source_id, link.a.field))
            out.add((link.b.system_id, link.b.source_id, link.b.field))
    return frozenset(out)


# -- template to source -------------------------------------------------------------------------


def _locator_sources(locator_map: Path) -> dict[str, str]:
    """``event_id`` to the source id of its locator key (the key's first segment)."""
    sources: dict[str, str] = {}
    try:
        with locator_map.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                if len(line) > MAX_LOCATOR_LINE_BYTES:
                    msg = f"{locator_map}: line {number}: longer than {MAX_LOCATOR_LINE_BYTES}"
                    raise MappingError(msg)
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    msg = f"{locator_map}: line {number}: {exc.msg}"
                    raise MappingError(msg) from exc
                key = record.get("key") if isinstance(record, dict) else None
                event_id = record.get("event_id") if isinstance(record, dict) else None
                if not isinstance(key, str) or not isinstance(event_id, str):
                    msg = f"{locator_map}: line {number}: expected key and event_id"
                    raise MappingError(msg)
                sources[event_id] = key.partition(":")[0]
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"{locator_map}: cannot read ({type(exc).__name__})"
        raise MappingError(msg) from exc
    return sources


@dataclass(frozen=True, slots=True)
class TemplateSources:
    sources: Mapping[TemplateKey, str]
    """The majority source per template."""
    ambiguous: Mapping[TemplateKey, Mapping[str, int]]
    """Templates seen under more than one source, with the counts."""
    locator_mismatches: int
    """Events whose locator source differs from the event's own ``source_id``."""
    events: int


def template_sources(verified: VerifiedBundle, locator_map: Path | None) -> TemplateSources:
    """Stream the bundle once (module docstring)."""
    by_locator = _locator_sources(locator_map) if locator_map is not None else {}
    counts: dict[TemplateKey, Counter[str]] = defaultdict(Counter)
    mismatches = 0
    events = 0
    try:
        for event in iter_events(verified):
            events += 1
            source = by_locator.get(event.event_id, event.source_id)
            if source != event.source_id:
                mismatches += 1
            counts[(event.system_id, event.template_id)][source] += 1
    except BundleError as exc:
        msg = f"bundle events unreadable: {exc}"
        raise MappingError(msg) from exc
    sources = {key: counter.most_common(1)[0][0] for key, counter in counts.items()}
    ambiguous = {key: dict(counter) for key, counter in counts.items() if len(counter) > 1}
    return TemplateSources(sources, ambiguous, mismatches, events)


# -- the mapping --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EngineField:
    """One engine field as ``fields.json`` lists it."""

    system_id: str
    template_id: str
    path: str
    policy: str
    forms: tuple[str, ...]

    @property
    def field_ref(self) -> str:
        return f"{self.system_id}/{self.template_id}/{self.path}"


@dataclass(frozen=True, slots=True)
class MappedLink:
    """A predicted link with what the policy needs from the engine record."""

    link: PredictedLink
    link_id: str
    status: str
    engine_type: str
    family_usable: bool


@dataclass(frozen=True, slots=True)
class MappingReport:
    """What mapped, what did not, and why; JSON-safe through :meth:`to_json`."""

    engine_fields: int
    tokenized_fields: int
    mapped_to_truth: int
    """Tokenized engine fields whose name the truth knows."""
    unmappable: tuple[tuple[str, str], ...]
    """``(field_ref, reason)``: masked name or a template no source carries."""
    not_in_truth: tuple[tuple[str, str], ...]
    """``(field_ref, mapped name)``: tokenized fields the truth does not name."""
    unreached_truth_fields: tuple[str, ...]
    """Truth link fields (``system/source/field``) no engine field maps to."""
    unlinked_truth_fields: tuple[str, ...]
    """Truth link fields some engine field maps to but no predicted link touches."""
    dropped_links: tuple[tuple[str, str], ...]
    """``(link id, reason)`` of engine links left out of the predictions."""
    pass1_only_fields: int
    """Tokenized fields of templates with no event in the bundle (seen in pass 1 only, ADR
    0030); they carry no identifier rows and cannot take part in a link."""
    same_field_pairs: int
    """Engine links between two templates' copies of one source field (not scored)."""
    form_variants: int
    """Engine links collapsed into another form pair of the same field pair (not scored)."""
    template_duplicates: int
    """Engine links of the same field pair and forms from another template (not scored)."""
    ambiguous_templates: Mapping[str, Mapping[str, int]]
    locator_mismatches: int
    engine_links: int
    predicted_links: int
    family_links: int
    composite_links: int
    by_engine_type: Mapping[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "engine_fields": self.engine_fields,
            "tokenized_fields": self.tokenized_fields,
            "mapped_to_truth": self.mapped_to_truth,
            "unmappable": [list(item) for item in self.unmappable],
            "not_in_truth": [list(item) for item in self.not_in_truth],
            "unreached_truth_fields": list(self.unreached_truth_fields),
            "unlinked_truth_fields": list(self.unlinked_truth_fields),
            "dropped_links": [list(item) for item in self.dropped_links],
            "pass1_only_fields": self.pass1_only_fields,
            "same_field_pairs": self.same_field_pairs,
            "form_variants": self.form_variants,
            "template_duplicates": self.template_duplicates,
            "ambiguous_templates": {k: dict(v) for k, v in self.ambiguous_templates.items()},
            "locator_mismatches": self.locator_mismatches,
            "engine_links": self.engine_links,
            "predicted_links": self.predicted_links,
            "family_links": self.family_links,
            "composite_links": self.composite_links,
            "by_engine_type": dict(self.by_engine_type),
        }

    def to_markdown(self) -> str:
        """The "Field mapping" section of the eval report."""
        lines = [
            "## Field mapping (ADR 0049)",
            "",
            (
                f"{self.engine_fields} engine fields in the bundle, {self.tokenized_fields} "
                f"tokenized, {self.mapped_to_truth} of them named by the truth; "
                f"{self.pass1_only_fields} of them in templates without events (pass 1 only). "
                f"{self.engine_links} engine links: {self.predicted_links} scored as predictions "
                f"({self.family_links} usable for families, {self.composite_links} composite), "
                f"{self.form_variants} form variants of a scored field pair, "
                f"{self.template_duplicates} template duplicates of one and "
                f"{self.same_field_pairs} same-field pairs across templates not scored; "
                f"{self.locator_mismatches} locator mismatches."
            ),
            "",
        ]
        lines.extend(
            _listed(
                "Truth link fields no engine field reached (a naming mismatch, not a miss):",
                self.unreached_truth_fields,
            )
        )
        lines.extend(
            _listed(
                "Truth link fields reached by an engine field but by no predicted link (misses):",
                self.unlinked_truth_fields,
            )
        )
        lines.extend(
            _listed(
                "Engine fields that could not be mapped (masked name or template without a "
                "source):",
                [f"{ref}: {reason}" for ref, reason in self.unmappable],
            )
        )
        lines.extend(
            _listed(
                "Tokenized engine fields the truth does not name (mapped to their leaf name):",
                [f"{ref} -> {name}" for ref, name in self.not_in_truth],
            )
        )
        lines.extend(
            _listed(
                "Engine links left out of the predictions:",
                [f"{link_id}: {reason}" for link_id, reason in self.dropped_links],
            )
        )
        if self.ambiguous_templates:
            lines.extend(
                _listed(
                    "Templates seen under more than one source (majority source used):",
                    [f"{key}: {counts}" for key, counts in self.ambiguous_templates.items()],
                )
            )
        return "\n".join(lines) + "\n"


def _listed(title: str, items: Sequence[str]) -> list[str]:
    if not items:
        return [f"{title} none.", ""]
    shown = list(items[:MAX_LISTED])
    lines = [title, *(f"- {item}" for item in shown)]
    if len(items) > MAX_LISTED:
        lines.append(f"- ... and {len(items) - MAX_LISTED} more (see the JSON)")
    lines.append("")
    return lines


def family_usable(link_type: str, role: str, score: float, threshold: float) -> bool:
    """ADR 0049 item 2 as the engine applies it (``KeyLink.is_usable_for_families``): proposed
    or accepted exact links and bridges of role ``transaction`` at or above the proposal
    threshold count as accepted for family building. A bridge of role ``batch`` (one file to
    many shipments) or ``association`` never merges families (spec 9.6, 9.7); composite links
    and association or batch role exact links do not either."""
    if score < threshold or link_type == "composite":
        return False
    return role == "transaction"


def policy_statement(threshold: float) -> str:
    return (
        "Acceptance policy (ADR 0049 item 2): the product keeps auto-accept off; here every "
        f"engine proposal at or above the proposal threshold {threshold:.2f} whose role is "
        "transaction, exact or bridge, counts as accepted for family building, so entity purity "
        "measures the engine and not a simulated reviewer. Families are connected components "
        "over fields (system, source, field), whatever forms the links use, and each member is "
        "reported once, as raw when a link reaches it as raw and else in the form most of its "
        "links use; a bridge of role batch or association (a file to its shipments) joins no "
        "family, as in the engine (spec 9.6, 9.7). Composite links count "
        "only toward the composite link metrics; association role links are reported but join "
        "no family; an exact link whose role is batch is reported as a batch key link (truth "
        "link type `batch`). The truth knows fields by source and names one form pair per link, "
        "so a link "
        "between two templates' copies of one field is not scored and, per field pair, the "
        "engine link with the most raw ends (then the highest score) stands for its form "
        "variants."
    )


class FieldMapping:
    """See the module docstring. Build with :meth:`from_bundle` or directly from the parts."""

    def __init__(
        self,
        *,
        sources: Mapping[TemplateKey, str],
        truth_fields: Collection[TruthField],
        truth_link_fields: Collection[TruthField] = (),
        engine_fields: Iterable[EngineField] = (),
        ambiguous: Mapping[TemplateKey, Mapping[str, int]] | None = None,
        locator_mismatches: int = 0,
    ) -> None:
        self._sources = dict(sources)
        self._truth = frozenset(truth_fields)
        self._truth_links = frozenset(truth_link_fields)
        self._engine_fields = tuple(engine_fields)
        self._ambiguous = dict(ambiguous or {})
        self._mismatches = locator_mismatches

    @classmethod
    def from_bundle(
        cls, verified: VerifiedBundle, truth: GroundTruth, *, locator_map: Path | None
    ) -> FieldMapping:
        """The mapping of one analyzer run against one ground truth."""
        found = template_sources(verified, locator_map)
        truth_sources = {(source.system_id, source.source_id) for source in truth.sources}
        unknown = sorted(
            key for key, source in found.sources.items() if (key[0], source) not in truth_sources
        )
        if unknown:
            msg = (
                f"{len(unknown)} templates belong to sources the ground truth does not list "
                f"(first: {unknown[0][0]}/{unknown[0][1]}); the analyzer configuration must use "
                "the source and system ids of ground_truth/sources.json"
            )
            raise MappingError(msg)
        _templates, fields = read_bundle_metadata(verified)
        return cls(
            sources=found.sources,
            truth_fields=truth_field_names(truth),
            truth_link_fields=truth_link_fields(truth),
            engine_fields=[
                EngineField(
                    item.system_id, item.template_id, item.path, item.policy, tuple(item.forms)
                )
                for item in fields
            ],
            ambiguous=found.ambiguous,
            locator_mismatches=found.locator_mismatches,
        )

    @property
    def sources(self) -> Mapping[TemplateKey, str]:
        return self._sources

    def source_of(self, system_id: str, template_id: str) -> str | None:
        return self._sources.get((system_id, template_id))

    def map_path(
        self, system_id: str, template_id: str, path: str
    ) -> tuple[FieldRef | None, str | None, bool]:
        """``(ref, reason, known)``: the truth field a path maps to, why it cannot, and whether
        the truth names that field."""
        source = self.source_of(system_id, template_id)
        if source is None:
            return None, "template carried by no source", False
        candidates = name_candidates(path)
        if not candidates:
            return None, "masked name", False
        for candidate in candidates:
            if (system_id, source, candidate) in self._truth:
                return FieldRef(system_id=system_id, source_id=source, field=candidate), None, True
        name = leaf_name(path) or candidates[0]
        return FieldRef(system_id=system_id, source_id=source, field=name), None, False

    def map_field_ref(self, field_ref: str) -> FieldRef | None:
        """The truth field of a ``system/template/path`` reference, or None."""
        system_id, template_id, path = split_field_ref(field_ref)
        ref, _reason, _known = self.map_path(system_id, template_id, path)
        return ref

    def map_end(self, end: Mapping[str, Any]) -> tuple[FieldRef | None, str | None]:
        """An ``engine_links.json`` end (``system_id``, ``template_id``, ``path``) to a truth
        field and the reason when it cannot be mapped."""
        ref, reason, _known = self.map_path(
            str(end.get("system_id", "")), str(end.get("template_id", "")), str(end.get("path", ""))
        )
        return ref, reason

    # -- links and entities -------------------------------------------------------------------

    def map_links(
        self, engine_links: Sequence[Mapping[str, Any]], *, threshold: float
    ) -> tuple[list[MappedLink], MappingReport]:
        """Predictions from ``engine_links.json`` records under the ADR 0049 policy."""
        candidates: list[MappedLink] = []
        dropped: list[tuple[str, str]] = []
        touched: set[TruthField] = set()
        by_type: Counter[str] = Counter()
        same_field = 0
        for record in engine_links:
            link_id = str(record.get("id", "?"))
            status = str(record.get("status", ""))
            if status not in ENGINE_STATUSES:
                dropped.append((link_id, f"status {status}"))
                continue
            end_a = record.get("a")
            end_b = record.get("b")
            if not isinstance(end_a, Mapping) or not isinstance(end_b, Mapping):
                dropped.append((link_id, "malformed ends"))
                continue
            ref_a, reason_a = self.map_end(end_a)
            ref_b, reason_b = self.map_end(end_b)
            if ref_a is None or ref_b is None:
                dropped.append((link_id, reason_a or reason_b or "unmapped end"))
                continue
            engine_type = str(record.get("link_type", "exact"))
            role = str(record.get("role", "association"))
            score = float(record.get("score", 0.0))
            link_type = _truth_link_type(engine_type, role)
            form_a = str(end_a.get("form", "raw"))
            form_b = str(end_b.get("form", "raw"))
            if ref_a == ref_b:
                same_field += 1
                continue
            predicted = PredictedLink(
                a=ref_a,
                form_a=form_a,
                b=ref_b,
                form_b=form_b,
                link_type=link_type,
                role=_truth_role(role),
                score=min(1.0, max(0.0, score)),
            )
            candidates.append(
                MappedLink(
                    link=predicted,
                    link_id=link_id,
                    status=status,
                    engine_type=engine_type,
                    family_usable=family_usable(engine_type, role, score, threshold),
                )
            )
        mapped = collapse_forms(candidates)
        kept = {_collapse_key(item): item for item in mapped}
        variants = sum(
            1
            for item in candidates
            if item not in mapped
            and (item.link.form_a, item.link.form_b)
            != (kept[_collapse_key(item)].link.form_a, kept[_collapse_key(item)].link.form_b)
        )
        for item in mapped:
            by_type[item.engine_type] += 1
            for ref in (item.link.a, item.link.b):
                touched.add((ref.system_id, ref.source_id, ref.field))
        report = self._report(
            mapped,
            dropped,
            touched,
            len(engine_links),
            by_type,
            same_field=same_field,
            form_variants=variants,
            template_duplicates=len(candidates) - len(mapped) - variants,
        )
        return mapped, report

    def map_entities(self, engine_entities: Sequence[Mapping[str, Any]]) -> list[PredictedEntity]:
        """The engine's own families as predictions; members that cannot be mapped are left
        out, a family left without members is dropped."""
        out: list[PredictedEntity] = []
        for record in engine_entities:
            members: list[EntityField] = []
            raw_members = record.get("members")
            if not isinstance(raw_members, list):
                continue
            for member in raw_members:
                if not isinstance(member, Mapping):
                    continue
                ref, _reason = self.map_end(member)
                if ref is not None:
                    members.append(EntityField(ref=ref, form=str(member.get("form", "raw"))))
            if members:
                out.append(
                    PredictedEntity(entity_id=str(record.get("entity_id", "?")), fields=members)
                )
        return out

    def _report(
        self,
        mapped: Sequence[MappedLink],
        dropped: Sequence[tuple[str, str]],
        touched: Collection[TruthField],
        engine_links: int,
        by_type: Mapping[str, int],
        *,
        same_field: int = 0,
        form_variants: int = 0,
        template_duplicates: int = 0,
    ) -> MappingReport:
        unmappable: list[tuple[str, str]] = []
        not_in_truth: list[tuple[str, str]] = []
        reached: set[TruthField] = set()
        tokenized = 0
        known_count = 0
        pass1_only = 0
        for engine_field in self._engine_fields:
            if engine_field.policy != "tokenize":
                continue
            tokenized += 1
            if (engine_field.system_id, engine_field.template_id) not in self._sources:
                pass1_only += 1
                continue
            ref, reason, known = self.map_path(
                engine_field.system_id, engine_field.template_id, engine_field.path
            )
            if ref is None:
                unmappable.append((engine_field.field_ref, reason or "unmapped"))
            elif known:
                known_count += 1
                reached.add((ref.system_id, ref.source_id, ref.field))
            else:
                not_in_truth.append((engine_field.field_ref, ref.field))
        unreached = sorted(self._truth_links - reached) if self._engine_fields else []
        unlinked = sorted((self._truth_links & reached) - set(touched))
        return MappingReport(
            engine_fields=len(self._engine_fields),
            tokenized_fields=tokenized,
            mapped_to_truth=known_count,
            unmappable=tuple(unmappable),
            not_in_truth=tuple(not_in_truth),
            unreached_truth_fields=tuple("/".join(item) for item in unreached),
            unlinked_truth_fields=tuple("/".join(item) for item in unlinked),
            dropped_links=tuple(dropped),
            pass1_only_fields=pass1_only,
            same_field_pairs=same_field,
            form_variants=form_variants,
            template_duplicates=template_duplicates,
            ambiguous_templates={
                f"{system}/{template}": counts
                for (system, template), counts in sorted(self._ambiguous.items())
            },
            locator_mismatches=self._mismatches,
            engine_links=engine_links,
            predicted_links=len(mapped),
            family_links=sum(1 for item in mapped if item.family_usable),
            composite_links=sum(1 for item in mapped if item.engine_type == "composite"),
            by_engine_type=dict(by_type),
        )


def _truth_link_type(engine_type: str, role: str) -> LinkType:
    if engine_type == "composite":
        return "composite"
    if engine_type == "bridge":
        return "bridge"
    if role == "batch":
        return "batch"
    return "exact"


def _truth_role(role: str) -> Any:
    return role if role in ("transaction", "association", "batch") else "association"


def _collapse_rank(item: MappedLink) -> tuple[int, float, str]:
    raws = sum(1 for form in (item.link.form_a, item.link.form_b) if form == "raw")
    return (-raws, -item.link.score, item.link_id)


def _collapse_key(item: MappedLink) -> tuple[TruthField, TruthField, str]:
    link = item.link
    first, second = sorted(
        (
            (link.a.system_id, link.a.source_id, link.a.field),
            (link.b.system_id, link.b.source_id, link.b.field),
        )
    )
    return (first, second, item.engine_type)


def collapse_forms(mapped: Sequence[MappedLink]) -> list[MappedLink]:
    """One link per unordered pair of truth fields and engine link type (module docstring):
    the most ``raw`` ends, then the highest score, then the link id. Order is kept. With the
    linker proposing one link per field pair this removes template duplicates only (the same
    source field in several templates)."""
    best: dict[tuple[TruthField, TruthField, str], MappedLink] = {}
    for item in mapped:
        key = _collapse_key(item)
        current = best.get(key)
        if current is None or _collapse_rank(item) < _collapse_rank(current):
            best[key] = item
    keep = {id(item) for item in best.values()}
    return [item for item in mapped if id(item) in keep]


def member_form(counts: Counter[str]) -> str:
    """The one form a family member is reported with (see :func:`policy_families`)."""
    if counts.get("raw"):
        return "raw"
    return min(counts, key=lambda form: (-counts[form], form)) if counts else "raw"


def policy_families(mapped: Sequence[MappedLink]) -> list[PredictedEntity]:
    """Connected components over the ``family_usable`` links (ADR 0049 item 2), keyed by field
    ``(system, source, field)`` so a field linked through ``raw`` here and ``digits.0`` there
    sits in one family (the engine's families are over fields too). The metric counts members
    as ``(field, form)``, so each field is one member with the form its links use: ``raw`` when
    any usable link reaches it as ``raw``, else the form most of them use (ties by name). One
    :class:`PredictedEntity` per component, id ``fam_<n>`` in member order."""
    parent: dict[TruthField, TruthField] = {}
    forms: dict[TruthField, Counter[str]] = defaultdict(Counter)

    def find(key: TruthField) -> TruthField:
        root = parent.setdefault(key, key)
        while root != parent[root]:
            root = parent[root]
        while parent[key] != root:
            parent[key], key = root, parent[key]
        return root

    for item in mapped:
        if not item.family_usable:
            continue
        link = item.link
        first = (link.a.system_id, link.a.source_id, link.a.field)
        second = (link.b.system_id, link.b.source_id, link.b.field)
        forms[first][link.form_a] += 1
        forms[second][link.form_b] += 1
        root_a, root_b = find(first), find(second)
        if root_a != root_b:
            if root_b < root_a:
                root_a, root_b = root_b, root_a
            parent[root_b] = root_a
    groups: dict[TruthField, set[TruthField]] = defaultdict(set)
    for key in list(parent):
        groups[find(key)].add(key)
    entities: list[PredictedEntity] = []
    for index, members in enumerate(sorted(sorted(group) for group in groups.values()), start=1):
        entities.append(
            PredictedEntity(
                entity_id=f"fam_{index:04d}",
                fields=[
                    EntityField(
                        ref=FieldRef(system_id=system, source_id=source, field=name),
                        form=member_form(forms[(system, source, name)]),
                    )
                    for system, source, name in members
                ],
            )
        )
    return entities


def truth_pairs(truth: GroundTruth, link_types: Collection[str]) -> frozenset[Any]:
    """The unordered ``(field, form)`` pairs of the truth links of ``link_types`` (what
    ``fit-weights`` labels against)."""
    return frozenset(
        link_pair(link.a, link.form_a, link.b, link.form_b)
        for link in truth.links
        if link.link_type in link_types
    )
