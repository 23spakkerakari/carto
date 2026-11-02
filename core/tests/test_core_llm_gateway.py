"""The gateway's controls (spec 9.9, 2.3 invariant 5; ADR 0054): disabled by default, payload
model enforced, retries on transient errors only, strict output parsing with invalid output
discarded and counted, one audit row per provider call, and logs that carry task, status and
latency only."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy
import structlog.testing

from carto_common.settings import LlmSettings
from carto_core.llm_gateway.audit import (
    AuditSinkError,
    CallAuditSink,
    CallRecord,
    CallStatus,
    InMemoryCallAuditSink,
    PostgresCallAuditSink,
)
from carto_core.llm_gateway.gateway import (
    MAX_OUTPUT_CHARS,
    DailyCapExceededError,
    GatewayDisabledError,
    GatewayMetrics,
    InvalidOutputError,
    LlmGateway,
    PayloadRefusedError,
    parse_suggestions,
)
from carto_core.llm_gateway.prompts import FENCE_BEGIN, FENCE_END, PROMPT_VERSION
from carto_core.llm_gateway.providers import TransientProviderError
from carto_core.llm_gateway.providers.fake import FakeBehaviour, FakeProvider
from carto_core.llm_gateway.schemas import (
    FieldSummary,
    LabelFieldRequest,
    LabelType,
    NameEntityRequest,
    NameNodeRequest,
    ProviderError,
    Suggestion,
    SuggestionList,
    TaskKind,
    rationale_is_clean,
)

T0 = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
TENANT = "default"
TOKEN = "t1.Zm9vYmFyYmF6cXV4MTIzND"  # noqa: S105 - token-shaped, not a secret


def _settings(
    tmp_path: Path, *, enabled: bool = True, max_calls_per_day: int = 2000
) -> LlmSettings:
    key = tmp_path / "key"
    key.write_text("sk-test")
    return LlmSettings(
        enabled=enabled,
        provider="anthropic",
        endpoint="https://llm.example",
        model="model-x",
        api_key_file=key,
        max_calls_per_day=max_calls_per_day,
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = T0
        self.ticks = 0.0

    def __call__(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        self.ticks += 0.125
        return self.ticks


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        provider: FakeProvider,
        *,
        enabled: bool = True,
        max_calls_per_day: int = 2000,
    ) -> None:
        self.provider = provider
        self.sink = InMemoryCallAuditSink()
        self.clock = FakeClock()
        self.sleeps: list[float] = []
        self.gateway = LlmGateway(
            _settings(tmp_path, enabled=enabled, max_calls_per_day=max_calls_per_day),
            provider,
            self.sink,
            self.clock,
            monotonic=self.clock.monotonic,
            sleep=self.sleeps.append,
        )

    def metric(self, name: str) -> int:
        return self.gateway.metrics.get(f"carto_llm_{name}_total")


def _field(field_id: str) -> FieldSummary:
    return FieldSummary(
        field_id=field_id,
        path=f"{field_id}.ref",
        system_name="Order system",
        top_shapes=["AAA-9999"],
        template_text="Created order <*>",
    )


def _label_request(*ids: str) -> LabelFieldRequest:
    return LabelFieldRequest(fields=[_field(i) for i in (ids or ("f1", "f2"))])


def _answer(*pairs: tuple[str, str], extra: dict[str, object] | None = None) -> str:
    suggestions: list[dict[str, object]] = [
        {"target_id": t, "label_type": "order", "label": label, "confidence": 0.7}
        for t, label in pairs
    ]
    for suggestion in suggestions:
        suggestion.update(extra or {})
    return json.dumps({"suggestions": suggestions})


# --- off by default, payload model ----------------------------------------------------------


def test_disabled_raises_before_any_call(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider(), enabled=False)
    assert harness.gateway.enabled is False
    with pytest.raises(GatewayDisabledError):
        harness.gateway.suggest_labels(TENANT, _label_request())
    assert harness.provider.calls == []
    assert harness.sink.records == ()
    assert LlmSettings().enabled is False, "off by default (ADR 0004)"


def test_the_wrong_model_is_refused_counted_and_audited(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider())
    entity = NameEntityRequest(entity_id="e1", fields=[_field("f1")])
    with pytest.raises(PayloadRefusedError, match="takes a LabelFieldRequest"):
        harness.gateway.suggest_labels(TENANT, entity)  # type: ignore[arg-type]
    with pytest.raises(PayloadRefusedError):
        harness.gateway.suggest_entity_name(TENANT, {"entity_id": "e1"})  # type: ignore[arg-type]
    with pytest.raises(PayloadRefusedError):
        harness.gateway.suggest_node_name(TENANT, _label_request())  # type: ignore[arg-type]
    assert harness.provider.calls == []
    assert harness.metric("refused") == 3
    assert [r.status for r in harness.sink.records] == [CallStatus.REFUSED] * 3
    assert harness.sink.records[0].error_class == "PayloadRefusedError"
    assert harness.sink.records[0].payload_sha256 == ""


def test_a_subclass_of_the_model_is_refused(tmp_path: Path) -> None:
    class Loose(LabelFieldRequest):
        model_config = {"extra": "allow"}

    harness = Harness(tmp_path, FakeProvider())
    with pytest.raises(PayloadRefusedError):
        harness.gateway.suggest_labels(TENANT, Loose(fields=[_field("f1")]))


# --- the happy path, prompts, audit ----------------------------------------------------------


def test_suggestions_come_back_with_an_audit_row(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider())
    request = _label_request("f1", "f2")
    result = harness.gateway.suggest_labels(TENANT, request)
    assert isinstance(result, SuggestionList)
    assert [s.target_id for s in result.suggestions] == ["f1", "f2"]
    assert all(s.label_type is LabelType.ORDER for s in result.suggestions)

    (record,) = harness.sink.records
    payload_json = request.model_dump_json()
    assert isinstance(record, CallRecord)
    assert record.tenant_id == TENANT
    assert record.at == T0
    assert record.task.value == "label_field"
    assert record.payload_sha256 == hashlib.sha256(payload_json.encode()).hexdigest()
    assert (record.provider, record.model) == ("fake", "fake-1")
    assert (record.input_tokens, record.output_tokens) == (120, 40)
    assert record.latency_ms == 125
    assert record.status is CallStatus.OK
    assert record.error_class is None
    assert len(record.id) == 26
    assert harness.metric("calls") == 1
    assert harness.metric("invalid_output") == 0


def test_the_prompt_fences_the_payload_and_states_the_contract(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider())
    request = _label_request("f1")
    harness.gateway.suggest_labels(TENANT, request)
    (call,) = harness.provider.calls
    lines = call.payload_json.splitlines()
    nonce = lines[0].split()[-2]
    assert len(nonce) == 16
    assert lines[0] == FENCE_BEGIN.format(nonce=nonce)
    assert lines[-1] == FENCE_END.format(nonce=nonce)
    assert "\n".join(lines[1:-1]) == request.model_dump_json()
    assert FENCE_BEGIN.format(nonce=nonce) in call.task_prompt
    assert "label_field" not in call.task_prompt or "Task:" in call.task_prompt
    assert '"label_type"' in call.task_prompt, "the JSON schema is in the prompt"
    assert "purchase_order" in call.task_prompt, "the label enumeration is in the prompt"
    assert PROMPT_VERSION in call.task_prompt
    assert PROMPT_VERSION == "2026-11-02.1", "bumped with the rationale rule"
    # The rationale rule the output validator enforces (rationale_is_clean) is stated to the
    # model: plain printable text, one line, no <>{} and no link, blanked when broken.
    assert "`rationale` is at most 200 characters of plain printable text" in call.task_prompt
    assert "no line breaks" in call.task_prompt and "no link or URL" in call.task_prompt
    assert "`<`, `>`, `{` or `}`" in call.task_prompt
    assert "is blanked" in call.task_prompt
    assert request.fields[0].template_text not in call.task_prompt
    assert call.output_schema["title"] == "SuggestionList"


def test_entity_and_node_tasks_work(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider())
    entity = harness.gateway.suggest_entity_name(
        TENANT, NameEntityRequest(entity_id="e1", fields=[_field("f1"), _field("f2")])
    )
    assert [s.target_id for s in entity.suggestions] == ["e1"]
    node = harness.gateway.suggest_node_name(
        TENANT, NameNodeRequest(node_id="n1", system_name="Warehouse", template_text="PO <*>")
    )
    assert [s.target_id for s in node.suggestions] == ["n1"]
    assert [r.task.value for r in harness.sink.records] == ["name_entity", "name_node"]


def test_a_provider_without_usage_gives_no_token_counts(tmp_path: Path) -> None:
    class Plain:
        name = "plain"

        def complete(
            self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
        ) -> str:
            return _answer(("f1", "Order number"))

    sink = InMemoryCallAuditSink()
    gateway = LlmGateway(_settings(tmp_path), Plain(), sink, lambda: T0)
    result = gateway.suggest_labels(TENANT, _label_request("f1"))
    assert len(result.suggestions) == 1
    (record,) = sink.records
    assert (record.input_tokens, record.output_tokens) == (None, None)
    assert record.model == "model-x", "falls back to llm.model"


# --- retries ---------------------------------------------------------------------------------


def test_transient_errors_are_retried_twice_and_each_attempt_is_audited(tmp_path: Path) -> None:
    script = [TransientProviderError("fake: http 5xx"), TransientProviderError("fake: timeout")]
    harness = Harness(tmp_path, FakeProvider(script=script))
    result = harness.gateway.suggest_labels(TENANT, _label_request("f1"))
    assert len(result.suggestions) == 1
    assert len(harness.provider.calls) == 3
    assert harness.sleeps == [0.5, 2.0]
    assert [r.status for r in harness.sink.records] == [
        CallStatus.PROVIDER_ERROR,
        CallStatus.PROVIDER_ERROR,
        CallStatus.OK,
    ]
    assert harness.sink.records[0].error_class == "TransientProviderError"
    assert harness.sink.records[0].input_tokens is None
    assert harness.metric("calls") == 3
    assert harness.metric("retries") == 2
    assert harness.metric("provider_errors") == 2


def test_three_transient_errors_raise(tmp_path: Path) -> None:
    script = [TransientProviderError("fake: http 5xx")] * 3
    harness = Harness(tmp_path, FakeProvider(script=script))
    with pytest.raises(TransientProviderError):
        harness.gateway.suggest_labels(TENANT, _label_request("f1"))
    assert len(harness.provider.calls) == 3
    assert harness.metric("provider_errors") == 3
    assert len(harness.sink.records) == 3


def test_a_non_transient_error_is_not_retried(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider(FakeBehaviour.ERROR))
    with pytest.raises(ProviderError):
        harness.gateway.suggest_labels(TENANT, _label_request("f1"))
    assert len(harness.provider.calls) == 1
    assert harness.sleeps == []
    assert harness.metric("provider_errors") == 1


# --- output contract ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "behaviour",
    [
        FakeBehaviour.INVALID_JSON,
        FakeBehaviour.JSON_ARRAY,
        FakeBehaviour.EXTRA_FIELDS,
        FakeBehaviour.TOKEN_LABEL,
        FakeBehaviour.OVERSIZE_LABEL,
    ],
)
def test_invalid_output_is_discarded_and_counted(tmp_path: Path, behaviour: FakeBehaviour) -> None:
    harness = Harness(tmp_path, FakeProvider(behaviour))
    result = harness.gateway.suggest_labels(TENANT, _label_request("f1", "f2"))
    assert result == SuggestionList(suggestions=[])
    assert harness.metric("invalid_output") == 1
    assert harness.metric("calls") == 1
    (record,) = harness.sink.records
    assert record.status is CallStatus.INVALID_OUTPUT
    assert record.error_class == "InvalidOutputError"
    assert (record.input_tokens, record.output_tokens) == (120, 40)


def test_unknown_targets_are_dropped_and_counted(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider(FakeBehaviour.UNKNOWN_TARGET))
    result = harness.gateway.suggest_labels(TENANT, _label_request("f1", "f2"))
    assert result.suggestions == []
    assert harness.metric("suggestions_dropped") == 2
    assert harness.sink.records[0].status is CallStatus.OK


def test_a_code_fence_is_stripped_and_nothing_else(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider(FakeBehaviour.FENCED_JSON))
    assert len(harness.gateway.suggest_labels(TENANT, _label_request("f1")).suggestions) == 1
    fenced_with_prose = "Here you go:\n```json\n" + _answer(("f1", "Order")) + "\n```"
    harness = Harness(tmp_path, FakeProvider(script=[fenced_with_prose]))
    assert harness.gateway.suggest_labels(TENANT, _label_request("f1")).suggestions == []
    assert harness.metric("invalid_output") == 1


REJECTED: dict[str, tuple[str, str]] = {
    "oversize": ("x" * (MAX_OUTPUT_CHARS + 1), "longer than the limit"),
    "empty": ("", "not JSON"),
    "nan": (
        (
            '{"suggestions": [{"target_id": "f1", "label_type": "order", "label": "O", '
            '"confidence": NaN}]}'
        ),
        "not JSON",
    ),
    "array": ("[]", "not a JSON object"),
    "extra_key": ('{"suggestions": [], "tool_calls": []}', "failed the schema"),
    "trailing": (_answer(("f1", "Order")) + " trailing", "not JSON"),
    "pattern": (_answer(("f1", "Order<script>")), "failed the schema"),
    "label_type": (
        _answer(("f1", "Order"), extra={"label_type": "credit_card"}),
        "failed the schema",
    ),
    "confidence": (_answer(("f1", "Order"), extra={"confidence": 1.5}), "failed the schema"),
    "token_rationale": (
        _answer(("f1", "Order"), extra={"rationale": f"see {TOKEN}"}),
        "token-shaped",
    ),
    "token_label": (_answer(("f1", TOKEN)), "token-shaped"),
    # ADR 0070 section 7: a label must not look like a link a reviewer might click.
    "url_label": (_answer(("f1", "http://evil.co/x")), "failed the schema"),
    "scheme_label": (_answer(("f1", "Order (see hxxp://x)")), "failed the schema"),
    "www_label": (_answer(("f1", "www.evil-orders.co")), "failed the schema"),
    "domain_label": (_answer(("f1", "orders.evil.co")), "failed the schema"),
    "domain_label_mixed_case": (_answer(("f1", "Order.COM")), "failed the schema"),
    "domain_in_label": (_answer(("f1", "Order evil.io ref")), "failed the schema"),
    "label_apostrophe": (_answer(("f1", "Customer's order")), "failed the schema"),
    "label_accent": (_answer(("f1", "Numéro")), "failed the schema"),
}


@pytest.mark.parametrize("case", list(REJECTED))
def test_parse_suggestions_rejects(case: str) -> None:
    text, reason = REJECTED[case]
    with pytest.raises(InvalidOutputError, match=reason):
        parse_suggestions(text, frozenset({"f1"}), 1)


@pytest.mark.parametrize(
    ("label", "rationale"),
    [
        ("Order no. 1", "Matches the order id, e.g. 'SO-1'; see template 3, ok?!"),
        ("PO ref (v1.2)", "version 1.2 of the PO reference"),
        ("Member id", "Shape 9999-AAA in every row; path orders.member_id."),
        ("Sales order / line", ""),
        # Review of 2026-11-02 (Low): quotes, an ampersand, accents and a dash are ordinary text.
        ("Order", 'Matches «numéro» d\'ordre & the "SO" prefix — see template 3, ok?'),
        ("Order", "Spelled naïve, Zoë & Łukasz; 100% of rows [SO-1]; a/b | c ~ d ^ e * f"),
    ],
)
def test_parse_suggestions_accepts_ordinary_punctuation(label: str, rationale: str) -> None:
    text = _answer(("f1", label), extra={"rationale": rationale})
    result, dropped = parse_suggestions(text, frozenset({"f1"}), 1)
    assert dropped == 0
    assert [(s.label, s.rationale) for s in result.suggestions] == [(label, rationale)]


BLANKED_RATIONALES: dict[str, str] = {
    "too_long": "r" * 201,
    "markup": "see <a href=x>here</a>",
    "script": "Order <script>alert(1)</script>",
    "braces": '{"tool": "reveal"}',
    "control": "line one\nline two",
    "tab": "a\tb",
    "url": "https://evil.co/x",
    "www": "see www.evil.co",
    "format_char": "zero\u200bwidth",
}


@pytest.mark.parametrize("case", list(BLANKED_RATIONALES))
def test_parse_suggestions_blanks_an_unclean_rationale_and_keeps_the_label(case: str) -> None:
    """Review of 2026-11-02 (Low): the rationale is a courtesy; one that carries markup, a
    brace, a control character, a link or too much text is blanked, and the label (what the
    console uses) survives. The label's own rules are unchanged (``REJECTED`` above)."""
    text = _answer(("f1", "Order"), extra={"rationale": BLANKED_RATIONALES[case]})
    result, dropped = parse_suggestions(text, frozenset({"f1"}), 1)
    assert dropped == 0
    assert [(s.label, s.rationale) for s in result.suggestions] == [("Order", "")]
    assert not rationale_is_clean(BLANKED_RATIONALES[case])
    blanked = Suggestion.model_validate(
        {
            "target_id": "f1",
            "label_type": "order",
            "label": "Order",
            "confidence": 0.5,
            "rationale": 42,
        }
    )
    assert blanked.rationale == ""


