"""carto_edge.pipeline.pipeline: parse, classify, redact and tokenize end to end (spec 5.4
steps 2 to 6, 7.1, 18.1): deterministic event ids, no raw value for a tokenized or dropped field,
actor handling, drop reasons, the plausible observed_at range (ADR 0036), the two-pass analyzer
mode and counters."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from carto_common.crypto import Keyring, TokenKey, generate_key
from carto_common.ids import derive_ulid, is_ulid
from carto_edge.config import (
    ClassifySettings,
    FieldPolicyPin,
    ParseConfig,
    RecordFormat,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import RawRecord
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.pipeline import (
    OUT_OF_RANGE_FUTURE,
    REASON_CONTRACT,
    REASON_TOO_OLD,
    REASON_UNKNOWN_SOURCE,
    EdgePipeline,
    actor_kind,
)
from carto_edge.pipeline.stats import FieldStatsStore
from carto_edge.pipeline.templates import TemplateStore
from carto_edge.pipeline.tokenize import Tokenizer
from carto_schema.event import ActorKind, EventKind, ObservedAtQuality, Severity

RECEIVED = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
KEYRING = Keyring(active=TokenKey(1, generate_key()))


def sources_file(
    pins: list[FieldPolicyPin] | None = None, actor_field: str | None = None
) -> SourcesFile:
    return SourcesFile(
        systems=[
            SystemConfig(id="sys_webstore", name="Webstore"),
            SystemConfig(id="sys_warehouse", name="Warehouse"),
        ],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_webstore",
                type=SourceType.UPLOAD,
                config={"paths": ["x.ndjson"]},
                parse=ParseConfig(actor_field=actor_field),
            ),
            SourceConfig(
                id="src_wms",
                system="sys_warehouse",
                type=SourceType.UPLOAD,
                config={"paths": ["po.csv"], "kind": "rows", "table": "purchase_orders"},
                parse=ParseConfig(actor_field="created_by", timestamp_field="updated_at"),
            ),
        ],
        field_policies=pins or [],
    )


def make(
    *,
    quarantine: int = 200,
    pins: list[FieldPolicyPin] | None = None,
    actor_field: str | None = None,
    observe_on_process: bool = True,
    sources: SourcesFile | None = None,
) -> EdgePipeline:
    sources = sources or sources_file(pins, actor_field)
    classify = ClassifySettings(quarantine_samples=quarantine, distinct_threshold=10)
    stats = FieldStatsStore(sample_size=64)
    detector = RegexDetector()
    classifier = Classifier(
        classify, sources.field_policies, detector, stats, policy_version=sources.policy_version
    )
    return EdgePipeline(
        sources=sources,
        templates=TemplateStore(),
        classifier=classifier,
        detector=detector,
        tokenizer=Tokenizer(KEYRING, "default"),
        tenant_id="default",
        retention_days=30,
        clock=lambda: RECEIVED,
        observe_on_process=observe_on_process,
    )


def raw_line(text: str, line: int = 1, source_id: str = "src_web") -> RawRecord:
    return RawRecord(
        source_id=source_id,
        system_id="sys_webstore",
        kind=EventKind.LOG,
        locator=f"app.ndjson:line:{line}",
        received_at=RECEIVED,
        text=text,
        sequence=line,
    )


def raw_row(fields: dict[str, Any], pk: str) -> RawRecord:
    return RawRecord(
        source_id="src_wms",
        system_id="sys_warehouse",
        kind=EventKind.ROW_CHANGE,
        locator=f"purchase_orders:row:{pk}",
        received_at=RECEIVED,
        fields=fields,
        template_hint="row_change purchase_orders",
    )


def web_line(i: int, **extra: Any) -> str:
    data = {
        "ts": f"2026-09-23T04:{i % 60:02d}:{(i * 7) % 60:02d}.000Z",
        "level": "info",
        "msg": "cart created",
        "cart_id": f"c-{88000 + i}",
        "items": i % 5 + 1,
        "region": ("us-east", "us-west", "eu-west")[i % 3],
    }
    data.update(extra)
    return json.dumps(data)


def test_event_id_is_derived_from_source_position() -> None:
    pipeline = make()
    line = web_line(1)
    first = pipeline.process(raw_line(line))
    second = make().process(raw_line(line))
    assert first.event is not None and second.event is not None
    assert first.event.event_id == second.event.event_id
    observed_ms = int(first.event.observed_at.timestamp() * 1000)
    assert first.event.event_id == derive_ulid(observed_ms, "src_web", "app.ndjson:line:1")
    assert is_ulid(first.event.event_id)
    # The same record read again (at-least-once delivery) gets the same id.
    again = pipeline.process(raw_line(line))
    assert again.event is not None and again.event.event_id == first.event.event_id


def test_streaming_mode_quarantines_and_tokenizes_identifier_shaped_values() -> None:
    pipeline = make()
    result = pipeline.process(raw_line(web_line(1)))
    event = result.event
    assert event is not None
    text = event.model_dump_json()
    assert "c-88001" not in text
    # First sighting of the names (ADR 0030): cart_id, items and region travel as "*" (the
    # first value is kept); ts, level and msg are configured names, trusted from the start.
    assert "cart_id" not in text
    assert "*" in {identifier.field for identifier in event.identifiers}
    assert event.kind is EventKind.LOG
    assert event.observed_at_quality is ObservedAtQuality.SOURCE
    assert event.ingested_at == RECEIVED
    # One sighting: the message's words are parameters until its cluster has three members.
    assert event.template_text == "<*> <*>"
    assert event.tenant_id == "default"
    assert event.redaction.policy_version
    assert event.actor is None
    # Vault entries exist only for raw-form tokens of the event.
    raw_tokens = {i.token for i in event.identifiers if i.form == "raw"}
    assert {entry.token for entry in result.vault_entries} == raw_tokens
    assert all(entry.expires_at > event.observed_at for entry in result.vault_entries)
    assert pipeline.counters.events == 1 and pipeline.counters.records == 1


def test_complete_statistics_keep_low_cardinality_and_tokenize_identifiers() -> None:
    pipeline = make(quarantine=1, observe_on_process=False)
    for i in range(60):
        assert pipeline.observe_only(raw_line(web_line(i), line=i + 1))
    assert pipeline.counters.observed == 60
    result = pipeline.process(raw_line(web_line(3), line=4))
    event = result.event
    assert event is not None
    assert event.attributes["region"] == "us-east"
    assert event.severity is Severity.INFO  # the parser consumed the level field
    assert "cart_id" not in event.attributes
    fields = {identifier.field for identifier in event.identifiers}
    assert "cart_id" in fields
    forms = {identifier.form for identifier in event.identifiers if identifier.field == "cart_id"}
    # "c-88003" is already lower case, so its norm form equals raw and is not repeated (spec 8.4).
    assert forms == {"raw", "alnum", "digits.0"}
    assert "c-88003" not in event.model_dump_json()
    assert event.identifiers[0].shape == "A-99999"


def test_actor_field_is_tokenized_never_kept() -> None:
    pipeline = make(quarantine=1, observe_on_process=False)
    rows = [
        raw_row(
            {
                "id": str(i),
                "po_num": f"88-{200 + i}",
                "status": "CREATED",
                "created_by": "svc_wms_integration" if i % 2 else "jsmith",
                "updated_at": "2026-09-23 00:18:52",
            },
            pk=str(i),
        )
        for i in range(40)
    ]
    for row in rows:
        pipeline.observe_only(row)
    service = pipeline.process(rows[1]).event
    human = pipeline.process(rows[2]).event
    assert service is not None and human is not None
    assert service.actor is not None and service.actor.kind is ActorKind.SERVICE
    assert human.actor is not None and human.actor.kind is ActorKind.HUMAN
    assert service.actor.token != human.actor.token
    for event in (service, human):
        text = event.model_dump_json()
        assert "created_by" not in event.attributes
        assert "jsmith" not in text and "svc_wms_integration" not in text
        assert event.attributes["status"] == "CREATED"
        assert event.kind is EventKind.ROW_CHANGE
        assert event.template_text == "row_change purchase_orders"
        assert event.observed_at_quality is ObservedAtQuality.SOURCE


def test_row_connector_overrides_name_the_actor_and_the_clock() -> None:
    """A row connector's actor_column and timestamp_column (spec Appendix A) win over the
    source's parse hints: the actor is tokenized into ``actor`` even when the source has no
    actor hint, never kept as a low-cardinality attribute (spec 7.1, 2.3 invariant 2)."""
    pipeline = make(quarantine=1, observe_on_process=False)
    rows = [
        RawRecord(
            source_id="src_web",  # a source with no actor and no timestamp hint
            system_id="sys_webstore",
            kind=EventKind.ROW_CHANGE,
            locator=f"purchase_orders:row:{i}",
            received_at=RECEIVED,
            fields={
                "id": str(i),
                "status": "CREATED",
                "entered_by": ("jsmith", "mlopez")[i % 2],
                "changed": "2026-09-23T04:05:06Z",
            },
            template_hint="row_change purchase_orders",
            timestamp_field="changed",
            actor_field="entered_by",
        )
        for i in range(30)
    ]
    for row in rows:
        assert pipeline.observe_only(row)
    event = pipeline.process(rows[2]).event
    assert event is not None
    assert event.actor is not None and event.actor.kind is ActorKind.HUMAN
    assert "entered_by" not in event.attributes
    assert "entered_by" not in {identifier.field for identifier in event.identifiers}
    assert "jsmith" not in event.model_dump_json()
    assert event.observed_at == datetime(2026, 9, 23, 4, 5, 6, tzinfo=UTC)
    assert event.observed_at_quality is ObservedAtQuality.SOURCE
    assert event.attributes["status"] == "CREATED"


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("svc_wms_integration", ActorKind.SERVICE),
        ("api-gateway", ActorKind.SERVICE),
        ("order_bot", ActorKind.SERVICE),
        ("SYSTEM", ActorKind.SERVICE),
        ("cron.daemon", ActorKind.SERVICE),
        ("jsmith", ActorKind.HUMAN),
        ("Maria Lopez", ActorKind.HUMAN),
        ("   ", ActorKind.UNKNOWN),
    ],
)
def test_actor_kind(value: str, kind: ActorKind) -> None:
    assert actor_kind(value) is kind


def test_dropped_fields_are_named_not_valued() -> None:
    pins = [
        FieldPolicyPin(
            field="sys_webstore/*/customer_name", field_class="person_name", policy="drop"
        )
    ]
    pipeline = make(quarantine=1, pins=pins, observe_on_process=False)
    for i in range(30):
        pipeline.observe_only(raw_line(web_line(i, customer_name="Mei Reyes"), line=i + 1))
    event = pipeline.process(raw_line(web_line(2, customer_name="Mei Reyes"), line=3)).event
    assert event is not None
    assert "customer_name" in event.dropped_fields
    assert "Mei Reyes" not in event.model_dump_json()


def test_pinned_keep_of_a_file_name_is_still_hygiene_checked() -> None:
    pins = [
        FieldPolicyPin(field="sys_webstore/*/note", field_class="low_card_attribute", policy="keep")
    ]
    pipeline = make(quarantine=1, pins=pins)
    event = pipeline.process(raw_line(web_line(1, note="contact me at mei@example.com"))).event
    assert event is not None
    assert "note" not in event.attributes
    assert "note" in event.dropped_fields
    assert "example.com" not in event.model_dump_json()
    assert pipeline.counters.attributes_refused == 1


def test_drop_reasons_are_counted() -> None:
    pipeline = make()
    unknown = pipeline.process(
        RawRecord(
            source_id="src_nope",
            system_id="sys_webstore",
            kind=EventKind.LOG,
            locator="x:line:1",
            received_at=RECEIVED,
            text="hello",
        )
    )
    assert unknown.event is None and unknown.dropped_reason == REASON_UNKNOWN_SOURCE
    empty = pipeline.process(raw_line("   "))
    assert empty.event is None and empty.dropped_reason == "empty"
    tiny = pipeline.process(raw_line("x"))
    assert tiny.event is not None  # a one-byte line is a text record, not a failure
    assert pipeline.counters.dropped[REASON_UNKNOWN_SOURCE] == 1
    assert pipeline.counters.dropped["empty"] == 1
    assert pipeline.counters.dropped_total == 2
    assert pipeline.counters.records == 3


def test_contract_violation_is_dropped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = make()

    def bad_ulid(*_args: object, **_kwargs: object) -> str:
        return "not-a-ulid"

    monkeypatch.setattr("carto_edge.pipeline.pipeline.derive_ulid", bad_ulid)
    result = pipeline.process(raw_line(web_line(1)))
    assert result.event is None
    assert result.dropped_reason == REASON_CONTRACT
    assert pipeline.counters.dropped[REASON_CONTRACT] == 1


def test_template_text_is_redacted_and_counted() -> None:
    """A mined template masks digits and e-mail tokens before Drain3 sees them; a connector's
    template hint is the path where a constant could still carry PII (spec 8.3)."""
    pipeline = make()
    hinted = RawRecord(
        source_id="src_wms",
        system_id="sys_warehouse",
        kind=EventKind.ROW_CHANGE,
        locator="purchase_orders:row:7",
        received_at=RECEIVED,
        fields={"id": "7", "status": "NEW", "updated_at": "2026-09-23 00:18:52"},
        template_hint="row_change owner support@example.com",
    )
    event = pipeline.process(hinted).event
    assert event is not None
    assert "example.com" not in event.template_text
    assert "<EMAIL_ADDRESS>" in event.template_text
    assert event.redaction.entities_masked == 1
    mined = pipeline.process(
        raw_line(json.dumps({"ts": "2026-09-23T04:00:00Z", "msg": "notify x@example.com now"}))
    ).event
    assert mined is not None
    assert "example.com" not in mined.model_dump_json()


def test_timestamp_falls_back_to_ingest_time() -> None:
    pipeline = make()
    event = pipeline.process(raw_line(json.dumps({"msg": "no clock here", "k": "v"}))).event
    assert event is not None
    assert event.observed_at_quality is ObservedAtQuality.INGEST
    assert event.observed_at == RECEIVED


identifier_values = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="-_"),
    min_size=6,
    max_size=24,
).filter(lambda s: any(c.isdigit() for c in s) and s[0].isalnum() and s[-1].isalnum())


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    values=st.lists(identifier_values, min_size=1, max_size=6, unique=True),
    names=st.lists(
        st.from_regex(r"[a-z][a-z0-9_]{1,10}_id", fullmatch=True), min_size=1, max_size=6
    ),
)
def test_property_no_raw_value_of_tokenized_or_dropped_field_in_event(
    values: list[str], names: list[str]
) -> None:
    """Spec 18.1: the pipeline never emits a raw value for a tokenized or dropped field."""
    pipeline = make()
    fields = dict(zip(names, values, strict=False))
    payload = {"ts": "2026-09-23T04:00:00Z", "msg": "order placed", **fields}
    event = pipeline.process(raw_line(json.dumps(payload))).event
    assert event is not None
    text = event.model_dump_json()
    kept = set(event.attributes)
    for name, value in fields.items():
        if name in kept:
            continue  # a kept value is low-cardinality and detector-clean by construction
        assert value not in text, name
        assert value.lower() not in text.lower(), name


@settings(max_examples=40, deadline=None)
@given(
    locator=st.from_regex(r"[a-z]{1,8}\.log:line:[1-9][0-9]{0,5}", fullmatch=True),
    ms=st.integers(min_value=0, max_value=2**48 - 1),
)
def test_property_derive_ulid_is_idempotent(locator: str, ms: int) -> None:
    first = derive_ulid(ms, "src_web", locator)
    assert first == derive_ulid(ms, "src_web", locator)
    assert is_ulid(first)
    assert first != derive_ulid(ms, "src_other", locator)


# ---------------------------------------------------------------------------------------------
# Review findings: values must not leave through names, one-off messages or rare kept values
# ---------------------------------------------------------------------------------------------


def _event_text(pipeline: EdgePipeline, payload: dict[str, Any], line: int = 1) -> str:
    event = pipeline.process(raw_line(json.dumps(payload), line=line)).event
    assert event is not None
    return event.model_dump_json()


def test_json_map_keys_that_are_values_are_masked_in_every_name() -> None:
    pipeline = make()
    text = _event_text(
        pipeline,
        {
            "ts": "2026-09-23T04:00:00Z",
            "msg": "stock levels",
            "stock": {"SKU-88231": 0, "SKU-55012": 2},
            "c-88213": {"status": "open"},
            "mei@example.com": "x",
        },
    )
    for value in ("88231", "55012", "c-88213", "mei@example.com"):
        assert value not in text, value


def test_a_csv_data_row_is_never_taken_for_a_header() -> None:
    sources = SourcesFile(
        systems=[SystemConfig(id="sys_webstore", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_webstore",
                type=SourceType.UPLOAD,
                config={"paths": ["x.csv"]},
                parse=ParseConfig(format=RecordFormat.CSV),
            )
        ],
    )
    pipeline = make(sources=sources)
    row = "4471,c-88213,MKALICE Smith,mkalice@example.com,PENDING"
    result = pipeline.process(raw_line(row, line=1))
    assert result.event is not None  # the row is data, with positional names
    text = result.event.model_dump_json()
    for value in ("4471", "c-88213", "mkalice", "MKALICE"):
        assert value not in text, value


def test_logfmt_bare_words_never_become_field_names() -> None:
    sources = SourcesFile(
        systems=[SystemConfig(id="sys_webstore", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_webstore",
                type=SourceType.UPLOAD,
                config={"paths": ["x.log"]},
                parse=ParseConfig(format=RecordFormat.LOGFMT),
            )
        ],
    )
    pipeline = make(sources=sources)
    line = "ts=2026-09-23T04:00:00Z msg=refund approved for MKBOB Higgins order_id=4471"
    result = pipeline.process(raw_line(line))
    assert result.event is None
    assert result.dropped_reason == "unparseable"


def test_a_one_off_message_keeps_no_word_in_its_template() -> None:
    pipeline = make()
    text = _event_text(
        pipeline,
        {"ts": "2026-09-23T04:00:00Z", "msg": "leave behind the blue gate codeword MKPINEAPPLE"},
    )
    assert "pineapple" not in text.lower()
    assert "blue gate" not in text


def test_rare_values_of_a_kept_field_stay_at_the_edge() -> None:
    """A low-cardinality field is kept, but a value seen fewer than three times stays at the
    edge, even when its shape is common ("Bob" has the shape of "yes")."""
    pipeline = make(quarantine=1, observe_on_process=False)
    rare = {30: "customer asked to call back MKTAIL", 60: "Bob", 90: "Bob"}
    records = [
        raw_line(
            json.dumps(
                {
                    "ts": "2026-09-23T04:00:00Z",
                    "msg": "callback",
                    "detail": rare.get(i, "yes" if i % 2 else "no"),
                }
            ),
            line=i + 1,
        )
        for i in range(300)
    ]
    for record in records:
        assert pipeline.observe_only(record)
    common = pipeline.process(records[1]).event
    assert common is not None
    assert common.attributes.get("detail") == "yes"
    for index in rare:
        event = pipeline.process(records[index]).event
        assert event is not None
        assert "detail" not in event.attributes
        text = event.model_dump_json()
        assert "MKTAIL" not in text
        assert "Bob" not in text


# --- the plausible observed_at range (ADR 0036) ------------------------------------------------


def _at(ts: str, line: int = 1) -> RawRecord:
    return raw_line(json.dumps({"ts": ts, "level": "info", "msg": "cart created"}), line)


def test_a_record_older_than_retention_plus_a_day_is_dropped_and_counted() -> None:
    pipeline = make()  # clock RECEIVED (2026-10-08 12:00), retention 30 days
    edge = pipeline.process(_at("2026-09-07T12:00:00.000Z"))  # the lower bound itself
    assert edge.event is not None and edge.out_of_range is None
    old = pipeline.process(_at("2026-09-07T11:59:59.999Z", 2))
    assert old.event is None
    assert old.dropped_reason == REASON_TOO_OLD
    assert old.out_of_range == REASON_TOO_OLD
    assert pipeline.counters.out_of_range == {REASON_TOO_OLD: 1}
    assert pipeline.counters.dropped[REASON_TOO_OLD] == 1
    assert pipeline.counters.snapshot()["out_of_range"] == {REASON_TOO_OLD: 1}


@pytest.mark.parametrize("ts", ["2026-10-09T12:00:00.001Z", "2099-01-01T00:00:00.000Z"])
def test_a_future_time_becomes_the_ingest_time_and_the_event_is_kept(ts: str) -> None:
    pipeline = make()
    result = pipeline.process(_at(ts))
    event = result.event
    assert event is not None
    assert result.out_of_range == OUT_OF_RANGE_FUTURE
    assert event.observed_at == RECEIVED
    assert event.observed_at_quality is ObservedAtQuality.INGEST
    observed_ms = int(RECEIVED.timestamp() * 1000)
    assert event.event_id == derive_ulid(observed_ms, "src_web", "app.ndjson:line:1")
    assert pipeline.counters.out_of_range == {OUT_OF_RANGE_FUTURE: 1}
    assert pipeline.counters.dropped_total == 0


def test_the_upper_bound_itself_is_in_range() -> None:
    result = make().process(_at("2026-10-09T12:00:00.000Z"))
    assert result.event is not None and result.out_of_range is None
    assert result.event.observed_at_quality is ObservedAtQuality.SOURCE


def test_the_range_reads_the_pipelines_clock_at_each_record() -> None:
    now = [RECEIVED]
    pipeline = EdgePipeline(
        sources=sources_file(),
        templates=TemplateStore(),
        classifier=Classifier(
            ClassifySettings(), [], RegexDetector(), FieldStatsStore(sample_size=64)
        ),
        detector=RegexDetector(),
        tokenizer=Tokenizer(KEYRING, "default"),
        tenant_id="default",
        retention_days=7,
        clock=lambda: now[0],
    )
    assert pipeline.process(_at("2026-10-01T00:00:00.000Z")).event is not None
    now[0] = datetime(2026, 10, 20, tzinfo=UTC)
    assert pipeline.process(_at("2026-10-01T00:00:00.000Z", 2)).out_of_range == REASON_TOO_OLD
