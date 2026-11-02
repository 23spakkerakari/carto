"""Scenario C ("people_ops") with faults on: native formats, faults, keys, ground truth
consistency, markers and the analyzer configuration (spec 19 scenario C, plan M2 package H)."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from carto_edge.config import SourceType, load_sources_file
from carto_simulator import clock
from carto_simulator.api import GenerationRequest, generate
from carto_simulator.ground_truth import (
    ActorKind,
    EventTruth,
    FieldRef,
    GroundTruth,
    iter_events,
    read_ground_truth,
)
from carto_simulator.names import FIRST_NAMES, LAST_NAMES
from carto_simulator.scenarios import get_scenario, list_scenarios
from carto_simulator.scenarios.people_ops_model import (
    HR_CLERKS,
    PAYROLL_CLERKS,
    PRODUCT_CLERKS,
    SOURCES,
    SRC_HRIS,
    SRC_LEGACY,
    SRC_LMS,
    SRC_ONBOARDING,
    SRC_PAYROLL,
    SRC_SKU_EXPORT,
    SRC_SKU_MASTER,
)
from carto_simulator.scenarios.people_ops_truth import AgreementRates, build_links

ROOT = Path(__file__).resolve().parents[2]
ANALYZE_FILE = ROOT / "simulator" / "analyze.people_ops.yaml"
START = date(2026, 9, 23)
DAYS = 28
SCENARIO = "people_ops"
LOGFMT_PAIR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')
LOGFMT_LINE = re.compile(
    r'^[A-Za-z_][A-Za-z0-9_]*=(?:"(?:[^"\\]|\\.)*"|[^\s"]*)'
    r'(?:\s[A-Za-z_][A-Za-z0-9_]*=(?:"(?:[^"\\]|\\.)*"|[^\s"]*))*$'
)
MARKER = re.compile(r"mk[0-9a-f]{8}")
LONDON = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class Run:
    out_dir: Path
    truth: GroundTruth
    events: list[EventTruth]

    def read(self, relative: str) -> str:
        return (self.out_dir / relative).read_text(encoding="utf-8")

    def lines(self, relative: str) -> list[str]:
        return self.read(relative).splitlines()

    def files(self, pattern: str) -> list[Path]:
        return sorted(self.out_dir.glob(pattern))

    def native_text(self) -> str:
        parts = [
            path.read_text(encoding="utf-8")
            for path in sorted(self.out_dir.rglob("*"))
            if path.is_file() and "ground_truth" not in path.parts and path.name != "README.md"
        ]
        return "\n".join(parts)

    def by_node(self) -> dict[str, list[EventTruth]]:
        grouped: dict[str, list[EventTruth]] = defaultdict(list)
        for event in self.events:
            grouped[event.node].append(event)
        return grouped

    def by_source(self, source_id: str) -> list[EventTruth]:
        return [e for e in self.events if e.source_id == source_id]


def day(number: int) -> date:
    return START + timedelta(days=number - 1)


def local_utc(number: int, at: time) -> datetime:
    return clock.to_utc(clock.local_wall(day(number), at))


def request(**overrides: object) -> GenerationRequest:
    base: dict[str, object] = {
        "scenario": SCENARIO,
        "days": DAYS,
        "seed": 5,
        "daily_volume": 200,
        "start_date": START,
        "noise_rate": 0.2,
    }
    base.update(overrides)
    return GenerationRequest(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    out_dir = tmp_path_factory.mktemp("people_ops")
    generate(request(), out_dir)
    return Run(out_dir, read_ground_truth(out_dir), list(iter_events(out_dir)))


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def payroll_rows(run: Run) -> list[dict[str, str]]:
    return [row for path in run.files("payroll/payroll_*.csv") for row in csv_rows(path)]


def hris_rows(run: Run) -> list[dict[str, str]]:
    return csv_rows(run.out_dir / "hris/employees.csv")


# ---------------------------------------------------------------------------------------------
# Registry and determinism
# ---------------------------------------------------------------------------------------------


def test_registry_lists_both_scenarios() -> None:
    assert list_scenarios() == ["people_ops", "shop"]
    scenario = get_scenario(SCENARIO)
    assert scenario.name == SCENARIO and "manual" in scenario.description
    with pytest.raises(KeyError, match="known scenarios: people_ops, shop"):
        get_scenario("payer")


def test_same_request_is_byte_identical_and_another_seed_differs(tmp_path: Path) -> None:
    small = request(days=5, daily_volume=60)
    a = generate(small, tmp_path / "a").manifest
    b = generate(small, tmp_path / "b").manifest
    assert a == b
    assert a.sha256
    events_a = (tmp_path / "a/ground_truth/event_txn.ndjson").read_bytes()
    events_b = (tmp_path / "b/ground_truth/event_txn.ndjson").read_bytes()
    assert events_a == events_b
    for relative in ("ground_truth/links.json", "ground_truth/markers.json", "README.md"):
        assert (tmp_path / f"a/{relative}").read_bytes() == (
            tmp_path / f"b/{relative}"
        ).read_bytes()
    other = generate(request(days=5, daily_volume=60, seed=6), tmp_path / "c").manifest
    assert other.sha256 != a.sha256


def test_three_day_run_is_small_and_complete(tmp_path: Path) -> None:
    """The CI-speed case: every source that can exist in three days does, and the truth loads."""
    manifest = generate(request(days=3, daily_volume=800, noise_rate=0.1), tmp_path).manifest
    assert manifest.counts["transactions"] > 100
    for source_id in (SRC_ONBOARDING, SRC_LEGACY, SRC_HRIS, SRC_LMS, SRC_SKU_EXPORT):
        assert manifest.counts[source_id] > 0, source_id
    assert manifest.counts[SRC_SKU_MASTER] == 0, "master rows are keyed the week after"
    truth = read_ground_truth(tmp_path)
    assert len(truth.links) == 14 and truth.batches == []
    assert sum(1 for _ in iter_events(tmp_path)) == sum(
        manifest.counts[s.source_id] for s in truth.sources
    )


# ---------------------------------------------------------------------------------------------
# Native formats (spec 8.2)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", ["onboarding/onboarding-*.ndjson", "lms/lms-*.ndjson"])
def test_json_logs_are_utc_and_one_file_per_day(run: Run, pattern: str) -> None:
    files = run.files(pattern)
    assert files
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["ts"].endswith("Z")
            parsed = datetime.fromisoformat(record["ts"])
            assert parsed.tzinfo == UTC
            assert parsed.date().isoformat() in path.name
            assert record["level"] == "info"


def test_legacy_log_is_logfmt_with_other_names_and_london_time(run: Run) -> None:
    files = run.files("onboarding_legacy/legacy-hires-*.log")
    assert files
    observed = {e.key: e.observed_at for e in run.by_source(SRC_LEGACY)}
    first_days = 0
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            assert LOGFMT_LINE.match(line), line
            pairs = dict(LOGFMT_PAIR.findall(line))
            assert pairs["lvl"] == "INFO" and set(pairs) & {"candidate_id", "ts", "level"} == set()
            stamp = pairs["time"].strip('"')
            local = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LONDON)
            assert local.date().isoformat() == path.name[13:23]
            key = f"{SRC_LEGACY}:{path.name}:line:{number}"
            assert local.astimezone(UTC) == observed[key]
            if pairs["msg"] == '"first day"':
                first_days += 1
                assert re.fullmatch(r"L\d{6}", pairs["cand_ref"])
                day_, month, year = pairs["startDt"].split("/")
                assert date(int(year), int(month), int(day_)).isoformat()
                assert pairs["mail"].endswith("@example.com") and "+" in pairs["mail"]
                assert pairs["applicant"].startswith('"')
                assert time(9, 0) <= local.time() <= time(10, 1)
    assert first_days == len(run.by_node()["onboarding_legacy:first_day"]) > 0


def test_main_onboarding_lines_carry_the_composite_and_bridge_fields(run: Run) -> None:
    started = [
        json.loads(line)
        for path in run.files("onboarding/onboarding-*.ndjson")
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"onboarding started"' in line
    ]
    assert len(started) == len(run.by_node()["onboarding:onboarding_started"]) > 0
    for record in started:
        assert re.fullmatch(r"C-\d{7}", record["candidate_id"])
        assert record["work_email"].endswith("@example.com")
        assert date.fromisoformat(record["start_date"]).weekday() < 5
        assert record["candidate_name"].count(" ") == 1
    applications = [
        json.loads(line)
        for path in run.files("onboarding/onboarding-*.ndjson")
        for line in path.read_text(encoding="utf-8").splitlines()
        if '"application received"' in line
    ]
    with_name = sum("candidate_name" in r for r in applications)
    assert 0 < with_name < len(applications), "pii_density applies to the application line"


def test_hris_sql_seed_executes_in_sqlite_and_matches_rows_and_keys(run: Run) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(run.read("hris/employees.sql"))
    count = connection.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    rows = hris_rows(run)
    events = run.by_source(SRC_HRIS)
    assert count == len(rows) == len(events) == run.truth.manifest.counts[SRC_HRIS] > 0
    ids = [row[0] for row in connection.execute("SELECT id FROM employees ORDER BY id")]
    assert ids == list(range(1, count + 1))
    assert sorted(int(e.key.rsplit(":", 1)[1]) for e in events) == ids
    assert list(rows[0]) == [
        "id", "employee_id", "full_name", "start_date", "source_ref", "department",
        "created_by", "created_at",
    ]  # fmt: skip
    for row in rows:
        assert re.fullmatch(r"E-\d{5}", row["employee_id"])
        assert row["source_ref"] == "" or re.fullmatch(r"C-\d{7}|L\d{6}", row["source_ref"])
    blank = sum(row["source_ref"] == "" for row in rows) / len(rows)
    assert 0.25 <= blank <= 0.55, "about 40% of source_ref values are blank"


def test_sku_sheets_and_master(run: Run) -> None:
    sheets = run.files("sku_sheets/sku_sheet_*.csv")
    assert len(sheets) == 4, "one export per Friday inside 28 days"
    for path in sheets:
        assert (
            date(int(path.name[10:14]), int(path.name[14:16]), int(path.name[16:18])).weekday() == 4
        )
        rows = csv_rows(path)
        assert rows and list(rows[0]) == [
            "legacy_sku",
            "description",
            "unit_price",
            "category",
            "added_at",
        ]
        for row in rows:
            assert re.fullmatch(r"\d{6}", row["legacy_sku"])
            assert re.fullmatch(r"\d+\.\d{2}", row["unit_price"])
    connection = sqlite3.connect(":memory:")
    connection.executescript(run.read("sku_master/sku_master.sql"))
    masters = csv_rows(run.out_dir / "sku_master/sku_master.csv")
    assert connection.execute("SELECT COUNT(*) FROM sku_master").fetchone()[0] == len(masters)
    assert len(masters) == run.truth.manifest.counts[SRC_SKU_MASTER] > 0
    assert all(re.fullmatch(r"[A-Z]{3}-\d{5}", row["sku"]) for row in masters)
    assert "legacy_sku" not in masters[0], "the master never carries the sheet's key"
    sheet_rows = [row for path in sheets for row in csv_rows(path)]
    prices = Counter(row["unit_price"] for row in sheet_rows)
    assert all(prices[row["unit_price"]] >= 1 for row in masters), "prices are copied exactly"
    exact = sum(row["description"] in {s["description"] for s in sheet_rows} for row in masters)
    assert 0.85 <= exact / len(masters) < 1.0, "3% description typos"


# ---------------------------------------------------------------------------------------------
# Timings, manual hops, faults
# ---------------------------------------------------------------------------------------------


def test_hris_and_payroll_rows_are_human_in_business_hours_after_the_start(run: Run) -> None:
    zone = clock.local_zone()
    started = {e.txn_id: e.observed_at for e in run.by_node()["onboarding:onboarding_started"]}
    started.update({e.txn_id: e.observed_at for e in run.by_node()["onboarding_legacy:first_day"]})
    hris = run.by_node()["hris:employee_created"]
    assert hris
    hris_at: dict[str, datetime] = {}
    for event in hris:
        assert event.actor_kind == ActorKind.HUMAN and event.txn_id is not None
        local = clock.to_local(event.observed_at, zone)
        assert local.weekday() < 5 and 8 <= local.hour < 18
        hris_at[event.txn_id] = event.observed_at
        start = started[event.txn_id]
        assert timedelta(hours=12) < event.observed_at - start < timedelta(days=5)
    for event in run.by_node()["payroll:worker_entered"]:
        assert event.actor_kind == ActorKind.HUMAN and event.txn_id is not None
        local = clock.to_local(event.observed_at, zone)
        assert local.weekday() < 5 and 8 <= local.hour < 18
        assert event.observed_at > hris_at[event.txn_id]
    for event in run.by_node()["lms:enrolled"]:
        assert event.actor_kind == ActorKind.SERVICE and event.txn_id is not None
        assert (
            timedelta(minutes=2)
            <= event.observed_at - started[event.txn_id]
            <= timedelta(minutes=30)
        )
    actors = {row["created_by"] for row in hris_rows(run)}
    assert actors == set(HR_CLERKS)
    assert {row["entered_by"] for row in payroll_rows(run)} == set(PAYROLL_CLERKS)
    masters = csv_rows(run.out_dir / "sku_master/sku_master.csv")
    assert {row["created_by"] for row in masters} == set(PRODUCT_CLERKS)


def test_manual_hops(run: Run) -> None:
    hops = {h.hop_id: h for h in run.truth.manual_hops}
    assert list(hops) == [f"H{n:02d}" for n in range(1, 11)]
    for hop_id in ("H01", "H02", "H03", "H04"):
        assert hops[hop_id].manual and hops[hop_id].share_manual == 1.0
        assert hops[hop_id].actors and hops[hop_id].typo_rate > 0
    assert hops["H03"].typo_rate == 0.02 and hops["H03"].actors == list(PAYROLL_CLERKS)
    assert hops["H04"].entity_id == "ent_sku"
    assert all(not hops[h].manual and hops[h].share_manual == 0.0 for h in list(hops)[4:])
    nodes = {e.node for e in run.events}
    for hop in hops.values():
        assert hop.from_node in nodes and hop.to_node in nodes, hop.hop_id


def test_week_3_payroll_backlog(run: Run) -> None:
    names = {path.name for path in run.files("payroll/payroll_*.csv")}
    for number in (13, 14, 15):
        assert f"payroll_{day(number):%Y%m%d}.csv" not in names, "no export on the backlog days"
    for number in (10, 16, 17):  # Friday before, Thursday and Friday of week 3
        assert f"payroll_{day(number):%Y%m%d}.csv" in names
    fault = next(f for f in run.truth.faults if f.fault_id == "c1_payroll_backlog_week3")
    assert fault.kind.value == "partial_stall" and fault.expected_alert
    assert fault.expected_alert_kind == "hop_deadline"
    assert fault.start == local_utc(13, time(8))
    assert fault.end is not None and clock.to_local(fault.end).date() == day(17)
    assert fault.attributes["hop"] == "H03"
    entered = {e.txn_id: e.observed_at for e in run.by_node()["payroll:worker_entered"]}
    hris = {e.txn_id: e.observed_at for e in run.by_node()["hris:employee_created"]}
    assert fault.affected_txn_ids
    for txn_id in fault.affected_txn_ids:
        assert clock.to_local(entered[txn_id]).date() == day(17)
        assert entered[txn_id] - hris[txn_id] > timedelta(days=2)
    landed = csv_rows(run.out_dir / f"payroll/payroll_{day(17):%Y%m%d}.csv")
    assert len(landed) > len(fault.affected_txn_ids), "day 17's file also holds its own rows"
    window = local_utc(13, time(0)), local_utc(16, time(0))
    assert not any(window[0] <= at < window[1] for at in entered.values())


def test_one_percent_never_reach_payroll(run: Run) -> None:
    fault = next(f for f in run.truth.faults if f.fault_id == "c2_payroll_never_reached")
    assert fault.expected_alert is False and fault.expected_alert_kind is None
    assert fault.end is None and fault.start == local_utc(1, time(0))
    paid = {e.txn_id for e in run.by_node()["payroll:worker_entered"]}
    hired = {e.txn_id for e in run.by_node()["hris:employee_created"]}
    assert fault.affected_txn_ids
    assert set(fault.affected_txn_ids) <= hired
    assert not set(fault.affected_txn_ids) & paid
    assert len(fault.affected_txn_ids) / len(hired) < 0.04
    assert [f.fault_id for f in run.truth.faults] == [
        "c1_payroll_backlog_week3",
        "c2_payroll_never_reached",
    ]


def test_no_faults_means_no_backlog_and_no_drops(tmp_path: Path) -> None:
    generate(request(faults=False, daily_volume=80), tmp_path)
    truth = read_ground_truth(tmp_path)
    assert truth.faults == []
    names = {path.name for path in sorted(tmp_path.glob("payroll/payroll_*.csv"))}
    assert {f"payroll_{day(n):%Y%m%d}.csv" for n in (13, 14, 15)} <= names
    events = list(iter_events(tmp_path))
    hired = {e.txn_id for e in events if e.node == "hris:employee_created"}
    paid = {e.txn_id for e in events if e.node == "payroll:worker_entered"}
    cutoff = local_utc(22, time(0))
    early = {
        e.txn_id for e in events if e.node == "hris:employee_created" and e.observed_at < cutoff
    }
    assert early <= paid, "every early HRIS row reaches payroll when faults are off"
    assert hired >= paid


# ---------------------------------------------------------------------------------------------
# Ground truth consistency (ADR 0006)
# ---------------------------------------------------------------------------------------------


def test_every_event_key_resolves(run: Run) -> None:
    per_file: dict[tuple[str, str], list[int]] = defaultdict(list)
    row_keys: dict[str, set[str]] = defaultdict(set)
    for event in run.events:
        source_id, locator = event.key.split(":", 1)
        assert source_id == event.source_id
        if event.source_id in (SRC_HRIS, SRC_PAYROLL, SRC_SKU_EXPORT, SRC_SKU_MASTER):
            table, marker, pk = locator.split(":")
            assert marker == "row"
            assert table == {
                SRC_HRIS: "employees", SRC_PAYROLL: "workers",
                SRC_SKU_EXPORT: "sku_sheet", SRC_SKU_MASTER: "sku_master",
            }[event.source_id]  # fmt: skip
            assert pk not in row_keys[event.source_id]
            row_keys[event.source_id].add(pk)
        else:
            name, _, number = locator.rsplit(":", 2)
            per_file[(event.source_id, name)].append(int(number))
    source_dirs = {s.source_id: s.path for s in run.truth.sources}
    for (source_id, name), numbers in per_file.items():
        count = len(run.lines(f"{source_dirs[source_id]}/{name}"))
        assert sorted(numbers) == list(range(1, count + 1)), (source_id, name)
    log_files = {
        (s.source_id, p.name)
        for s in run.truth.sources
        if s.kind.value == "log_file"
        for p in run.files(f"{s.path}/*")
    }
    assert set(per_file) == log_files
    assert row_keys[SRC_HRIS] == {row["id"] for row in hris_rows(run)}
    assert row_keys[SRC_PAYROLL] == {row["worker_number"] for row in payroll_rows(run)}
    sheet = {row["legacy_sku"] for p in run.files("sku_sheets/*.csv") for row in csv_rows(p)}
    assert row_keys[SRC_SKU_EXPORT] == sheet
    masters = csv_rows(run.out_dir / "sku_master/sku_master.csv")
    assert row_keys[SRC_SKU_MASTER] == {row["id"] for row in masters}


def test_manifest_counts_hashes_and_volumes(run: Run) -> None:
    manifest = run.truth.manifest
    counts = Counter(e.source_id for e in run.events)
    for source in run.truth.sources:
        assert manifest.counts[source.source_id] == counts[source.source_id]
    assert manifest.counts["events"] == sum(1 for e in run.events if e.txn_id is not None)
    assert manifest.counts["noise"] == sum(1 for e in run.events if e.node.endswith(":noise"))
    assert manifest.counts["files"] == len(manifest.sha256)
    for relative, digest in manifest.sha256.items():
        assert hashlib.sha256((run.out_dir / relative).read_bytes()).hexdigest() == digest
    assert "ground_truth/manifest.json" not in manifest.sha256
    assert manifest.scenario == SCENARIO and manifest.seed == 5 and manifest.faults_enabled
    # daily_volume 200: 10 main (14, 12, 10, 8, 6 = 50 per week) and 3 legacy (4, 4, 3, 2, 2 = 15)
    # hires per weekday; 2 SKUs per weekday, exported on the 4 Fridays (3 + 5 + 5 + 5 days).
    started = run.by_node()["onboarding:onboarding_started"]
    assert len(started) == 4 * 50
    assert len(run.by_node()["onboarding_legacy:first_day"]) == 4 * 15
    assert len(run.by_node()["sku_sheets:sku_added"]) == 2 * 18
    assert manifest.counts["transactions"] == 4 * 65 + 2 * 18
    per_weekday = Counter(clock.to_local(e.observed_at).weekday() for e in started)
    assert [per_weekday[d] for d in range(5)] == [56, 48, 40, 32, 24]
    assert set(per_weekday) == set(range(5))


def test_noise_events_carry_no_transaction(run: Run) -> None:
    noise = [e for e in run.events if e.node.endswith(":noise")]
    assert noise
    assert all(e.txn_id is None and e.batch_id is None and e.actor_kind is None for e in noise)
    assert {e.node for e in noise} == {"onboarding:noise", "onboarding_legacy:noise", "lms:noise"}
    keys = [(e.observed_at, e.key) for e in run.events]
    assert keys == sorted(keys)
    assert all(
        local_utc(1, time(0)) <= e.observed_at < local_utc(DAYS + 1, time(0)) for e in run.events
    )


def test_links_entities_and_composite_rates(run: Run) -> None:
    links = {link.link_id: link for link in run.truth.links}
    assert list(links) == [f"C{n:02d}" for n in range(1, 15)]
    assert {links[c].link_type for c in ("C01", "C02", "C06", "C09", "C10")} == {"exact"}
    assert {links[c].link_type for c in ("C03", "C07", "C11", "C12", "C13")} == {"bridge"}
    assert all(links[c].direction == "undirected" for c in ("C03", "C07", "C11", "C12", "C13"))
    for link_id in ("C04", "C05", "C08", "C14"):
        link = links[link_id]
        assert link.link_type == "composite" and link.manual and link.components
        assert link.components[0].a == link.a and link.components[0].form_a == link.form_a
    rates = {(c.a.field, c.form_a): c.agreement_rate for c in links["C08"].components}
    assert rates[("start_date", "date")] == 1.0
    assert (
        rates[("full_name", "phonetic.0")] is not None
        and 0.9 <= rates[("full_name", "phonetic.0")] < 1.0
    )
    assert (
        rates[("employee_id", "digits.0")] is not None
        and 0.94 <= rates[("employee_id", "digits.0")] < 1.0
    )
    for link_id in ("C04", "C05"):
        name_rate = links[link_id].components[0].agreement_rate
        assert name_rate is not None and 0.9 <= name_rate < 1.0
        assert links[link_id].components[1].agreement_rate == 1.0
    sku = {(c.a.field, c.form_a): c.agreement_rate for c in links["C14"].components}
    assert sku[("unit_price", "amount")] == 1.0
    assert sku[("description", "alnum")] == sku[("description", "phonetic.0")]
    assert sku[("description", "alnum")] is not None and 0.85 <= sku[("description", "alnum")] < 1.0
    assert links["C01"].manual and links["C06"].manual and not links["C09"].manual
    entities = {e.entity_id: e for e in run.truth.entities}
    assert set(entities) == {"ent_employee", "ent_sku"}
    assert len(entities["ent_employee"].fields) == 14 and len(entities["ent_sku"].fields) == 4
    members = {
        (f.ref.system_id, f.ref.source_id, f.ref.field, f.form)
        for e in entities.values()
        for f in e.fields
    }
    for link in links.values():
        if link.link_type != "composite":
            assert (link.a.system_id, link.a.source_id, link.a.field, link.form_a) in members
            assert (link.b.system_id, link.b.source_id, link.b.field, link.form_b) in members
    assert run.truth.batches == []
    assert {e.entity_id for e in run.truth.entities} >= {link.entity_id for link in links.values()}


def test_link_fields_exist_in_the_data(run: Run) -> None:
    """Every field a link names is present in the native data of its source."""
    present: dict[str, set[str]] = defaultdict(set)
    for path in run.files("onboarding/*.ndjson") + run.files("lms/*.ndjson"):
        source = SRC_ONBOARDING if path.parent.name == "onboarding" else SRC_LMS
        for line in path.read_text(encoding="utf-8").splitlines()[:400]:
            present[source].update(json.loads(line))
    for path in run.files("onboarding_legacy/*.log"):
        for line in path.read_text(encoding="utf-8").splitlines()[:400]:
            present[SRC_LEGACY].update(dict(LOGFMT_PAIR.findall(line)))
    present[SRC_HRIS].update(hris_rows(run)[0])
    present[SRC_PAYROLL].update(payroll_rows(run)[0])
    present[SRC_SKU_EXPORT].update(csv_rows(run.files("sku_sheets/*.csv")[0])[0])
    present[SRC_SKU_MASTER].update(csv_rows(run.out_dir / "sku_master/sku_master.csv")[0])
    for link in run.truth.links:
        for ref in (link.a, link.b):
            assert ref.field in present[ref.source_id], (link.link_id, str(ref))
        for component in link.components:
            assert component.a.field in present[component.a.source_id]
            assert component.b.field in present[component.b.source_id]
    for entity in run.truth.entities:
        for member in entity.fields:
            assert member.ref.field in present[member.ref.source_id], str(member.ref)


def test_markers_cover_every_pii_value_and_stay_out_of_the_truth(run: Run) -> None:
    markers = run.truth.markers
    assert markers.marker_tokens == sorted(set(markers.marker_tokens))
    assert all(re.fullmatch(r"mk[0-9a-f]{8}", token) for token in markers.marker_tokens)
    native = run.native_text()
    assert set(MARKER.findall(native)) == set(markers.marker_tokens)
    pii_text = "\n".join(markers.pii_values)
    assert all(token in pii_text for token in markers.marker_tokens)
    assert all(value in native for value in markers.pii_values)
    assert all(value in native for value in markers.amount_values)
    for key, values in markers.identifier_values.items():
        assert values == sorted(set(values))
        assert all(value in native for value in values[:100]), key
    # every name, email and username in the data is listed
    for row in hris_rows(run):
        assert row["full_name"] in markers.pii_values
    for row in payroll_rows(run):
        assert row["legal_name"] in markers.pii_values
    for path in run.files("lms/*.ndjson"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if "username" in record:
                assert record["username"] in markers.pii_values
                assert record["user_email"] in markers.pii_values
    for path in run.files("onboarding_legacy/*.log"):
        for line in path.read_text(encoding="utf-8").splitlines():
            pairs = dict(LOGFMT_PAIR.findall(line))
            if "applicant" in pairs:
                assert json.loads(pairs["applicant"]) in markers.pii_values
            if "mail" in pairs:
                assert pairs["mail"] in markers.pii_values
    assert set(markers.actor_values) == set(HR_CLERKS) | set(PAYROLL_CLERKS) | set(PRODUCT_CLERKS)
    truth_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((run.out_dir / "ground_truth").glob("*"))
        if path.name != "markers.json"
    )
    assert not MARKER.findall(truth_text)
    assert not any(name in truth_text for name in FIRST_NAMES + LAST_NAMES)
    events_text = run.read("ground_truth/event_txn.ndjson")
    assert all(value not in events_text for value in markers.pii_values[:200])


def test_identifier_values_are_keyed_by_field_ref(run: Run) -> None:
    values = run.truth.markers.identifier_values
    expected = {
        str(FieldRef(system_id=s, source_id=src, field=f))
        for s, src, f in [
            ("sys_onboarding", SRC_ONBOARDING, "candidate_id"),
            ("sys_onboarding_legacy", SRC_LEGACY, "cand_ref"),
            ("sys_hris", SRC_HRIS, "employee_id"),
            ("sys_hris", SRC_HRIS, "source_ref"),
            ("sys_payroll", SRC_PAYROLL, "worker_number"),
            ("sys_payroll", SRC_PAYROLL, "employee_ref"),
            ("sys_lms", SRC_LMS, "username"),
            ("sys_sku_sheets", SRC_SKU_EXPORT, "legacy_sku"),
            ("sys_sku_sheets", SRC_SKU_EXPORT, "description"),
            ("sys_sku_master", SRC_SKU_MASTER, "sku"),
            ("sys_sku_master", SRC_SKU_MASTER, "description"),
        ]
    }
    assert set(values) == expected
    assert values["sys_hris/src_hris_db/employee_id"][0] == "E-40210"
    assert values["sys_payroll/src_payroll_csv/worker_number"][0] == "W-300118"
    assert values["sys_onboarding/src_onboarding_log/candidate_id"][0] == "C-1000481"
    assert values["sys_sku_sheets/src_sku_export_csv/legacy_sku"][0] == "500100"
    assert "" not in values["sys_hris/src_hris_db/source_ref"]


def test_typo_shares(run: Run) -> None:
    employee_of = {
        e.txn_id: e.key.rsplit(":", 1)[1] for e in run.by_node()["hris:employee_created"]
    }
    hris_by_id = {row["id"]: row for row in hris_rows(run)}
    txn_of_worker = {
        e.key.rsplit(":", 1)[1]: e.txn_id for e in run.by_node()["payroll:worker_entered"]
    }
    rows = payroll_rows(run)
    ref_typos = name_typos = 0
    for row in rows:
        txn_id = txn_of_worker[row["worker_number"]]
        assert txn_id is not None
        hris = hris_by_id[employee_of[txn_id]]
        assert row["start_date"] == hris["start_date"]
        ref_typos += row["employee_ref"] != hris["employee_id"]
        name_typos += row["legal_name"] != hris["full_name"]
    assert 0 < ref_typos / len(rows) <= 0.05
    assert 0 < name_typos / len(rows) <= 0.07
    assert run.read("README.md").startswith("# Scenario C (people_ops)")
    readme = run.read("README.md")
    assert "c1_payroll_backlog_week3" in readme and f"| {day(13):%Y-%m-%d} 08:00 to" in readme


# ---------------------------------------------------------------------------------------------
# simulator/analyze.people_ops.yaml (spec 4.1, 8.1.1, 8.3 pins)
# ---------------------------------------------------------------------------------------------


def test_analyze_config_names_the_ground_truth_sources_and_matches_the_files(run: Run) -> None:
    config = load_sources_file(ANALYZE_FILE)
    assert sorted(s.id for s in config.sources) == sorted(s.source_id for s in SOURCES)
    assert sorted(s.id for s in config.systems) == sorted({s.system_id for s in SOURCES})
    truth_system = {s.source_id: s.system_id for s in SOURCES}
    truth_names = {s.system_id: s.system_name for s in SOURCES}
    truth_zone = {s.source_id: s.timezone for s in SOURCES}
    for source in config.sources:
        assert source.system == truth_system[source.id] and source.enabled
        assert source.type is SourceType.UPLOAD and source.secret_ref is None
        assert source.parse.timezone == truth_zone[source.id], source.id
        for pattern in source.config["paths"]:
            assert not Path(pattern).is_absolute() and ".." not in pattern
            matched = list(run.out_dir.glob(pattern))
            assert matched, f"{source.id}: {pattern} matches nothing under the run"
            assert all(p.is_file() and "ground_truth" not in p.parts for p in matched)
    for system in config.systems:
        assert system.name == truth_names[system.id] and system.owner_group
    by_id = {source.id: source for source in config.sources}
    rows = {
        SRC_HRIS: ("employees", "id", "created_at", "created_by"),
        SRC_PAYROLL: ("workers", "worker_number", "entered_at", "entered_by"),
        SRC_SKU_EXPORT: ("sku_sheet", "legacy_sku", "added_at", None),
        SRC_SKU_MASTER: ("sku_master", "id", "created_at", "created_by"),
    }
    for source_id, (table, pk, stamp, actor) in rows.items():
        cfg = by_id[source_id].config
        assert cfg["kind"] == "rows" and cfg["table"] == table and cfg["primary_key"] == pk
        assert cfg["timestamp_column"] == stamp and cfg.get("actor_column") == actor
        assert by_id[source_id].parse.format.value == "csv"
        assert by_id[source_id].parse.timestamp_format == "%Y-%m-%d %H:%M:%S"
        assert by_id[source_id].parse.actor_field == actor
    for source_id in (SRC_ONBOARDING, SRC_LMS):
        assert by_id[source_id].parse.format.value == "ndjson"
        assert by_id[source_id].parse.timestamp_field == "ts"
    legacy = by_id[SRC_LEGACY].parse
    assert legacy.format.value == "logfmt" and legacy.timestamp_field == "time"
    assert legacy.severity_field == "lvl" and legacy.timestamp_format == "%Y-%m-%d %H:%M:%S"
    assert by_id[SRC_LMS].parse.actor_field == "actor"


def test_analyze_config_pins_the_composite_and_email_fields() -> None:
    config = load_sources_file(ANALYZE_FILE)
    assert config.policy_version
    pins = {pin.field: pin for pin in config.field_policies}
    for field in (
        "sys_onboarding/*/candidate_name",
        "sys_onboarding_legacy/*/applicant",
        "sys_hris/*/full_name",
        "sys_payroll/*/legal_name",
    ):
        assert pins[field].field_class == "person_name" and pins[field].policy == "tokenize"
        assert pins[field].forms == ["phonetic"] and pins[field].reason
    for field in (
        "sys_onboarding/*/work_email",
        "sys_onboarding_legacy/*/mail",
        "sys_lms/*/user_email",
        "sys_lms/*/username",
    ):
        assert pins[field].field_class == "contact" and pins[field].policy == "tokenize"
        assert pins[field].forms == ["raw"]
    for field in ("sys_sku_sheets/*/description", "sys_sku_master/*/description"):
        assert pins[field].policy == "tokenize" and pins[field].forms == ["alnum", "phonetic"]
    assert not any(pin.policy == "keep" for pin in pins.values())
    # every pinned field name exists in the truth's links or entities
    rates = AgreementRates(1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    truth_fields = {
        f"{ref.system_id}/*/{ref.field}" for link in build_links(rates) for ref in (link.a, link.b)
    }
    assert set(pins) <= truth_fields