def test_parse_suggestions_filters_targets_duplicates_and_cap() -> None:
    text = _answer(("f1", "A"), ("f9", "B"), ("f1", "C"), ("f2", "D"), ("f3", "E"))
    result, dropped = parse_suggestions(text, frozenset({"f1", "f2", "f3"}), 2)
    assert [(s.target_id, s.label) for s in result.suggestions] == [("f1", "A"), ("f2", "D")]
    assert dropped == 3
    fenced = "```\n" + _answer(("f1", "A")) + "\n```\n"
    assert parse_suggestions(fenced, frozenset({"f1"}), 1)[0].suggestions[0].label == "A"


# --- the daily cap ---------------------------------------------------------------------------


def test_the_daily_cap_refuses_the_third_call_and_resets_on_the_next_utc_day(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, FakeProvider(), max_calls_per_day=2)
    request = _label_request("f1")
    assert len(harness.gateway.suggest_labels(TENANT, request).suggestions) == 1
    assert len(harness.gateway.suggest_labels(TENANT, request).suggestions) == 1
    assert harness.gateway.calls_today == 2
    with pytest.raises(DailyCapExceededError, match="max_calls_per_day"):
        harness.gateway.suggest_labels(TENANT, request)
    assert len(harness.provider.calls) == 2, "the provider was never asked"
    assert harness.metric("daily_cap") == 1
    assert harness.metric("calls") == 2
    assert [r.status for r in harness.sink.records] == [
        CallStatus.OK,
        CallStatus.OK,
        CallStatus.REFUSED_CAP,
    ]
    refused = harness.sink.records[-1]
    assert refused.error_class == "DailyCapExceededError"
    assert refused.payload_sha256 == hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    assert (refused.input_tokens, refused.output_tokens, refused.latency_ms) == (None, None, 0)
    assert "carto_llm_daily_cap_total 1" in harness.gateway.metrics.render()

    # The day is the UTC day of the injected clock: one second before midnight is still today.
    harness.clock.now = datetime(2026, 10, 18, 23, 59, 59, tzinfo=UTC)
    with pytest.raises(DailyCapExceededError):
        harness.gateway.suggest_labels(TENANT, request)
    assert harness.metric("daily_cap") == 2
    harness.clock.now = datetime(2026, 10, 19, 0, 0, tzinfo=UTC)
    assert len(harness.gateway.suggest_labels(TENANT, request).suggestions) == 1
    assert harness.gateway.calls_today == 1
    assert len(harness.provider.calls) == 3
    assert harness.sink.records[-1].status is CallStatus.OK
    assert harness.sink.records[-1].at == datetime(2026, 10, 19, 0, 0, tzinfo=UTC)


