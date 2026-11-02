"""Composite matching end to end (spec 9.5, 18.4) on the in-memory store: a payments system A
and a warehouse system B where 70% of A events get a B row one to a few business hours later
carrying the same date and amount and, for 97% of them, the same phonetic name; B also holds
unrelated rows drawn from the same date, amount and name pools. The matcher produces one
composite link whose matched pairs reach precision 0.85 and recall 0.70 against the planted
truth, the rule is persisted under the link id, nothing in the evidence or the logs is a token,
and a :class:`LinkPass` with auto-accept enabled auto-accepts the exact link next to it while
the composite link stays ``proposed``."""

from __future__ import annotations

import base64
import hashlib
import json
import random
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest
import structlog
from datasketch import MinHash

from carto_common.ids import derive_ulid
from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash
from carto_core.linker.composite import (
    OUTCOME_STAGES,
    CompositeMatcher,
    CompositeReport,
    InMemoryCompositeRuleRepository,
)
from carto_core.linker.model import Direction, LinkRole, LinkStatus, LinkType
from carto_core.linker.repository import InMemoryEntityRepository, InMemoryLinkRepository
from carto_core.linker.review import ReviewService, ScoredLink, link_id_for
from carto_core.linker.run import LinkPass
from carto_core.profiler.model import FieldProfile, ShapeShare, WindowSketch
from carto_core.settings import LinkerSettings, ProfilerSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import FieldForm, Window
from carto_schema.event import CanonicalEvent

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BASE_MS = 1_790_000_000_000
TENANT = "default"
PROFILER = ProfilerSettings()
WINDOW = Window(NOW - timedelta(days=PROFILER.window_days), NOW + timedelta(days=1))
NAMES = {"sys_pay": "Payments", "sys_wh": "Warehouse", "sys_ord": "Order system"}

PAY_TOTAL = FieldForm("sys_pay/tpl_pay/amount", "amount")
WH_TOTAL = FieldForm("sys_wh/tpl_po/total", "amount")
PAY_NAME = FieldForm("sys_pay/tpl_pay/cardholder", "phonetic.0")
WH_NAME = FieldForm("sys_wh/tpl_po/customer", "phonetic.0")


def token(value: str) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return f"t1.{body[:22]}"


def event(
    seed: str,
    system_id: str,
    template_id: str,
    observed_at: datetime,
    identifiers: Sequence[tuple[str, str, str, str]],
    attributes: dict[str, str] | None = None,
) -> CanonicalEvent:
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(BASE_MS, seed),
        "system_id": system_id,
        "source_id": f"src_{system_id.removeprefix('sys_')}",
        "template_id": template_id,
        "observed_at": observed_at,
        "ingested_at": observed_at,
        "attributes": attributes or {},
        "identifiers": [
            {"field": f, "form": form, "token": token(value), "shape": shape, "len": len(value)}
            for f, form, value, shape in identifiers
        ],
    }
    return CanonicalEvent.model_validate(data)


HOUR_WEIGHTS = [0, 0, 0, 0, 0, 0, 0, 1, 4, 8, 9, 7, 5, 8, 9, 6, 4, 2, 1, 0, 0, 0, 0, 0]
"""A business-hours shape (UTC) so the hourly volumes of both systems carry a daily pattern."""


def business_instant(rng: random.Random, day: date) -> datetime:
    hour = rng.choices(range(24), weights=HOUR_WEIGHTS)[0]
    return datetime(day.year, day.month, day.day, hour, rng.randrange(60), tzinfo=UTC)


def keyed_later(rng: random.Random, at: datetime) -> datetime:
    """A clerk keys the row one to a few hours later, sometimes the next business day."""
    return at + timedelta(hours=rng.choice([1, 1, 2, 3, 4, 20, 26]), minutes=rng.randrange(60))


