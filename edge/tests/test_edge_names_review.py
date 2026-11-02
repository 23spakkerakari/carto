"""Regression tests for the M1 review findings on field names and structure (plan
M1-review-fixes, package A1; ADR 0030 amendment of 2026-10-12).

Every value is synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from carto_common.crypto import Keyring, TokenKey, generate_key
from carto_edge.config import (
    ClassifySettings,
    ParseConfig,
    RecordFormat,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import FieldClass, ParsedRecord, PipelineResult, RawRecord
from carto_edge.pipeline.parse.common import clean_text, flatten
from carto_edge.pipeline.parse.json import parse_json
from carto_edge.pipeline.parse.names import FieldNameStore
from carto_edge.pipeline.parse.xml import parse_xml
from carto_edge.pipeline.parser import RecordParser
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.pipeline import EdgePipeline
from carto_edge.pipeline.stats import FieldStatsStore
from carto_edge.pipeline.templates import TemplateStore
from carto_edge.pipeline.tokenize import Tokenizer
from carto_schema.event import EventKind

SYS = "sys_web"
RECEIVED = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
LONE = chr(0xD800)
FFFD = chr(0xFFFD)


def _source(parse: ParseConfig | None = None) -> SourceConfig:
    return SourceConfig(
        id="src_web",
        system=SYS,
        type=SourceType.UPLOAD,
        config={"paths": ["x.log"]},
        parse=parse or ParseConfig(),
    )


def _pipeline(parse: ParseConfig | None = None, *, quarantine: int = 200) -> EdgePipeline:
    sources = SourcesFile(
        systems=[SystemConfig(id=SYS, name="Web")],
        sources=[_source(parse)],
    )
    detector = RegexDetector()
    classifier = Classifier(
        ClassifySettings(quarantine_samples=quarantine, distinct_threshold=10),
        sources.field_policies,
        detector,
        FieldStatsStore(sample_size=64),
        policy_version=sources.policy_version,
    )
    return EdgePipeline(
        sources=sources,
        templates=TemplateStore(),
        classifier=classifier,
        detector=detector,
        tokenizer=Tokenizer(Keyring(active=TokenKey(1, generate_key())), "default"),
        tenant_id="default",
        retention_days=30,
        clock=lambda: RECEIVED,
    )


def _dump(result: PipelineResult) -> str:
    """Everything a result would carry off the edge or into the vault, as one text."""
    assert result.event is not None, result.dropped_reason
    vault = [entry.raw_value for entry in result.vault_entries]
    return result.event.model_dump_json() + json.dumps(vault)


def _raw(text: str, line: int = 1, locator: str | None = None) -> RawRecord:
    return RawRecord(
        source_id="src_web",
        system_id=SYS,
        kind=EventKind.LOG,
        locator=locator or f"app.log:line:{line}",
        received_at=RECEIVED,
        text=text,
        sequence=line,
    )


# -- finding 7: lone surrogates are replaced at the decode boundary ------------------------------


def test_clean_text_replaces_lone_surrogates_only() -> None:
    assert clean_text("plain") == "plain"
    assert clean_text("café \U0001f600") == "café \U0001f600"
    assert clean_text(f"a{LONE}b{chr(0xDFFF)}c") == f"a{FFFD}b{FFFD}c"


def test_flatten_and_json_never_return_a_lone_surrogate() -> None:
    flat = parse_json('{"msg": "payment \\ud800 received", "k\\udc00": {"n\\ud801": 1}}')
    assert flat is not None
    text = json.dumps(flat.fields, ensure_ascii=False) + "".join(flat.top_keys)
    text.encode("utf-8")  # raises on a lone surrogate
    assert flat.fields["msg"] == f"payment {FFFD} received"
    nested = flatten({f"a{LONE}": [f"v{LONE}"]})
    "".join([*nested.fields, *nested.fields.values()]).encode("utf-8")


def test_a_text_record_with_a_lone_surrogate_parses() -> None:
    parser = RecordParser(_source(), TemplateStore(), FieldNameStore(RegexDetector()))
    result = parser.parse(_raw(f"2026-09-23T04:00:00Z INFO payment {LONE} received"))
    assert isinstance(result, ParsedRecord)
    assert parser.internal_errors == 0
    json.dumps(result.fields, ensure_ascii=False).encode("utf-8")


def test_the_name_register_persists_names_with_surrogates(tmp_path: Path) -> None:
    names = FieldNameStore(RegexDetector(), tmp_path, flush_interval_seconds=0)
    for _ in range(4):
        names.apply("sys_b", {f"k{LONE}": "1", "plain_key": "2"}, [f"k{LONE}", "plain_key"])
    names.close()
    again = FieldNameStore(RegexDetector(), tmp_path)
    gated = again.apply("sys_b", {"plain_key": "2"}, ["plain_key"])
    assert gated.fields == {"plain_key": "2"}  # the register was written and read back


# -- finding 4: paths are segment tuples until the end -----------------------------------------


def _gate_nested(
    names: FieldNameStore, record: dict[str, object], trusted: frozenset[str] = frozenset()
) -> dict[str, str]:
    flat = flatten(record)
    return names.apply(SYS, flat.fields, flat.top_keys, trusted, structure=flat.structure).fields


def test_flatten_reports_the_segments_of_nested_paths() -> None:
    flat = flatten({"a.b": 1, "m": {"x.y": 2, "42": 3}, "l": [4]})
    assert flat.fields == {"a.b": "1", "m.x.y": "2", "m.42": "3", "l.0": "4"}
    assert "a.b" not in flat.structure  # one segment
    assert flat.structure["m.x.y"] == (("m", False), ("x.y", False))
    assert flat.structure["m.42"] == (("m", False), ("42", False))
    assert flat.structure["l.0"] == (("l", False), ("0", True))


def test_a_dotted_map_key_stays_one_value() -> None:
    names = FieldNameStore(RegexDetector(), map_min_keys=16)
    for i in range(20):
        _gate_nested(names, {"balances": {f"k{chr(97 + i)}x.quixote": 5}})
    gated = _gate_nested(names, {"balances": {"dclfre.zebulon": 5}})
    assert gated == {"balances.0.key": "dclfre.zebulon", "balances.0.value": "5"}


def test_a_numeric_map_key_is_a_name_not_an_index() -> None:
    names = FieldNameStore(RegexDetector())
    gated = _gate_nested(names, {"stock": {"42": 1}}, names.trusted_set(["stock"]))
    assert gated == {"stock.*": "1"}  # gated like any name, not passed as an array index


def test_xml_paths_carry_their_segments() -> None:
    record = parse_xml('<r><svc.name a.b="1">x<i>1</i></svc.name><i>2</i><i>3</i></r>')
    assert record is not None
    assert record.structure["svc.name.@a.b"] == (("svc.name", False), ("@a.b", False))
    assert record.structure["i.1"] == (("i", False), ("1", True))


# -- finding 1: secret and PII name hints survive the name gate --------------------------------


def test_a_secret_named_field_is_dropped_by_the_gate_while_its_name_is_masked() -> None:
    pipeline = _pipeline(ParseConfig(format=RecordFormat.NDJSON))
    for line in range(1, 4):
        record = {
            "ts": "2026-09-23T04:00:00Z",
            "level": "info",
            "user_password": f"Hunter{line}Hunter",
            "region": "eu",
        }
        result = pipeline.process(_raw(json.dumps(record), line))
        assert "Hunter" not in _dump(result), line
        assert result.vault_entries == [], line


def test_a_pii_named_field_keeps_its_class_while_its_name_is_masked() -> None:
    parser = RecordParser(_source(), TemplateStore(), FieldNameStore(RegexDetector()))
    parsed = parser.parse(_raw(json.dumps({"customer_email": "mkzed@example.com"})))
    assert isinstance(parsed, ParsedRecord)
    assert parsed.fields == {"*": "mkzed@example.com"}
    assert parsed.name_classes == {"*": FieldClass.CONTACT}

    pipeline = _pipeline(ParseConfig(format=RecordFormat.NDJSON))
    for line in range(1, 3):
        record = {"ts": "2026-09-23T04:00:00Z", "customer_email": f"mk{line}zed@example.com"}
        result = pipeline.process(_raw(json.dumps(record), line))
        assert "zed@example.com" not in _dump(result)
        assert result.vault_entries == []
        assert result.event is not None and result.event.dropped_fields == ["*"]


def test_distinct_masked_names_never_share_one_path() -> None:
    names = FieldNameStore(RegexDetector())
    gated = names.apply(SYS, {"cart_ref": "c-1", "region": "eu"}, ["cart_ref", "region"])
    assert gated.fields == {"*": "c-1", "*~1": "eu"}
    nested = flatten({"order": {"ref": "r-1", "lines": [{"sku": "s-1", "qty": 2}]}})
    gated = names.apply(SYS, nested.fields, nested.top_keys, structure=nested.structure)
    assert gated.fields == {"*.*": "r-1", "*.*~1.0.*": "s-1", "*.*~1.0.*~1": "2"}


def test_hint_words_are_not_exempt_from_masking() -> None:
    names = FieldNameStore(RegexDetector())
    for _ in range(2):
        gated = names.apply(SYS, {"alice_email": "x"}, ["alice_email"])
    assert gated.fields == {"*": "x"}
    assert "alice" not in json.dumps(gated.top_keys)


# -- finding 8: a name counts once per record, not once per delivery --------------------------


def test_one_record_delivered_three_times_does_not_trust_its_names() -> None:
    names = FieldNameStore(RegexDetector())
    for _ in range(5):
        gated = names.apply(SYS, {"Zebulon Quixote": "vip"}, ["Zebulon Quixote"], record_id="r7")
    assert gated.fields == {"*": "vip"}
    for record_id in ("r8", "r9"):
        names.apply(SYS, {"Zebulon Quixote": "vip"}, ["Zebulon Quixote"], record_id=record_id)
    trusted = names.apply(SYS, {"Zebulon Quixote": "vip"}, ["Zebulon Quixote"], record_id="r10")
    assert trusted.fields == {"Zebulon Quixote": "vip"}  # three distinct records


def test_the_parser_identifies_a_redelivered_line() -> None:
    parser = RecordParser(_source(), TemplateStore(), FieldNameStore(RegexDetector()))
    line = json.dumps({"ts": "2026-09-23T04:00:00Z", "Zebulon Quixote": "vip"})
    for _ in range(4):
        parsed = parser.parse(_raw(line, 7))
        assert isinstance(parsed, ParsedRecord)
        assert "Zebulon Quixote" not in parsed.fields
        assert "Zebulon" not in parsed.template_text
