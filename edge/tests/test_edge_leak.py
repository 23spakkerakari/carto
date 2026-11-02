"""Leak test (spec 18.3, the M1 acceptance arbiter; ADR 0011 for scenario B).

The simulator plants unique marker values in every sensitive and high-cardinality field and
lists them in ``ground_truth/markers.json``. Nothing that crosses from edge to core may contain
one: not the offline bundle (events, fields, templates, both manifests, signature), not the
batches the edge would forward, not what ``ingest-api`` writes to ClickHouse, not its responses,
and not the product logs (spec 2.3 invariants 2 and 7).

``test_leak_small_run`` generates a small two-day run of each scenario (``shop`` and
``people_ops``), captures every product log line from before the analyzer starts, analyzes in
process with the configured PII detector and the locator map, scans the bundle, wraps the
events into zstd ``IngestBatch`` payloads posted to ``carto_core.ingest.app`` with in-memory
stores, and scans the payloads, the rows the writer received (rendered through
``event_row``/``identifier_rows``), the responses and the logs. The simulator's own markers
include the ``secret_values`` it plants into noise lines (ADR 0068); the test plants probes of
its own on top (:func:`_plant_probes`), among them a message repeated three times verbatim.
``test_leak_full_scenario_bundle`` scans ``<sim-out>/<scenario>.carto`` when ``make analyze``
produced it (``make leak SCENARIO=...``; skipped otherwise).

Marker rule (what counts as a leak):

- every string anywhere in ``markers.json`` (all top-level categories, walked recursively) is a
  marker, compared case-insensitively; a marker shorter than 4 characters, and so any purely
  numeric marker of fewer than 4 digits, is skipped as indistinguishable from ordinary text, and
  so is a marker that is its own shape (only 9s, As and punctuation, such as order id ``9999``),
  which cannot be told apart from the ``shape`` the contract carries next to every token;
- a marker that contains a letter matches anywhere, as a substring;
- a marker without a letter (order ids such as ``4471``, PO numbers such as ``88-210``) matches
  only as a whole token, not preceded or followed by a letter or digit, so it is not "found"
  inside a ULID, a token, a hash or a longer number;
- JSON documents (event lines, fields, templates, manifest, signature, batch payloads, response
  bodies, JSON log lines) are scanned through their string leaves, keys and values: every value
  slot of the contracts is a string, while JSON numbers are counts and sizes that could equal a
  numeric order id by chance;
- the paths of this test's own temporary directory and of the repository are removed from log
  and summary text before scanning (a ``pytest-4471`` directory is not a leak).

A failure names the marker category, the output, the location (JSON path, event kind, source
and template) and the marker, so the pipeline can be fixed; the test is never weakened.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import warnings
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml
import zstandard

with warnings.catch_warnings():
    # starlette deprecates httpx under its TestClient at import time (as in the core tests).
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_common.ids import new_ulid
from carto_common.logging import configure_logging
from carto_common.mtls import ActAsRole
from carto_core.bundle import iter_events, verify_bundle
from carto_core.db.clickhouse import WriteResult, event_row, identifier_rows
from carto_core.ingest.app import create_app
from carto_core.ingest.health import InMemorySourceHealthStore
from carto_core.ingest.ledger import InMemoryBatchLedger
from carto_core.settings import CoreSettings
from carto_edge.bundle import BUNDLE_FILES, default_locator_map_path
from carto_edge.cli.analyze import analyze, format_summary
from carto_edge.config import load_sources_file
from carto_edge.pipeline.buffer import compress_batch
from carto_schema.bundle import (
    EVENTS_FILE,
    FIELDS_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
    SIGNATURE_FILE,
    TEMPLATES_FILE,
)
from carto_schema.event import CanonicalEvent
from carto_schema.ingest import MAX_EVENTS_PER_BATCH, IngestBatch
from carto_simulator.api import GenerationRequest, generate

PINNED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
"""The edge tests' wall clock (``conftest.PINNED_NOW``); the ingest app here runs on it too."""