def planted_store(
    *, payments: int = 420, reach: float = 0.7, typo: float = 0.03, noise: int = 90
) -> tuple[InMemoryEventStore, set[tuple[str, str]]]:
    """A and B as in the module docstring plus ``sys_ord`` carrying an exact link to A."""
    rng = random.Random(2026)
    store = InMemoryEventStore()
    events: list[CanonicalEvent] = []
    truth: set[tuple[str, str]] = set()
    amounts = [f"{n * 7 % 1000}.{n * 13 % 100:02d}" for n in range(300)]
    names = [f"NM{n:03d}" for n in range(150)]
    days = [
        d for d in (WINDOW.start.date() + timedelta(days=n) for n in range(1, 7)) if d.weekday() < 5
    ]
    for n in range(payments):
        day = rng.choice(days)
        at = business_instant(rng, day)
        amount = rng.choice(amounts)
        name = rng.choice(names)
        order = f"{n + 1:04d}"
        pay = event(
            f"pay{n}",
            "sys_pay",
            "tpl_pay",
            at,
            [
                ("amount", "amount", amount, "999.99"),
                ("ts", "date", day.isoformat(), "9999-99-99"),
                ("cardholder", "phonetic.0", name, "AAAA"),
                ("merchant_ref", "raw", f"X9-{order}", "A9-9999"),
            ],
            {"currency": "USD"},
        )
        events.append(pay)
        events.append(
            event(
                f"ord{n}",
                "sys_ord",
                "tpl_ord",
                at - timedelta(minutes=5),
                [("merchant_ref", "raw", f"X9-{order}", "A9-9999")],
            )
        )
        if rng.random() < reach:
            keyed = rng.choice(names) if rng.random() < typo else name
            po = event(
                f"po{n}",
                "sys_wh",
                "tpl_po",
                keyed_later(rng, at),
                [
                    ("total", "amount", amount, "999.99"),
                    ("order_date", "date", day.isoformat(), "9999-99-99"),
                    ("customer", "phonetic.0", keyed, "AAAA"),
                    ("po_num", "raw", f"88-{n:04d}", "99-9999"),
                ],
                {"currency": "USD"},
            )
            events.append(po)
            truth.add((pay.event_id, po.event_id))
    for n in range(noise):
        day = rng.choice(days)
        events.append(
            event(
                f"noise{n}",
                "sys_wh",
                "tpl_po",
                keyed_later(rng, business_instant(rng, day)),
                [
                    ("total", "amount", rng.choice(amounts), "999.99"),
                    ("order_date", "date", day.isoformat(), "9999-99-99"),
                    ("customer", "phonetic.0", rng.choice(names), "AAAA"),
                    ("po_num", "raw", f"89-{n:04d}", "99-9999"),
                ],
                {"currency": "USD"},
            )
        )
    store.add_events(events, stored_at=NOW)
    return store, truth


def leak_free(blob: object) -> None:
    text = json.dumps(blob, default=str)
    assert "t1." not in text, "a token reached evidence or logs"
    assert "NM0" not in text, "a name code (a value) reached evidence or logs"


# -- the matcher alone -------------------------------------------------------------------------


def run_matcher(
    **over: Any,
) -> tuple[CompositeReport, InMemoryCompositeRuleRepository, set[tuple[str, str]]]:
    store, truth = planted_store()
    rules = InMemoryCompositeRuleRepository()
    matcher = CompositeMatcher(
        store=store,
        settings=LinkerSettings(**over),
        rules=rules,
        clock=lambda: NOW,
        system_names=NAMES,
    )
    with structlog.testing.capture_logs() as logs:
        report = matcher.run(TENANT, WINDOW, [])
    leak_free(logs)
    assert matcher.last_report is report
    return report, rules, truth


