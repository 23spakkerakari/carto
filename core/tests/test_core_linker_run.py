"""The link pass end to end on the in-memory store (spec 9.2 to 9.6) and the evidence sentence
(spec 9.6; ADR 0057). Two systems where 95% of A's order ids appear in B one to three hours
later, a bridge field in A with full pairing consistency, and a noisy field that overlaps A by
chance: the pass proposes the exact link (transaction, A to B, score above 0.8) and the bridge,
not the noise; accepting them builds one family named Order; nothing stored carries a token."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from datasketch import MinHash

from carto_common.ids import derive_ulid
from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash
from carto_core.linker.model import (
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
    RejectReason,
)
from carto_core.linker.repository import InMemoryEntityRepository, InMemoryLinkRepository
from carto_core.linker.review import link_id_for
from carto_core.linker.run import LinkPass
from carto_core.linker.sentence import (
    MAX_SENTENCE_LEN,
    describe_lag,
    evidence_sentence,
    field_noun,
    form_phrase,
    system_label,
)
from carto_core.profiler.model import FieldProfile, ShapeShare, WindowSketch
from carto_core.profiler.run import profile_window
from carto_core.settings import LinkerSettings, ProfilerSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import FieldForm, Window
from carto_schema.event import CanonicalEvent

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BASE_MS = 1_790_000_000_000
TENANT = "default"
PROFILER = ProfilerSettings()

ORDER_ID = FieldForm("sys_a/tpl_a/order_id", "raw")
MERCHANT = FieldForm("sys_a/tpl_a/merchant_ref", "raw")
ORDER_REF = FieldForm("sys_b/tpl_b/order_ref", "digits.0")
NOISE = FieldForm("sys_c/tpl_c/ticket_no", "raw")


def token(value: str) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return f"t1.{body[:22]}"


def event(
    seed: str,
    system_id: str,
    template_id: str,
    observed_at: datetime,
    identifiers: Sequence[tuple[str, str, str, str]],
) -> CanonicalEvent:
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(BASE_MS, seed),
        "system_id": system_id,
        "source_id": f"src_{system_id.removeprefix('sys_')}",
        "template_id": template_id,
        "observed_at": observed_at,
        "ingested_at": observed_at,
        "attributes": {},
        "identifiers": [
            {"field": f, "form": form, "token": token(value), "shape": shape, "len": len(value)}
            for f, form, value, shape in identifiers
        ],
    }
    return CanonicalEvent.model_validate(data)


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
    """Profiles and window sketches computed from the store, the way package B will."""

    def __init__(self, store: InMemoryEventStore, window: Window) -> None:
        tokens: dict[FieldForm, set[str]] = defaultdict(set)
        counts: dict[FieldForm, int] = defaultdict(int)
        shapes: dict[FieldForm, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for row in store.identifier_rows(TENANT, window):
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


def shop_store(orders: int = 600) -> InMemoryEventStore:
    store = InMemoryEventStore()
    events: list[CanonicalEvent] = []
    for n in range(orders):
        value = f"{n + 1:04d}"
        at = NOW - timedelta(days=5) + timedelta(minutes=7 * n)
        events.append(
            event(
                f"a{n}",
                "sys_a",
                "tpl_a",
                at,
                [
                    ("order_id", "raw", value, "9999"),
                    ("merchant_ref", "raw", f"X9-{n:04d}", "A9-9999"),
                ],
            )
        )
        if n % 20 != 0:  # 95% reach B, one to three hours later
            lag = timedelta(hours=1, minutes=(n * 13) % 120)
            events.append(
                event(
                    f"b{n}",
                    "sys_b",
                    "tpl_b",
                    at + lag,
                    [("order_ref", "digits.0", value, "9999")],
                )
            )
        # Noise: a ticket number space that overlaps the order space by about 5%.
        noise = value if n % 20 == 0 else f"{9000 + n:04d}"
        events.append(event(f"c{n}", "sys_c", "tpl_c", at, [("ticket_no", "raw", noise, "9999")]))
    store.add_events(events, stored_at=NOW)
    return store


def make_pass(
    store: InMemoryEventStore, **over: Any
) -> tuple[
    LinkPass, InMemoryLinkRepository, InMemoryEntityRepository, Recorder, list[dict[str, Any]]
]:
    links = InMemoryLinkRepository()
    entities = InMemoryEntityRepository()
    recorder = Recorder()
    hook_calls: list[dict[str, Any]] = []
    settings = LinkerSettings(**over)
    window = Window(NOW - timedelta(days=PROFILER.window_days), NOW + timedelta(days=1))
    link_pass = LinkPass(
        store=store,
        profiles=StoreProfiles(store, window),
        links=links,
        entities=entities,
        audit=recorder,
        settings=settings,
        profiler_settings=PROFILER,
        clock=lambda: NOW,
        system_names={"sys_a": "Order system", "sys_b": "Warehouse"},
        composite_hook=lambda **kwargs: hook_calls.append(kwargs),
    )
    return link_pass, links, entities, recorder, hook_calls


def test_window_is_the_profiler_read_window() -> None:
    """The join runs over the same days the sketches cover (profile_window): the last
    ``window_days`` UTC days plus the day ahead, aligned to day starts."""
    link_pass, *_ = make_pass(InMemoryEventStore())
    window = link_pass.window_for(NOW)
    today = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    assert window.start == today - timedelta(days=6)
    assert window.end == today + timedelta(days=2)
    assert window == profile_window(NOW, PROFILER)


def test_end_to_end_proposes_the_exact_link_and_the_bridge_not_the_noise() -> None:
    store = shop_store()
    link_pass, links, entities, recorder, hook_calls = make_pass(store)
    report = link_pass.run(TENANT, full=True, now=NOW)

    assert report.candidate_ends == 4
    assert report.bridge_candidates == 1
    assert report.proposed == 2
    assert report.families == 0 and report.auto_accepted == 0
    assert report.weights_version == "v1-hand-set"
    assert recorder.entries == []

    stored = {(link.a, link.b): link for link in links.list_links(TENANT)}
    assert set(stored) == {(ORDER_ID, ORDER_REF), (MERCHANT, ORDER_ID)}
    exact = stored[(ORDER_ID, ORDER_REF)]
    assert exact.link_type == LinkType.EXACT
    assert exact.status == LinkStatus.PROPOSED
    assert exact.role == LinkRole.TRANSACTION
    assert exact.direction == Direction.A_TO_B
    assert exact.score > 0.8
    assert exact.features.c_ab == pytest.approx(0.95)
    assert exact.features.n_match == 570
    assert exact.affected_volume == 570
    assert exact.extra["shape_a"] == "9999" and exact.extra["shape_b"] == "9999"
    sentence = exact.extra["sentence"]
    assert sentence.startswith("95% of Order system orders (order_id, 9999) match an order")
    assert "the digits inside the order number" in sentence, "shape 9999 is a number"

    bridge = stored[(MERCHANT, ORDER_ID)]
    assert bridge.link_type == LinkType.BRIDGE
    assert bridge.role == LinkRole.TRANSACTION
    assert bridge.score >= 0.98
    assert bridge.features.pairing_consistency == 1.0
    assert bridge.features.n_match == 600
    assert "appear together in the same Order system events" in bridge.extra["sentence"]

    assert not any(NOISE in key for key in stored), "the chance overlap is never proposed"
    for link in stored.values():
        blob = json.dumps(link.features.as_evidence()) + json.dumps(link.extra)
        assert "t1." not in blob
    assert len(hook_calls) == 1
    assert hook_calls[0]["tenant_id"] == TENANT
    assert len(hook_calls[0]["links"]) == 2

    # The reviewer accepts both; the next pass builds one family named Order.
    for link in stored.values():
        link_pass.review.accept(TENANT, link.id, actor="alice", expected_version=link.version)
    second = link_pass.run(TENANT, full=False, now=NOW)
    assert second.proposed == 0 and second.updated == 2
    assert second.stale == 0
    assert second.families == 1 and second.families_named == 1
    family = entities.list_entities(TENANT)[0]
    assert family.members == (MERCHANT, ORDER_ID, ORDER_REF)
    assert family.suggested_name == "Order"
    assert family.name is None
    for link in links.list_links(TENANT):
        assert link.entity_id == family.id
        assert link.status == LinkStatus.ACCEPTED
        assert link.version == 3, "proposal, decision, refresh"
    assert [e.action for e in recorder.entries] == ["link.accept", "link.accept"]


def test_rejected_pairs_are_re_verified_only_on_a_full_pass() -> None:
    store = shop_store()
    link_pass, links, _entities, recorder, _hook = make_pass(store)
    link_pass.run(TENANT, full=True, now=NOW)
    exact_id = link_id_for(TENANT, ORDER_ID, ORDER_REF)
    link_pass.review.reject(
        TENANT, exact_id, actor="alice", expected_version=1, reason=RejectReason.COINCIDENCE
    )
    incremental = link_pass.run(TENANT, full=False, now=NOW)
    rejected = links.get(TENANT, exact_id)
    assert rejected is not None and rejected.status == LinkStatus.REJECTED
    assert rejected.version == 2, "an incremental pass does not touch a rejected pair"
    full = link_pass.run(TENANT, full=True, now=NOW)
    assert full.candidates_considered >= incremental.candidates_considered
    rejected = links.get(TENANT, exact_id)
    assert rejected is not None and rejected.status == LinkStatus.REJECTED
    assert [e.action for e in recorder.entries] == ["link.reject"]


def test_auto_accept_through_the_pass_builds_families_at_once() -> None:
    store = shop_store()
    link_pass, _links, entities, recorder, _hook = make_pass(store, auto_accept_enabled=True)
    report = link_pass.run(TENANT, full=True, now=NOW)
    assert report.auto_accepted == 2
    assert report.families == 1
    assert entities.list_entities(TENANT)[0].suggested_name == "Order"
    assert {e.action for e in recorder.entries} == {"link.auto_accept"}


def test_an_accepted_link_goes_stale_when_b_stops_seeing_the_tokens() -> None:
    store = shop_store()
    link_pass, links, _entities, recorder, _hook = make_pass(store)
    link_pass.run(TENANT, full=True, now=NOW)
    exact_id = link_id_for(TENANT, ORDER_ID, ORDER_REF)
    link_pass.review.accept(TENANT, exact_id, actor="alice", expected_version=1)
    # Nine days later the window holds A's new orders but none of B's rows.
    later = NOW + timedelta(days=9)
    store.add_events(
        [
            event(
                f"late{n}",
                "sys_a",
                "tpl_a",
                later - timedelta(hours=n % 48),
                [("order_id", "raw", f"{5000 + n:04d}", "9999")],
            )
            for n in range(100)
        ],
        stored_at=later,
    )
    link_pass2 = LinkPass(
        store=store,
        profiles=StoreProfiles(store, link_pass.window_for(later)),
        links=links,
        entities=InMemoryEntityRepository(),
        audit=recorder,
        settings=LinkerSettings(),
        profiler_settings=PROFILER,
        clock=lambda: later,
    )
    report = link_pass2.run(TENANT, full=False, now=later)
    assert report.stale == 1
    stale = links.get(TENANT, exact_id)
    assert stale is not None and stale.status == LinkStatus.STALE
    assert recorder.entries[-1].action == "link.stale"


def test_form_variants_collapse_to_one_link_per_field_pair() -> None:
    store = InMemoryEventStore()
    events: list[CanonicalEvent] = []
    for n in range(300):
        value = f"{n + 1:04d}"
        at = NOW - timedelta(days=3) + timedelta(minutes=5 * n)
        events.append(
            event(
                f"a{n}",
                "sys_a",
                "tpl_a",
                at,
                [
                    ("order_id", "raw", value, "9999"),
                    ("order_id", "alnum", value, "9999"),
                    ("merchant_ref", "raw", f"X9-{n:04d}", "A9-9999"),
                    ("merchant_ref", "alnum", f"X9{n:04d}", "A99999"),
                ],
            )
        )
        events.append(
            event(
                f"b{n}",
                "sys_b",
                "tpl_b",
                at + timedelta(hours=1),
                [("order_ref", "digits.0", value, "9999"), ("order_ref", "alnum", value, "9999")],
            )
        )
    store.add_events(events, stored_at=NOW)
    link_pass, links, _entities, _recorder, _hook = make_pass(store)
    report = link_pass.run(TENANT, full=True, now=NOW)
    stored = links.list_links(TENANT)
    by_fields = {(link.a.field_ref, link.b.field_ref, link.link_type): link for link in stored}
    assert len(stored) == len(by_fields) == 2, "one row per field pair and type"
    exact = by_fields[(ORDER_ID.field_ref, ORDER_REF.field_ref, LinkType.EXACT)]
    assert exact.a.form == "raw", "the representative has the most raw ends"
    assert exact.b.form in ("digits.0", "alnum")
    variants = exact.extra["form_variants"]
    assert len(variants) == 3
    assert all(len(v) == 3 and 0.0 <= v[2] <= 1.0 for v in variants)
    assert {tuple(v[:2]) for v in variants} >= {("alnum", "alnum")}
    bridge = by_fields[(MERCHANT.field_ref, ORDER_ID.field_ref, LinkType.BRIDGE)]
    assert (bridge.a.form, bridge.b.form) == ("raw", "raw")
    assert len(bridge.extra["form_variants"]) == 3
    assert report.proposed == 2

    # A decision on the representative sticks to the field pair across passes.
    link_pass.review.accept(TENANT, exact.id, actor="alice", expected_version=exact.version)
    link_pass.run(TENANT, full=True, now=NOW)
    assert len(links.list_links(TENANT)) == 2
    accepted = links.get(TENANT, exact.id)
    assert accepted is not None and accepted.status == LinkStatus.ACCEPTED


# -- the sentence --------------------------------------------------------------------------------

PO = FieldForm("sys_warehouse/tpl_po/po_num", "raw")
REF = FieldForm("sys_orders/tpl_o/order_ref", "digits.0")
NAMES = {"sys_warehouse": "Warehouse", "sys_orders": "Order system"}
SHAPES = {PO: "99-999", REF: "AA-9999999"}


def features(**over: Any) -> PairFeatures:
    base: dict[str, Any] = {
        "n_a": 543,
        "n_b": 600,
        "n_match": 500,
        "c_ab": 0.92,
        "c_ba": 0.83,
        "lag_median": 7000.0,
        "lag_iqr": 600.0,
        "lag_positive_share": 0.95,
        "fanout_ab_median": 1.0,
        "fanout_ab_p99": 2.0,
        "fanout_ba_median": 1.0,
        "fanout_ba_p99": 1.0,
        "concentration": 0.02,
        "chance_ratio": 0.001,
        "domain_size": 1e7,
        "pairing_consistency": None,
    }
    base.update(over)
    return PairFeatures(**base)


def key_link(
    a: FieldForm = PO,
    b: FieldForm = REF,
    *,
    direction: Direction = Direction.A_TO_B,
    role: LinkRole = LinkRole.TRANSACTION,
    link_type: LinkType = LinkType.EXACT,
    **over: Any,
) -> KeyLink:
    return KeyLink(
        id="lnk_test",
        a=a,
        b=b,
        link_type=link_type,
        role=role,
        direction=direction,
        score=0.9,
        status=LinkStatus.PROPOSED,
        features=features(**over),
        affected_volume=500,
    )


def test_sentence_follows_the_spec_example() -> None:
    sentence = evidence_sentence(key_link(), system_names=NAMES, shapes=SHAPES)
    assert sentence == (
        "92% of Warehouse purchase orders (po_num, 99-999) match an order (order_ref, "
        "AA-9999999) within about 2 hours; the match uses the digits inside the order "
        "reference. 500 shared values in the window."
    )


def test_sentence_is_direction_aware() -> None:
    backwards = evidence_sentence(
        key_link(direction=Direction.B_TO_A, lag_median=-90.0), system_names=NAMES, shapes=SHAPES
    )
    assert backwards.startswith(
        "83% of Order system orders (order_ref, AA-9999999) match a purchase order "
        "(po_num, 99-999) within about 2 minutes"
    )
    undirected = evidence_sentence(
        key_link(direction=Direction.UNDIRECTED, lag_median=30.0), system_names=NAMES, shapes=SHAPES
    )
    assert "share their value with an order" in undirected
    assert "at about the same time in no fixed order" in undirected


def test_sentence_for_bridges_composites_and_roles() -> None:
    order_id = FieldForm("sys_orders/tpl_pay/order_id", "raw")
    merchant = FieldForm("sys_orders/tpl_pay/merchant_ref", "raw")
    bridge = evidence_sentence(
        key_link(
            order_id, merchant, link_type=LinkType.BRIDGE, pairing_consistency=0.995, lag_median=0.0
        ),
        system_names=NAMES,
    )
    assert bridge.startswith(
        "Order system orders (order_id) and merchant refs (merchant_ref) appear together in the "
        "same Order system events; 100% of them always carry the same merchant ref for one order "
        "reference"
    )
    composite = evidence_sentence(key_link(link_type=LinkType.COMPOSITE), system_names=NAMES)
    assert "composite rule" in composite
    association = evidence_sentence(key_link(role=LinkRole.ASSOCIATION), shapes=SHAPES)
    assert "groups records rather than identifying one" in association
    batch = evidence_sentence(key_link(role=LinkRole.BATCH))
    assert batch.endswith("a batch. 500 shared values in the window.")


def test_sentence_shapes_fall_back_to_the_stored_extra_and_systems_to_their_ids() -> None:
    link = key_link()
    link.extra.update({"shape_a": "99-999", "shape_b": "AA-9999999"})
    assert "(po_num, 99-999)" in evidence_sentence(link)
    assert evidence_sentence(link).startswith("92% of Warehouse purchase orders")
    assert system_label("sys_people_ops", None) == "People ops"
    assert system_label("sys_x", {"sys_x": "HRIS"}) == "HRIS"


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ("raw", None),
        ("digits.1", "the digits inside the order reference"),
        ("alnum", "the letters and digits of the order reference"),
        ("norm", "the order reference ignoring case and spacing"),
        ("phonetic.0", "how the name sounds"),
        ("date", "the date in the order reference"),
        ("amount", "the amount in the order reference"),
    ],
)
def test_form_phrases(form: str, expected: str | None) -> None:
    assert form_phrase(form, "order reference") == expected


def test_field_nouns_and_lags() -> None:
    assert field_noun(PO, "99-999") == "PO number"
    assert field_noun(REF, "AA-9999999") == "order reference"
    assert field_noun(FieldForm("sys/t/merchant_ref", "raw")) == "merchant ref"
    assert field_noun(FieldForm("sys/t/items.3.skuCode", "raw")) == "SKU reference"
    assert describe_lag(None) == ""
    assert describe_lag(12.0) == "at about the same time"
    assert describe_lag(-130.0) == "within about 3 minutes"
    assert describe_lag(3600.0) == "within about 1 hour"
    assert describe_lag(200_000.0) == "within about 3 days"


def test_sentence_is_bounded_and_never_empty() -> None:
    long_a = FieldForm("sys_a/tpl/" + "x_" * 600, "alnum")
    long_b = FieldForm("sys_b/tpl/" + "y_" * 600, "digits.0")
    sentence = evidence_sentence(key_link(long_a, long_b, n_match=0, lag_median=None))
    assert len(sentence) <= MAX_SENTENCE_LEN
    assert sentence.endswith(".")
    short = evidence_sentence(
        key_link(
            n_match=0, lag_median=None, lag_positive_share=None, direction=Direction.UNDIRECTED
        )
    )
    assert (short[0].isupper() or short[0].isdigit()) and short.endswith(".")
    assert "shared values" not in short