ROOT = Path(__file__).resolve().parents[2]
NL = "\n"
MIN_MARKER_LEN = 4
MAX_REPORTED = 40
BATCH_SIZE = MAX_EVENTS_PER_BATCH
ZSTD_HEADERS = {"content-type": "application/json", "content-encoding": "zstd"}


# --- markers ---------------------------------------------------------------------------------


def _trie_pattern(words: Iterable[str]) -> str:
    """One regex alternation factored as a trie, so tens of thousands of literal markers are
    matched in a single pass in C (an unfactored alternation is tried word by word)."""
    trie: dict[str, Any] = {}
    for word in words:
        node = trie
        for char in word:
            node = node.setdefault(char, {})
        node[""] = {}

    def build(node: dict[str, Any]) -> str:
        branches = [re.escape(char) + build(child) for char, child in sorted(node.items()) if char]
        if not branches:
            return ""
        body = branches[0] if len(branches) == 1 else "(?:" + "|".join(branches) + ")"
        if "" in node:
            return f"(?:{body})?"
        return body

    return build(trie)


_TOKEN = re.compile(r"t[1-9][0-9]{0,3}\.[A-Za-z0-9_-]{22}")


def _is_own_shape(text: str) -> bool:
    """Whether ``text`` is a fixed point of the spec 8.3 shape (digits to 9, letters to A): such
    a marker cannot be told apart from the ``shape`` the contract requires next to a token."""
    return all(char in "9a" or not char.isalnum() for char in text)


@dataclass(frozen=True)
class Markers:
    """The planted values of one simulator run, compiled for scanning (see the module rule)."""

    category: dict[str, str]
    anywhere: re.Pattern[str] | None
    whole_token: re.Pattern[str] | None

    @classmethod
    def load(cls, path: Path) -> Markers:
        data = json.loads(path.read_text(encoding="utf-8"))
        category: dict[str, str] = {}

        def walk(value: object, where: str) -> None:
            if isinstance(value, str):
                text = value.strip().lower()
                # Under 4 characters (so numeric markers under 4 digits) or a value that is its
                # own shape (only 9s, As and punctuation, like order id 9999): skipped.
                if len(text) >= MIN_MARKER_LEN and not _is_own_shape(text):
                    category.setdefault(text, where)
            elif isinstance(value, dict):
                for key, child in value.items():
                    walk(child, f"{where}/{key}")
            elif isinstance(value, list):
                for child in value:
                    walk(child, where)

        for top, value in data.items():
            walk(value, top)
        with_letter = [m for m in category if any(char.isalpha() for char in m)]
        without = [m for m in category if not any(char.isalpha() for char in m)]
        anywhere = re.compile(_trie_pattern(with_letter)) if with_letter else None
        whole = (
            re.compile(rf"(?<![0-9a-z])(?:{_trie_pattern(without)})(?![0-9a-z])")
            if without
            else None
        )
        return cls(category, anywhere, whole)

    def find(self, text: str) -> list[str]:
        lowered = text.lower()
        found: list[str] = []
        for pattern in (self.anywhere, self.whole_token):
            if pattern is not None:
                found.extend(match.group(0) for match in pattern.finditer(lowered))
        return found


@dataclass(frozen=True)
class Finding:
    output: str
    location: str
    category: str


