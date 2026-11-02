"""Review fixes in carto_edge.pipeline.parser (M1 review, package A2): product logs name no
customer file (spec 2.3 invariant 7)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import structlog

from carto_edge.config import SourceConfig, SourceType
from carto_edge.pipeline.model import RawRecord
from carto_edge.pipeline.parser import REASON_INTERNAL, ParseFailure, RecordParser
from carto_edge.pipeline.templates import TemplateStore
from carto_schema.event import EventKind

RECEIVED = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def _source() -> SourceConfig:
    return SourceConfig(
        id="src_up", system="sys_up", type=SourceType.UPLOAD, config={"paths": ["*.log"]}
    )


def test_an_internal_error_log_names_no_upload_file(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = RecordParser(_source(), TemplateStore())

    def boom(_raw: RawRecord) -> None:
        raise RuntimeError

    monkeypatch.setattr(parser, "_parse", boom)
    raw = RawRecord(
        source_id="src_up",
        system_id="sys_up",
        kind=EventKind.LOG,
        locator="payroll_ZebulonQuixote_2026.log:line:3",
        received_at=RECEIVED,
        text="hello",
    )
    with structlog.testing.capture_logs() as logs:
        outcome = parser.parse(raw)
    assert outcome == ParseFailure(REASON_INTERNAL)
    assert [entry["event"] for entry in logs] == ["parse_internal_error"]
    assert "Zebulon" not in repr(logs)
    assert "payroll" not in repr(logs)
    assert logs[0]["record_kind"] == "log"