def test_every_attempt_counts_against_the_daily_cap(tmp_path: Path) -> None:
    script = [TransientProviderError("fake: http 5xx"), TransientProviderError("fake: timeout")]
    harness = Harness(tmp_path, FakeProvider(script=script), max_calls_per_day=2)
    with pytest.raises(DailyCapExceededError):
        harness.gateway.suggest_labels(TENANT, _label_request("f1"))
    assert len(harness.provider.calls) == 2
    assert harness.sleeps == [0.5], "refused before the second back-off"
    assert [r.status for r in harness.sink.records] == [
        CallStatus.PROVIDER_ERROR,
        CallStatus.PROVIDER_ERROR,
        CallStatus.REFUSED_CAP,
    ]
    assert harness.metric("retries") == 1


def test_a_refused_payload_takes_no_allowance(tmp_path: Path) -> None:
    harness = Harness(tmp_path, FakeProvider(), max_calls_per_day=1)
    with pytest.raises(PayloadRefusedError):
        harness.gateway.suggest_labels(TENANT, {"fields": []})  # type: ignore[arg-type]
    assert harness.gateway.calls_today == 0
    assert len(harness.gateway.suggest_labels(TENANT, _label_request("f1")).suggestions) == 1
    assert harness.metric("daily_cap") == 0


