"""The worker-side wiring of the link pass (plan M2 "How the engine runs" steps 2 to 4):
``LinkPass.from_context`` composes the PostgreSQL repositories, the profiler's seams and the
composite matcher; the profiler's estimators are used when the package imports and the linker's
defaults otherwise; ``SketchProfileSource`` turns the profiler's day sketches into the per-end
profiles and window sketches a pass needs and the pass proposes the exact link from them;
``MetadataProfileSource`` is the degraded source; the gateway namer falls back to the dictionary
on every failure of the LLM path; the worker's handler factory now has a ``link`` handler."""

from __future__ import annotations

import base64
import hashlib
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy

from carto_common.ids import derive_ulid
from carto_common.settings import LlmSettings
from carto_core.jobs import JobKind
from carto_core.linker.candidates import default_containment, default_minhash_decoder
from carto_core.linker.families import FamilyBuilder
from carto_core.linker.gateway_namer import GatewayNamer
from carto_core.linker.model import LinkStatus, LinkType
from carto_core.linker.profile_source import (
    LINKER_DEFAULTS,
    PROFILER_SOURCE,
    MetadataProfileSource,
    SketchProfileSource,
    profile_source_for,
    profiler_seams,
)
from carto_core.linker.repository import InMemoryEntityRepository, InMemoryLinkRepository
from carto_core.linker.review import link_id_for
from carto_core.linker.run import LinkPass, LinkReport, ProfileSource
from carto_core.linker.verify import default_domain_size
from carto_core.llm_gateway.audit import InMemoryCallAuditSink
from carto_core.llm_gateway.gateway import LlmGateway
from carto_core.llm_gateway.payload import FieldRecord as ReaderField
from carto_core.llm_gateway.payload import InMemoryMetadataReader
from carto_core.llm_gateway.providers.fake import FakeBehaviour, FakeProvider
from carto_core.metadata import FieldRecord, InMemoryMetadataStore
from carto_core.profiler.repository import InMemorySketchRepository
from carto_core.profiler.run import profile_window
from carto_core.profiler.sketches import DaySketchBuilder
from carto_core.settings import CoreSettings, ProfilerSettings
from carto_core.store.memory import InMemoryEventStore
from carto_core.store.model import FieldForm
from carto_core.worker import WorkerContext, default_handler_factory
from carto_schema.event import CanonicalEvent
from carto_schema.inventory import EdgeInventory, InventorySystem

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BASE_MS = 1_790_000_000_000
TENANT = "default"
PROFILER = ProfilerSettings()
ORDER_ID = FieldForm("sys_a/tpl_a/order_id", "raw")
ORDER_REF = FieldForm("sys_b/tpl_b/order_ref", "digits.0")


class Recorder:
    def __init__(self) -> None:
        self.entries: list[object] = []

    def record(self, entry: object) -> str:
        self.entries.append(entry)
        return "0" * 64

    def latest_hash(self, tenant_id: str) -> str:
        return "0" * 64


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


def shop_store(orders: int = 400) -> InMemoryEventStore:
    """A's order ids reach B 95% of the time, one to three hours later."""
    store = InMemoryEventStore()
    events: list[CanonicalEvent] = []
    for n in range(orders):
        value = f"{n + 1:04d}"
        at = NOW - timedelta(days=4) + timedelta(minutes=9 * n)
        events.append(event(f"a{n}", "sys_a", "tpl_a", at, [("order_id", "raw", value, "9999")]))
        if n % 20 != 0:
            lag = timedelta(hours=1, minutes=(n * 13) % 120)
            events.append(
                event(
                    f"b{n}", "sys_b", "tpl_b", at + lag, [("order_ref", "digits.0", value, "9999")]
                )
            )
    store.add_events(events, stored_at=NOW)
    return store


def sketch_repository(store: InMemoryEventStore) -> InMemorySketchRepository:
    """Day sketches the way the profile pass writes them (package B's builders)."""
    builders: dict[tuple[FieldForm, datetime], DaySketchBuilder] = {}
    grouped: dict[tuple[FieldForm, object], list[object]] = defaultdict(list)
    window = profile_window(NOW, PROFILER)
    for row in store.identifier_rows(TENANT, window):
        end = FieldForm(row.field_ref, row.form)
        day = row.observed_at.astimezone(UTC).date()
        key = (end, day)
        builder = builders.get(key)  # type: ignore[arg-type]
        if builder is None:
            builder = builders[key] = DaySketchBuilder(end, day, PROFILER)  # type: ignore[index]
        builder.add(row)
        grouped[key].append(row)
    repository = InMemorySketchRepository()
    repository.put_days(TENANT, [builder.build() for builder in builders.values()])
    return repository


def fake_engine() -> sqlalchemy.Engine:
    """An engine object that never connects; the repositories only keep a reference."""
    return sqlalchemy.create_engine("postgresql+psycopg://carto:x@127.0.0.1:1/carto")