def test_end_to_end_finds_one_composite_link_with_the_target_quality() -> None:
    report, rules, truth = run_matcher()
    assert report.pairs_triggered >= 1
    outcomes = [o for o in report.outcomes if o.rule is not None]
    assert len(outcomes) == 1, [(o.trigger.system_a, o.trigger.system_b) for o in report.outcomes]
    outcome = outcomes[0]
    assert (outcome.trigger.system_a, outcome.trigger.system_b) == ("sys_pay", "sys_wh")
    assert outcome.trigger.correlation >= LinkerSettings().composite_adjacency_min_correlation
    assert outcome.capped is False and outcome.blocked_pairs > len(truth)
    assert outcome.stage == "rule" and outcome.components >= 3
    assert outcome.em_prior is not None and 0.0 < outcome.em_prior < 1.0
    assert 1 <= outcome.em_iterations <= LinkerSettings().composite_em_iterations
    assert outcome.rule is not None and outcome.threshold == outcome.rule.threshold
    assert outcome.estimated_precision == outcome.rule.estimated_precision
    # Every other triggered ordering says where it stopped.
    assert all(o.stage in OUTCOME_STAGES for o in report.outcomes)
    assert all(o.stage != "rule" for o in report.outcomes if o is not outcome)

    matched = {(m.event_id_a, m.event_id_b) for m in outcome.matches}
    precision = len(matched & truth) / len(matched)
    recall = len(matched & truth) / len(truth)
    assert precision >= 0.85, precision
    assert recall >= 0.70, recall

    rule = outcome.rule
    assert rule is not None
    assert rule.estimated_precision is not None
    assert rule.estimated_precision >= LinkerSettings().composite_target_precision
    assert rule.window_seconds == 3 * 86_400
    kinds = {component.kind for component in rule.components}
    assert {"date", "amount", "phonetic"} <= kinds
    assert all(c.m > c.u for c in rule.components if c.kind in {"date", "amount", "phonetic"})
    phonetic = next(c for c in rule.components if c.kind == "phonetic")
    assert phonetic.m > 0.9, "97% of keyed names sound the same"
    assert rule.components == tuple(sorted(rule.components, key=lambda c: -c.weight))

    link = outcome.link
    assert link is not None
    assert link.link_type == LinkType.COMPOSITE
    assert link.role == LinkRole.TRANSACTION
    assert link.direction == Direction.A_TO_B
    assert (link.a, link.b) == (rule.components[0].a, rule.components[0].b)
    assert (link.a, link.b) in {(PAY_TOTAL, WH_TOTAL), (PAY_NAME, WH_NAME)}
    assert link.a.system_id == "sys_pay" and link.b.system_id == "sys_wh"
    assert 0 < link.features.n_a <= 420, "A events carrying the field with a whole window inside"
    assert link.features.n_match == len(matched)
    assert 0.6 <= link.features.c_ab <= 1.0 and 0.5 <= link.features.c_ba <= 1.0
    assert link.features.lag_positive_share == 1.0
    assert link.features.lag_median is not None and link.features.lag_median > 0
    assert link.features.fanout_ab_median is None and link.features.chance_ratio is None
    assert link.score == pytest.approx(rule.estimated_precision * link.features.c_ab)
    assert link.score >= LinkerSettings().proposal_threshold
    assert link.affected_volume == len(matched)

    extra = dict(link.extra)
    assert extra["estimated_precision"] == rule.estimated_precision
    assert extra["threshold"] == rule.threshold
    assert extra["window_start"] == WINDOW.start.isoformat()
    assert extra["component_kinds"] == [c.kind for c in rule.components]
    assert len(extra["components"]) == len(rule.components)
    assert set(extra["components"][0]) == {"a", "b", "weight", "m", "u"}
    sentence = extra["sentence"]
    assert sentence.startswith("Payments ") and "share no identifier" in sentence
    assert "composite rule" in sentence and f"{len(matched)} shared values" in sentence
    leak_free(extra)
    leak_free(link.features.as_evidence())

    stored = rules.get(TENANT, link_id_for(TENANT, link.a, link.b))
    assert stored is not None and stored.rule == rule
    assert [row.id for row in rules.list_rules(TENANT)] == [stored.id]