# --- audit sinks -----------------------------------------------------------------------------


def test_an_audit_failure_is_counted_and_the_suggestion_still_returned(tmp_path: Path) -> None:
    class Failing:
        def record(self, record: CallRecord) -> None:
            raise AuditSinkError("llm_calls insert failed: OperationalError")

    assert isinstance(Failing(), CallAuditSink)
    metrics = GatewayMetrics()
    gateway = LlmGateway(
        _settings(tmp_path), FakeProvider(), Failing(), lambda: T0, metrics=metrics
    )
    result = gateway.suggest_labels(TENANT, _label_request("f1"))
    assert len(result.suggestions) == 1
    assert metrics.get("carto_llm_audit_failures_total") == 1
    assert "carto_llm_audit_failures_total 1" in metrics.render()


def _record(status: CallStatus = CallStatus.OK) -> CallRecord:
    return CallRecord(
        id="01K7A0000000000000000000A1",
        tenant_id=TENANT,
        at=T0,
        task=TaskKind.LABEL_FIELD,
        payload_sha256="ab" * 32,
        provider="anthropic",
        model="model-x",
        input_tokens=10,
        output_tokens=5,
        latency_ms=321,
        status=status,
        error_class=None if status is CallStatus.OK else "InvalidOutputError",
    )


