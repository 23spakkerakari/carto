"""The review state machine (spec 9.6; ADR 0051, 0053): proposals are upserted and refreshed,
decisions are versioned and audited exactly once with nothing but status, score, role and
version in the audit row, stale versions and invalid transitions are refused, a rejected pair
comes back only when its score rises by more than the delta, auto-accept is off by default and
limited to exact and bridge links at the threshold, and staleness follows the baseline."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash
from carto_core.linker.model import (
    Direction,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
    RejectReason,
)
from carto_core.linker.repository import InMemoryLinkRepository
from carto_core.linker.review import (
    LINKER_ACTOR,
    DecisionError,
    DecisionKind,
    RequestContext,
    ReviewService,
    ScoredLink,
    link_id_for,
)
from carto_core.settings import LinkerSettings
from carto_core.store.model import FieldForm

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
A = FieldForm("sys_a/tpl_a/order_id", "raw")
B = FieldForm("sys_b/tpl_b/order_ref", "digits.0")
TENANT = "default"


class Recorder:
    """An in-memory :class:`AuditRecorder` that chains like the PostgreSQL one."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._head = GENESIS_HASH

    def record(self, entry: AuditEntry) -> str:
        self.entries.append(entry)
        self._head = chain_hash(self._head, entry.content())
        return self._head

    def latest_hash(self, tenant_id: str) -> str:
        return self._head


def features(c_ab: float = 0.9, n_match: int = 500) -> PairFeatures:
    return PairFeatures(
        n_a=int(n_match / c_ab) if c_ab else 0,
        n_b=n_match,
        n_match=n_match,
        c_ab=c_ab,
        c_ba=1.0,
        lag_median=3600.0,
        lag_iqr=300.0,
        lag_positive_share=0.95,
        fanout_ab_median=1.0,
        fanout_ab_p99=2.0,
        fanout_ba_median=1.0,
        fanout_ba_p99=1.0,
        concentration=0.02,
        chance_ratio=0.01,
        domain_size=1e6,
    )


def scored(
    score: float = 0.9,
    *,
    a: FieldForm = A,
    b: FieldForm = B,
    link_type: LinkType = LinkType.EXACT,
    role: LinkRole = LinkRole.TRANSACTION,
    c_ab: float = 0.9,
) -> ScoredLink:
    return ScoredLink(
        a=a,
        b=b,
        link_type=link_type,
        role=role,
        direction=Direction.A_TO_B,
        score=score,
        features=features(c_ab=c_ab),
        affected_volume=500,
        extra={"sentence": "90% of A match a B within about an hour."},
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def service(**over: Any) -> tuple[ReviewService, InMemoryLinkRepository, Recorder, Clock]:
    repository = InMemoryLinkRepository()
    recorder = Recorder()
    clock = Clock()
    return (
        ReviewService(repository, recorder, LinkerSettings(**over), clock=clock),
        repository,
        recorder,
        clock,
    )


LINK_ID = link_id_for(TENANT, A, B)


# -- proposals ---------------------------------------------------------------------------------


def test_propose_creates_versioned_proposals_without_audit_rows() -> None:
    review, repository, recorder, _clock = service()
    report = review.propose(
        TENANT, [scored(0.9), scored(0.3, a=A, b=FieldForm("sys_c/t/c", "raw"))], now=NOW
    )
    assert report.proposed == 1 and report.below_threshold == 1
    link = repository.get(TENANT, LINK_ID)
    assert link is not None
    assert link.status == LinkStatus.PROPOSED
    assert link.version == 1
    assert link.first_proposed_at == NOW
    assert link.extra["sentence"].startswith("90%")
    assert recorder.entries == [], "proposals are not human actions and are not audited"
    assert len(repository.list_links(TENANT)) == 1


def test_propose_refreshes_an_existing_proposal() -> None:
    review, repository, _recorder, _clock = service()
    review.propose(TENANT, [scored(0.9)], now=NOW)
    later = NOW + timedelta(hours=1)
    report = review.propose(TENANT, [scored(0.7, role=LinkRole.ASSOCIATION)], now=later)
    assert report.updated == 1 and report.proposed == 0
    link = repository.get(TENANT, LINK_ID)
    assert link is not None
    assert link.version == 2
    assert link.score == 0.7
    assert link.role == LinkRole.ASSOCIATION, "a proposal takes the new role"
    assert link.first_proposed_at == NOW
    assert link.updated_at == later


# -- decisions ---------------------------------------------------------------------------------


def test_accept_is_versioned_audited_once_and_sets_the_baseline() -> None:
    review, repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.9, c_ab=0.88)], now=NOW)
    link = review.accept(
        TENANT,
        LINK_ID,
        actor="alice",
        expected_version=1,
        note="looks right",
        context=RequestContext(request_id="req-1", ip="10.0.0.1", user_agent="ua"),
    )
    assert link.status == LinkStatus.ACCEPTED
    assert link.version == 2
    assert link.decided_by == "alice"
    assert link.baseline_match_rate == pytest.approx(0.88)
    decisions = repository.decisions(TENANT, LINK_ID)
    assert [(d.version, d.status, d.note) for d in decisions] == [
        (2, LinkStatus.ACCEPTED, "looks right")
    ]
    assert decisions[0].score_at_decision == 0.9
    assert len(recorder.entries) == 1
    entry = recorder.entries[0]
    assert entry.action == "link.accept"
    assert entry.actor == "alice"
    assert entry.target == f"key_link:{LINK_ID}"
    assert entry.request_id == "req-1" and entry.ip == "10.0.0.1" and entry.user_agent == "ua"
    assert entry.before == {"status": "proposed", "score": 0.9, "role": "transaction", "version": 1}
    assert entry.after == {"status": "accepted", "score": 0.9, "role": "transaction", "version": 2}


