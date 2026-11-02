"""``GET /entities``, ``GET /entities/{id}``, ``PATCH /entities/{id}`` (spec 9.6 families and
naming, 12). Naming is ``review_links`` (spec 14.2 "Review links, name entities and nodes").
A patch carries ``If-Match``; a new name becomes the confirmed name (the heuristic or LLM
suggestion stays in ``suggested_name`` until then, spec 9.9). Audited with name, confirmation
flag and version only.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from carto_common.logging import get_logger
from carto_core.api.auth import Principal
from carto_core.api.context import ApiContext, context_of, record_audit
from carto_core.api.etag import etag_of, require_if_match
from carto_core.api.ports import MAX_CURSOR_LEN, DecisionError, EntityRecord
from carto_core.api.problems import Problem
from carto_core.api.routes_links import decision_problem
from carto_core.api.sessions import Guard
from carto_schema.api import (
    MAX_PAGE_SIZE,
    EntityFieldView,
    EntityPatch,
    EntityView,
    Page,
    Permission,
)

__all__ = ["entity_view", "router"]

log = get_logger(component="api")
router = APIRouter(tags=["entities"])

VIEW: Final = Guard(Permission.VIEW)
REVIEW: Final = Guard(Permission.REVIEW_LINKS)

EntityId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
Cursor = Annotated[str | None, Query(max_length=MAX_CURSOR_LEN)]
Limit = Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)]


def entity_view(record: EntityRecord) -> EntityView:
    return EntityView(
        id=record.id,
        name=record.name,
        suggested_name=record.suggested_name,
        name_confirmed=record.name_confirmed,
        description=record.description,
        fields=[
            EntityFieldView(
                field_id=row.field_id, form=row.form, system_id=row.system_id, path=row.path
            )
            for row in record.fields
        ],
        link_ids=list(record.link_ids),
        system_count=record.system_count,
        updated_at=record.updated_at,
        etag=etag_of(record.version),
    )


def _audit_view(record: EntityRecord) -> dict[str, Any]:
    return {
        "name": record.name,
        "name_confirmed": record.name_confirmed,
        "description": record.description,
        "version": record.version,
    }


async def _load(context: ApiContext, entity_id: str) -> EntityRecord:
    record = await run_in_threadpool(context.ports.entities.get, context.tenant_id, entity_id)
    if record is None:
        raise Problem(404, "not found", "no such entity")
    return record


@router.get("/entities", response_model=Page[EntityView], **VIEW.route())
async def list_entities(
    request: Request,
    cursor: Cursor = None,
    limit: Limit = None,
    principal: Principal = VIEW.principal,
) -> Page[EntityView]:
    del principal
    context = context_of(request)
    page = await run_in_threadpool(
        context.ports.entities.list, context.tenant_id, cursor, context.page_limit(limit)
    )
    return Page[EntityView](
        items=[entity_view(record) for record in page.items], next_cursor=page.next_cursor
    )


@router.get("/entities/{entity_id}", response_model=EntityView, **VIEW.route())
async def get_entity(
    request: Request,
    response: Response,
    entity_id: EntityId,
    principal: Principal = VIEW.principal,
) -> EntityView:
    del principal
    record = await _load(context_of(request), entity_id)
    response.headers["ETag"] = etag_of(record.version)
    return entity_view(record)


@router.patch("/entities/{entity_id}", response_model=EntityView, **REVIEW.route())
async def patch_entity(
    request: Request,
    response: Response,
    entity_id: EntityId,
    body: EntityPatch,
    principal: Principal = REVIEW.principal,
) -> EntityView:
    context = context_of(request)
    current = await _load(context, entity_id)
    expected = require_if_match(request, current.version)
    at = context.clock()
    try:
        updated = await run_in_threadpool(
            lambda: context.ports.entities.patch(
                context.tenant_id,
                entity_id,
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
        action="entity.patch",
        target=f"entity:{entity_id}",
        before=_audit_view(current),
        after=_audit_view(updated),
    )
    log.info("entity patched", entity_id=entity_id, subject=principal.subject)
    response.headers["ETag"] = etag_of(updated.version)
    return entity_view(updated)
