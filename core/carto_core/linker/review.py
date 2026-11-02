"""The review state machine for key links (spec 9.6; ADR 0051, 0053).

:class:`ReviewService` is the one place a link's status changes, for the linker and for the
API alike:

- :meth:`~ReviewService.propose` takes the scored pairs of a pass and upserts them. A new pair
  at or above ``proposal_threshold`` becomes ``proposed`` (version 1, ``first_proposed_at``).
  A pair already proposed gets its evidence refreshed. An accepted, auto-accepted or stale pair
  keeps its status and the reviewer's role; its evidence is refreshed and its match rate checked
  against the baseline (ADR 0053): below ``(1 - stale_drop_share) x baseline`` it becomes
  ``stale``, back above it returns to ``accepted``, each with a decision row and an audit row.
  A rejected pair stays rejected unless the new score exceeds the score at rejection by more
  than ``repropose_score_delta``; then it is proposed again with a new version and an audit
  row. Auto-accept, when enabled, promotes exact and bridge proposals at or above
  ``auto_accept_threshold`` with the actor ``linker``.
- :meth:`~ReviewService.accept`, :meth:`~ReviewService.reject` and
  :meth:`~ReviewService.mark_association` are the reviewer's actions: the transition is
  validated, ``key_links`` and ``key_link_decisions`` change in one transaction guarded by
  ``expected_version`` (the API's ``If-Match``), and one audit row records ``status``, ``score``,
  ``role`` and ``version`` before and after; nothing else, so no token can reach the audit log.

Errors are :class:`DecisionError` with a :class:`DecisionKind` the API maps to a status code;
their messages never carry data.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

from carto_core.audit import AuditEntry, scrub
from carto_core.linker.model import (
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
    RejectReason,
)
from carto_core.linker.repository import DecisionWrite, NotFoundError, StaleVersionError

if TYPE_CHECKING:
    from carto_core.audit import AuditRecorder
    from carto_core.linker.repository import LinkRepository
    from carto_core.settings import LinkerSettings
    from carto_core.store.model import FieldForm

__all__ = [
    "LINKER_ACTOR",
    "DecisionError",
    "DecisionKind",
    "ProposeReport",
    "RequestContext",
    "ReviewService",
    "ScoredLink",
    "link_id_for",
]

LINKER_ACTOR: Final = "linker"
"""The actor of auto-accept, staleness and re-proposal audit rows."""

_ACCEPT_FROM: Final = frozenset(
    {LinkStatus.PROPOSED, LinkStatus.AUTO_ACCEPTED, LinkStatus.STALE, LinkStatus.REJECTED}
)
_REJECT_FROM: Final = frozenset(
    {LinkStatus.PROPOSED, LinkStatus.ACCEPTED, LinkStatus.AUTO_ACCEPTED, LinkStatus.STALE}
)
_ASSOCIATION_FROM: Final = frozenset(
    {
        LinkStatus.PROPOSED,
        LinkStatus.ACCEPTED,
        LinkStatus.AUTO_ACCEPTED,
        LinkStatus.STALE,
        LinkStatus.REJECTED,
    }
)


class DecisionKind(StrEnum):
    NOT_FOUND = "not_found"
    STALE_VERSION = "stale_version"
    INVALID_TRANSITION = "invalid_transition"
    REASON_REQUIRED = "reason_required"


class DecisionError(Exception):
    """A decision could not be applied; ``kind`` says why. The message never carries data."""

    def __init__(self, kind: DecisionKind, message: str | None = None) -> None:
        super().__init__(message or kind.value.replace("_", " "))
        self.kind = kind


@dataclass(frozen=True, slots=True)
class RequestContext:
    """What the audit row records about the request behind a human decision."""

    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True, slots=True)
class ScoredLink:
    """One verified and scored pair the pass hands to :meth:`ReviewService.propose`."""

    a: FieldForm
    b: FieldForm
    link_type: LinkType
    role: LinkRole
    direction: Direction
    score: float
    features: PairFeatures
    affected_volume: int
    extra: Mapping[str, Any] = field(default_factory=dict)
    """Review-item metadata stored with the evidence (sentence, shapes); never a token."""


@dataclass(frozen=True, slots=True)
class ProposeReport:
    proposed: int = 0
    """New rows."""
    updated: int = 0
    """Existing rows whose evidence was refreshed."""
    reproposed: int = 0
    kept_rejected: int = 0
    below_threshold: int = 0
    auto_accepted: int = 0
    stale: int = 0
    recovered: int = 0


def link_id_for(tenant_id: str, a: FieldForm, b: FieldForm) -> str:
    """A stable id per pair: the same pair gets the same id in every pass and every run."""
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(
        "\n".join((tenant_id, a.field_ref, a.form, b.field_ref, b.form)).encode("utf-8")
    ).hexdigest()
    return f"lnk_{digest[:20]}"


def _view(link: KeyLink | None) -> dict[str, Any] | None:
    if link is None:
        return None
    return {
        "status": link.status.value,
        "score": round(link.score, 6),
        "role": link.role.value,
        "version": link.version,
    }


class ReviewService:
    """See the module docstring."""

    def __init__(
        self,
        repository: LinkRepository,
        audit: AuditRecorder,
        settings: LinkerSettings,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._links = repository
        self._audit = audit
        self._settings = settings
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    # -- the linker's side ----------------------------------------------------------------------

    def propose(
        self,
        tenant_id: str,
        scored: Sequence[ScoredLink],
        *,
        now: datetime | None = None,
        request_id: str | None = None,
    ) -> ProposeReport:
        at = now if now is not None else self._clock()
        counts = dict.fromkeys(ProposeReport.__dataclass_fields__, 0)
        for item in scored:
            existing = self._links.find_by_ends(tenant_id, item.a, item.b)
            if existing is None:
                if item.score < self._settings.proposal_threshold:
                    counts["below_threshold"] += 1
                    continue
                stored = self._links.upsert(tenant_id, self._new_row(tenant_id, item, at), at)
                counts["proposed"] += 1
                if self._auto_accept(tenant_id, stored, at, request_id) is not None:
                    counts["auto_accepted"] += 1
                continue
            if existing.status == LinkStatus.PROPOSED:
                stored = self._links.upsert(tenant_id, self._refresh(existing, item), at)
                counts["updated"] += 1
                if self._auto_accept(tenant_id, stored, at, request_id) is not None:
                    counts["auto_accepted"] += 1
            elif existing.status == LinkStatus.REJECTED:
                if self._repropose(tenant_id, existing, item, at, request_id):
                    counts["reproposed"] += 1
                else:
                    counts["kept_rejected"] += 1
            else:
                stored = self._links.upsert(
                    tenant_id, self._refresh(existing, item, keep_role=True), at
                )
                counts["updated"] += 1
                outcome = self.refresh_staleness(
                    tenant_id, stored, item.features.c_ab, now=at, request_id=request_id
                )
                if outcome is not None and outcome.status == LinkStatus.STALE:
                    counts["stale"] += 1
                elif outcome is not None:
                    counts["recovered"] += 1
        return ProposeReport(**counts)

    def _new_row(self, tenant_id: str, item: ScoredLink, at: datetime) -> KeyLink:
        return KeyLink(
            id=link_id_for(tenant_id, item.a, item.b),
            a=item.a,
            b=item.b,
            link_type=item.link_type,
            role=item.role,
            direction=item.direction,
            score=item.score,
            status=LinkStatus.PROPOSED,
            features=item.features,
            affected_volume=item.affected_volume,
            version=1,
            first_proposed_at=at,
            extra=dict(item.extra),
        )

    @staticmethod
    def _refresh(existing: KeyLink, item: ScoredLink, *, keep_role: bool = False) -> KeyLink:
        extra = dict(existing.extra) | dict(item.extra)
        return KeyLink(
            id=existing.id,
            a=existing.a,
            b=existing.b,
            link_type=item.link_type,
            role=existing.role if keep_role else item.role,
            direction=item.direction,
            score=item.score,
            status=existing.status,
            features=item.features,
            affected_volume=item.affected_volume,
            version=existing.version,
            entity_id=existing.entity_id,
            reject_reason=existing.reject_reason,
            decided_by=existing.decided_by,
            decided_at=existing.decided_at,
            ambiguous_merges=existing.ambiguous_merges,
            composite=existing.composite,
            first_proposed_at=existing.first_proposed_at,
            baseline_match_rate=existing.baseline_match_rate,
            extra=extra,
        )

    def _repropose(
        self,
        tenant_id: str,
        existing: KeyLink,
        item: ScoredLink,
        at: datetime,
        request_id: str | None,
    ) -> bool:
        decisions = self._links.decisions(tenant_id, existing.id)
        rejections = [d for d in decisions if d.status == LinkStatus.REJECTED]
        at_rejection = rejections[-1].score_at_decision if rejections else existing.score
        if item.score <= at_rejection + self._settings.repropose_score_delta:
            return False
        refreshed = self._refresh(existing, item)
        row = KeyLink(
            id=refreshed.id,
            a=refreshed.a,
            b=refreshed.b,
            link_type=refreshed.link_type,
            role=refreshed.role,
            direction=refreshed.direction,
            score=refreshed.score,
            status=LinkStatus.PROPOSED,
            features=refreshed.features,
            affected_volume=refreshed.affected_volume,
            version=refreshed.version,
            entity_id=None,
            reject_reason=None,
            decided_by=None,
            decided_at=None,
            ambiguous_merges=refreshed.ambiguous_merges,
            composite=refreshed.composite,
            first_proposed_at=refreshed.first_proposed_at,
            baseline_match_rate=None,
            extra=refreshed.extra,
        )
        stored = self._links.upsert(tenant_id, row, at)
        self._record(
            tenant_id,
            at,
            LINKER_ACTOR,
            "link.repropose",
            existing,
            stored,
            reason=f"score rose by more than {self._settings.repropose_score_delta:g}",
            context=RequestContext(request_id=request_id),
        )
        return True

    def _auto_accept(
        self, tenant_id: str, link: KeyLink, at: datetime, request_id: str | None
    ) -> KeyLink | None:
        if not self._settings.auto_accept_enabled or link.status != LinkStatus.PROPOSED:
            return None
        if link.link_type not in (LinkType.EXACT, LinkType.BRIDGE):
            return None
        if link.score < self._settings.auto_accept_threshold:
            return None
        write = DecisionWrite(
            status=LinkStatus.AUTO_ACCEPTED,
            decided_by=LINKER_ACTOR,
            decided_at=at,
            score_at_decision=link.score,
            baseline_match_rate=link.features.c_ab,
        )
        stored = self._links.decide(tenant_id, link.id, expected_version=link.version, write=write)
        self._record(
            tenant_id,
            at,
            LINKER_ACTOR,
            "link.auto_accept",
            link,
            stored,
            reason=f"score at or above {self._settings.auto_accept_threshold:g}",
            context=RequestContext(request_id=request_id),
        )
        return stored

    def auto_accept(
        self, tenant_id: str, link_id: str, *, now: datetime | None = None
    ) -> KeyLink | None:
        """Promote one proposal when the auto-accept policy allows it; None when it does not."""
        link = self._links.get(tenant_id, link_id)
        if link is None:
            raise DecisionError(DecisionKind.NOT_FOUND)
        return self._auto_accept(tenant_id, link, now if now is not None else self._clock(), None)

    def refresh_staleness(
        self,
        tenant_id: str,
        link: KeyLink,
        current_c_ab: float,
        *,
        now: datetime | None = None,
        request_id: str | None = None,
    ) -> KeyLink | None:
        """ADR 0053: mark an accepted link stale when its match rate fell below
        ``(1 - stale_drop_share) x baseline``; return a stale link to ``accepted`` when it is
        back above. Returns the changed link, or None when nothing changed."""
        baseline = link.baseline_match_rate
        if baseline is None:
            return None
        at = now if now is not None else self._clock()
        floor = (1.0 - self._settings.stale_drop_share) * baseline
        if link.status in (LinkStatus.ACCEPTED, LinkStatus.AUTO_ACCEPTED) and current_c_ab < floor:
            target, action = LinkStatus.STALE, "link.stale"
        elif link.status == LinkStatus.STALE and current_c_ab >= floor:
            target, action = LinkStatus.ACCEPTED, "link.recover"
        else:
            return None
        write = DecisionWrite(
            status=target,
            decided_by=LINKER_ACTOR,
            decided_at=at,
            score_at_decision=link.score,
        )
        stored = self._links.decide(tenant_id, link.id, expected_version=link.version, write=write)
        self._record(
            tenant_id,
            at,
            LINKER_ACTOR,
            action,
            link,
            stored,
            reason=f"match rate {current_c_ab:.3f} against baseline {baseline:.3f}",
            context=RequestContext(request_id=request_id),
        )
        return stored

    # -- the reviewer's side --------------------------------------------------------------------

    def accept(
        self,
        tenant_id: str,
        link_id: str,
        *,
        actor: str,
        expected_version: int,
        note: str | None = None,
        context: RequestContext | None = None,
    ) -> KeyLink:
        link = self._load(tenant_id, link_id, expected_version, _ACCEPT_FROM)
        write = DecisionWrite(
            status=LinkStatus.ACCEPTED,
            decided_by=actor,
            decided_at=self._clock(),
            score_at_decision=link.score,
            note=note,
            baseline_match_rate=link.features.c_ab,
        )
        return self._apply(tenant_id, link, write, "link.accept", "", context)

    def reject(
        self,
        tenant_id: str,
        link_id: str,
        *,
        actor: str,
        expected_version: int,
        reason: RejectReason | None,
        note: str | None = None,
        context: RequestContext | None = None,
    ) -> KeyLink:
        if reason is None:
            raise DecisionError(DecisionKind.REASON_REQUIRED)
        link = self._load(tenant_id, link_id, expected_version, _REJECT_FROM)
        write = DecisionWrite(
            status=LinkStatus.REJECTED,
            decided_by=actor,
            decided_at=self._clock(),
            score_at_decision=link.score,
            reject_reason=reason,
            note=note,
        )
        return self._apply(tenant_id, link, write, "link.reject", reason.value, context)

    def mark_association(
        self,
        tenant_id: str,
        link_id: str,
        *,
        actor: str,
        expected_version: int,
        note: str | None = None,
        context: RequestContext | None = None,
    ) -> KeyLink:
        """Spec 9.6 "Mark as association": the link is real but never merges records. It is
        stored accepted with the association role, so the map shows it and families skip it."""
        link = self._load(tenant_id, link_id, expected_version, _ASSOCIATION_FROM)
        if link.status == LinkStatus.ACCEPTED and link.role == LinkRole.ASSOCIATION:
            raise DecisionError(DecisionKind.INVALID_TRANSITION)
        write = DecisionWrite(
            status=LinkStatus.ACCEPTED,
            decided_by=actor,
            decided_at=self._clock(),
            score_at_decision=link.score,
            role=LinkRole.ASSOCIATION,
            note=note,
            baseline_match_rate=link.features.c_ab,
        )
        return self._apply(tenant_id, link, write, "link.association", "", context)

    # -- internals ------------------------------------------------------------------------------

    def _load(
        self,
        tenant_id: str,
        link_id: str,
        expected_version: int,
        allowed_from: frozenset[LinkStatus],
    ) -> KeyLink:
        link = self._links.get(tenant_id, link_id)
        if link is None:
            raise DecisionError(DecisionKind.NOT_FOUND)
        if link.version != expected_version:
            raise DecisionError(DecisionKind.STALE_VERSION)
        if link.status not in allowed_from:
            raise DecisionError(DecisionKind.INVALID_TRANSITION)
        return link

    def _apply(
        self,
        tenant_id: str,
        link: KeyLink,
        write: DecisionWrite,
        action: str,
        reason: str,
        context: RequestContext | None,
    ) -> KeyLink:
        try:
            stored = self._links.decide(
                tenant_id, link.id, expected_version=link.version, write=write
            )
        except NotFoundError as exc:
            raise DecisionError(DecisionKind.NOT_FOUND) from exc
        except StaleVersionError as exc:
            raise DecisionError(DecisionKind.STALE_VERSION) from exc
        self._record(
            tenant_id,
            write.decided_at,
            write.decided_by,
            action,
            link,
            stored,
            reason=reason,
            context=context if context is not None else RequestContext(),
        )
        return stored

    def _record(
        self,
        tenant_id: str,
        at: datetime,
        actor: str,
        action: str,
        before: KeyLink | None,
        after: KeyLink,
        *,
        reason: str,
        context: RequestContext,
    ) -> None:
        self._audit.record(
            AuditEntry(
                at=at,
                actor=actor,
                action=action,
                target=f"key_link:{after.id}",
                before=scrub(_view(before)),
                after=scrub(_view(after)),
                reason=reason,
                request_id=context.request_id,
                ip=context.ip,
                user_agent=context.user_agent,
                tenant_id=tenant_id,
            )
        )