@dataclass
class LeakReport:
    markers: Markers
    environment: Sequence[str] = ()
    findings: Counter[Finding] = field(default_factory=Counter)
    examples: dict[Finding, str] = field(default_factory=dict)
    scanned: Counter[str] = field(default_factory=Counter)

    def _clean(self, text: str) -> str:
        for path in self.environment:
            text = text.replace(path, "<env>")
        # carto tokens are base64url HMAC output: a 4-digit order id can appear inside one by
        # chance between '-' or '_' characters. Tokens carry no value, so they are not scanned.
        return _TOKEN.sub("<token>", text)

    def text(self, output: str, text: str, location: str = "text") -> None:
        self.scanned[output] += 1
        for marker in self.markers.find(self._clean(text)):
            finding = Finding(output, location, self.markers.category[marker])
            self.findings[finding] += 1
            self.examples.setdefault(finding, marker)

    def json_document(self, output: str, document: object, context: str = "") -> None:
        """Scan the string leaves; on a hit, walk again to name the JSON path of each one."""
        leaves = list(_leaves(document, ""))
        self.scanned[output] += 1
        joined = self._clean("\n".join(text for _, text in leaves))
        if not self.markers.find(joined):
            return
        for path, text in leaves:
            for marker in self.markers.find(self._clean(text)):
                location = f"{context} {path}".strip()
                finding = Finding(output, location, self.markers.category[marker])
                self.findings[finding] += 1
                self.examples.setdefault(finding, marker)

    def event(self, output: str, data: dict[str, Any]) -> None:
        context = (
            f"[kind={data.get('kind')} source={data.get('source_id')} "
            f"template={data.get('template_id')}]"
        )
        self.json_document(output, data, context)

    def assert_clean(self) -> None:
        if not self.findings:
            return
        lines = [f"{sum(self.findings.values())} marker hit(s) crossed the edge (spec 18.3):"]
        for finding, count in self.findings.most_common(MAX_REPORTED):
            lines.append(
                f"- {finding.output}: category {finding.category} at {finding.location} "
                f"x{count}, e.g. {self.examples[finding]!r}"
            )
        if len(self.findings) > MAX_REPORTED:
            lines.append(f"- ... and {len(self.findings) - MAX_REPORTED} more locations")
        pytest.fail("\n".join(lines), pytrace=False)


def _leaves(value: object, path: str) -> Iterator[tuple[str, str]]:
    if isinstance(value, str):
        yield path or "$", value
    elif isinstance(value, dict):
        for key, child in value.items():
            name = str(key)
            yield f"{path}{{key}}", name
            yield from _leaves(child, f"{path}.{name}" if path else name)
    elif isinstance(value, list):
        for child in value:
            yield from _leaves(child, f"{path}[]")


def _log_lines(report: LeakReport, output: str, text: str) -> None:
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            report.text(output, line, "non-JSON line")
            continue
        event_name = data.get("event", "?") if isinstance(data, dict) else "?"
        report.json_document(output, data, f"[log event={event_name}]")


def scan_bundle(report: LeakReport, bundle: Path) -> int:
    """Scan every file of a bundle; returns the number of event lines."""
    events = 0
    with (
        (bundle / EVENTS_FILE).open("rb") as raw,
        zstandard.ZstdDecompressor().stream_reader(raw) as reader,
        io.TextIOWrapper(reader, encoding="utf-8") as lines,
    ):
        for line in lines:
            if line.strip():
                events += 1
                report.event(EVENTS_FILE, json.loads(line))
    for name in (FIELDS_FILE, TEMPLATES_FILE, MANIFEST_FILE, SIGNATURE_FILE):
        report.json_document(name, json.loads((bundle / name).read_text(encoding="utf-8")))
    report.text(MANIFEST_MD_FILE, (bundle / MANIFEST_MD_FILE).read_text(encoding="utf-8"))
    return events


# --- in-memory core ----------------------------------------------------------------------------


class FakeWriter:
    """The ClickHouse writer seam of ingest-api: keeps the rows it would insert."""

    def __init__(self) -> None:
        self.events: list[CanonicalEvent] = []

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        self.events.extend(events)
        return WriteResult(len(events), sum(len(event.identifiers) for event in events))

    def ping(self) -> bool:
        return True