def context(settings: CoreSettings | None = None) -> WorkerContext:
    metadata = InMemoryMetadataStore()
    metadata.replace_inventory(
        EdgeInventory(
            schema_version="1",
            tenant_id=TENANT,
            edge_id="edge",
            sent_at=NOW,
            sources_file_sha256="c" * 64,
            systems=[InventorySystem(id="sys_a", name="Order system")],
            sources=[],
        ),
        NOW,
    )
    return WorkerContext(
        settings=settings if settings is not None else CoreSettings(_env_file=None),
        tenant_id=TENANT,
        events=InMemoryEventStore(),
        metadata=metadata,
        engine=fake_engine(),
        clock=lambda: NOW,
    )


# --- seams ---------------------------------------------------------------------------------------


def test_profiler_seams_come_from_the_profiler_package() -> None:
    seams = profiler_seams(PROFILER)
    assert seams.source == PROFILER_SOURCE
    assert seams.domain_size("AA-9999") == 26.0**2 * 10.0**4
    assert seams.decode_minhash is not default_minhash_decoder
    assert seams.containment is not default_containment
    assert seams.containment.keywords == {"k": PROFILER.bottom_k}  # type: ignore[attr-defined]


def test_profiler_seams_fall_back_to_the_linker_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "carto_core.profiler.sketches", None)
    seams = profiler_seams(PROFILER)
    assert seams.source == LINKER_DEFAULTS
    assert seams.containment is default_containment
    assert seams.domain_size is default_domain_size
    assert seams.decode_minhash is default_minhash_decoder


# --- profile sources -----------------------------------------------------------------------------


def test_sketch_profile_source_feeds_a_pass_that_proposes_the_exact_link() -> None:
    store = shop_store()
    source = SketchProfileSource(sketch_repository(store), store, PROFILER, clock=lambda: NOW)
    assert isinstance(source, ProfileSource)
    profiles = {profile.end: profile for profile in source.profiles(TENANT)}
    assert set(profiles) == {ORDER_ID, ORDER_REF}
    assert all(profile.is_candidate for profile in profiles.values()), "q and distinct pass"
    assert profiles[ORDER_ID].count == 400 and profiles[ORDER_ID].top_shape == "9999"
    assert 370 <= profiles[ORDER_REF].distinct <= 410, "HyperLogLog estimate"
    sketch = source.window_sketch(TENANT, ORDER_ID)
    assert sketch is not None and sketch.count == 400 and len(sketch.bottom_k) == 400
    assert len(sketch.days) >= 1
    assert source.window_sketch(TENANT, FieldForm("sys_z/tpl/none", "raw")) is None
    fresh = SketchProfileSource(sketch_repository(store), store, PROFILER, clock=lambda: NOW)
    on_demand = fresh.window_sketch(TENANT, ORDER_REF)
    assert on_demand is not None and on_demand.count == 380, "read without a profiles() call"

    links, entities, recorder = InMemoryLinkRepository(), InMemoryEntityRepository(), Recorder()
    seams = profiler_seams(PROFILER)
    link_pass = LinkPass(
        store=store,
        profiles=source,
        links=links,
        entities=entities,
        audit=recorder,
        settings=CoreSettings(_env_file=None).linker,
        profiler_settings=PROFILER,
        clock=lambda: NOW,
        containment=seams.containment,
        domain_size=seams.domain_size,
        decode_minhash=seams.decode_minhash,
        system_names={"sys_a": "Order system", "sys_b": "Warehouse"},
    )
    report = link_pass.run(TENANT, full=True, now=NOW)
    assert isinstance(report, LinkReport)
    assert report.new_sketches is False
    assert report.candidate_ends == 2 and report.proposed == 1
    stored = links.get(TENANT, link_id_for(TENANT, ORDER_ID, ORDER_REF))
    assert stored is not None
    assert stored.link_type == LinkType.EXACT and stored.status == LinkStatus.PROPOSED
    assert stored.features.c_ab == pytest.approx(0.95)
    assert stored.extra["sentence"].startswith("95% of Order system orders")
    assert "t1." not in str(stored.extra)


def test_metadata_profile_source_reads_the_fields_table_and_has_no_sketches() -> None:
    metadata = InMemoryMetadataStore()
    metadata.upsert_fields(
        TENANT,
        [
            FieldRecord(
                field_ref=ORDER_REF.field_ref,
                system_id="sys_b",
                template_id="tpl_b",
                path="order_ref",
                field_class="identifier",
                policy="tokenize",
                pinned=False,
                forms=("raw", "digits.0"),
                count=380,
                shapes=(("9999", 1.0),),
                first_seen=NOW,
                last_seen=NOW,
                distinct_estimate=380,
                quality=0.8,
                is_candidate=True,
            )
        ],
    )
    source = MetadataProfileSource(metadata)
    profiles = source.profiles(TENANT)
    assert [p.end for p in profiles] == [FieldForm(ORDER_REF.field_ref, "raw"), ORDER_REF]
    assert profiles[1].is_candidate and profiles[1].quality == 0.8
    assert profiles[1].top_shape == "9999"
    assert source.window_sketch(TENANT, ORDER_REF) is None


