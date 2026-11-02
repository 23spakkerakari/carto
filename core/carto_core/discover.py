"""``carto-core discover``: one full profile pass and one full link pass over the store, the
engine's links and families written as files (plan M2 "How the engine runs" step 6; spec 21
M2 gate; ADR 0046, 0048, 0049).

:func:`discover` is the injectable core: it takes the engine's collaborators as
:class:`EngineParts` (the eval harness hands it in-memory ones, :func:`run_discover` the
deployment ones), runs :class:`~carto_core.profiler.run.ProfilePass` with ``full=True`` and
:class:`~carto_core.linker.run.LinkPass` with ``full=True`` at ``as_of`` with the
:class:`~carto_core.linker.composite.CompositeMatcher` hooked in, and writes to ``out_dir``:

- ``engine_links.json``: every stored key link (status, type, role, direction, score, the two
  ends as ``system_id/template_id/path`` plus form, affected volume, the spec 9.3 evidence and
  the review-item extras);
- ``engine_entities.json``: the entity families with their member ends;
- ``discover_summary.json``: the counts of both passes and of the composite run, the profiler
  and linker settings in effect, the weights version, ``as_of`` and the link window.

Nothing written is a token or a raw value: field references, forms, counts, scores and the
sentences built from metadata (spec 2.3 invariant 2).

**The reference time.** The window of both passes ends at ``as_of``. When the caller gives
none, a loaded bundle supplies it: the manifest's ``last_observed_at`` plus one second, so a
bundle analyzed long after its data was exported is discovered over its own last seven days.
Without a bundle the store's latest ``stored_at`` (``EventStore.max_stored_at``) stands in, which
is the time of the latest write, not of the latest observation: right for a live install, where
the two are minutes apart, and the reason ``carto-core discover --as-of`` exists for a bundle
loaded earlier with ``load-bundle``. An empty store falls back to the clock.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from carto_common.logging import get_logger
from carto_core.bundle import (
    LoadResult,
    VerifiedBundle,
    load_bundle,
    record_bundle_metadata,
    verify_bundle,
)
from carto_core.fieldpaths import split_field_ref
from carto_core.linker.composite import CompositeMatcher, CompositeReport
from carto_core.linker.profile_source import SketchProfileSource, profiler_seams
from carto_core.linker.run import LinkPass, LinkReport
from carto_core.profiler.repository import MetadataFieldProfileSink
from carto_core.profiler.run import ProfilePass, ProfileReport
from carto_core.store.model import FieldForm, StoreError, Window

if TYPE_CHECKING:
    from carto_core.audit import AuditRecorder
    from carto_core.linker.composite import CompositeRuleRepository
    from carto_core.linker.model import KeyLink
    from carto_core.linker.repository import Entity, EntityRepository, LinkRepository
    from carto_core.metadata import MetadataStore
    from carto_core.profiler.repository import SketchRepository, WatermarkStore
    from carto_core.settings import CoreSettings
    from carto_core.store.model import EventStore

__all__ = [
    "ENGINE_ENTITIES_FILE",
    "ENGINE_LINKS_FILE",
    "SUMMARY_FILE",
    "DiscoverError",
    "DiscoverResult",
    "EngineParts",
    "default_as_of",
    "discover",
    "end_view",
    "entity_view",
    "link_view",
    "parse_as_of",
    "run_discover",
    "summary_lines",
]

log = get_logger(component="discover")

ENGINE_LINKS_FILE: Final = "engine_links.json"
ENGINE_ENTITIES_FILE: Final = "engine_entities.json"
SUMMARY_FILE: Final = "discover_summary.json"
_AS_OF_STEP: Final = timedelta(seconds=1)


class DiscoverError(Exception):
    """Discovery could not run; the message never carries data."""


@dataclass(frozen=True, slots=True)
class EngineParts:
    """Everything the two passes read and write. The eval harness builds it from the in-memory
    doubles, :func:`run_discover` from ClickHouse and PostgreSQL."""

    store: EventStore
    metadata: MetadataStore
    sketches: SketchRepository
    watermarks: WatermarkStore
    links: LinkRepository
    entities: EntityRepository
    composite_rules: CompositeRuleRepository
    audit: AuditRecorder
    settings: CoreSettings


@dataclass(frozen=True, slots=True)
class DiscoverResult:
    """What one discovery run did and where it wrote."""

    tenant_id: str
    as_of: datetime
    window: Window
    profile: ProfileReport
    link: LinkReport
    composite: CompositeReport | None
    links: tuple[KeyLink, ...]
    entities: tuple[Entity, ...]
    out_dir: Path
    bundle: LoadResult | None = None
    bundle_id: str | None = None
    extra_counts: Mapping[str, int] = field(default_factory=dict)


def parse_as_of(text: str) -> datetime:
    """``--as-of``: an ISO 8601 date (midnight UTC) or a time with an offset. Raises
    :class:`DiscoverError` naming the expected forms, never the text."""
    value = text.strip()
    try:
        if len(value) == len("2026-10-08"):
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), UTC)
        parsed = datetime.fromisoformat(value)
    except ValueError:
        msg = "--as-of must be an ISO 8601 date or a time with an offset"
        raise DiscoverError(msg) from None
    if parsed.tzinfo is None:
        msg = "--as-of needs an offset (for example 2026-10-08T00:00:00Z)"
        raise DiscoverError(msg)
    return parsed.astimezone(UTC)


def default_as_of(
    store: EventStore,
    tenant_id: str,
    *,
    bundle: VerifiedBundle | None = None,
    clock: Callable[[], datetime] | None = None,
) -> datetime:
    """The reference time when the caller gives none (module docstring)."""
    if bundle is not None and bundle.manifest.last_observed_at is not None:
        return bundle.manifest.last_observed_at.astimezone(UTC) + _AS_OF_STEP
    latest = store.max_stored_at(tenant_id)
    if latest is not None:
        return latest.astimezone(UTC) + _AS_OF_STEP
    now = clock if clock is not None else (lambda: datetime.now(UTC))
    return now()


# -- views --------------------------------------------------------------------------------------


def end_view(end: FieldForm) -> dict[str, str]:
    system_id, template_id, path = split_field_ref(end.field_ref)
    return {
        "field_ref": end.field_ref,
        "system_id": system_id,
        "template_id": template_id,
        "path": path,
        "form": end.form,
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def link_view(link: KeyLink) -> dict[str, Any]:
    """One ``engine_links.json`` record; the evidence is :meth:`PairFeatures.as_evidence`."""
    return {
        "id": link.id,
        "status": link.status.value,
        "link_type": link.link_type.value,
        "role": link.role.value,
        "direction": link.direction.value,
        "score": link.score,
        "a": end_view(link.a),
        "b": end_view(link.b),
        "affected_volume": link.affected_volume,
        "entity_id": link.entity_id,
        "version": link.version,
        "first_proposed_at": _iso(link.first_proposed_at),
        "updated_at": _iso(link.updated_at),
        "evidence": link.features.as_evidence(),
        "extra": _json_safe(dict(link.extra)),
    }


def entity_view(entity: Entity) -> dict[str, Any]:
    return {
        "entity_id": entity.id,
        "name": entity.name,
        "suggested_name": entity.suggested_name,
        "name_confirmed": entity.name_confirmed,
        "members": [end_view(member) for member in entity.members],
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _counts(report: object) -> dict[str, int | bool | str]:
    """The scalar fields of a pass report (counts, flags, days as ISO text)."""
    out: dict[str, int | bool | str] = {}
    for name in getattr(type(report), "__dataclass_fields__", {}):
        value = getattr(report, name)
        if isinstance(value, bool | int | str):
            out[name] = value
        elif isinstance(value, date):
            out[name] = value.isoformat()
    return out


def _write_json(path: Path, payload: object) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


# -- the run ------------------------------------------------------------------------------------


def discover(
    parts: EngineParts,
    *,
    tenant_id: str,
    out_dir: Path,
    as_of: datetime,
    bundle: LoadResult | None = None,
    bundle_id: str | None = None,
) -> DiscoverResult:
    """Run both passes at ``as_of`` and write the three files (module docstring)."""
    settings = parts.settings
    clock = lambda: as_of  # noqa: E731 - the passes read "now" from this
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"cannot create {out_dir}: {type(exc).__name__}"
        raise DiscoverError(msg) from exc
    system_names = {system.id: system.name for system in parts.metadata.list_systems(tenant_id)}
    try:
        profile = ProfilePass(
            parts.store,
            parts.sketches,
            MetadataFieldProfileSink(parts.metadata),
            parts.watermarks,
            settings.profiler,
        ).run(tenant_id, full=True, now=as_of)
        seams = profiler_seams(settings.profiler)
        matcher = CompositeMatcher(
            store=parts.store,
            settings=settings.linker,
            rules=parts.composite_rules,
            clock=clock,
            system_names=system_names,
        )
        link_pass = LinkPass(
            store=parts.store,
            profiles=SketchProfileSource(
                parts.sketches, parts.store, settings.profiler, clock=clock
            ),
            links=parts.links,
            entities=parts.entities,
            audit=parts.audit,
            settings=settings.linker,
            profiler_settings=settings.profiler,
            clock=clock,
            containment=seams.containment,
            domain_size=seams.domain_size,
            decode_minhash=seams.decode_minhash,
            system_names=system_names,
            composite_hook=matcher.hook,
        )
        link = link_pass.run(tenant_id, full=True, now=as_of)
        links = tuple(parts.links.list_links(tenant_id))
        entities = tuple(parts.entities.list_entities(tenant_id))
    except (StoreError, ValueError) as exc:
        msg = f"discovery failed: {type(exc).__name__}"
        raise DiscoverError(msg) from exc
    except Exception as exc:
        # Repository errors of the profiler and linker packages are both named RepositoryError.
        if type(exc).__name__ not in ("RepositoryError", "CandidateError", "WeightsError"):
            raise
        msg = f"discovery failed: {type(exc).__name__}"
        raise DiscoverError(msg) from exc
    result = DiscoverResult(
        tenant_id=tenant_id,
        as_of=as_of,
        window=link.window,
        profile=profile,
        link=link,
        composite=matcher.last_report,
        links=links,
        entities=entities,
        out_dir=out_dir,
        bundle=bundle,
        bundle_id=bundle_id,
    )
    try:
        _write_json(out_dir / ENGINE_LINKS_FILE, [link_view(item) for item in links])
        _write_json(out_dir / ENGINE_ENTITIES_FILE, [entity_view(item) for item in entities])
        _write_json(out_dir / SUMMARY_FILE, summary_document(result, settings))
    except OSError as exc:
        msg = f"cannot write to {out_dir}: {type(exc).__name__}"
        raise DiscoverError(msg) from exc
    log.info(
        "discover done",
        tenant_id=tenant_id,
        links=len(links),
        entities=len(entities),
        candidates=link.candidates_considered,
        composite_links=link.composite_links,
    )
    return result


def summary_document(result: DiscoverResult, settings: CoreSettings) -> dict[str, Any]:
    """``discover_summary.json``: counts, settings, weights version, ``as_of`` and window."""
    link = result.link
    composite: dict[str, Any] | None = None
    if result.composite is not None:
        composite = {
            "pairs_considered": result.composite.pairs_considered,
            "pairs_triggered": result.composite.pairs_triggered,
            "rules": result.composite.rules,
            "matched_pairs": result.composite.matched_pairs,
            "capped": result.composite.capped,
            "triggers": [
                {
                    "system_a": outcome.trigger.system_a,
                    "system_b": outcome.trigger.system_b,
                    "reason": outcome.trigger.reason,
                    "correlation": outcome.trigger.correlation,
                    "lag_hours": outcome.trigger.lag_hours,
                    "events_a": outcome.events_a,
                    "events_b": outcome.events_b,
                    "blocked_pairs": outcome.blocked_pairs,
                    "matched_pairs": len(outcome.matches),
                    "rule": outcome.rule is not None,
                }
                for outcome in result.composite.outcomes
            ],
        }
    status_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for item in result.links:
        status_counts[item.status.value] = status_counts.get(item.status.value, 0) + 1
        type_counts[item.link_type.value] = type_counts.get(item.link_type.value, 0) + 1
    profile_counts = _counts(result.profile)
    link_counts = {
        key: value for key, value in _counts(link).items() if key not in ("tenant_id", "window")
    }
    document: dict[str, Any] = {
        "tenant_id": result.tenant_id,
        "as_of": _iso(result.as_of),
        "window": {"start": _iso(result.window.start), "end": _iso(result.window.end)},
        "weights_version": link.weights_version,
        "profile": profile_counts,
        "link": link_counts,
        "composite": composite,
        "links": {"total": len(result.links), "by_status": status_counts, "by_type": type_counts},
        "entities": len(result.entities),
        "settings": {
            "profiler": settings.profiler.model_dump(mode="json"),
            "linker": settings.linker.model_dump(mode="json"),
        },
    }
    if result.bundle is not None:
        document["bundle"] = {
            "bundle_id": result.bundle_id,
            "events_written": result.bundle.events_written,
            "chunks": result.bundle.chunks,
            "duplicates": result.bundle.duplicates,
            "too_old": result.bundle.too_old,
            "future": result.bundle.future,
        }
    if result.extra_counts:
        document["extra"] = dict(result.extra_counts)
    return document


def summary_lines(result: DiscoverResult) -> list[str]:
    """Human-readable lines for the command: counts and paths only."""
    link = result.link
    lines = [
        (
            f"discover: tenant {result.tenant_id}, as of {_iso(result.as_of)}, window "
            f"{_iso(result.window.start)} to {_iso(result.window.end)}"
        ),
        (
            f"  profile    {result.profile.rows_read:,} identifier rows, "
            f"{result.profile.ends_touched:,} ends, {result.profile.profiles_written:,} profiles"
        ),
        (
            f"  candidates {link.candidate_ends:,} candidate ends, "
            f"{link.candidates_considered:,} pairs verified ({link.bridge_candidates:,} bridges, "
            f"{link.truncated:,} beyond the cap), {link.filtered:,} filtered"
        ),
        (
            f"  links      {len(result.links):,} stored ({link.proposed:,} proposed this pass, "
            f"{link.auto_accepted:,} auto-accepted, {link.stale:,} stale), "
            f"{link.composite_links:,} composite, weights {link.weights_version}"
        ),
        f"  families   {len(result.entities):,} ({link.families_named:,} named)",
        (
            f"  wrote      {result.out_dir / ENGINE_LINKS_FILE}, {ENGINE_ENTITIES_FILE}, "
            f"{SUMMARY_FILE}"
        ),
    ]
    if result.bundle is not None:
        lines.insert(
            1,
            (
                f"  bundle     {result.bundle_id}: {result.bundle.events_written:,} events "
                f"written in {result.bundle.chunks:,} chunks, {result.bundle.duplicates:,} "
                "already present"
            ),
        )
    return lines


# -- deployment wiring ---------------------------------------------------------------------------


def run_discover(
    settings: CoreSettings, *, out_dir: Path, bundle: Path | None, as_of: str | None
) -> list[str]:
    """``carto-core discover``: the real stores, the bundle (verified and loaded when given),
    one discovery, the summary lines. Raises :class:`DiscoverError`, :class:`BundleError` and
    the stores' errors for the command to report."""
    from carto_core.audit_postgres import PostgresAuditLog  # noqa: PLC0415 - wiring only
    from carto_core.db.clickhouse import ClickHouseWriter, create_client  # noqa: PLC0415
    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.ingest.ledger import PostgresBatchLedger  # noqa: PLC0415
    from carto_core.linker.composite import PostgresCompositeRuleRepository  # noqa: PLC0415
    from carto_core.linker.repository import (  # noqa: PLC0415
        PostgresEntityRepository,
        PostgresLinkRepository,
    )
    from carto_core.metadata import PostgresMetadataStore  # noqa: PLC0415
    from carto_core.profiler.repository import (  # noqa: PLC0415
        PostgresSketchRepository,
        PostgresWatermarkStore,
    )
    from carto_core.store.clickhouse import ClickHouseStore  # noqa: PLC0415

    reference = parse_as_of(as_of) if as_of is not None else None
    verified = verify_bundle(bundle) if bundle is not None else None
    client = create_client(settings.clickhouse)
    try:
        engine = create_engine_from_settings(settings.postgres)
        try:
            store = ClickHouseStore(client)
            metadata = PostgresMetadataStore(engine)
            loaded: LoadResult | None = None
            if verified is not None:
                loaded = load_bundle(
                    verified,
                    ClickHouseWriter(client),
                    PostgresBatchLedger(engine),
                    retention=settings.retention,
                    claim_timeout=timedelta(seconds=settings.ingest.claim_timeout_seconds),
                )
                record_bundle_metadata(verified, metadata, templates=loaded.events_written > 0)
            parts = EngineParts(
                store=store,
                metadata=metadata,
                sketches=PostgresSketchRepository(engine),
                watermarks=PostgresWatermarkStore(engine),
                links=PostgresLinkRepository(engine),
                entities=PostgresEntityRepository(engine),
                composite_rules=PostgresCompositeRuleRepository(engine),
                audit=PostgresAuditLog(engine),
                settings=settings,
            )
            if reference is None:
                reference = default_as_of(store, settings.tenant_id, bundle=verified)
            result = discover(
                parts,
                tenant_id=settings.tenant_id,
                out_dir=out_dir,
                as_of=reference,
                bundle=loaded,
                bundle_id=verified.manifest.bundle_id if verified is not None else None,
            )
        finally:
            engine.dispose()
    finally:
        client.close()
    return summary_lines(result)
