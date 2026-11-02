"""The review queue: ``GET /links``, ``GET /links/{id}`` and the three decisions (spec 9.6, 12;
ADR 0057).

The queue lists one status at a time (``proposed`` by default) in the order the port keeps
(``score x affected_volume`` descending, spec 9.6). A decision carries the version the
reviewer saw in ``If-Match`` (428 without it, 412 when the link moved); the port checks it
again under its own lock and raises ``stale_version`` for the race the header cannot catch.
``reject`` needs a reason (spec 9.6). Every decision writes one audit row with ``before`` and
``after`` limited to status, role, score and version (never evidence, never field paths), the
request id, the socket address and the user agent.

A view is built from the record only; the evidence keys the contract does not name are dropped
and evidence that does not validate degrades to zero counts with a warning, so one bad row
cannot hide the queue.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Final

from fastapi import APIRouter, Path, Query, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from carto_common.logging import get_logger
from carto_core.api.auth import Principal
from carto_core.api.context import ApiContext, context_of, record_audit
from carto_core.api.etag import etag_of, require_if_match
from carto_core.api.ports import MAX_CURSOR_LEN, DecisionError, LinkEndRecord, LinkRecord
from carto_core.api.problems import Problem
from carto_core.api.sessions import Guard
from carto_schema.api import (
    MAX_PAGE_SIZE,
    DecisionRequest,
    EvidenceView,
    LinkEnd,
    LinkStatus,
    LinkView,
    Page,
    Permission,
    RejectReason,
)

__all__ = ["DECISION_STATUS", "link_view", "router"]

log = get_logger(component="api")
router = APIRouter(tags=["links"])

VIEW: Final = Guard(Permission.VIEW)
REVIEW: Final = Guard(Permission.REVIEW_LINKS)
DECISION_STATUS: Final[dict[str, int]] = {
    "not_found": 404,
    "stale_version": 412,
    "invalid_transition": 409,
    "reason_required": 422,
}
_DECISION_TITLES: Final[dict[str, str]] = {
    "not_found": "not found",
    "stale_version": "precondition failed",
    "invalid_transition": "conflict",
    "reason_required": "request failed validation",
}
_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset(EvidenceView.model_fields)

LinkId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
Cursor = Annotated[str | None, Query(max_length=MAX_CURSOR_LEN)]
Limit = Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)]


def decision_problem(exc: DecisionError) -> Problem:
    status = DECISION_STATUS[exc.kind]
    detail = {
        "not_found": "no such resource",
        "stale_version": "the resource changed; read it again",
        "invalid_transition": "the link is not in a state this decision applies to",
        "reason_required": "a reject needs a reason",
    }[exc.kind]
    errors = (
        [{"loc": ["body", "reason"], "msg": "a reason is required", "type": "missing"}]
        if exc.kind == "reason_required"
        else None
    )
    return Problem(status, _DECISION_TITLES[exc.kind], detail, errors=errors)


def _end(record: LinkEndRecord) -> LinkEnd:
    return LinkEnd(
        field_id=record.field_id,
        system_id=record.system_id,
        template_id=record.template_id,
        path=record.path,
        form=record.form,
        label=record.label,
        top_shape=record.top_shape,
    )


def _evidence(record: LinkRecord) -> EvidenceView:
    data = {key: value for key, value in record.evidence.items() if key in _EVIDENCE_KEYS}
    try:
        return EvidenceView.model_validate(data)
    except ValidationError:
        log.warning("link evidence does not validate; shown as empty", link_id=record.id)
        return EvidenceView(n_a=0, n_b=0, n_match=0, c_ab=0.0, c_ba=0.0)


def link_view(record: LinkRecord) -> LinkView:
    return LinkView(
        id=record.id,
        a=_end(record.a),
        b=_end(record.b),
        link_type=record.link_type,
        role=record.role,
        direction=record.direction,
        score=record.score,
        status=record.status,
        affected_volume=record.affected_volume,
        sentence=record.sentence,
        evidence=_evidence(record),
        entity_id=record.entity_id,
        reject_reason=record.reject_reason,
        decided_by=record.decided_by,
        decided_at=record.decided_at,
        ambiguous_merges=record.ambiguous_merges,
        version=record.version,
        first_proposed_at=record.first_proposed_at,
        updated_at=record.updated_at,
        etag=etag_of(record.version),
    )


def _audit_view(record: LinkRecord) -> dict[str, Any]:
    return {
        "status": record.status.value,
        "role": record.role.value,
        "score": record.score,
        "version": record.version,
    }


@router.get("/links", response_model=Page[LinkView], **VIEW.route())
async def list_links(
    request: Request,
    status: Annotated[LinkStatus, Query()] = LinkStatus.PROPOSED,
    cursor: Cursor = None,
    limit: Limit = None,
    principal: Principal = VIEW.principal,
) -> Page[LinkView]:
    del principal
    context = context_of(request)
    page = await run_in_threadpool(
        context.ports.links.list, context.tenant_id, status, cursor, context.page_limit(limit)
    )
    return Page[LinkView](
        items=[link_view(record) for record in page.items], next_cursor=page.next_cursor
    )


async def _load(context: ApiContext, link_id: str) -> LinkRecord:
    record = await run_in_threadpool(context.ports.links.get, context.tenant_id, link_id)
    if record is None:
        raise Problem(404, "not found", "no such link")
    return record


@router.get("/links/{link_id}", response_model=LinkView, **VIEW.route())
async def get_link(
    request: Request, response: Response, link_id: LinkId, principal: Principal = VIEW.principal
) -> LinkView:
    del principal
    record = await _load(context_of(request), link_id)
    response.headers["ETag"] = etag_of(record.version)
    return link_view(record)


Decide = Callable[..., LinkRecord]


async def _decide(
    request: Request,
    response: Response,
    link_id: str,
    body: DecisionRequest,
    principal: Principal,
    *,
    action: str,
    method: Callable[[ApiContext], Decide],
    reason: RejectReason | None,
) -> LinkView:
    context = context_of(request)
    current = await _load(context, link_id)
    expected = require_if_match(request, current.version)
    at = context.clock()
    port_method = method(context)
    try:
        updated = await run_in_threadpool(
            lambda: port_method(
                context.tenant_id,
                link_id,
                actor=principal.subject,
                expected_version=expected,
                reason=reason,
                note=body.note,
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
        action=action,
        target=f"link:{link_id}",
        before=_audit_view(current),
        after=_audit_view(updated),
        reason=(reason.value if reason is not None else "") or body.note,
    )
    log.info("link decision", action=action, link_id=link_id, subject=principal.subject)
    response.headers["ETag"] = etag_of(updated.version)
    return link_view(updated)


@router.post("/links/{link_id}/accept", response_model=LinkView, **REVIEW.route())
async def accept_link(
    request: Request,
    response: Response,
    link_id: LinkId,
    body: DecisionRequest,
    principal: Principal = REVIEW.principal,
) -> LinkView:
    return await _decide(
        request,
        response,
        link_id,
        body,
        principal,
        action="link.accept",
        method=lambda context: context.ports.links.accept,
        reason=None,
    )


@router.post("/links/{link_id}/reject", response_model=LinkView, **REVIEW.route())
async def reject_link(
    request: Request,
    response: Response,
    link_id: LinkId,
    body: DecisionRequest,
    principal: Principal = REVIEW.principal,
) -> LinkView:
    if body.reason is None:
        raise decision_problem(DecisionError("reason_required"))
    return await _decide(
        request,
        response,
        link_id,
        body,
        principal,
        action="link.reject",
        method=lambda context: context.ports.links.reject,
        reason=body.reason,
    )


@router.post("/links/{link_id}/association", response_model=LinkView, **REVIEW.route())
async def mark_association(
    request: Request,
    response: Response,
    link_id: LinkId,
    body: DecisionRequest,
    principal: Principal = REVIEW.principal,
) -> LinkView:
    return await _decide(
        request,
        response,
        link_id,
        body,
        principal,
        action="link.association",
        method=lambda context: context.ports.links.mark_association,
        reason=None,
    )
