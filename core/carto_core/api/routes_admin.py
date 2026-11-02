"""``GET /audit`` for ``admin`` and ``auditor`` (spec 12 Admin, 14.2 "View audit log", 14.9).

Newest first, cursor paginated. ``before`` and ``after`` are the metadata views the mutation
recorded (scrubbed on write); ``row_hash`` lets an auditor spot-check a row against
``carto-core audit-verify``. ``GET /users`` and the role-binding routes are M6 (plan M2
"Deferred").
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from carto_core.api.auth import Principal
from carto_core.api.context import context_of
from carto_core.api.ports import MAX_CURSOR_LEN, AuditRow
from carto_core.api.sessions import Guard
from carto_schema.api import MAX_PAGE_SIZE, AuditEntryView, Page, Permission

__all__ = ["audit_view", "router"]

router = APIRouter(tags=["admin"])

AUDIT: Final = Guard(Permission.VIEW_AUDIT)
Cursor = Annotated[str | None, Query(max_length=MAX_CURSOR_LEN)]
Limit = Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)]


def audit_view(row: AuditRow) -> AuditEntryView:
    return AuditEntryView(
        id=row.id,
        at=row.at,
        actor=row.actor,
        action=row.action,
        target=row.target,
        before=dict(row.before) if row.before is not None else None,
        after=dict(row.after) if row.after is not None else None,
        reason=row.reason,
        request_id=row.request_id,
        row_hash=row.row_hash,
    )


@router.get("/audit", response_model=Page[AuditEntryView], **AUDIT.route())
async def list_audit(
    request: Request,
    cursor: Cursor = None,
    limit: Limit = None,
    principal: Principal = AUDIT.principal,
) -> Page[AuditEntryView]:
    del principal
    context = context_of(request)
    page = await run_in_threadpool(
        context.ports.audit.list, context.tenant_id, cursor, context.page_limit(limit)
    )
    return Page[AuditEntryView](
        items=[audit_view(row) for row in page.items], next_cursor=page.next_cursor
    )
