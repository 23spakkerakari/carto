"""The edge configurations that describe scenario A: the offline analyzer's ``analyze.shop.yaml``,
the Compose stack's ``sources.dev.yaml`` and the collector's ``collector.dev.yaml`` (spec 4.1,
8.1.1, 8.1.2, Appendix A). They must load with the edge's own loader and name exactly the sources
and systems the ground truth declares."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from carto_edge.config import SourcesFile, SourceType, load_sources_file
from carto_simulator.api import GenerationRequest, generate
from carto_simulator.scenarios.shop_model import (
    PO_TABLE,
    SOURCES,
    SRC_PAYMENTS,
    SRC_SHIP_SFTP,
    SRC_WMS_DB,
    SRC_WMS_EXPORT,
    SYS_PAYMENTS,
    SYS_WAREHOUSE,
)

ROOT = Path(__file__).resolve().parents[2]
ANALYZE_FILE = ROOT / "simulator" / "analyze.shop.yaml"
DEV_SOURCES_FILE = ROOT / "deploy" / "compose" / "sources.dev.yaml"
COLLECTOR_FILE = ROOT / "otel" / "collector.dev.yaml"
LOCAL_ZONE = "America/New_York"
WAREHOUSE_FORMAT = "%Y-%m-%d %H:%M:%S"


@pytest.fixture(scope="module")
def analyze() -> SourcesFile:
    return load_sources_file(ANALYZE_FILE)


@pytest.fixture(scope="module")
def dev() -> SourcesFile:
    return load_sources_file(DEV_SOURCES_FILE)


@pytest.fixture(scope="module")
def collector() -> dict[str, Any]:
    data = yaml.safe_load(COLLECTOR_FILE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("run") / "shop"
    generate(GenerationRequest(days=2, daily_volume=5, seed=2, noise_rate=0.1), out)
    return out


def _ids_match_ground_truth(config: SourcesFile) -> None:
    assert sorted(s.id for s in config.sources) == sorted(s.source_id for s in SOURCES)
    assert sorted(s.id for s in config.systems) == sorted({s.system_id for s in SOURCES})
    truth_system = {s.source_id: s.system_id for s in SOURCES}
    for source in config.sources:
        assert source.system == truth_system[source.id], source.id
        assert source.enabled
    truth_names = {s.system_id: s.system_name for s in SOURCES}
    for system in config.systems:
        assert system.name == truth_names[system.id]
        assert system.owner_group, f"{system.id} names an owner group"


def _pins_enable_phonetic_names(config: SourcesFile) -> None:
    pins = {pin.field: pin for pin in config.field_policies}
    for field in (f"{SYS_WAREHOUSE}/*/customer_name", f"{SYS_PAYMENTS}/*/cardholderName"):
        pin = pins[field]
        assert pin.field_class == "person_name"
        assert pin.policy == "tokenize"
        assert pin.forms == ["phonetic"]
        assert pin.reason


# -- analyze.shop.yaml -------------------------------------------------------------------------


def test_analyze_config_names_the_ground_truth_sources_and_systems(analyze: SourcesFile) -> None:
    _ids_match_ground_truth(analyze)
    assert all(source.type is SourceType.UPLOAD for source in analyze.sources)
    assert all(source.secret_ref is None for source in analyze.sources)


def test_analyze_config_kinds_and_row_columns(analyze: SourcesFile) -> None:
    by_id = {source.id: source for source in analyze.sources}
    rows = by_id[SRC_WMS_DB].config
    assert rows["kind"] == "rows"
    assert rows["table"] == PO_TABLE
    assert rows["primary_key"] == "id"
    # created_at, not updated_at: the truth times a row at the PO's creation and the linker's
    # temporal adjacency to the payments depends on it (M2 eval follow-up).
    assert rows["timestamp_column"] == "created_at"
    assert rows["actor_column"] == "created_by"
    assert rows["paths"] == [f"warehouse/{PO_TABLE}*.csv"]  # the CSV and, after F4, .renamed.csv
    assert by_id[SRC_SHIP_SFTP].config["kind"] == "files"
    for source_id, source in by_id.items():
        if source_id not in (SRC_WMS_DB, SRC_SHIP_SFTP):
            assert source.config.get("kind", "log") == "log", source_id
        for path in source.config["paths"]:
            assert not Path(path).is_absolute(), f"{source_id}: paths are relative to --input"
            assert ".." not in path and not path.startswith("ground_truth")


def test_analyze_config_paths_match_the_generated_files(analyze: SourcesFile, run: Path) -> None:
    for source in analyze.sources:
        matched = [p for pattern in source.config["paths"] for p in run.glob(pattern)]
        assert matched, f"{source.id}: {source.config['paths']} match nothing under the run"
        assert all(p.is_file() for p in matched)
        assert all("ground_truth" not in p.parts for p in matched)


def test_analyze_config_parse_hints_follow_the_simulator_readme(analyze: SourcesFile) -> None:
    by_id = {source.id: source for source in analyze.sources}
    for source_id in (SRC_WMS_DB, SRC_WMS_EXPORT):
        parse = by_id[source_id].parse
        assert parse.timezone == LOCAL_ZONE, source_id
        assert parse.timestamp_format == WAREHOUSE_FORMAT, source_id
    assert by_id[SRC_WMS_DB].parse.actor_field == "created_by"
    assert by_id[SRC_WMS_DB].parse.format.value == "csv"
    assert by_id[SRC_WMS_EXPORT].parse.format.value == "text"
    assert by_id["src_webstore_log"].parse.format.value == "ndjson"
    assert by_id["src_webstore_log"].parse.message_field == "msg"
    assert by_id["src_orders_log"].parse.format.value == "logfmt"
    assert by_id["src_orders_log"].parse.message_field == "msg"
    assert by_id["src_ship_log"].parse.format.value == "logfmt"
    assert by_id["src_ship_log"].parse.message_field == "msg"
    assert by_id[SRC_PAYMENTS].parse.format.value == "xml"
    assert by_id[SRC_PAYMENTS].parse.timestamp_field == "timestamp"
    for source_id in ("src_webstore_log", "src_orders_log", "src_ship_log", SRC_PAYMENTS):
        assert by_id[source_id].parse.timestamp_format == "iso8601", source_id
        assert by_id[source_id].parse.timezone == "UTC", source_id


def test_analyze_config_pins_phonetic_forms_for_the_composite_link(analyze: SourcesFile) -> None:
    _pins_enable_phonetic_names(analyze)
    assert analyze.policy_version


# -- deploy/compose/sources.dev.yaml -----------------------------------------------------------


def _scenario_sources(config: SourcesFile) -> SourcesFile:
    """The sources the simulator produces data for: without the push-only webhook example of the
    dev stack (ADR 0032), which no scenario A system sends."""
    kept = [source for source in config.sources if source.type is not SourceType.WEBHOOK]
    return config.model_copy(update={"sources": kept})


def test_dev_sources_name_the_same_sources_systems_and_pins(
    dev: SourcesFile, analyze: SourcesFile
) -> None:
    _ids_match_ground_truth(_scenario_sources(dev))
    _pins_enable_phonetic_names(dev)
    assert dev.field_policies == analyze.field_policies
    assert dev.policy_version == analyze.policy_version
    dev_systems = [s.model_dump() for s in sorted(dev.systems, key=lambda s: s.id)]
    analyzer_systems = [s.model_dump() for s in sorted(analyze.systems, key=lambda s: s.id)]
    assert dev_systems == analyzer_systems


def test_dev_sources_route_each_source_through_the_right_connector(dev: SourcesFile) -> None:
    by_id = {source.id: source for source in dev.sources}
    for source_id in (
        "src_webstore_log", "src_orders_log", SRC_PAYMENTS, SRC_WMS_EXPORT, "src_ship_log",
    ):  # fmt: skip
        assert by_id[source_id].type is SourceType.OTLP, source_id
        assert by_id[source_id].config == {}, source_id
    sftp = by_id[SRC_SHIP_SFTP]
    assert sftp.type is SourceType.SFTP
    assert sftp.config["directories"] == ["local:///data/sim/shipping/outbound"]
    assert sftp.config["filename_patterns"] == ["SHIP_*.csv"]
    assert sftp.secret_ref is None
    rows = by_id[SRC_WMS_DB]
    assert rows.type is SourceType.UPLOAD
    assert rows.config["paths"] == [f"/data/sim/warehouse/{PO_TABLE}.ndjson"]
    assert rows.parse.format.value == "ndjson"
    assert rows.parse.timezone == LOCAL_ZONE
    assert rows.parse.timestamp_format == WAREHOUSE_FORMAT
    assert rows.parse.actor_field == "created_by"


def test_dev_sources_parse_hints_match_the_analyzer(dev: SourcesFile, analyze: SourcesFile) -> None:
    analyzer = {source.id: source.parse for source in analyze.sources}
    for source in _scenario_sources(dev).sources:
        if source.id == SRC_WMS_DB:
            continue  # the row stream is NDJSON in the dev stack, CSV for the analyzer
        assert source.parse == analyzer[source.id], source.id


def test_dev_sources_add_one_webhook_source_for_the_push_route(dev: SourcesFile) -> None:
    """The only source beyond scenario A: the example the CI push-route check posts to."""
    [hooks] = [source for source in dev.sources if source.type is SourceType.WEBHOOK]
    assert hooks.id == "src_shop_hooks"
    assert hooks.secret_ref == "local://shop_webhook_secret"  # noqa: S105 - a reference, not a value
    assert hooks.system == "sys_webstore"


def test_dev_sources_header_documents_the_row_stream_gap() -> None:
    text = DEV_SOURCES_FILE.read_text(encoding="utf-8")
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    assert "purchase_orders.ndjson" in header
    assert "rows" in header and "CSV" in header


# -- otel/collector.dev.yaml -------------------------------------------------------------------


def test_collector_dev_receivers_cover_every_otlp_source(
    collector: dict[str, Any], dev: SourcesFile
) -> None:
    receivers = collector["receivers"]
    filelogs = {name: cfg for name, cfg in receivers.items() if name.startswith("filelog/")}
    assert len(filelogs) == 5
    assert not any(name.startswith(("syslog", "splunk_hec")) for name in receivers)
    otlp_sources = {s.id for s in dev.sources if s.type is SourceType.OTLP}
    tagged = set()
    for name, cfg in filelogs.items():
        assert cfg["start_at"] == "beginning", name
        assert cfg["storage"] == "file_storage", name
        assert cfg["operators"] == [], name
        for pattern in cfg["include"]:
            assert pattern.startswith("/data/sim/"), name
            assert "ground_truth" not in pattern
        tagged.add(cfg["resource"]["carto.source_id"])
    assert tagged == otlp_sources
    pipeline = collector["service"]["pipelines"]["logs"]
    assert sorted(pipeline["receivers"]) == sorted(filelogs)
    assert pipeline["exporters"] == ["otlphttp/edge"]
    assert "filter/require_source" in pipeline["processors"]


def test_collector_dev_exports_to_the_gateway_over_mtls(collector: dict[str, Any]) -> None:
    exporter = collector["exporters"]["otlphttp/edge"]
    assert exporter["endpoint"] == "https://edge-gateway:8443"
    tls = exporter["tls"]
    assert tls["ca_file"] == "/etc/carto/pki/to-edge-gateway-ca.crt"  # ADR 0031
    assert tls["cert_file"] == "/etc/carto/pki/to-edge-gateway.crt"
    assert tls["key_file"] == "/etc/carto/pki/to-edge-gateway.key"
    assert exporter["sending_queue"]["storage"] == "file_storage"
    storage = collector["extensions"]["file_storage"]
    assert storage["directory"] == "/var/lib/otelcol/storage"
    assert "file_storage" in collector["service"]["extensions"]


def test_collector_dev_patterns_match_the_simulator_layout(
    collector: dict[str, Any], run: Path
) -> None:
    for name, cfg in collector["receivers"].items():
        if not name.startswith("filelog/"):
            continue
        for pattern in cfg["include"]:
            relative = pattern.removeprefix("/data/sim/")
            assert list(run.glob(relative)), f"{name}: {pattern} matches nothing in a run"