def test_reject_needs_a_reason_and_records_it() -> None:
    review, repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.9)], now=NOW)
    with pytest.raises(DecisionError) as error:
        review.reject(TENANT, LINK_ID, actor="alice", expected_version=1, reason=None)
    assert error.value.kind == DecisionKind.REASON_REQUIRED
    link = review.reject(
        TENANT, LINK_ID, actor="alice", expected_version=1, reason=RejectReason.COINCIDENCE
    )
    assert link.status == LinkStatus.REJECTED
    assert link.reject_reason == RejectReason.COINCIDENCE
    assert repository.decisions(TENANT, LINK_ID)[0].reject_reason == RejectReason.COINCIDENCE
    assert recorder.entries[-1].action == "link.reject"
    assert recorder.entries[-1].reason == "coincidence"


def test_mark_association_keeps_the_link_out_of_families() -> None:
    review, repository, _recorder, _clock = service()
    review.propose(TENANT, [scored(0.9)], now=NOW)
    link = review.mark_association(TENANT, LINK_ID, actor="alice", expected_version=1)
    assert link.status == LinkStatus.ACCEPTED
    assert link.role == LinkRole.ASSOCIATION
    assert not link.is_usable_for_families
    with pytest.raises(DecisionError) as error:
        review.mark_association(TENANT, LINK_ID, actor="alice", expected_version=2)
    assert error.value.kind == DecisionKind.INVALID_TRANSITION
    review.propose(TENANT, [scored(0.95, role=LinkRole.TRANSACTION)], now=NOW)
    refreshed = repository.get(TENANT, LINK_ID)
    assert refreshed is not None
    assert refreshed.role == LinkRole.ASSOCIATION, "the reviewer's role survives a refresh"
    assert refreshed.score == 0.95 and refreshed.status == LinkStatus.ACCEPTED


def test_stale_version_not_found_and_invalid_transitions() -> None:
    review, _repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.9)], now=NOW)
    with pytest.raises(DecisionError) as stale:
        review.accept(TENANT, LINK_ID, actor="alice", expected_version=7)
    assert stale.value.kind == DecisionKind.STALE_VERSION
    with pytest.raises(DecisionError) as missing:
        review.accept(TENANT, "lnk_missing", actor="alice", expected_version=1)
    assert missing.value.kind == DecisionKind.NOT_FOUND
    review.accept(TENANT, LINK_ID, actor="alice", expected_version=1)
    with pytest.raises(DecisionError) as twice:
        review.accept(TENANT, LINK_ID, actor="alice", expected_version=2)
    assert twice.value.kind == DecisionKind.INVALID_TRANSITION
    assert len(recorder.entries) == 1, "a refused decision writes no audit row"
    with pytest.raises(DecisionError) as reversed_version:
        review.reject(
            TENANT, LINK_ID, actor="bob", expected_version=1, reason=RejectReason.WRONG_DIRECTION
        )
    assert reversed_version.value.kind == DecisionKind.STALE_VERSION