def _cell(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    return str(value)


def _row_text(row: Sequence[object]) -> str:
    return "\t".join(_cell(value) for value in row)


def batches_of(events: Sequence[CanonicalEvent], tenant_id: str) -> list[IngestBatch]:
    by_source: dict[str, list[CanonicalEvent]] = {}
    for event in events:
        by_source.setdefault(event.source_id, []).append(event)
    now = datetime.now(UTC)
    return [
        IngestBatch(
            schema_version="1",
            tenant_id=tenant_id,
            source_id=source_id,
            batch_id=new_ulid(),
            sent_at=now,
            events=chunk[start : start + BATCH_SIZE],
        )
        for source_id, chunk in by_source.items()
        for start in range(0, len(chunk), BATCH_SIZE)
    ]


# --- scenarios ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Where the probes go in a scenario's output: the analyzer configuration, one ndjson log
    source (glob and file-name prefix before the day) and one logfmt source with the prefix
    its lines carry before ``msg=`` (timestamp and severity in that source's own names)."""

    config: Path
    ndjson_glob: str
    ndjson_prefix: str
    logfmt_glob: str
    logfmt_prefix: str


LAYOUTS: dict[str, Layout] = {
    "shop": Layout(
        config=ROOT / "simulator" / "analyze.shop.yaml",
        ndjson_glob="webstore/app-*.ndjson",
        ndjson_prefix="app-",
        logfmt_glob="orders/order-svc-*.log",
        logfmt_prefix="ts={day}T05:30:00.000Z level=info",
    ),
    "people_ops": Layout(
        config=ROOT / "simulator" / "analyze.people_ops.yaml",
        ndjson_glob="onboarding/onboarding-*.ndjson",
        ndjson_prefix="onboarding-",
        logfmt_glob="onboarding_legacy/legacy-hires-*.log",
        logfmt_prefix='time="{day} 05:30:00" lvl=INFO',
    ),
}


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    configured, config = structlog.is_configured(), structlog.get_config()
    yield
    if configured:
        structlog.configure(**config)
    else:
        structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)


def _environment(*paths: Path) -> list[str]:
    variants: set[str] = set()
    for path in paths:
        for candidate in (path, path.resolve()):
            variants.update({str(candidate), candidate.as_posix()})
    return sorted(variants, key=len, reverse=True)


# --- tests -------------------------------------------------------------------------------------


def test_marker_rule() -> None:
    markers = Markers(
        category={"c-88213": "a", "4471": "b", "88-210": "c"},
        anywhere=re.compile(_trie_pattern(["c-88213"])),
        whole_token=re.compile(
            rf"(?<![0-9a-z])(?:{_trie_pattern(['4471', '88-210'])})(?![0-9a-z])"
        ),
    )
    assert markers.find("cart C-88213 created") == ["c-88213"]
    assert markers.find("xc-88213y") == ["c-88213"]
    assert markers.find("order_id=4471 po=88-210") == ["4471", "88-210"]
    assert markers.find("01J9ZK4471AB t1.ab4471 44710 14471 88-2101") == []
    assert _trie_pattern(["ab", "abc", "b"]) in {"(?:a(?:b(?:c)?)|b)", "(?:ab(?:c)?|b)"}
    assert re.fullmatch(_trie_pattern(["ab", "abc", "b"]), "abc")
    assert _is_own_shape("9999")
    assert _is_own_shape("99-999")
    assert not _is_own_shape("4471")


@pytest.mark.parametrize("scenario", sorted(LAYOUTS))
def test_leak_small_run(scenario: str, tmp_path: Path, restore_logging: None) -> None:
    logs = io.StringIO()
    configure_logging("edge", level="debug", stream=logs)  # before anything runs

    layout = LAYOUTS[scenario]
    sim = tmp_path / scenario
    # Two recent days: ingest-api refuses events older than the events retention (ADR 0036),
    # so the simulator's fixed default start date would be refused once it is 30 days past.
    generate(
        GenerationRequest(
            scenario=scenario,
            days=2,
            daily_volume=20,
            seed=7,
            start_date=PINNED_NOW.date() - timedelta(days=4),
            noise_rate=0.5,
            pii_density=0.5,
        ),
        sim,
    )
    config = tmp_path / "analyze.yaml"
    probes = _plant_probes(sim, layout, config)
    markers_file = tmp_path / "markers.json"
    planted = json.loads((sim / "ground_truth" / "markers.json").read_text(encoding="utf-8"))
    assert len(planted["secret_values"]) == 7, "the simulator planted no secrets (ADR 0068)"
    planted["probes"] = probes
    markers_file.write_text(json.dumps(planted), encoding="utf-8")
    out = tmp_path / f"{scenario}.carto"
    result = analyze(config, sim, out, locator_map=True)
    manifest = result.manifest
    assert manifest.counts.events > 0
    verified = verify_bundle(out)
    events = list(iter_events(verified))
    assert len(events) == manifest.counts.events

    report = LeakReport(
        Markers.load(markers_file),
        environment=_environment(tmp_path, ROOT),
    )
    assert report.markers.category, "markers.json holds no marker"
    assert {report.markers.category[m] for m in report.markers.category} >= {
        "secret_values",
        "probes/thrice",
    }
    assert scan_bundle(report, out) == manifest.counts.events
    for line in format_summary(result):
        report.text("analyze summary", line)

    # The locator map: eval only, one line per event, next to the bundle, never in it.
    locator = default_locator_map_path(out)
    assert result.locator_map == locator
    assert locator.parent == out.resolve().parent
    assert {entry.name for entry in out.iterdir()} == BUNDLE_FILES
    lines = [json.loads(line) for line in locator.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == manifest.counts.events
    source_ids = tuple(f"{source.id}:" for source in load_sources_file(config).sources)
    assert all(line["key"].startswith(source_ids) for line in lines)

    # What the edge would forward, and what ingest-api would write.
    writer = FakeWriter()
    app = create_app(
        CoreSettings(),
        writer,
        InMemoryBatchLedger(),
        InMemorySourceHealthStore(),
        clock=lambda: PINNED_NOW,  # the edge tests' clock (conftest): the run is four days old
    )
    client = TestClient(ActAsRole(app, "edge-gateway"))  # the edge forwards (ADR 0031)
    for batch in batches_of(events, manifest.tenant_id):
        payload = compress_batch(batch)
        decompressed = zstandard.ZstdDecompressor().decompress(payload)
        report.json_document("forwarded batch payload", json.loads(decompressed))
        response = client.post("/internal/ingest", content=payload, headers=ZSTD_HEADERS)
        assert response.status_code == 200, response.text
        report.json_document("ingest-api response", response.json())
    assert len(writer.events) == manifest.counts.events
    stored_at = datetime.now(UTC)
    for event in writer.events:
        report.text(
            "clickhouse events row", _row_text(event_row(event, stored_at)), event.kind.value
        )
        for row in identifier_rows(event, stored_at):
            report.text("clickhouse event_identifiers row", _row_text(row), event.kind.value)

    log_text = logs.getvalue()
    assert log_text, "no product log line was captured"
    _log_lines(report, "product logs", log_text)
    assert report.scanned["product logs"] > 0
    report.assert_clean()


@pytest.mark.slow
def test_leak_full_scenario_bundle(request: pytest.FixtureRequest) -> None:
    scenario = str(request.config.getoption("--scenario"))
    sim_out = Path(str(request.config.getoption("--sim-out")))
    if not sim_out.is_absolute():
        sim_out = ROOT / sim_out
    bundle = sim_out / f"{scenario}.carto"
    markers = sim_out / scenario / "ground_truth" / "markers.json"
    if not bundle.is_dir() or not markers.is_file():
        pytest.skip(f"run `make sim` and `make analyze SCENARIO={scenario}` first")
    verified = verify_bundle(bundle)
    report = LeakReport(Markers.load(markers))
    assert scan_bundle(report, bundle) == verified.manifest.counts.events
    assert not (bundle / default_locator_map_path(bundle).name).exists()
    report.assert_clean()


def _letters(n: int) -> str:
    """``n`` written with the letters a to z only (probe keys must not hold digits, which
    ADR 0027 would mask anyway)."""
    return "".join(chr(97 + (n // 26**place) % 26) for place in (2, 1, 0))


_PROBE_SOURCES: list[dict[str, Any]] = [
    {
        "id": "src_probe_xml",
        "system": "sys_probes",
        "type": "upload",
        "config": {"kind": "log", "paths": ["probes/xml-*.xml"]},
        "parse": {
            "format": "xml",
            "timestamp_field": "timestamp",
            "timestamp_format": "iso8601",
            "timezone": "UTC",
        },
    },
    {
        "id": "src_probe_access",
        "system": "sys_probes",
        "type": "upload",
        "config": {"kind": "log", "paths": ["probes/access-*.log"]},
        "parse": {"format": "access_log", "timezone": "UTC"},
    },
    {
        "id": "src_probe_files",
        "system": "sys_probes",
        "type": "upload",
        "config": {"kind": "files", "paths": ["probes/drops/*.csv"]},
    },
]
"""Three upload sources neither scenario configures, for the template channels that carry
customer text without template mining (ADR 0039 amendment of 2026-11-02): an XML source
whose root element name is the template, an access-log source whose request line is, and a
file source whose file name is the ``file_arrived`` hint."""


def _plant_probes(sim: Path, layout: Layout, config: Path) -> dict[str, list[str]]:
    """Records that exercise the leak classes the simulator does not generate (review): a JSON
    map keyed by a value, maps keyed by names without digits (rare keys, and enough distinct keys
    to make the parent a map, ADR 0030), free text in a field, a one-off message, a message seen
    twice (the analyzer reads its input twice), a message repeated three times verbatim (ADR
    0068: identical repeats must not make its words template constants), a logfmt line whose
    message runs on unquoted, rare free text in an otherwise low-cardinality field, and the
    three template channels of :data:`_PROBE_SOURCES` (an XML root name, a route segment and
    a request method, a file name), each seen in two records: one short of the name gate's
    three, and the analyzer's second pass must not count them again. Writes the scenario's
    analyzer configuration plus the probe sources to ``config``. Returns the probe markers,
    which are scanned like the simulator's own."""
    ndjson = min(sim.glob(layout.ndjson_glob))
    logfmt = min(sim.glob(layout.logfmt_glob))
    day = ndjson.stem.removeprefix(layout.ndjson_prefix)
    probes = {
        "map_key": [f"mkprobe{n:03d}key" for n in range(3)],
        "rare_name_key": [f"mkprobekeyowner{_letters(n)}" for n in range(2)],
        "map_name_key": [f"mkprobemapowner{_letters(n)}" for n in range(300)],
        "free_text": [f"please call mrs mkprobe{n:03d}note back tomorrow" for n in range(3)],
        "one_off": ["mkprobeoneoffword"],
        "twice": ["mkprobetwiceword"],
        "thrice": ["mkprobethriceword"],
        "logfmt_words": ["mkprobebareword"],
        "rare_kept": [f"refund issued by mkprobe{n:03d}rare after review" for n in range(2)],
        "xml_root": ["mkprobexmlroot"],
        "route_word": ["mkprobepathword"],
        "method": ["mkprobemethod"],
        "file_name": ["mkprobefilename"],
    }
    _plant_channel_probes(sim, day)
    data = yaml.safe_load(layout.config.read_text(encoding="utf-8"))
    data["systems"].append(
        {"id": "sys_probes", "name": "Probes", "owner_group": "Tests", "criticality": "low"}
    )
    data["sources"].extend(_PROBE_SOURCES)
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8", newline=NL)
    lines: list[str] = []
    for n, key in enumerate(probes["map_key"]):
        stock = {key: n}
        lines.append(json.dumps({"ts": f"{day}T05:0{n}:00.000Z", "msg": "stock", "stock": stock}))
    for n, owner in enumerate(probes["rare_name_key"]):
        held = {"held_by": {owner: n + 1}}
        lines.append(json.dumps({"ts": f"{day}T04:0{n}:00.000Z", "msg": "hold", **held}))
    for n, owner in enumerate(probes["map_name_key"]):
        minute, second = divmod(n, 60)
        by_owner = {"stock_by_owner": {owner: n % 7, probes["map_name_key"][n - 1]: 1}}
        lines.append(
            json.dumps({"ts": f"{day}T07:{minute:02d}:{second:02d}.000Z", "msg": "own", **by_owner})
        )
    for n, note in enumerate(probes["free_text"]):
        lines.append(json.dumps({"ts": f"{day}T05:1{n}:00.000Z", "msg": "note", "note": note}))
    lines.append(
        json.dumps({"ts": f"{day}T05:20:00.000Z", "msg": "gate code mkprobeoneoffword today"})
    )
    for twice_minute in ("21", "22"):
        message = "parcel left with neighbour mkprobetwiceword"
        lines.append(json.dumps({"ts": f"{day}T05:{twice_minute}:00.000Z", "msg": message}))
    for thrice_minute in ("23", "24", "25"):
        message = "refund approved for mkprobethriceword by manager"
        lines.append(json.dumps({"ts": f"{day}T05:{thrice_minute}:00.000Z", "msg": message}))
    for n in range(60):
        detail = probes["rare_kept"][n // 30] if n % 30 == 7 else "ok"
        lines.append(
            json.dumps({"ts": f"{day}T06:{n:02d}:00.000Z", "msg": "callback", "callback": detail})
        )
    with ndjson.open("a", encoding="utf-8", newline=NL) as handle:
        handle.writelines(line + NL for line in lines)
    with logfmt.open("a", encoding="utf-8", newline=NL) as handle:
        prefix = layout.logfmt_prefix.format(day=day)
        handle.write(f"{prefix} msg=refund approved for mkprobebareword order_id=4471{NL}")
    return probes


def _plant_channel_probes(sim: Path, day: str) -> None:
    """The inputs of :data:`_PROBE_SOURCES` under ``<sim>/probes``: two XML documents whose
    root element carries a marker, two access-log lines whose route segment carries one and
    two more with a marker as the request method (on another route: a word three distinct
    records carry is trusted, which is the rule, not a leak), and two files whose names
    generalize to the same ``file_arrived`` hint, stamped on ``day`` like the simulator's own
    drops."""
    probes = sim / "probes"
    drops = probes / "drops"
    drops.mkdir(parents=True)
    stamp = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    xml = "".join(
        f"<mkprobexmlroot><timestamp>{day}T05:3{n}:00Z</timestamp><status>OK</status>"
        f"</mkprobexmlroot>{NL}"
        for n in range(2)
    )
    (probes / f"xml-{day}.xml").write_text(xml, encoding="utf-8", newline=NL)
    clf_day = stamp.strftime("%d/%b/%Y")
    requests = ["GET /customers/mkprobepathword"] * 2 + ["MKPROBEMETHOD /health"] * 2
    access = "".join(
        f'203.0.113.9 - - [{clf_day}:05:4{n}:00 +0000] "{request} HTTP/1.1" 200 12{NL}'
        for n, request in enumerate(requests)
    )
    (probes / f"access-{day}.log").write_text(access, encoding="utf-8", newline=NL)
    for n in range(2):
        path = drops / f"DROP_mkprobefilename_{n}.csv"
        path.write_text(f"x{NL}", encoding="utf-8", newline=NL)
        arrived = (stamp + timedelta(hours=5, minutes=50 + n)).timestamp()
        os.utime(path, (arrived, arrived))