def test_a_single_shared_component_is_not_a_composite_rule() -> None:
    """Two systems whose rows share only an amount (a cart total and the payment of it): the
    pair is blocked and the amounts agree, but one weak pair is not a composite (spec 9.5)."""
    rng = random.Random(7)
    store = InMemoryEventStore()
    events: list[CanonicalEvent] = []
    days = [
        d for d in (WINDOW.start.date() + timedelta(days=n) for n in range(1, 7)) if d.weekday() < 5
    ]
    for n in range(300):
        day = rng.choice(days)
        at = business_instant(rng, day)
        amount = f"{n * 7 % 1000}.{n * 13 % 100:02d}"
        events.append(
            event(f"cart{n}", "sys_web", "tpl_cart", at, [("total", "amount", amount, "999.99")])
        )
        events.append(
            event(
                f"pay{n}",
                "sys_pay",
                "tpl_pay",
                at + timedelta(minutes=rng.randrange(1, 30)),
                [("amount", "amount", amount, "999.99")],
            )
        )
    store.add_events(events, stored_at=NOW)
    matcher = CompositeMatcher(
        store=store,
        settings=LinkerSettings(),
        rules=InMemoryCompositeRuleRepository(),
        clock=lambda: NOW,
    )
    with structlog.testing.capture_logs() as logs:
        report = matcher.run(TENANT, WINDOW, [])
    triggered = [o for o in report.outcomes if o.blocked_pairs > 0]
    assert triggered, "the pair is adjacent and blocks on the amount"
    assert report.rules == 0 and report.links == ()
    assert any(entry["event"] == "composite needs at least two components" for entry in logs)
    assert all(o.stage == "few_components" and o.components == 1 for o in triggered)
    outcome_logs = [entry for entry in logs if entry["event"] == "composite pair outcome"]
    assert len(outcome_logs) == len(report.outcomes)
    leak_free(logs)


def test_disabled_or_uncorrelated_runs_produce_nothing() -> None:
    off, rules, _truth = run_matcher(composite_enabled=False)
    assert off.pairs_considered == 0 and off.links == () and rules.list_rules(TENANT) == []
    strict, rules, _truth = run_matcher(composite_adjacency_min_correlation=1.0)
    assert strict.pairs_triggered == 0 and strict.links == ()


def test_the_cap_is_honoured_and_logged_as_a_count_only() -> None:
    store, _truth = planted_store()
    matcher = CompositeMatcher(
        store=store,
        settings=LinkerSettings(composite_max_pairs=1000),
        rules=InMemoryCompositeRuleRepository(),
        clock=lambda: NOW,
    )
    with structlog.testing.capture_logs() as logs:
        report = matcher.run(TENANT, WINDOW, [])
    capped = [o for o in report.outcomes if o.capped]
    assert capped and all(o.blocked_pairs == 1000 for o in capped)
    assert report.capped == len(capped)
    warning = next(entry for entry in logs if entry["event"] == "composite blocking capped")
    assert warning["pairs"] == 1000 and set(warning) <= {
        "event",
        "log_level",
        "component",
        "tenant_id",
        "system_a",
        "system_b",
        "pairs",
    }


