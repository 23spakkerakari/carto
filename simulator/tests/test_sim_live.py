"""Live mode (spec 19 "Live mode"): the batch run replayed in real time with shifted clocks."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from carto_simulator import cli, live
from carto_simulator.api import GenerationRequest, generate
from carto_simulator.ground_truth import (
    GROUND_TRUTH_DIR,
    EventTruth,
    iter_events,
    read_ground_truth,
)
from carto_simulator.scenarios.shop_model import (
    SRC_ORDERS,
    SRC_PAYMENTS,
    SRC_SHIP_LOG,
    SRC_SHIP_SFTP,
    SRC_WEBSTORE,
    SRC_WMS_DB,
    SRC_WMS_EXPORT,
    WAREHOUSE_SKEW,
)

REQUEST = GenerationRequest(days=2, daily_volume=6, seed=3, noise_rate=0.2)
NOW = datetime(2026, 10, 9, 15, 0, 0, 250_000, tzinfo=UTC)
LOCAL = ZoneInfo("America/New_York")
FAST = 1e9  # simulated seconds per real second: the replay never sleeps


def _no_sleep(_seconds: float) -> None:
    return None


def _log_files(root: Path) -> list[Path]:
    """Every per-day log file of the five log sources, relative order stable."""
    patterns = (
        "webstore/app-*.ndjson",
        "orders/order-svc-*.log",
        "payments/messages-*.xml",
        "warehouse/export-job-*.log",
        "shipping/shipping-app-*.log",
    )
    return [path for pattern in patterns for path in sorted(root.glob(pattern))]


def _line_timestamp(source_id: str, line: str) -> datetime:
    """The rendered timestamp of a line as the source's clock shows it, in UTC."""
    if source_id == SRC_WEBSTORE:
        text = str(json.loads(line)["ts"])
        return datetime.fromisoformat(text)
    if source_id in (SRC_ORDERS, SRC_SHIP_LOG):
        match = re.match(r"ts=(\S+) ", line)
        assert match is not None, "logfmt line starts with ts="
        return datetime.fromisoformat(match.group(1))
    if source_id == SRC_PAYMENTS:
        match = re.search(r"<timestamp>(.*?)</timestamp>", line)
        assert match is not None, "payment document carries a timestamp"
        return datetime.fromisoformat(match.group(1)).astimezone(UTC)
    naive = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    return naive.replace(tzinfo=LOCAL).astimezone(UTC)


SOURCE_OF_DIRECTORY = {
    "webstore": SRC_WEBSTORE,
    "orders": SRC_ORDERS,
    "payments": SRC_PAYMENTS,
    "warehouse": SRC_WMS_EXPORT,
    "shipping": SRC_SHIP_LOG,
}