def test_decisions_are_versioned_in_order_and_reversible() -> None:
    review, repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.9)], now=NOW)
    review.accept(TENANT, LINK_ID, actor="alice", expected_version=1)
    review.reject(
        TENANT, LINK_ID, actor="bob", expected_version=2, reason=RejectReason.DIFFERENT_MEANING
    )
    link = review.accept(TENANT, LINK_ID, actor="carol", expected_version=3)
    assert link.version == 4 and link.status == LinkStatus.ACCEPTED
    assert link.reject_reason is None
    assert [(d.version, d.status) for d in repository.decisions(TENANT, LINK_ID)] == [
        (2, LinkStatus.ACCEPTED),
        (3, LinkStatus.REJECTED),
        (4, LinkStatus.ACCEPTED),
    ]
    assert [e.action for e in recorder.entries] == ["link.accept", "link.reject", "link.accept"]
    for entry in recorder.entries:
        for view in (entry.before, entry.after):
            assert view is not None
            assert set(view) <= {"status", "score", "role", "version"}
            assert not any("token" in key for key in view)


# -- re-proposal -------------------------------------------------------------------------------


def test_rejected_pair_returns_only_above_the_delta() -> None:
    review, repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.6)], now=NOW)
    review.reject(
        TENANT, LINK_ID, actor="alice", expected_version=1, reason=RejectReason.COINCIDENCE
    )
    kept = review.propose(TENANT, [scored(0.75)], now=NOW)
    assert kept.kept_rejected == 1 and kept.reproposed == 0
    link = repository.get(TENANT, LINK_ID)
    assert link is not None and link.status == LinkStatus.REJECTED and link.score == 0.6
    back = review.propose(TENANT, [scored(0.81)], now=NOW)
    assert back.reproposed == 1
    link = repository.get(TENANT, LINK_ID)
    assert link is not None
    assert link.status == LinkStatus.PROPOSED
    assert link.version == 3
    assert link.reject_reason is None and link.decided_by is None
    assert link.score == 0.81
    assert recorder.entries[-1].action == "link.repropose"
    assert recorder.entries[-1].actor == LINKER_ACTOR


@h_settings(max_examples=100, deadline=None)
@given(
    at_rejection=st.floats(min_value=0.5, max_value=1.0),
    later=st.floats(min_value=0.0, max_value=1.0),
)
def test_reproposal_property(at_rejection: float, later: float) -> None:
    delta = LinkerSettings().repropose_score_delta
    assume(abs(later - (at_rejection + delta)) > 1e-6)
    review, repository, _recorder, _clock = service()
    review.propose(TENANT, [scored(at_rejection)], now=NOW)
    review.reject(
        TENANT, LINK_ID, actor="alice", expected_version=1, reason=RejectReason.COINCIDENCE
    )
    report = review.propose(TENANT, [scored(later)], now=NOW)
    link = repository.get(TENANT, LINK_ID)
    assert link is not None
    if later > at_rejection + delta:
        assert report.reproposed == 1 and link.status == LinkStatus.PROPOSED
    else:
        assert report.kept_rejected == 1 and link.status == LinkStatus.REJECTED


# -- auto-accept -------------------------------------------------------------------------------


def test_auto_accept_is_off_by_default() -> None:
    review, repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.99)], now=NOW)
    link = repository.get(TENANT, LINK_ID)
    assert link is not None and link.status == LinkStatus.PROPOSED
    assert recorder.entries == []


def test_auto_accept_limited_to_exact_and_bridge_at_the_threshold() -> None:
    review, repository, recorder, _clock = service(auto_accept_enabled=True)
    c = FieldForm("sys_c/t/c", "raw")
    d = FieldForm("sys_d/t/d", "raw")
    report = review.propose(
        TENANT,
        [
            scored(0.96),
            scored(0.97, a=A, b=c, link_type=LinkType.BRIDGE),
            scored(0.99, a=A, b=d, link_type=LinkType.COMPOSITE),
            scored(0.94, a=B, b=c),
        ],
        now=NOW,
    )
    assert report.proposed == 4 and report.auto_accepted == 2
    exact = repository.get(TENANT, LINK_ID)
    assert exact is not None
    assert exact.status == LinkStatus.AUTO_ACCEPTED
    assert exact.decided_by == LINKER_ACTOR
    assert exact.version == 2
    assert exact.baseline_match_rate == pytest.approx(0.9)
    assert exact.is_usable_for_families
    composite = repository.get(TENANT, link_id_for(TENANT, A, d))
    assert composite is not None and composite.status == LinkStatus.PROPOSED
    below = repository.get(TENANT, link_id_for(TENANT, B, c))
    assert below is not None and below.status == LinkStatus.PROPOSED
    assert [e.action for e in recorder.entries] == ["link.auto_accept", "link.auto_accept"]
    # A refreshed proposal that climbs over the threshold is promoted too.
    review.propose(TENANT, [scored(0.96, a=B, b=c)], now=NOW)
    climbed = repository.get(TENANT, link_id_for(TENANT, B, c))
    assert climbed is not None and climbed.status == LinkStatus.AUTO_ACCEPTED
    assert review.auto_accept(TENANT, link_id_for(TENANT, A, d)) is None
    with pytest.raises(DecisionError):
        review.auto_accept(TENANT, "lnk_missing")