def test_a_strong_exact_link_between_the_systems_switches_the_pair_off() -> None:
    store, _truth = planted_store()
    matcher = CompositeMatcher(
        store=store,
        settings=LinkerSettings(),
        rules=InMemoryCompositeRuleRepository(),
        clock=lambda: NOW,
    )
    covered = ScoredLink(
        a=FieldForm("sys_pay/tpl_pay/merchant_ref", "raw"),
        b=FieldForm("sys_wh/tpl_po/po_num", "raw"),
        link_type=LinkType.EXACT,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=0.97,
        features=_features(c_ab=0.95, c_ba=0.95),
        affected_volume=399,
    )
    report = matcher.run(TENANT, WINDOW, [covered])
    assert not any(
        t.triggered
        for t in (o.trigger for o in report.outcomes)
        if (t.system_a, t.system_b) == ("sys_pay", "sys_wh")
    )
    thin = ScoredLink(
        a=covered.a,
        b=covered.b,
        link_type=LinkType.EXACT,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=0.97,
        features=_features(c_ab=0.4, c_ba=0.6),
        affected_volume=170,
    )
    report = matcher.run(TENANT, WINDOW, [thin])
    assert any(o.trigger.reason == "low_coverage" and o.rule is not None for o in report.outcomes)
    thin_trigger = next(
        t for t in report.triggers if (t.system_a, t.system_b) == ("sys_pay", "sys_wh")
    )
    wh_events = sum(
        count.events
        for count in store.template_counts(TENANT, WINDOW)
        if count.system_id == "sys_wh"
    )
    assert thin_trigger.later_end_events == wh_events > 0
    assert thin_trigger.later_end_coverage == pytest.approx(int(420 * 0.4) / wh_events)


def _features(*, c_ab: float, c_ba: float) -> Any:
    from carto_core.linker.model import PairFeatures  # noqa: PLC0415

    return PairFeatures(
        n_a=420,
        n_b=300,
        n_match=int(420 * c_ab),
        c_ab=c_ab,
        c_ba=c_ba,
        lag_median=3600.0,
        lag_iqr=600.0,
        lag_positive_share=1.0,
        fanout_ab_median=1.0,
        fanout_ab_p99=1.0,
        fanout_ba_median=1.0,
        fanout_ba_p99=1.0,
        concentration=0.05,
        chance_ratio=0.001,
        domain_size=1e6,
    )


# -- through the pass and the review service --------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._head = GENESIS_HASH

    def record(self, entry: AuditEntry) -> str:
        self.entries.append(entry)
        self._head = chain_hash(self._head, entry.content())
        return self._head

    def latest_hash(self, tenant_id: str) -> str:
        return self._head