@pytest.fixture(scope="module")
def batch(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("batch") / "shop"
    generate(REQUEST, out)
    assert list(out.glob("shipping/outbound/SHIP_*.csv")), "the fixture run has a file drop"
    return out


@pytest.fixture(scope="module")
def plan(tmp_path_factory: pytest.TempPathFactory) -> live.LivePlan:
    return live.plan_live(REQUEST, tmp_path_factory.mktemp("plan") / "live", now=NOW)


@pytest.fixture(scope="module")
def replayed(plan: live.LivePlan) -> tuple[live.LivePlan, live.ReplayStats]:
    live.write_static_files(plan)
    stats = live.replay(plan, speed=FAST, sleep=_no_sleep)
    return plan, stats


# -- planning ---------------------------------------------------------------------------------


def test_plan_anchors_the_first_record_at_now(plan: live.LivePlan, batch: Path) -> None:
    batch_events = list(iter_events(batch))
    first = batch_events[0].observed_at
    assert abs(plan.anchor - NOW) <= timedelta(milliseconds=500)
    assert plan.delta.microseconds == 0, "the shift is a whole number of seconds"
    assert first + plan.delta == plan.anchor
    assert plan.events[0].observed_at == plan.anchor == plan.first_at
    assert len(plan.items) == len(batch_events) == len(plan.events)


def test_plan_keeps_locator_keys_and_shifts_every_time(plan: live.LivePlan, batch: Path) -> None:
    batch_events = list(iter_events(batch))
    assert [event.key for event in plan.events] == [event.key for event in batch_events]
    for shifted, original in zip(plan.events, batch_events, strict=True):
        assert shifted.observed_at - original.observed_at == plan.delta
        assert shifted.model_dump(exclude={"observed_at"}) == original.model_dump(
            exclude={"observed_at"}
        )
    batch_truth = read_ground_truth(batch)
    assert [f.fault_id for f in plan.truth.faults] == [f.fault_id for f in batch_truth.faults]
    for shifted_fault, fault in zip(plan.truth.faults, batch_truth.faults, strict=True):
        assert shifted_fault.start - fault.start == plan.delta
        if fault.end is not None:
            assert shifted_fault.end is not None
            assert shifted_fault.end - fault.end == plan.delta
    assert plan.truth.links == batch_truth.links
    assert plan.truth.entities == batch_truth.entities
    assert plan.truth.batches == batch_truth.batches
    assert plan.truth.markers == batch_truth.markers
    assert plan.truth.manual_hops == batch_truth.manual_hops
    assert plan.truth.sources == batch_truth.sources


def test_plan_manifest_is_the_batch_manifest_with_a_shifted_window(
    plan: live.LivePlan, batch: Path
) -> None:
    batch_manifest = read_ground_truth(batch).manifest
    manifest = plan.truth.manifest
    assert plan.batch_manifest == batch_manifest
    for source_id in (
        SRC_WEBSTORE, SRC_ORDERS, SRC_PAYMENTS, SRC_WMS_DB, SRC_WMS_EXPORT, SRC_SHIP_SFTP,
        SRC_SHIP_LOG, "transactions", "events", "noise",
    ):  # fmt: skip
        assert manifest.counts[source_id] == batch_manifest.counts[source_id]
    assert manifest.seed == batch_manifest.seed and manifest.days == batch_manifest.days
    window_start = datetime.combine(batch_manifest.start_date, datetime.min.time(), tzinfo=LOCAL)
    assert manifest.start_date == (window_start + plan.delta).astimezone(LOCAL).date()
    assert "warehouse/purchase_orders.ndjson" in manifest.sha256
    assert "warehouse/purchase_orders.csv" not in manifest.sha256
    assert manifest.counts["files"] == len(manifest.sha256)


def test_plan_is_deterministic_and_leaves_the_generator_alone(
    tmp_path: Path, plan: live.LivePlan, batch: Path
) -> None:
    again = live.plan_live(REQUEST, tmp_path / "again", now=NOW)
    assert again.items == plan.items
    assert again.events == plan.events
    assert again.sql_text == plan.sql_text
    assert again.truth.manifest.sha256 == plan.truth.manifest.sha256
    assert plan.batch_manifest.sha256 == read_ground_truth(batch).manifest.sha256


def test_plan_items_are_ordered_for_append(plan: live.LivePlan) -> None:
    times = [item.observed_at for item in plan.items]
    assert times == sorted(times)
    per_file: dict[str, int] = {}
    for item in plan.items:
        if item.kind == "line":
            assert item.line_no == per_file.get(item.relative_path, 0) + 1
            per_file[item.relative_path] = item.line_no
        else:
            assert item.line_no == 0


def test_plan_rejects_naive_now_and_unknown_scenarios(tmp_path: Path) -> None:
    with pytest.raises(live.LiveError, match="timezone-aware"):
        live.plan_live(REQUEST, tmp_path / "x", now=NOW.replace(tzinfo=None))
    with pytest.raises(live.LiveError, match="live mode"):
        live.plan_live(
            GenerationRequest(scenario="payer", days=1, daily_volume=1), tmp_path / "y", now=NOW
        )


# -- replay ----------------------------------------------------------------------------------


def test_zero_shift_replay_reproduces_the_batch_files_byte_for_byte(
    tmp_path: Path, batch: Path
) -> None:
    first = next(iter_events(batch)).observed_at
    plan = live.plan_live(REQUEST, tmp_path / "zero", now=first)
    assert plan.delta == timedelta(0)
    live.write_static_files(plan)
    stats = live.replay(plan, speed=FAST, sleep=_no_sleep)
    assert stats.stopped_by == "complete" and stats.written == stats.planned
    for path in _log_files(batch):
        relative = path.relative_to(batch)
        assert (plan.out_dir / relative).read_bytes() == path.read_bytes(), str(relative)
    for path in sorted(batch.glob("shipping/outbound/SHIP_*.csv")):
        assert (plan.out_dir / "shipping/outbound" / path.name).read_bytes() == path.read_bytes()
    sql = "warehouse/purchase_orders.sql"
    assert (plan.out_dir / sql).read_bytes() == (batch / sql).read_bytes()
    events = (plan.out_dir / GROUND_TRUTH_DIR / "event_txn.ndjson").read_bytes()
    assert events == (batch / GROUND_TRUTH_DIR / "event_txn.ndjson").read_bytes()


def test_replay_writes_the_batch_file_names_with_the_same_line_counts(
    replayed: tuple[live.LivePlan, live.ReplayStats], batch: Path
) -> None:
    plan, stats = replayed
    assert stats.stopped_by == "complete"
    assert stats.written == stats.planned == len(plan.items)
    assert stats.lines + stats.rows + stats.files == stats.written
    batch_logs = [p.relative_to(batch) for p in _log_files(batch)]
    assert batch_logs, "the fixture run has log files"
    assert [p.relative_to(plan.out_dir) for p in _log_files(plan.out_dir)] == batch_logs
    for relative in batch_logs:
        expected = (batch / relative).read_text(encoding="utf-8").count("\n")
        assert (plan.out_dir / relative).read_text(encoding="utf-8").count("\n") == expected
    assert sorted(p.name for p in plan.out_dir.glob("shipping/outbound/*")) == sorted(
        p.name for p in batch.glob("shipping/outbound/*")
    )
    assert not (plan.out_dir / "warehouse/purchase_orders.csv").exists()


def test_replayed_lines_carry_shifted_monotonic_timestamps(
    replayed: tuple[live.LivePlan, live.ReplayStats], batch: Path
) -> None:
    plan, _ = replayed
    shifted_at = {event.key: event.observed_at for event in plan.events}
    checked = 0
    for path in _log_files(plan.out_dir):
        source_id = SOURCE_OF_DIRECTORY[path.parent.name]
        skew = WAREHOUSE_SKEW if source_id == SRC_WMS_EXPORT else timedelta(0)
        previous: datetime | None = None
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            rendered = _line_timestamp(source_id, line)
            assert previous is None or rendered >= previous, f"{path.name}:{number}"
            previous = rendered
            expected = shifted_at[f"{source_id}:{path.name}:line:{number}"] + skew
            assert rendered == expected, f"{path.name}:{number}"
            checked += 1
    assert checked == sum(1 for item in plan.items if item.kind == "line")
    batch_last = max(event.observed_at for event in iter_events(batch))
    assert plan.anchor > batch_last + timedelta(days=1), "the fixture shift moves every clock"


def test_ship_files_land_with_their_shifted_arrival_mtime(
    replayed: tuple[live.LivePlan, live.ReplayStats],
) -> None:
    plan, _ = replayed
    arrivals = {
        event.key.removeprefix(f"{SRC_SHIP_SFTP}:file:"): event.observed_at
        for event in plan.events
        if event.source_id == SRC_SHIP_SFTP
    }
    assert arrivals
    for name, arrived_at in arrivals.items():
        path = plan.out_dir / "shipping/outbound" / name
        assert path.is_file()
        assert abs(path.stat().st_mtime - arrived_at.timestamp()) < 0.001
        assert arrived_at >= plan.anchor


def test_row_stream_is_one_shifted_json_object_per_row(
    replayed: tuple[live.LivePlan, live.ReplayStats], batch: Path
) -> None:
    plan, _ = replayed
    stream = plan.out_dir / "warehouse/purchase_orders.ndjson"
    rows = [json.loads(line) for line in stream.read_text(encoding="utf-8").splitlines()]
    created_at = {
        event.key.removeprefix(f"{SRC_WMS_DB}:purchase_orders:row:"): event.observed_at
        for event in plan.events
        if event.source_id == SRC_WMS_DB
    }
    assert len(rows) == len(created_at) > 0
    assert [str(row["id"]) for row in rows] == [
        str(row["id"]) for row in sorted(rows, key=lambda r: created_at[str(r["id"])])
    ]
    batch_rows = {
        row.split(",", 1)[0]: row
        for row in (batch / "warehouse/purchase_orders.csv")
        .read_text(encoding="utf-8")
        .splitlines()[1:]
    }
    for row in rows:
        row_id = str(row["id"])
        local_created = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
        rendered = local_created.replace(tzinfo=LOCAL).astimezone(UTC)
        assert rendered == created_at[row_id] + WAREHOUSE_SKEW
        local_updated = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
        assert local_updated >= local_created
        assert row["po_num"] in batch_rows[row_id] and row["order_ref"] in batch_rows[row_id]
        assert row["created_by"] in batch_rows[row_id]
        assert row["order_date"] >= plan.anchor.astimezone(LOCAL).date().isoformat()
    sql = (plan.out_dir / "warehouse/purchase_orders.sql").read_text(encoding="utf-8")
    assert sql.count("INSERT INTO purchase_orders") == len(rows)
    assert rows[0]["created_at"] in sql


def test_ground_truth_of_the_live_run_loads_and_matches_the_files(
    replayed: tuple[live.LivePlan, live.ReplayStats],
) -> None:
    plan, _ = replayed
    truth = read_ground_truth(plan.out_dir)
    events = list(iter_events(plan.out_dir))
    assert events == list(plan.events)
    assert all(isinstance(event, EventTruth) for event in events)
    counted = sum(truth.manifest.counts.get(s.source_id, 0) for s in truth.sources)
    assert counted == len(events)
    for relative, digest in truth.manifest.sha256.items():
        actual = hashlib.sha256((plan.out_dir / relative).read_bytes()).hexdigest()
        assert actual == digest, relative
    readme = (plan.out_dir / "README.md").read_text(encoding="utf-8")
    assert "live" in readme and "purchase_orders.ndjson" in readme
    assert str(plan.anchor.isoformat()) in readme


def test_duration_cap_and_stop_request_end_the_replay_cleanly(tmp_path: Path) -> None:
    plan = live.plan_live(REQUEST, tmp_path / "capped", now=NOW)
    live.write_static_files(plan)
    clock = {"now": 0.0}

    def monotonic() -> float:
        clock["now"] += 0.25
        return clock["now"]

    stats = live.replay(plan, speed=1.0, duration_seconds=2.0, sleep=_no_sleep, monotonic=monotonic)
    assert stats.stopped_by == "duration"
    assert 0 < stats.written < stats.planned
    seen = {"calls": 0}

    def should_stop() -> bool:
        seen["calls"] += 1
        return seen["calls"] > 3

    plan2 = live.plan_live(REQUEST, tmp_path / "stopped", now=NOW)
    live.write_static_files(plan2)
    stats2 = live.replay(plan2, speed=FAST, sleep=_no_sleep, should_stop=should_stop)
    assert stats2.stopped_by == "stop"
    assert stats2.written < stats2.planned


def test_replay_paces_by_simulated_time_over_speed(tmp_path: Path) -> None:
    plan = live.plan_live(REQUEST, tmp_path / "paced", now=NOW)
    live.write_static_files(plan)
    sleeps: list[float] = []
    clock = {"now": 100.0}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["now"] += seconds

    stats = live.replay(plan, speed=60.0, sleep=sleep, monotonic=lambda: clock["now"])
    assert stats.stopped_by == "complete"
    span = (plan.events[-1].observed_at - plan.events[0].observed_at).total_seconds()
    assert abs(sum(sleeps) - span / 60.0) < 0.01
    assert all(0 < s <= 0.5 for s in sleeps)


def test_write_static_files_refuses_a_populated_foreign_directory(tmp_path: Path) -> None:
    out = tmp_path / "keep"
    out.mkdir()
    (out / "precious.txt").write_text("keep me", encoding="utf-8")
    plan = live.plan_live(REQUEST, out, now=NOW)
    with pytest.raises(ValueError, match="refusing to empty"):
        live.write_static_files(plan)
    assert (out / "precious.txt").read_text(encoding="utf-8") == "keep me"


def test_write_static_files_replaces_a_previous_run(tmp_path: Path) -> None:
    out = tmp_path / "rerun"
    first = live.plan_live(REQUEST, out, now=NOW)
    live.write_static_files(first)
    live.replay(first, speed=FAST, sleep=_no_sleep)
    stale = out / "webstore" / "stale.ndjson"
    stale.write_text("{}\n", encoding="utf-8")
    second = live.plan_live(REQUEST, out, now=NOW + timedelta(hours=1))
    live.write_static_files(second)
    assert not stale.exists()
    assert not list(out.glob("webstore/*.ndjson"))
    assert (out / GROUND_TRUTH_DIR / "manifest.json").is_file()


# -- command line ----------------------------------------------------------------------------


def test_cli_live_replays_and_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    argv = [
        "live", "--scenario", "shop", "--days", "1", "--seed", "3", "--daily-volume", "4",
        "--noise-rate", "0.1", "--speed", "1e9", "--out", str(out),
    ]  # fmt: skip
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert "speed: 1e+09" in captured.out or "speed: 1000000000" in captured.out
    assert "records:" in captured.out and "complete" in captured.out
    assert (out / GROUND_TRUTH_DIR / "manifest.json").is_file()
    assert list(out.glob("webstore/app-*.ndjson"))
    assert (out / "warehouse/purchase_orders.ndjson").is_file()
    truth = read_ground_truth(out)
    assert truth.manifest.days == 1 and truth.manifest.daily_volume == 4
    for line in captured.out.splitlines():
        assert "mk" not in line.lower() or "mkdir" in line, "no record values on stdout"


def test_cli_live_defaults_are_two_days_at_sixty_x() -> None:
    args = cli.build_parser().parse_args(["live", "--out", "x"])
    assert args.days == live.DEFAULT_LIVE_DAYS == 2
    assert args.speed == live.DEFAULT_SPEED == 60.0
    assert args.duration_seconds is None


def test_cli_live_rejects_bad_requests(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["live", "--scenario", "nope", "--out", str(tmp_path / "a")]) == 2
    assert "live mode" in capsys.readouterr().err
    assert cli.main(["live", "--speed", "0", "--out", str(tmp_path / "b")]) == 2
    assert "speed" in capsys.readouterr().err
    assert cli.main(["live", "--days", "0", "--out", str(tmp_path / "c")]) == 2
    assert "days" in capsys.readouterr().err
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    argv = ["live", "--days", "1", "--daily-volume", "1", "--out", str(target)]
    assert cli.main(argv) == 2
    assert "not a directory" in capsys.readouterr().err


def test_cli_generate_and_list_are_unchanged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out.split() == ["people_ops", "shop"]
    argv = ["generate", "--days", "1", "--daily-volume", "2", "--out", str(tmp_path / "g")]
    assert cli.main(argv) == 0
    assert (tmp_path / "g/warehouse/purchase_orders.csv").is_file()