def test_auto_accepted_links_are_reversible() -> None:
    review, _repository, _recorder, _clock = service(auto_accept_enabled=True)
    review.propose(TENANT, [scored(0.99)], now=NOW)
    link = review.reject(
        TENANT, LINK_ID, actor="alice", expected_version=2, reason=RejectReason.DIFFERENT_MEANING
    )
    assert link.status == LinkStatus.REJECTED
    confirmed_service, _r, _a, _c = service(auto_accept_enabled=True)
    confirmed_service.propose(TENANT, [scored(0.99)], now=NOW)
    confirmed = confirmed_service.accept(TENANT, LINK_ID, actor="alice", expected_version=2)
    assert confirmed.status == LinkStatus.ACCEPTED


# -- staleness ---------------------------------------------------------------------------------


def test_stale_and_recovery_follow_the_baseline() -> None:
    review, repository, recorder, _clock = service()
    review.propose(TENANT, [scored(0.9, c_ab=0.9)], now=NOW)
    review.accept(TENANT, LINK_ID, actor="alice", expected_version=1)
    steady = review.propose(TENANT, [scored(0.9, c_ab=0.5)], now=NOW)
    assert steady.updated == 1 and steady.stale == 0
    link = repository.get(TENANT, LINK_ID)
    assert link is not None and link.status == LinkStatus.ACCEPTED
    assert link.baseline_match_rate == pytest.approx(0.9), "the baseline is the rate at acceptance"
    dropped = review.propose(TENANT, [scored(0.9, c_ab=0.4)], now=NOW)
    assert dropped.stale == 1
    link = repository.get(TENANT, LINK_ID)
    assert link is not None and link.status == LinkStatus.STALE
    assert not link.is_usable_for_families
    assert recorder.entries[-1].action == "link.stale"
    assert recorder.entries[-1].actor == LINKER_ACTOR
    recovered = review.propose(TENANT, [scored(0.9, c_ab=0.46)], now=NOW)
    assert recovered.recovered == 1
    link = repository.get(TENANT, LINK_ID)
    assert link is not None and link.status == LinkStatus.ACCEPTED
    assert recorder.entries[-1].action == "link.recover"
    assert [d.status for d in repository.decisions(TENANT, LINK_ID)] == [
        LinkStatus.ACCEPTED,
        LinkStatus.STALE,
        LinkStatus.ACCEPTED,
    ]


def test_stale_threshold_is_a_setting_and_needs_a_baseline() -> None:
    review, repository, _recorder, _clock = service(stale_drop_share=0.1)
    review.propose(TENANT, [scored(0.9, c_ab=0.9)], now=NOW)
    review.accept(TENANT, LINK_ID, actor="alice", expected_version=1)
    assert review.propose(TENANT, [scored(0.9, c_ab=0.8)], now=NOW).stale == 1
    link = repository.get(TENANT, LINK_ID)
    assert link is not None
    assert review.refresh_staleness(TENANT, link, 0.0) is None, "a stale link below stays stale"
    fresh = repository.upsert(TENANT, replace(link, baseline_match_rate=None), NOW)
    assert review.refresh_staleness(TENANT, fresh, 0.0) is None


def test_reviewer_can_reaccept_a_stale_link() -> None:
    review, _repository, _recorder, _clock = service()
    review.propose(TENANT, [scored(0.9, c_ab=0.9)], now=NOW)
    review.accept(TENANT, LINK_ID, actor="alice", expected_version=1)
    review.propose(TENANT, [scored(0.9, c_ab=0.1)], now=NOW)
    link = review.accept(TENANT, LINK_ID, actor="alice", expected_version=4)
    assert link.status == LinkStatus.ACCEPTED
    assert link.baseline_match_rate == pytest.approx(0.1), "re-accepting resets the baseline"
