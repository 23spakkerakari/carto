"""Review fixes in carto_edge.runtime (M1 review, package A2): a failing flush never stops
shutdown from saving the statistics and closing the vault and the audit file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from carto_edge.config import (
    EdgeSettings,
    PiiSettings,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.runtime import build_runtime

LONE_SURROGATE = chr(0xD800)


def _sources() -> SourcesFile:
    return SourcesFile(
        systems=[SystemConfig(id="sys_web", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_web",
                type=SourceType.UPLOAD,
                config={"paths": ["web/*.ndjson"]},
            )
        ],
    )


def test_close_runs_every_step_when_the_template_flush_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = EdgeSettings(state_dir=tmp_path / "state", pii=PiiSettings(enabled=False))
    runtime = build_runtime(
        settings, _sources(), mode="analyze", init_local_keys=True, detector=RegexDetector()
    )
    closed: list[str] = []

    def failing_flush() -> None:
        closed.append("pipeline.flush")
        raise UnicodeEncodeError("utf-8", LONE_SURROGATE, 0, 1, "surrogates not allowed")

    def templates_close() -> None:
        closed.append("templates.close")

    monkeypatch.setattr(runtime.pipeline, "flush", failing_flush)
    monkeypatch.setattr(runtime.templates, "close", templates_close)
    stats_path = tmp_path / "state" / "field_stats.json"
    runtime.stats.path = stats_path

    with pytest.raises(UnicodeEncodeError):
        runtime.close()
    assert closed == ["pipeline.flush", "templates.close"]
    assert stats_path.is_file()  # the statistics were saved after the failed flush
    with pytest.raises(sqlite3.ProgrammingError):
        runtime.vault._conn.execute("select 1")  # the vault is closed
    assert runtime.audit._handle.closed
