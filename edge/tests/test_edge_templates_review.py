"""Review fixes in carto_edge.pipeline.templates (M1 review, package A2): template state that
any input can be persisted, secrets never become template constants, and repetition counts
records, not deliveries (spec 2.3 invariants 2 and 8, ADR 0027)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carto_common.crypto import Keyring, TokenKey, generate_key
from carto_edge.config import (
    ClassifySettings,
    ParseConfig,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.pipeline.classify import Classifier
from carto_edge.pipeline.model import RawRecord
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.pipeline.pipeline import EdgePipeline
from carto_edge.pipeline.redact import redact_template_text, template_cache_clear
from carto_edge.pipeline.stats import FieldStatsStore
from carto_edge.pipeline.templates import MASK, TemplateStore, compute_template_id
from carto_edge.pipeline.tokenize import Tokenizer
from carto_schema.event import EventKind

LONE_SURROGATE = chr(0xD800)
RECEIVED = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def make(*, quarantine: int = 200) -> EdgePipeline:
    sources = SourcesFile(
        systems=[SystemConfig(id="sys_webstore", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_webstore",
                type=SourceType.UPLOAD,
                config={"paths": ["x.ndjson"]},
                parse=ParseConfig(),
            )
        ],
    )
    classify = ClassifySettings(quarantine_samples=quarantine, distinct_threshold=10)
    detector = RegexDetector()
    classifier = Classifier(classify, [], detector, FieldStatsStore(sample_size=64))
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


def raw_line(text: str, line: int = 1) -> RawRecord:
    return RawRecord(
        source_id="src_web",
        system_id="sys_webstore",
        kind=EventKind.LOG,
        locator=f"app.ndjson:line:{line}",
        received_at=RECEIVED,
        text=text,
        sequence=line,
    )


def test_a_lone_surrogate_never_breaks_template_persistence(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path, flush_interval_seconds=0)
    store.mine("sys_a", f"payment {LONE_SURROGATE} received")
    for _ in range(3):
        store.mine("sys_a", "an ordinary message here")
    store.flush()
    raw = (tmp_path / "sys_a.json").read_bytes()
    assert raw.isascii()
    json.loads(raw)
    reloaded = TemplateStore(tmp_path)
    assert reloaded.cluster_count("sys_a") == 2


def test_a_template_with_a_lone_surrogate_has_an_id() -> None:
    store = TemplateStore()
    for _ in range(3):
        text, _params = store.mine("sys_a", f"payment {LONE_SURROGATE} received")
    assert text == f"payment {LONE_SURROGATE} received"
    assert compute_template_id("sys_a", text).startswith("tpl_")


# -- secrets in free text (finding: a repeated secret became a template constant) ------------

SECRET = "CorrectHorseBatteryStaple"  # noqa: S105
JWT_NO_DIGITS = "eyJabcdefghijk.eyJabcdefghijk.abcdefghijklmn"


def test_a_repeated_password_never_becomes_a_template_constant() -> None:
    pipeline = make(quarantine=200)
    for line in range(1, 5):
        text = (
            f"2026-09-23T04:00:0{line}Z INFO connecting to ftp as user=svc_sync "
            f"password={SECRET} attempt"
        )
        result = pipeline.process(raw_line(text, line))
        assert result.event is not None
        assert SECRET not in result.event.model_dump_json()
    assert result.event is not None
    assert result.event.template_text == (
        f"connecting to ftp as user={MASK} password={MASK} attempt"
    )


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        (f"login as svc_sync with password={SECRET} done", SECRET),
        (f"login as svc_sync with password: {SECRET} done", SECRET),
        (f"login as svc_sync with password {SECRET} done", SECRET),
        (f"call upstream Authorization: Bearer {SECRET} done", SECRET),
        (f"call upstream authorization={SECRET} done", SECRET),
        (f'call upstream "api_key":"{SECRET}" done', SECRET),
    ],
)
def test_secret_tokens_are_masked_before_mining_and_never_params(
    message: str, secret: str, tmp_path: Path
) -> None:
    store = TemplateStore(tmp_path)
    for _ in range(4):
        text, params = store.mine("sys_a", message)
        assert secret not in text
        assert all(secret not in param for param in params)
    store.flush()
    assert secret not in (tmp_path / "sys_a.json").read_text(encoding="utf-8")


def test_a_secret_shaped_token_is_masked_and_its_field_dropped(tmp_path: Path) -> None:
    store = TemplateStore(tmp_path)
    for _ in range(4):
        text, _params = store.mine("sys_a", f"call upstream with {JWT_NO_DIGITS} done")
        assert JWT_NO_DIGITS not in text
    store.flush()
    assert JWT_NO_DIGITS not in (tmp_path / "sys_a.json").read_text(encoding="utf-8")
    pipeline = make(quarantine=1)
    for line in range(1, 5):
        text = f"2026-09-23T04:00:0{line}Z INFO call upstream with {JWT_NO_DIGITS} done"
        result = pipeline.process(raw_line(text, line))
        assert result.event is not None
        assert JWT_NO_DIGITS not in result.event.model_dump_json()


def test_the_value_half_of_a_key_value_token_is_a_parameter() -> None:
    store = TemplateStore()
    for user in ("alice", "bob", "carol"):
        text, params = store.mine("sys_a", f"session opened user={user} host:edge")
    assert text == f"session opened user={MASK} host:{MASK}"
    assert params == ["carol", "edge"]


def test_template_redaction_masks_secret_shaped_tokens() -> None:
    template_cache_clear()
    masked, count = redact_template_text(
        f"call upstream with {JWT_NO_DIGITS} done", RegexDetector()
    )
    assert JWT_NO_DIGITS not in masked
    assert count == 1
