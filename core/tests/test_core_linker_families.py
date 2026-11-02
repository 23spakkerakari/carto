"""Entity families (spec 9.4, 9.6): connected components over usable links only, deterministic
content-addressed entity ids, names carried over to a family that grew, suggestions for new
ones, the rebuild through the repositories, and the property that a rejected pair whose only
link is the rejected one never shares a family."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from carto_core.linker.families import (
    FamilyBuilder,
    build_families,
    entity_id_for,
    inherit_names,
)
from carto_core.linker.model import (
    Direction,
    KeyLink,
    LinkRole,
    LinkStatus,
    LinkType,
    PairFeatures,
)
from carto_core.linker.repository import (
    Entity,
    InMemoryEntityRepository,
    InMemoryLinkRepository,
)
from carto_core.store.model import FieldForm

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
ENDS = [FieldForm(f"sys_{i}/tpl/field_{i}", "raw") for i in range(8)]
A, B, C, D, E, F, G, H = ENDS
FEATURES = PairFeatures(
    n_a=100,
    n_b=100,
    n_match=90,
    c_ab=0.9,
    c_ba=0.9,
    lag_median=10.0,
    lag_iqr=1.0,
    lag_positive_share=0.9,
    fanout_ab_median=1.0,
    fanout_ab_p99=1.0,
    fanout_ba_median=1.0,
    fanout_ba_p99=1.0,
    concentration=0.1,
    chance_ratio=0.01,
    domain_size=1e4,
)


def link(
    a: FieldForm,
    b: FieldForm,
    *,
    status: LinkStatus = LinkStatus.ACCEPTED,
    role: LinkRole = LinkRole.TRANSACTION,
    link_type: LinkType = LinkType.EXACT,
) -> KeyLink:
    a, b = sorted((a, b))
    return KeyLink(
        id=f"lnk_{a.field_ref[-1]}{b.field_ref[-1]}",
        a=a,
        b=b,
        link_type=link_type,
        role=role,
        direction=Direction.A_TO_B,
        score=0.9,
        status=status,
        features=FEATURES,
        affected_volume=90,
    )


def test_families_use_accepted_transaction_links_and_bridges_only() -> None:
    """Spec 9.6: accepted links of role transaction, exact or bridge; a bridge of role batch or
    association never merges families (it would glue a file to the records it carries)."""
    links = [
        link(A, B),
        link(
            B,
            C,
            status=LinkStatus.AUTO_ACCEPTED,
            link_type=LinkType.BRIDGE,
            role=LinkRole.TRANSACTION,
        ),
        link(C, D, role=LinkRole.ASSOCIATION),
        link(D, E, status=LinkStatus.PROPOSED),
        link(E, F, status=LinkStatus.REJECTED),
        link(F, G, status=LinkStatus.STALE),
        link(G, H, role=LinkRole.BATCH),
        link(C, H, link_type=LinkType.BRIDGE, role=LinkRole.BATCH),
    ]
    families = build_families(links)
    assert [family.members for family in families] == [(A, B, C)]
    assert families[0].link_ids == ("lnk_01", "lnk_12")
    assert families[0].entity_id == entity_id_for([C, A, B])
    assert build_families([]) == []


def test_entity_ids_are_deterministic_and_order_free() -> None:
    first = entity_id_for([A, B, C])
    assert first == entity_id_for([C, B, A])
    assert first.startswith("ent_") and len(first) == 20
    assert first != entity_id_for([A, B])


def test_bridges_make_links_transitive() -> None:
    links = [
        link(A, B, link_type=LinkType.BRIDGE),
        link(B, C, link_type=LinkType.BRIDGE),
        link(C, D),
        link(E, F),
    ]
    families = build_families(links)
    members = sorted(family.members for family in families)
    assert members == [(A, B, C, D), (E, F)]


def test_names_carry_over_to_a_family_that_grew() -> None:
    previous = [
        Entity(id=entity_id_for([A, B]), members=(A, B), name="Order", name_confirmed=True),
        Entity(id=entity_id_for([E, F]), members=(E, F), suggested_name="Shipment"),
        Entity(id=entity_id_for([G, H]), members=(G, H), name="Gone", name_confirmed=True),
    ]
    families = build_families([link(A, B), link(B, C), link(E, F), link(D, G)])
    entities, inherited, suggested = inherit_names(families, previous, lambda _m: "Thing")
    by_members = {entity.members: entity for entity in entities}
    grown = by_members[(A, B, C)]
    assert grown.name == "Order" and grown.name_confirmed
    assert grown.id == entity_id_for([A, B, C])
    same = by_members[(E, F)]
    assert same.suggested_name == "Shipment" and same.name is None
    fresh = by_members[(D, G)]
    assert fresh.name is None and fresh.suggested_name == "Thing"
    assert inherited == 2 and suggested == 1


def test_largest_overlap_wins_and_each_previous_entity_is_used_once() -> None:
    previous = [
        Entity(id="ent_small", members=(D, E), name="Ship", name_confirmed=True),
        Entity(id="ent_big", members=(A, B, C), name="Order", name_confirmed=True),
    ]
    families = build_families([link(A, B), link(B, C), link(C, D), link(D, E)])
    entities, inherited, _suggested = inherit_names(families, previous, None)
    assert entities[0].name == "Order"
    assert inherited == 1


def test_a_shrunken_family_gets_a_new_suggestion() -> None:
    previous = [Entity(id=entity_id_for([A, B, C]), members=(A, B, C), name="Order")]
    families = build_families([link(A, B)])
    entities, inherited, suggested = inherit_names(families, previous, lambda _m: None)
    assert inherited == 0 and suggested == 0
    assert entities[0].name is None and entities[0].suggested_name is None


def test_rebuild_writes_entities_and_assigns_links() -> None:
    links = InMemoryLinkRepository()
    entities = InMemoryEntityRepository()
    for item in (
        link(A, B),
        link(B, C, link_type=LinkType.BRIDGE),
        link(D, E),
        link(F, G, status=LinkStatus.REJECTED),
    ):
        links.upsert("default", item, NOW)
    builder = FamilyBuilder(links, entities, namer=lambda members: f"Family of {len(members)}")
    report = builder.rebuild("default", now=NOW)
    assert report.families == 2
    assert report.links_assigned == 3
    assert report.names_suggested == 2
    stored = {entity.members: entity for entity in entities.list_entities("default")}
    assert stored[(A, B, C)].suggested_name == "Family of 3"
    assert stored[(D, E)].suggested_name == "Family of 2"
    by_id = {item.id: item for item in links.list_links("default")}
    assert by_id["lnk_01"].entity_id == entity_id_for([A, B, C])
    assert by_id["lnk_12"].entity_id == entity_id_for([A, B, C])
    assert by_id["lnk_34"].entity_id == entity_id_for([D, E])
    assert by_id["lnk_56"].entity_id is None
    assert by_id["lnk_01"].version == 1, "assigning a family is not a reviewed change"

    entities.set_name(
        "default", entity_id_for([A, B, C]), name="Order", description="Web orders", now=NOW
    )
    links.upsert("default", link(C, D), NOW)
    again = builder.rebuild("default", now=NOW)
    assert again.families == 1 and again.names_inherited == 1
    merged = entities.list_entities("default")
    assert len(merged) == 1
    assert merged[0].name == "Order" and merged[0].description == "Web orders"
    assert merged[0].members == (A, B, C, D, E)


# -- property: a rejected pair never shares a family through its own link ----------------------

end_index = st.integers(min_value=0, max_value=7)


@h_settings(max_examples=150, deadline=None)
@given(
    usable=st.lists(st.tuples(end_index, end_index), max_size=10),
    rejected=st.lists(st.tuples(end_index, end_index), min_size=1, max_size=5),
)
def test_rejected_pairs_never_share_a_family_unless_accepted_links_connect_them(
    usable: Sequence[tuple[int, int]], rejected: Sequence[tuple[int, int]]
) -> None:
    accepted = [link(ENDS[i], ENDS[j]) for i, j in usable if i != j]
    accepted_pairs = {(item.a, item.b) for item in accepted}
    rejects = [
        link(ENDS[i], ENDS[j], status=LinkStatus.REJECTED)
        for i, j in rejected
        if i != j and tuple(sorted((ENDS[i], ENDS[j]))) not in accepted_pairs
    ]
    families = build_families([*accepted, *rejects])
    family_of = {end: family.entity_id for family in families for end in family.members}
    # Reachability through accepted links alone.
    parent = {end: end for end in ENDS}

    def find(end: FieldForm) -> FieldForm:
        while parent[end] != end:
            end = parent[end]
        return end

    for item in accepted:
        parent[find(item.a)] = find(item.b)
    for item in rejects:
        same_family = family_of.get(item.a) is not None and family_of.get(item.a) == family_of.get(
            item.b
        )
        connected = find(item.a) == find(item.b)
        assert same_family == connected
        assert all(item.id not in family.link_ids for family in families)


def test_form_variants_of_one_field_pair_are_one_edge() -> None:
    a_alnum = FieldForm(A.field_ref, "alnum")
    b_digits = FieldForm(B.field_ref, "digits.0")
    variants = [
        link(A, B),
        replace(link(a_alnum, b_digits), id="lnk_variant", score=0.95),
        replace(link(a_alnum, B), id="lnk_variant2", score=0.99),
    ]
    families = build_families(variants)
    assert len(families) == 1
    assert families[0].members == (A, B), "the most raw ends stand for the field pair"
    assert families[0].link_ids == ("lnk_01",)
    only_variants = build_families(variants[1:])
    assert only_variants[0].link_ids == ("lnk_variant2",), "then the higher score"


def test_forms_of_one_field_are_one_node() -> None:
    """A raw link into B and a digits link out of B belong to one family (spec 9.6 builds
    families over fields, not over forms)."""
    b_digits = FieldForm(B.field_ref, "digits.0")
    families = build_families(
        [link(A, B), replace(link(b_digits, C), id="lnk_bc_digits"), link(D, E)]
    )
    assert [family.members for family in families] == sorted(
        [tuple(sorted((A, B, b_digits, C))), (D, E)], key=entity_id_for
    )
    joined = next(family for family in families if A in family.members)
    assert joined.link_ids == ("lnk_01", "lnk_bc_digits")
