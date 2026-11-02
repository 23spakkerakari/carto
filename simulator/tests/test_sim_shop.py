"""Scenario A with faults on: native formats, faults, keys, ground truth consistency (spec 19)."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

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
from carto_simulator.names import FIRST_NAMES, LAST_NAMES, SERVICE_ACCOUNT
from carto_simulator.scenarios.shop_model import CounterBases

START = date(2026, 9, 23)
DAYS = 14
SEED = 7
LOGFMT_PAIR = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|[^\s"]*)')
LOGFMT_LINE = re.compile(
    r'^[A-Za-z_][A-Za-z0-9_]*=(?:"(?:[^"\\]|\\.)*"|[^\s"]*)'
    r'(?:\s[A-Za-z_][A-Za-z0-9_]*=(?:"(?:[^"\\]|\\.)*"|[^\s"]*))*$'
)
TEXT_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (INFO|ERROR) \S.*$")
MARKER = re.compile(r"mk[0-9a-f]{8}")  # case-sensitive: tokens are embedded unchanged


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


def day(number: int) -> date:
    return START + timedelta(days=number - 1)


def local_utc(number: int, at: time) -> datetime:
    return clock.to_utc(clock.local_wall(day(number), at))


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> Run:
    out_dir = tmp_path_factory.mktemp("shop")
    request = GenerationRequest(
        days=DAYS, seed=SEED, daily_volume=40, start_date=START, noise_rate=0.25
    )
    generate(request, out_dir)
    return Run(out_dir, read_ground_truth(out_dir), list(iter_events(out_dir)))


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def warehouse_rows(run: Run) -> list[dict[str, str]]:
    """Every purchase_orders row: the pre-rename CSV plus the renamed one (days 12 to 14)."""
    plain = csv_rows(run.out_dir / "warehouse/purchase_orders.csv")
    renamed = csv_rows(run.out_dir / "warehouse/purchase_orders.renamed.csv")
    assert plain and renamed, "a 14-day run has rows on both sides of the rename"
    return plain + renamed


# ---------------------------------------------------------------------------------------------
# Native formats (spec 8.2)
# ---------------------------------------------------------------------------------------------


def test_webstore_lines_are_json_with_utc_timestamps(run: Run) -> None:
    files = run.files("webstore/app-*.ndjson")
    assert files
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            assert record["ts"].endswith("Z")
            parsed = datetime.fromisoformat(record["ts"])
            assert parsed.tzinfo == UTC
            assert parsed.date().isoformat() == path.name[4:14]
            assert record["level"] == "info"


@pytest.mark.parametrize("pattern", ["orders/order-svc-*.log", "shipping/shipping-app-*.log"])
def test_logfmt_lines_parse(run: Run, pattern: str) -> None:
    files = run.files(pattern)
    assert files
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            assert LOGFMT_LINE.match(line), line
            pairs = dict(LOGFMT_PAIR.findall(line))
            assert datetime.fromisoformat(pairs["ts"]).tzinfo == UTC
            assert pairs["level"] in {"info", "error"}
            assert pairs["msg"].startswith('"')


def test_payments_lines_are_single_xml_documents_with_offsets(run: Run) -> None:
    files = run.files("payments/messages-*.xml")
    assert files
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            root = ET.fromstring(line)  # noqa: S314  (synthetic data written by this package)
            assert root.tag in {"paymentMessage", "heartbeat"}
            stamp = root.findtext("timestamp")
            assert stamp is not None and stamp.endswith(("-04:00", "-05:00"))
            local = datetime.fromisoformat(stamp)
            assert local.utcoffset() in {timedelta(hours=-4), timedelta(hours=-5)}
            assert local.date().isoformat() == path.name[9:19]


def test_export_log_is_plain_text_lines(run: Run) -> None:
    for path in run.files("warehouse/export-job-*.log"):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert TEXT_LINE.match(line), line


def test_sql_seed_executes_in_sqlite_and_matches_row_keys(run: Run) -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(run.read("warehouse/purchase_orders.sql"))
    count = connection.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
    row_keys = [e for e in run.events if e.source_id == "src_wms_db"]
    assert count == len(row_keys) == run.truth.manifest.counts["src_wms_db"]
    columns = {row[1] for row in connection.execute("PRAGMA table_info(purchase_orders)")}
    assert "po_number" in columns
    assert "po_num" not in columns
    ids = [row[0] for row in connection.execute("SELECT id FROM purchase_orders ORDER BY id")]
    first = CounterBases.for_seed(SEED).row
    assert ids == list(range(first, first + count))
    assert 600_000 <= first < 700_000
    totals = connection.execute("SELECT typeof(order_total), typeof(id) FROM purchase_orders")
    assert set(totals.fetchall()) <= {("real", "integer"), ("integer", "integer")}


def test_csv_headers_and_rename_split(run: Run) -> None:
    plain = csv_rows(run.out_dir / "warehouse/purchase_orders.csv")
    renamed = csv_rows(run.out_dir / "warehouse/purchase_orders.renamed.csv")
    assert list(plain[0]) == [
        "id", "po_num", "order_ref", "status", "warehouse_code", "order_total", "order_date",
        "customer_name", "ship_to_address", "created_by", "created_at", "updated_at",
    ]  # fmt: skip
    assert list(renamed[0]) == [c.replace("po_num", "po_number") for c in plain[0]]
    rename_at = local_utc(12, time(0))
    observed = {e.key: e.observed_at for e in run.events if e.source_id == "src_wms_db"}
    for row in plain:
        assert observed[f"src_wms_db:purchase_orders:row:{row['id']}"] < rename_at
    for row in renamed:
        assert observed[f"src_wms_db:purchase_orders:row:{row['id']}"] >= rename_at
    assert renamed, "day 12 is inside a 14-day run, so renamed rows must exist"
    assert len(plain) + len(renamed) == run.truth.manifest.counts["src_wms_db"]
    sql = run.read("warehouse/purchase_orders.sql")
    assert sql.count("ALTER TABLE purchase_orders RENAME COLUMN po_num TO po_number;") == 1
    assert sql.index("ALTER TABLE") > sql.index(f"VALUES ({plain[-1]['id']}, ")
    assert sql.index("ALTER TABLE") < sql.index(f"VALUES ({renamed[0]['id']}, ")


# ---------------------------------------------------------------------------------------------
# Faults (spec 19 scenario A)
# ---------------------------------------------------------------------------------------------


def test_one_ship_file_per_day_and_none_on_day_9(run: Run) -> None:
    files = run.files("shipping/outbound/SHIP_*.csv")
    days = [path.name[5:13] for path in files]
    assert len(days) == len(set(days)), "at most one SHIP file per day"
    assert f"SHIP_{day(9):%Y%m%d}" not in {name[:13] for name in days}
    assert any(path.name.startswith(f"SHIP_{day(10):%Y%m%d}") for path in files)
    assert len(files) == DAYS - 1
    for path in files:
        rows = csv_rows(path)
        assert rows and list(rows[0]) == [
            "po_num", "order_ref", "warehouse_code", "carrier", "service_level", "weight_kg",
        ]  # fmt: skip


def test_day_9_export_fails_with_twelve_permission_denied_lines(run: Run) -> None:
    lines = run.lines(f"warehouse/export-job-{day(9):%Y-%m-%d}.log")
    failures = [line for line in lines if "SFTP upload failed: Permission denied" in line]
    assert len(failures) == 12
    assert all("ERROR" in line for line in failures)
    assert not any("upload complete" in line for line in lines)
    finished = [line for line in lines if "PO export finished" in line]
    assert len(finished) == 1
    file_name = finished[0].split(" to ")[-1]
    assert all(file_name in line for line in failures)
    assert all(line < failures[0] for line in finished), "failures follow the export line"
    failed_events = run.by_node()["warehouse:upload_failed"]
    assert len(failed_events) == 12
    assert all(e.is_error and e.txn_id is None and e.batch_id is None for e in failed_events)
    first, last = local_utc(9, time(21, 13)), local_utc(9, time(21, 19))
    assert all(first <= e.observed_at <= last for e in failed_events)
    # the next night's file lists both days' POs
    day_10_lines = run.lines(f"warehouse/export-job-{day(10):%Y-%m-%d}.log")
    finished_10 = next(ln for ln in day_10_lines if "PO export finished" in ln)
    listed = int(finished_10.split(": ")[1].split()[0])
    fault = next(f for f in run.truth.faults if f.fault_id == "f2_missing_nightly_file")
    assert listed > len(fault.affected_txn_ids) > 0


def test_missing_file_fault_needs_an_export_that_failed(tmp_path: Path) -> None:
    """A day 9 with no due PO runs no export, leaves no evidence, and so records no F2."""
    start = date(2026, 9, 26)
    generate(GenerationRequest(days=9, seed=1, daily_volume=1, start_date=start), tmp_path)
    day_9 = start + timedelta(days=8)
    export_log = (tmp_path / f"warehouse/export-job-{day_9:%Y-%m-%d}.log").read_text("utf-8")
    assert "PO export finished" not in export_log, "precondition: nothing was due on day 9"
    assert "Permission denied" not in export_log
    truth = read_ground_truth(tmp_path)
    assert "f2_missing_nightly_file" not in {f.fault_id for f in truth.faults}
    assert not any(e.node == "warehouse:upload_failed" for e in iter_events(tmp_path))


def test_webstore_outage_window_has_no_webstore_lines_but_orders_continue(run: Run) -> None:
    start, end = local_utc(10, time(14)), local_utc(10, time(16))
    webstore = [
        e for e in run.events if e.source_id == "src_webstore_log" and start <= e.observed_at < end
    ]
    assert not webstore
    for line in run.lines(f"webstore/app-{day(10):%Y-%m-%d}.ndjson"):
        stamp = datetime.fromisoformat(json.loads(line)["ts"])
        assert not start <= stamp < end
    orders = [
        e for e in run.events if e.source_id == "src_orders_log" and start <= e.observed_at < end
    ]
    assert orders
    fault = next(f for f in run.truth.faults if f.fault_id == "f3_webstore_outage")
    assert fault.affected_txn_ids
    assert fault.expected_alert_kind == "freshness"
    assert "visibility gap" in fault.notes


def test_dc03_rows_after_day_13_never_ship(run: Run) -> None:
    stall_from = local_utc(13, time(10))
    observed = {e.key: e.observed_at for e in run.events if e.source_id == "src_wms_db"}
    rows = csv_rows(run.out_dir / "warehouse/purchase_orders.csv") + csv_rows(
        run.out_dir / "warehouse/purchase_orders.renamed.csv"
    )
    stalled = [
        row
        for row in rows
        if row["warehouse_code"] == "DC-03"
        and observed[f"src_wms_db:purchase_orders:row:{row['id']}"] >= stall_from
    ]
    assert stalled
    stalled_pos = {row.get("po_num") or row["po_number"] for row in stalled}
    assert all(row["status"] == "CREATED" for row in stalled)
    ship_text = "\n".join(p.read_text(encoding="utf-8") for p in run.files("shipping/outbound/*"))
    log_text = "\n".join(p.read_text(encoding="utf-8") for p in run.files("shipping/*.log"))
    for po_num in stalled_pos:
        assert po_num not in ship_text
        assert f"po_num={po_num}" not in log_text
    fault = next(f for f in run.truth.faults if f.fault_id == "f5_partial_stall_dc03")
    assert fault.attributes == {"warehouse_code": "DC-03"}
    assert len(fault.affected_txn_ids) == len(stalled)


def test_warehouse_timestamps_run_90_seconds_fast(run: Run) -> None:
    zone = clock.local_zone()
    observed = {e.key: e.observed_at for e in run.events if e.source_id == "src_wms_db"}
    rows = warehouse_rows(run)
    assert len(rows) == len(observed)
    for row in rows:
        rendered = datetime.fromisoformat(row["created_at"]).replace(tzinfo=zone)
        truth = observed[f"src_wms_db:purchase_orders:row:{row['id']}"]
        assert rendered.astimezone(UTC) == truth + timedelta(seconds=90)
        updated = datetime.fromisoformat(row["updated_at"]).replace(tzinfo=zone)
        assert updated.astimezone(UTC) >= truth + timedelta(seconds=90)
    skew = next(f for f in run.truth.faults if f.fault_id == "f6_warehouse_clock_skew")
    assert skew.expected_alert is False
    assert skew.attributes == {"offset_seconds": "90"}
    export_events = run.by_node()["warehouse:export_finished"]
    for event in export_events:
        local = clock.to_local(event.observed_at + timedelta(seconds=90), zone)
        line = f"{local:%Y-%m-%d %H:%M:%S} INFO PO export finished"
        assert line in run.read(f"warehouse/export-job-{local:%Y-%m-%d}.log")


def test_payment_spike_on_day_6(run: Run) -> None:
    fault = next(f for f in run.truth.faults if f.fault_id == "f1_payments_503_spike")
    start, end = local_utc(6, time(10, 20)), local_utc(6, time(11))
    assert (fault.start, fault.end) == (start, end)
    errors = run.by_node()["payments:error_503"]
    assert errors and all(e.is_error for e in errors)
    assert all(start <= e.observed_at < end + timedelta(seconds=15) for e in errors)
    assert {e.txn_id for e in errors} == set(fault.affected_txn_ids)
    failed = run.by_node()["orders:payment_failed"]
    assert len(failed) == len(errors)
    requested = Counter(e.txn_id for e in run.by_node()["orders:payment_requested"])
    authorized = Counter(e.txn_id for e in run.by_node()["payments:authorized"])
    for txn_id in fault.affected_txn_ids:
        assert requested[txn_id] >= 2
        assert authorized[txn_id] == 1
    assert "Service Unavailable" in run.read(f"payments/messages-{day(6):%Y-%m-%d}.xml")


def test_schema_rename_fault_truth(run: Run) -> None:
    fault = next(f for f in run.truth.faults if f.fault_id == "f4_schema_rename")
    assert fault.start == local_utc(12, time(0))
    assert fault.end is None
    assert fault.attributes == {"field": "po_num", "renamed_to": "po_number"}
    assert fault.affected_txn_ids == []
    link_ids = [link.link_id for link in run.truth.links]
    assert "L07b" in link_ids
    order = next(e for e in run.truth.entities if e.entity_id == "ent_order")
    assert any(f.ref.field == "po_number" for f in order.fields)


def test_fault_ids_and_kinds(run: Run) -> None:
    assert [f.fault_id for f in run.truth.faults] == [
        "f1_payments_503_spike",
        "f2_missing_nightly_file",
        "f3_webstore_outage",
        "f4_schema_rename",
        "f5_partial_stall_dc03",
        "f6_warehouse_clock_skew",
    ]
    f2 = run.truth.faults[1]
    arrival = next(
        e
        for e in run.by_node()["shipping:file_arrived"]
        if e.key.endswith(f"SHIP_{day(10):%Y%m%d}" + e.key[-9:])
    )
    assert f2.end == arrival.observed_at
    assert f2.start == local_utc(9, time(21, 30))


# ---------------------------------------------------------------------------------------------
# Ground truth consistency (ADR 0006)
# ---------------------------------------------------------------------------------------------


def test_every_event_key_resolves(run: Run) -> None:
    per_file: dict[tuple[str, str], list[int]] = defaultdict(list)
    row_ids: list[int] = []
    file_names: list[str] = []
    for event in run.events:
        source_id, locator = event.key.split(":", 1)
        assert source_id == event.source_id
        if event.source_id == "src_wms_db":
            table, _, pk = locator.split(":")
            assert table == "purchase_orders"
            row_ids.append(int(pk))
        elif event.source_id == "src_ship_sftp":
            assert locator.startswith("file:")
            file_names.append(locator[5:])
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
        if p.is_file() and not p.name.startswith("purchase_orders")
    }
    assert set(per_file) == log_files
    first = CounterBases.for_seed(SEED).row
    assert sorted(row_ids) == list(range(first, first + len(row_ids)))
    assert sorted(file_names) == [p.name for p in run.files("shipping/outbound/*.csv")]


def test_noise_events_carry_no_transaction(run: Run) -> None:
    noise = [e for e in run.events if e.node.endswith(":noise")]
    assert noise
    assert all(e.txn_id is None and e.batch_id is None and e.actor_kind is None for e in noise)
    assert run.truth.manifest.counts["noise"] == len(noise)
    nodes = {e.node for e in noise}
    assert nodes == {
        "webstore:noise", "orders:noise", "payments:noise", "warehouse:noise", "shipping:noise",
    }  # fmt: skip


def test_complete_transactions_have_the_expected_node_counts(run: Run) -> None:
    per_txn: dict[str, Counter[str]] = defaultdict(Counter)
    for event in run.events:
        if event.txn_id is not None:
            per_txn[event.txn_id][event.node] += 1
    complete = [c for c in per_txn.values() if c["warehouse:po_created"] == 1]
    assert len(complete) > 0.9 * len(per_txn)
    for counts in complete:
        assert counts["webstore:cart_created"] <= 1
        assert counts["webstore:checkout_completed"] <= 1
        assert counts["orders:order_created"] == 1
        assert counts["orders:payment_requested"] >= 1
        assert counts["payments:authorized"] == 1
        assert counts["orders:order_released"] == 1
    outage = set(run.truth.faults[2].affected_txn_ids)
    for txn_id, counts in per_txn.items():
        if counts["warehouse:po_created"] == 1 and txn_id not in outage:
            assert counts["webstore:cart_created"] == 1
            assert counts["webstore:checkout_completed"] == 1


def test_clerk_rows_are_human_and_inside_business_hours(run: Run) -> None:
    zone = clock.local_zone()
    rows = warehouse_rows(run)
    actor = {e.key: e.actor_kind for e in run.events if e.source_id == "src_wms_db"}
    observed = {e.key: e.observed_at for e in run.events if e.source_id == "src_wms_db"}
    assert len(rows) == len(actor)
    human = 0
    for row in rows:
        key = f"src_wms_db:purchase_orders:row:{row['id']}"
        if row["created_by"] == SERVICE_ACCOUNT:
            assert actor[key] == ActorKind.SERVICE
            continue
        human += 1
        assert actor[key] == ActorKind.HUMAN
        local = clock.to_local(observed[key], zone)
        assert local.weekday() < 5
        assert 8 <= local.hour < 18
    assert human > 0
    hop = next(h for h in run.truth.manual_hops if h.hop_id == "hop_payments_to_warehouse")
    assert hop.manual and hop.share_manual == 0.3 and hop.typo_rate == 0.02
    assert len(hop.actors) == 8
    assert all(not h.manual for h in run.truth.manual_hops if h.hop_id != hop.hop_id)


def test_batches_match_events(run: Run) -> None:
    batch_ids = {b.batch_id for b in run.truth.batches}
    referenced = {e.batch_id for e in run.events if e.batch_id is not None}
    assert referenced <= batch_ids
    assert batch_ids <= referenced
    by_batch: dict[str, set[str]] = defaultdict(set)
    for event in run.events:
        if event.batch_id is not None and event.txn_id is not None:
            by_batch[event.batch_id].add(event.txn_id)
    for batch in run.truth.batches:
        assert batch.txn_ids == sorted(batch.txn_ids)
        assert by_batch[batch.batch_id] <= set(batch.txn_ids)
        if batch.key_field.field == "file_name":
            assert batch.key_value.startswith("SHIP_") and batch.key_value.endswith(".csv")
            assert batch.batch_id == "batch_" + batch.key_value.removesuffix(".csv")
            listed = {
                row["po_num"]
                for row in csv_rows(run.out_dir / "shipping/outbound" / batch.key_value)
            }
            assert len(listed) == len(batch.txn_ids)
        else:
            assert batch.key_field.field == "manifest_id"
            assert re.fullmatch(r"MAN-\d{8}-01", batch.key_value)
            assert by_batch[batch.batch_id] == set(batch.txn_ids)


def test_markers_present_in_native_files_and_absent_from_ground_truth(run: Run) -> None:
    markers = run.truth.markers
    assert markers.marker_tokens == sorted(set(markers.marker_tokens))
    assert all(re.fullmatch(r"mk[0-9a-f]{8}", token) for token in markers.marker_tokens)
    native = run.native_text()
    found = set(MARKER.findall(native))
    assert set(markers.marker_tokens) == found
    pii_text = "\n".join(markers.pii_values)
    assert all(token in pii_text for token in markers.marker_tokens), "exact substrings"
    assert not re.search(r"Mk[0-9a-f]{8}", native)
    truth_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((run.out_dir / "ground_truth").glob("*"))
        if path.name != "markers.json"
    )
    assert not MARKER.findall(truth_text)
    assert not any(name in truth_text for name in FIRST_NAMES + LAST_NAMES)
    events_text = run.read("ground_truth/event_txn.ndjson")
    assert all(value not in events_text for value in markers.pii_values[:200])
    assert all(value in native for value in markers.pii_values)


def test_identifier_values_are_keyed_by_field_ref(run: Run) -> None:
    values = run.truth.markers.identifier_values
    expected = {
        str(FieldRef(system_id=s, source_id=src, field=f))
        for s, src, f in [
            ("sys_webstore", "src_webstore_log", "cart_id"),
            ("sys_orders", "src_orders_log", "cart_id"),
            ("sys_orders", "src_orders_log", "order_id"),
            ("sys_orders", "src_orders_log", "merchant_ref"),
            ("sys_payments", "src_payments_xml", "merchantRef"),
            ("sys_warehouse", "src_wms_db", "order_ref"),
            ("sys_warehouse", "src_wms_db", "po_num"),
            ("sys_warehouse", "src_wms_db", "po_number"),
            ("sys_shipping", "src_ship_log", "po_num"),
            ("sys_shipping", "src_ship_log", "shipment_no"),
            ("sys_shipping", "src_ship_log", "manifest_id"),
            ("sys_shipping", "src_ship_log", "file"),
            ("sys_shipping", "src_ship_sftp", "file_name"),
        ]
    }
    assert set(values) == expected
    assert all(items == sorted(set(items)) for items in values.values())
    webstore = run.native_text()
    assert all(
        f'"cart_id": "{v}"' in webstore
        for v in values["sys_webstore/src_webstore_log/cart_id"][:50]
    )
    bases = CounterBases.for_seed(SEED)
    assert min(values["sys_orders/src_orders_log/order_id"], key=int) == str(bases.order)
    assert min(values["sys_warehouse/src_wms_db/po_num"]) == f"{bases.po // 1000}-{bases.po % 1000}"
    assert min(values["sys_shipping/src_ship_log/shipment_no"]) == f"SH-{bases.shipment}"
    assert min(values["sys_orders/src_orders_log/merchant_ref"]) == f"X9-{bases.merchant}"
    assert min(values["sys_webstore/src_webstore_log/cart_id"]) == f"c-{bases.cart}"
    shapes = {
        "sys_webstore/src_webstore_log/cart_id": r"c-\d{8}",
        "sys_orders/src_orders_log/order_id": r"\d{4,5}",
        "sys_orders/src_orders_log/merchant_ref": r"X9-\d{6}",
        "sys_warehouse/src_wms_db/po_num": r"\d{2,3}-\d{3}",
        "sys_shipping/src_ship_log/shipment_no": r"SH-\d{7}",
    }
    for key, pattern in shapes.items():
        assert all(re.fullmatch(pattern, v) for v in values[key]), key
    # The digit runs of the dense counters never share a value (M2 eval follow-up): the forms
    # `digits.0`/`alnum` of one identifier equal those of another only for the true link L05.
    digit_sets = {
        key: {int(re.sub(r"\D", "", v)) for v in values[key]}
        for key in (*shapes, "sys_warehouse/src_wms_db/order_ref")
    }
    digit_sets["row_id"] = {int(row["id"]) for row in warehouse_rows(run)}
    for a, numbers in digit_sets.items():
        for b, others in digit_sets.items():
            if a < b and {a, b} != {
                "sys_orders/src_orders_log/order_id",
                "sys_warehouse/src_wms_db/order_ref",
            }:
                assert not numbers & others, (a, b)
    refs = digit_sets["sys_warehouse/src_wms_db/order_ref"]
    shared = refs & digit_sets["sys_orders/src_orders_log/order_id"]
    assert len(shared) / len(refs) > 0.97, "L05 holds except for the 2% clerk typos"


def test_links_and_entities(run: Run) -> None:
    links = {link.link_id: link for link in run.truth.links}
    assert list(links) == [
        "L01",
        "L02",
        "L03",
        "L04",
        "L05",
        "L06",
        "L07",
        "L07b",
        "L08",
        "L09",
        "L10",
        "L11",
        "L12",
        "L13",
        "L11b",
        "L13b",
        "L14",
        "L15",
    ]
    assert links["L05"].form_b == "digits.0" and links["L05"].b.field == "order_ref"
    for link_id in ("L11", "L12", "L13", "L11b", "L13b"):
        link = links[link_id]
        assert link.link_type == "bridge" and link.role == "transaction"
        assert link.direction == "undirected" and link.entity_id == "ent_order"
        assert link.a.source_id == link.b.source_id == "src_wms_db"
    assert (links["L11"].a.field, links["L11"].b.field) == ("id", "po_num")
    assert (links["L12"].b.field, links["L12"].form_b) == ("order_ref", "digits.0")
    assert (links["L13"].a.field, links["L13"].form_a, links["L13"].b.field) == (
        "order_ref", "digits.0", "po_num",
    )  # fmt: skip
    assert links["L11b"].b.field == links["L13b"].b.field == "po_number"
    for link_id in ("L14", "L15"):
        link = links[link_id]
        assert link.link_type == "bridge" and link.role == "batch"
        assert link.entity_id == "ent_shipping_file" and link.a.field == "file"
        assert link.a.source_id == link.b.source_id == "src_ship_log"
    assert {links["L14"].b.field, links["L15"].b.field} == {"shipment_no", "po_num"}
    composite = links["L06"]
    assert composite.link_type == "composite" and composite.manual
    rates = {c.a.field: c.agreement_rate for c in composite.components}
    assert rates["amount"] == 1.0
    assert rates["timestamp"] is not None and rates["timestamp"] > 0.9
    assert rates["cardholderName"] is not None and 0.1 < rates["cardholderName"] < 0.5
    assert links["L09"].role == "batch" and links["L09"].entity_id == "ent_shipping_file"
    assert links["L10"].role == "batch" and links["L10"].entity_id == "ent_manifest"
    entities = {e.entity_id: e for e in run.truth.entities}
    assert set(entities) == {"ent_order", "ent_shipping_file", "ent_manifest"}
    assert len(entities["ent_order"].fields) == 11
    assert any(
        f.ref.field == "id" and f.ref.source_id == "src_wms_db"
        for f in entities["ent_order"].fields
    )


def test_manifest_counts_and_hashes(run: Run) -> None:
    manifest = run.truth.manifest
    counts = Counter(e.source_id for e in run.events)
    for source in run.truth.sources:
        assert manifest.counts[source.source_id] == counts[source.source_id]
    assert manifest.counts["events"] == sum(1 for e in run.events if e.txn_id is not None)
    assert manifest.counts["transactions"] == 10 * 40 + 4 * 20
    assert manifest.counts["files"] == len(manifest.sha256)
    for relative, digest in manifest.sha256.items():
        assert hashlib.sha256((run.out_dir / relative).read_bytes()).hexdigest() == digest
    assert "ground_truth/manifest.json" not in manifest.sha256
    assert manifest.generator_version == "0.1.0"
    assert manifest.scenario == "shop" and manifest.seed == 7 and manifest.faults_enabled


def test_events_are_sorted_and_readme_written(run: Run) -> None:
    keys = [(e.observed_at, e.key) for e in run.events]
    assert keys == sorted(keys)
    assert all(run.events[0].observed_at >= local_utc(1, time(0)) for _ in [0])
    assert all(e.observed_at < local_utc(DAYS + 1, time(0)) for e in run.events)
    readme = run.read("README.md")
    assert "| src_wms_db |" in readme and "f2_missing_nightly_file" in readme
    rows = {line.split(" | ")[0].strip("| "): line for line in readme.splitlines()}
    assert f"| {day(6):%Y-%m-%d} 10:20 to 11:00 |" in rows["f1_payments_503_spike"]
    assert f"| {day(9):%Y-%m-%d} 21:30 to {day(10):%Y-%m-%d} 21:" in rows["f2_missing_nightly_file"]
    assert f"| {day(13):%Y-%m-%d} 10:00 to open |" in rows["f5_partial_stall_dc03"]
    assert hashlib.sha256(run.read("ground_truth/event_txn.ndjson").encode()).hexdigest() in readme


def test_ship_file_mtime_is_the_arrival_instant(run: Run) -> None:
    for event in run.by_node()["shipping:file_arrived"]:
        path = run.out_dir / "shipping/outbound" / event.key.split(":file:")[1]
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        assert abs(mtime - event.observed_at) < timedelta(seconds=1)
        assert event.txn_id is None and event.actor_kind is None
        local = clock.to_local(event.observed_at)
        assert time(21, 10) <= local.time() <= time(21, 35)


def test_shipping_lifecycle_timing(run: Run) -> None:
    nodes = run.by_node()
    arrivals = {e.batch_id: e.observed_at for e in nodes["shipping:file_arrived"]}
    for shipment in nodes["shipping:shipment_created"]:
        assert shipment.batch_id in arrivals
        lag = shipment.observed_at - arrivals[shipment.batch_id]
        assert timedelta(minutes=5) <= lag <= timedelta(minutes=70)
    for pickup in nodes["shipping:carrier_pickup"]:
        local = clock.to_local(pickup.observed_at)
        assert time(22, 0) <= local.time() <= time(22, 30)
        assert pickup.batch_id is not None and pickup.batch_id.startswith("batch_MAN-")
