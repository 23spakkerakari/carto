"""Read-only inventory (ADR 0050; spec 7.3, 12): ``/systems``, ``/sources``, ``/fields``.

Everything here is what the edge reported or the profiler computed; the console shows it as
text (spec 13), and connector configuration arrives without secrets (``secret_ref`` is a name).
The one write is ``PATCH /systems/{id}`` (name and description), which spec 14.2 files under
"name entities and nodes", so it is ``review_links``; ``manage_sources`` stays for the
control plane of M6. Fields are addressed by their ``field_ref`` (``system/template/path``), so
the path parameter takes slashes.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from carto_common.logging import get_logger
from carto_core.api.auth import Principal
from carto_core.api.context import ApiContext, context_of, record_audit
from carto_core.api.etag import etag_of, require_if_match
from carto_core.api.ports import (
    MAX_CURSOR_LEN,
    DecisionError,
    FieldRecord,
    SourceRecord,
    SystemRecord,
)
from carto_core.api.problems import Problem
from carto_core.api.routes_links import decision_problem
from carto_core.api.sessions import Guard
from carto_core.ingest.health import SourceHealthRecord
from carto_schema.api import (
    MAX_PAGE_SIZE,
    FieldView,
    Page,
    Permission,
    SourceHealthView,
    SourceView,
    SystemPatch,
    SystemView,
)
from carto_schema.bundle import ShapeShare

__all__ = ["field_view", "router", "source_view", "system_view"]

log = get_logger(component="api")
router = APIRouter(tags=["inventory"])

VIEW: Final = Guard(Permission.VIEW)
REVIEW: Final = Guard(Permission.REVIEW_LINKS)

Ident = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
FieldId = Annotated[str, Path(min_length=1, max_length=512)]
Cursor = Annotated[str | None, Query(max_length=MAX_CURSOR_LEN)]
Limit = Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)]
SystemFilter = Annotated[
    str | None, Query(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
]


def system_view(record: SystemRecord) -> SystemView:
    return SystemView(
        id=record.id,
        name=record.name,
        description=record.description,
        owner_group=record.owner_group,
        criticality=record.criticality,
        source_count=record.source_count,
        updated_at=record.updated_at,
        etag=etag_of(record.version),
    )


def health_view(record: SourceHealthRecord) -> SourceHealthView:
    return SourceHealthView(
        status=record.status,
        last_success_at=record.last_success_at,
        lag_seconds=record.lag_seconds,
        error_count=record.error_count,
        buffer_depth=record.buffer_depth,
        oldest_buffered_at=record.oldest_buffered_at,
        message=record.message,
        received_at=record.received_at,
    )


def source_view(record: SourceRecord) -> SourceView:
    return SourceView(
        id=record.id,
        system_id=record.system_id,
        type=record.type,
        enabled=record.enabled,
        config=dict(record.config),
        secret_ref=record.secret_ref,
        backfill_days=record.backfill_days,
        schedule=record.schedule,
        health=health_view(record.health) if record.health is not None else None,
        last_test=record.last_test,
        updated_at=record.updated_at,
    )


def field_view(record: FieldRecord) -> FieldView:
    return FieldView(
        id=record.id,
        system_id=record.system_id,
        template_id=record.template_id,
        path=record.path,
        field_class=record.field_class,  # type: ignore[arg-type]
        policy=record.policy,  # type: ignore[arg-type]
        pinned=record.pinned,
        count=record.count,
        distinct_estimate=record.distinct_estimate,
        null_rate=record.null_rate,
        shapes=[ShapeShare(shape=shape, share=share) for shape, share in record.shapes[:8]],
        forms=list(record.forms[:16]),
        quality=record.quality,
        label=record.label,
        suggested_label=record.suggested_label,
        first_seen=record.first_seen,
        last_seen=record.last_seen,
    )


# --- systems -----------------------------------------------------------------------------------


async def _load_system(context: ApiContext, system_id: str) -> SystemRecord:
    record = await run_in_threadpool(context.ports.systems.get, context.tenant_id, system_id)
    if record is None:
        raise Problem(404, "not found", "no such system")
    return record


@router.get("/systems", response_model=Page[SystemView], **VIEW.route())
async def list_systems(
    request: Request,
    cursor: Cursor = None,
    limit: Limit = None,
    principal: Principal = VIEW.principal,
) -> Page[SystemView]:
    del principal
    context = context_of(request)
    page = await run_in_threadpool(
        context.ports.systems.list, context.tenant_id, cursor, context.page_limit(limit)
    )
    return Page[SystemView](
        items=[system_view(record) for record in page.items], next_cursor=page.next_cursor
    )


@router.get("/systems/{system_id}", response_model=SystemView, **VIEW.route())
async def get_system(
    request: Request, response: Response, system_id: Ident, principal: Principal = VIEW.principal
) -> SystemView:
    del principal
    record = await _load_system(context_of(request), system_id)
    response.headers["ETag"] = etag_of(record.version)
    return system_view(record)


def _system_audit(record: SystemRecord) -> dict[str, Any]:
    return {"name": record.name, "description": record.description, "version": record.version}


@router.patch("/systems/{system_id}", response_model=SystemView, **REVIEW.route())
async def patch_system(
    request: Request,
    response: Response,
    system_id: Ident,
    body: SystemPatch,
    principal: Principal = REVIEW.principal,
) -> SystemView:
    context = context_of(request)
    current = await _load_system(context, system_id)
    expected = require_if_match(request, current.version)
    at = context.clock()
    try:
        updated = await run_in_threadpool(
            lambda: context.ports.systems.patch(
                context.tenant_id,
                system_id,
                name=body.name,
                description=body.description,
                expected_version=expected,
                actor=principal.subject,
                at=at,
            )
        )
    except DecisionError as exc:
        raise decision_problem(exc) from None
    context.metrics.inc("carto_api_decisions_total")
    await record_audit(
        context,
        request,
        principal,
        action="system.patch",
        target=f"system:{system_id}",
        before=_system_audit(current),
        after=_system_audit(updated),
    )
    log.info("system patched", system_id=system_id, subject=principal.subject)
    response.headers["ETag"] = etag_of(updated.version)
    return system_view(updated)


# --- sources -----------------------------------------------------------------------------------


async def _load_source(context: ApiContext, source_id: str) -> SourceRecord:
    record = await run_in_threadpool(context.ports.sources.get, context.tenant_id, source_id)
    if record is None:
        raise Problem(404, "not found", "no such source")
    return record


@router.get("/sources", response_model=Page[SourceView], **VIEW.route())
async def list_sources(
    request: Request,
    cursor: Cursor = None,
    limit: Limit = None,
    principal: Principal = VIEW.principal,
) -> Page[SourceView]:
    del principal
    context = context_of(request)
    page = await run_in_threadpool(
        context.ports.sources.list, context.tenant_id, cursor, context.page_limit(limit)
    )
    return Page[SourceView](
        items=[source_view(record) for record in page.items], next_cursor=page.next_cursor
    )


@router.get("/sources/{source_id}", response_model=SourceView, **VIEW.route())
async def get_source(
    request: Request, source_id: Ident, principal: Principal = VIEW.principal
) -> SourceView:
    del principal
    return source_view(await _load_source(context_of(request), source_id))


@router.get("/sources/{source_id}/health", response_model=SourceHealthView, **VIEW.route())
async def get_source_health(
    request: Request, source_id: Ident, principal: Principal = VIEW.principal
) -> SourceHealthView:
    del principal
    record = await _load_source(context_of(request), source_id)
    if record.health is None:
        raise Problem(404, "not found", "the source has not reported yet")
    return health_view(record.health)


# --- fields ------------------------------------------------------------------------------------


@router.get("/fields", response_model=Page[FieldView], **VIEW.route())
async def list_fields(
    request: Request,
    system: SystemFilter = None,
    cursor: Cursor = None,
    limit: Limit = None,
    principal: Principal = VIEW.principal,
) -> Page[FieldView]:
    del principal
    context = context_of(request)
    page = await run_in_threadpool(
        context.ports.fields.list, context.tenant_id, system, cursor, context.page_limit(limit)
    )
    return Page[FieldView](
        items=[field_view(record) for record in page.items], next_cursor=page.next_cursor
    )


@router.get("/fields/{field_id:path}", response_model=FieldView, **VIEW.route())
async def get_field(
    request: Request, field_id: FieldId, principal: Principal = VIEW.principal
) -> FieldView:
    del principal
    context = context_of(request)
    record = await run_in_threadpool(context.ports.fields.get, context.tenant_id, field_id)
    if record is None:
        raise Problem(404, "not found", "no such field")
    return field_view(record)