class StoreProfiles:
    """Profiles and window sketches computed from the store, the way package B does."""

    def __init__(self, store: InMemoryEventStore, window: Window) -> None:
        tokens: dict[FieldForm, set[str]] = defaultdict(set)
        counts: dict[FieldForm, int] = defaultdict(int)
        shapes: dict[FieldForm, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in store.identifier_rows(TENANT, window, forms=frozenset({"raw"})):
            end = FieldForm(row.field_ref, row.form)
            tokens[end].add(row.token)
            counts[end] += 1
            shapes[end][row.shape] += 1
        self._profiles: list[FieldProfile] = []
        self._sketches: dict[FieldForm, WindowSketch] = {}
        for end, values in tokens.items():
            top = sorted(shapes[end].items(), key=lambda item: -item[1])
            self._profiles.append(
                FieldProfile(
                    end=end,
                    count=counts[end],
                    distinct=len(values),
                    null_rate=0.0,
                    shapes=tuple(ShapeShare(s, n / counts[end]) for s, n in top),
                    length_stats=None,
                    top_share=0.02,
                    quality=0.9,
                    is_candidate=len(values) >= PROFILER.min_distinct,
                    first_seen=None,
                    last_seen=None,
                )
            )
            minhash = MinHash(num_perm=PROFILER.minhash_permutations)
            for value in values:
                minhash.update(value.encode())
            bottom = sorted(
                int.from_bytes(hashlib.sha256(v.encode()).digest()[:8], "big") for v in values
            )
            self._sketches[end] = WindowSketch(
                end=end,
                days=(date(2026, 10, 17),),
                count=counts[end],
                shape_counts=dict(shapes[end]),
                length_counts={},
                minhash=np.asarray(minhash.hashvalues, dtype=np.uint64).astype(">u8").tobytes(),
                hll=b"",
                bottom_k=tuple(bottom[: PROFILER.bottom_k]),
                top_token_counts={},
                first_seen=None,
                last_seen=None,
            )

    def profiles(self, tenant_id: str) -> Sequence[FieldProfile]:
        return list(self._profiles)

    def window_sketch(self, tenant_id: str, end: FieldForm) -> WindowSketch | None:
        return self._sketches.get(end)


def test_pass_with_auto_accept_keeps_composite_links_proposed() -> None:
    store, _truth = planted_store()
    links = InMemoryLinkRepository()
    recorder = Recorder()
    rules = InMemoryCompositeRuleRepository()
    settings = LinkerSettings(auto_accept_enabled=True)
    matcher = CompositeMatcher(
        store=store, settings=settings, rules=rules, clock=lambda: NOW, system_names=NAMES
    )
    link_pass = LinkPass(
        store=store,
        profiles=StoreProfiles(store, WINDOW),
        links=links,
        entities=InMemoryEntityRepository(),
        audit=recorder,
        settings=settings,
        profiler_settings=PROFILER,
        clock=lambda: NOW,
        system_names=NAMES,
        composite_hook=matcher.hook,
    )
    with structlog.testing.capture_logs() as logs:
        report = link_pass.run(TENANT, full=True, now=NOW)
    leak_free(logs)
    assert report.composite_links == 1 and report.composite_proposed == 1
    assert report.proposed == 1 and report.auto_accepted == 1, "the exact merchant_ref link"
    assert matcher.last_report is not None and matcher.last_report.rules == 1

    by_type = {link.link_type: link for link in links.list_links(TENANT)}
    assert set(by_type) == {LinkType.EXACT, LinkType.COMPOSITE}
    exact = by_type[LinkType.EXACT]
    assert exact.status == LinkStatus.AUTO_ACCEPTED
    assert {exact.a.system_id, exact.b.system_id} == {"sys_ord", "sys_pay"}
    composite = by_type[LinkType.COMPOSITE]
    assert composite.status == LinkStatus.PROPOSED
    assert composite.decided_by is None and composite.version == 1
    assert composite.score >= settings.auto_accept_threshold or composite.score >= 0.5
    assert composite.extra["components"] and "sentence" in composite.extra
    assert rules.get(TENANT, composite.id) is not None
    assert [e.action for e in recorder.entries] == ["link.auto_accept"]
    assert all("t1." not in json.dumps(e.content(), default=str) for e in recorder.entries)

    # A second pass refreshes the composite proposal instead of duplicating it.
    second = link_pass.run(TENANT, full=False, now=NOW)
    assert second.composite_links == 1 and second.composite_proposed == 0
    refreshed = links.get(TENANT, composite.id)
    assert refreshed is not None and refreshed.status == LinkStatus.PROPOSED
    assert refreshed.version == 2


def test_review_service_never_auto_accepts_a_composite_link() -> None:
    links = InMemoryLinkRepository()
    service = ReviewService(
        links,
        Recorder(),
        LinkerSettings(auto_accept_enabled=True, auto_accept_threshold=0.5),
        clock=lambda: NOW,
    )
    composite = ScoredLink(
        a=PAY_TOTAL,
        b=WH_TOTAL,
        link_type=LinkType.COMPOSITE,
        role=LinkRole.TRANSACTION,
        direction=Direction.A_TO_B,
        score=0.99,
        features=_features(c_ab=0.7, c_ba=0.9),
        affected_volume=290,
        extra={"components": []},
    )
    report = service.propose(TENANT, [composite], now=NOW)
    assert report.proposed == 1 and report.auto_accepted == 0
    stored = links.find_by_ends(TENANT, PAY_TOTAL, WH_TOTAL)
    assert stored is not None and stored.status == LinkStatus.PROPOSED
    assert service.auto_accept(TENANT, stored.id, now=NOW) is None