def test_profile_source_for_picks_sketches_or_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(profile_source_for(context()), SketchProfileSource)
    monkeypatch.setitem(sys.modules, "carto_core.profiler.repository", None)
    assert isinstance(profile_source_for(context()), MetadataProfileSource)


# --- from_context --------------------------------------------------------------------------------


def test_from_context_composes_the_worker_pass() -> None:
    link_pass = LinkPass.from_context(context())
    assert isinstance(link_pass, LinkPass)
    assert isinstance(link_pass._profiles, SketchProfileSource)
    assert link_pass._system_names == {"sys_a": "Order system"}
    assert link_pass._composite_hook is not None
    assert link_pass._domain_size("9999") == 10.0**4
    assert type(link_pass._links).__name__ == "PostgresLinkRepository"
    assert type(link_pass._review._audit).__name__ == "PostgresAuditLog"
    assert link_pass._families._namer == link_pass._default_namer


def test_from_context_keeps_the_dictionary_namer_when_the_provider_is_not_built() -> None:
    settings = CoreSettings(
        _env_file=None,
        llm=LlmSettings(enabled=True, provider="bedrock", endpoint="https://llm.example"),
    )
    link_pass = LinkPass.from_context(context(settings))
    assert link_pass._families._namer == link_pass._default_namer


def test_the_worker_now_has_a_link_handler() -> None:
    handlers = default_handler_factory(context())
    assert JobKind.LINK in handlers
    assert JobKind.PROFILE in handlers


# --- the gateway namer ---------------------------------------------------------------------------


def _reader() -> InMemoryMetadataReader:
    reader = InMemoryMetadataReader()
    reader.add(ReaderField(TENANT, ORDER_ID.field_ref, "order_id", "Order system", ("9999",)))
    reader.add(ReaderField(TENANT, ORDER_REF.field_ref, "order_ref", "Warehouse", ("9999",)))
    return reader


def _gateway(
    provider: FakeProvider, *, enabled: bool = True
) -> tuple[LlmGateway, InMemoryCallAuditSink]:
    sink = InMemoryCallAuditSink()
    settings = LlmSettings(enabled=enabled, provider="openai", endpoint="https://llm.example")
    return LlmGateway(settings, provider, sink, lambda: NOW), sink


def test_gateway_namer_uses_the_suggestion_and_audits_the_call() -> None:
    provider = FakeProvider()
    gateway, sink = _gateway(provider)
    namer = GatewayNamer(gateway, _reader(), TENANT, fallback=lambda _m: "Dictionary")
    assert namer([ORDER_ID, ORDER_REF]) == "Order number"
    assert len(sink.records) == 1 and sink.records[0].status.value == "ok"
    payload = provider.calls[0].payload_json
    assert "order_id" in payload and "Order system" in payload and "t1." not in payload
    assert ORDER_ID.field_ref in payload, "the family in hand, not entity_fields rows"


@pytest.mark.parametrize(
    "behaviour", [FakeBehaviour.ERROR, FakeBehaviour.INVALID_JSON, FakeBehaviour.UNKNOWN_TARGET]
)
def test_gateway_namer_falls_back_on_provider_errors_and_bad_output(
    behaviour: FakeBehaviour,
) -> None:
    gateway, _sink = _gateway(FakeProvider(behaviour))
    namer = GatewayNamer(gateway, _reader(), TENANT, fallback=lambda _m: "Dictionary")
    assert namer([ORDER_ID, ORDER_REF]) == "Dictionary"


def test_gateway_namer_falls_back_when_disabled_or_without_metadata() -> None:
    disabled, sink = _gateway(FakeProvider(), enabled=False)
    namer = GatewayNamer(disabled, _reader(), TENANT, fallback=lambda _m: "Dictionary")
    assert namer([ORDER_ID]) == "Dictionary" and sink.records == ()
    enabled, _sink = _gateway(FakeProvider())
    empty = GatewayNamer(
        enabled, InMemoryMetadataReader(), TENANT, fallback=lambda _m: "Dictionary"
    )
    assert empty([ORDER_ID]) == "Dictionary", "no member in the fields table yet"
    without = GatewayNamer(_gateway(FakeProvider(FakeBehaviour.ERROR))[0], _reader(), TENANT)
    assert without([ORDER_ID]) is None


def test_the_family_builder_takes_the_gateway_namer() -> None:
    links, entities = InMemoryLinkRepository(), InMemoryEntityRepository()
    gateway, _sink = _gateway(FakeProvider())
    builder = FamilyBuilder(links, entities, namer=GatewayNamer(gateway, _reader(), TENANT))
    assert builder.rebuild(TENANT, now=NOW).families == 0