def test_the_postgres_sink_writes_one_row_per_record() -> None:
    engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "CREATE TABLE llm_calls (id TEXT PRIMARY KEY, tenant_id TEXT, at TIMESTAMP, "
                "task TEXT, payload_sha256 TEXT, provider TEXT, model TEXT, input_tokens INTEGER, "
                "output_tokens INTEGER, latency_ms INTEGER, status TEXT, error_class TEXT, "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
        )
    sink = PostgresCallAuditSink(engine)
    assert isinstance(sink, CallAuditSink)
    sink.record(_record())
    with engine.connect() as connection:
        row = connection.execute(sqlalchemy.text("SELECT * FROM llm_calls")).mappings().one()
    assert row["task"] == "label_field"
    assert row["status"] == "ok"
    assert row["payload_sha256"] == "ab" * 32
    assert row["latency_ms"] == 321
    assert row["error_class"] is None
    with pytest.raises(AuditSinkError, match="IntegrityError"):
        sink.record(_record(CallStatus.INVALID_OUTPUT))  # same id twice


def test_the_postgres_sink_reports_a_missing_table_as_a_sink_error() -> None:
    engine = sqlalchemy.create_engine("sqlite+pysqlite:///:memory:")
    with pytest.raises(AuditSinkError, match="OperationalError"):
        PostgresCallAuditSink(engine).record(_record())


# --- logs ------------------------------------------------------------------------------------


def test_logs_carry_task_status_and_latency_only(tmp_path: Path) -> None:
    request = _label_request("f1")
    secret_text = request.fields[0].template_text
    with structlog.testing.capture_logs() as logs:
        Harness(tmp_path, FakeProvider()).gateway.suggest_labels(TENANT, request)
        Harness(tmp_path, FakeProvider(FakeBehaviour.INVALID_JSON)).gateway.suggest_labels(
            TENANT, request
        )
        with pytest.raises(ProviderError):
            Harness(tmp_path, FakeProvider(FakeBehaviour.ERROR)).gateway.suggest_labels(
                TENANT, request
            )
    assert [entry["event"] for entry in logs] == [
        "llm call ok",
        "llm output discarded",
        "llm call failed",
    ]
    for entry in logs:
        assert entry["task"] == "label_field"
        assert entry["status"] in {"ok", "invalid_output", "provider_error"}
        assert isinstance(entry["latency_ms"], int)
        rendered = json.dumps(entry, default=str)
        assert secret_text not in rendered
        assert "Order number" not in rendered
        assert "Sure!" not in rendered
        assert "f1" not in rendered.replace("label_field", "")
