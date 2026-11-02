"""carto_edge.connectors.upload: spec 8.1.1 limits (zip bomb, traversal, symlink entries, sizes),
the three kinds with ADR 0006 locators, cursor resume, and the simulator's scenario A files."""

from __future__ import annotations

import gzip
import io
import os
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from carto_edge.config import SourceConfig, SourceType
from carto_edge.connectors import upload as upload_module
from carto_edge.connectors.base import (
    ConnectorContext,
    ConnectorError,
    InvalidConfigError,
    ReadConnector,
    ReadOnlyStatus,
    ResolvedHost,
)
from carto_edge.connectors.upload import (
    MAX_ZIP_ENTRIES,
    UploadConnector,
    UploadLimitError,
    check_zip,
)
from carto_edge.pipeline.model import RawRecord
from carto_schema.event import EventKind

SIM_OUT = Path(__file__).resolve().parents[2] / "sim-out" / "shop"


class Secrets:
    def resolve(self, secret_ref: str) -> str:
        raise AssertionError("the upload connector never resolves a secret")


class Network:
    def resolve(self, host: str, port: int) -> ResolvedHost:
        raise AssertionError("the upload connector never opens a connection")


def make(config: dict[str, object], source_id: str = "src_upload") -> UploadConnector:
    source = SourceConfig(id=source_id, system="sys_orders", type=SourceType.UPLOAD, config=config)
    return UploadConnector(source, ConnectorContext(secrets=Secrets(), network=Network()))


async def collect(
    connector: UploadConnector, cursor: dict[str, object] | None = None
) -> list[RawRecord]:
    return [record async for record in connector.read(cursor)]


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    directory = tmp_path / "orders"
    directory.mkdir()
    (directory / "order-svc-2026-09-23.log").write_bytes(b"ts=1 msg=a\nts=2 msg=b\n\nts=3 msg=c\n")
    (directory / "order-svc-2026-09-24.log").write_bytes(b"ts=4 msg=d\r\nts=5 msg=e")
    (directory / "notes.md").write_bytes(b"ignored: not an accepted extension\n")
    return directory


# ---------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------


def test_is_a_read_connector(logs: Path) -> None:
    connector = make({"paths": [str(logs)]})
    assert isinstance(connector, ReadConnector)
    assert connector.type == "upload"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"paths": []},
        {"paths": ["x"], "kind": "rows"},
        {"paths": ["x"], "kind": "rows", "table": "t"},
        {"paths": ["x"], "kind": "video"},
        {"paths": ["x"], "encoding": "no-such-encoding"},
        {"paths": ["x"], "max_file_bytes": 3 * 1024**3},
        {"paths": ["x"], "bogus": True},
        {"paths": ["a\x00b"]},
    ],
)
def test_invalid_configs(config: dict[str, object]) -> None:
    with pytest.raises(InvalidConfigError):
        make(config)


# ---------------------------------------------------------------------------------------------
# records of the log kind
# ---------------------------------------------------------------------------------------------


async def test_log_lines_with_physical_line_numbers(logs: Path) -> None:
    records = await collect(make({"paths": [str(logs)]}))
    assert [record.locator for record in records] == [
        "order-svc-2026-09-23.log:line:1",
        "order-svc-2026-09-23.log:line:2",
        "order-svc-2026-09-23.log:line:4",  # the blank line 3 is counted, not emitted
        "order-svc-2026-09-24.log:line:1",
        "order-svc-2026-09-24.log:line:2",
    ]
    assert [record.text for record in records] == [
        "ts=1 msg=a",
        "ts=2 msg=b",
        "ts=3 msg=c",
        "ts=4 msg=d",
        "ts=5 msg=e",
    ]
    first = records[0]
    assert first.kind is EventKind.LOG
    assert first.fields is None
    assert first.source_id == "src_upload"
    assert first.system_id == "sys_orders"
    assert first.received_at.tzinfo is UTC
    assert first.size_bytes == len("ts=1 msg=a\n")
    assert first.commit_cursor == {"file": str(logs / "order-svc-2026-09-23.log"), "line": 1}


async def test_globs_relative_to_base_dir_and_include_patterns(logs: Path) -> None:
    connector = make(
        {"paths": ["orders/*.log"], "base_dir": str(logs.parent), "include": ["*-23.log"]}
    )
    records = await collect(connector)
    assert {record.locator.split(":")[0] for record in records} == {"order-svc-2026-09-23.log"}


async def test_cursor_resume_skips_what_was_done(logs: Path) -> None:
    connector = make({"paths": [str(logs)]})
    records = await collect(connector)
    cursor = dict(records[2].commit_cursor or {})
    rest = await collect(connector, cursor)
    assert [record.locator for record in rest] == [
        "order-svc-2026-09-24.log:line:1",
        "order-svc-2026-09-24.log:line:2",
    ]
    done = await collect(connector, dict(records[-1].commit_cursor or {}))
    assert done == []
    # A cursor naming a file that no longer exists starts over (core dedupes by event_id).
    assert len(await collect(connector, {"file": "gone.log", "line": 9})) == 5


async def test_gzip_is_streamed_and_named_without_the_suffix(tmp_path: Path) -> None:
    path = tmp_path / "app-2026-10-01.ndjson.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b'{"a": 1}\n{"a": 2}\n')
    records = await collect(make({"paths": [str(path)]}))
    assert [record.locator for record in records] == [
        "app-2026-10-01.ndjson:line:1",
        "app-2026-10-01.ndjson:line:2",
    ]


async def test_zip_entries_are_streamed(tmp_path: Path) -> None:
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("logs/a.log", "one\ntwo\n")
        archive.writestr("logs/b.txt", "three\n")
        archive.writestr("logs/", "")
        archive.writestr("image.png", "not a log")
    records = await collect(make({"paths": [str(path)]}))
    assert [record.locator for record in records] == [
        "a.log:line:1",
        "a.log:line:2",
        "b.txt:line:1",
    ]
    assert records[0].commit_cursor == {"file": f"{path}!logs/a.log", "line": 1}
    rest = await collect(make({"paths": [str(path)]}), dict(records[1].commit_cursor or {}))
    assert [record.locator for record in rest] == ["b.txt:line:1"]


async def test_overlong_line_is_cut_but_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upload_module, "MAX_LINE_BYTES", 16)
    path = tmp_path / "x.log"
    path.write_bytes(b"short\n" + b"y" * 100 + b"\nafter\n")
    records = await collect(make({"paths": [str(path)]}))
    assert [record.locator for record in records] == [
        "x.log:line:1",
        "x.log:line:2",
        "x.log:line:3",
    ]
    assert records[1].text == "y" * 16
    assert records[1].size_bytes == 101
    assert records[2].text == "after"


async def test_latin1_encoding(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    path.write_bytes("caf\xe9\n".encode("latin-1"))
    records = await collect(make({"paths": [str(path)], "encoding": "latin-1"}))
    assert records[0].text == "caf\xe9"


# ---------------------------------------------------------------------------------------------
# records of the rows kind
# ---------------------------------------------------------------------------------------------


async def test_rows_from_csv(tmp_path: Path) -> None:
    path = tmp_path / "purchase_orders.csv"
    path.write_text(
        "id,po_num,status,updated_at\n"
        "1,88-210,SHIPPED,2026-09-23 22:04:03\n"
        "2,88-211,OPEN,2026-09-23 21:27:41\n",
        encoding="utf-8",
    )
    connector = make(
        {
            "paths": [str(path)],
            "kind": "rows",
            "table": "purchase_orders",
            "primary_key": "id",
            "timestamp_column": "updated_at",
            "actor_column": "created_by",
        }
    )
    records = await collect(connector)
    assert [record.locator for record in records] == [
        "purchase_orders:row:1",
        "purchase_orders:row:2",
    ]
    assert records[0].kind is EventKind.ROW_CHANGE
    assert records[0].fields == {
        "id": "1",
        "po_num": "88-210",
        "status": "SHIPPED",
        "updated_at": "2026-09-23 22:04:03",
    }
    assert records[0].template_hint == "row_change purchase_orders"
    assert records[0].timestamp_field == "updated_at"
    assert records[0].actor_field == "created_by"
    assert records[0].text is None
    assert records[1].commit_cursor == {"file": str(path), "line": 2}
    rest = await collect(connector, {"file": str(path), "line": 1})
    assert [record.locator for record in rest] == ["purchase_orders:row:2"]


async def test_rows_need_a_header_with_the_primary_key(tmp_path: Path) -> None:
    path = tmp_path / "t.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ConnectorError, match="primary_key"):
        await collect(
            make({"paths": [str(path)], "kind": "rows", "table": "t", "primary_key": "id"})
        )


def rows_source(path: Path) -> UploadConnector:
    return make({"paths": [str(path)], "kind": "rows", "table": "t", "primary_key": "id"})


async def test_csv_cells_past_the_csv_modules_default_field_limit_are_read(
    tmp_path: Path,
) -> None:
    """The csv module stops at 128 KiB per cell unless told otherwise; an exported JSON blob
    column is routinely larger."""
    blob = "x" * 300_000
    path = tmp_path / "t.csv"
    path.write_text(f"id,blob\n1,{blob}\n2,small\n", encoding="utf-8")
    records = await collect(rows_source(path))
    assert [record.locator for record in records] == ["t:row:1", "t:row:2"]
    assert records[0].fields is not None and len(records[0].fields["blob"]) == 300_000


async def test_a_csv_row_the_reader_cannot_parse_is_skipped_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad row (here a cell past the field limit) is skipped and counted; the rows after
    it are still read and the cursor still counts the skipped row."""
    monkeypatch.setattr(upload_module, "MAX_CSV_FIELD_BYTES", 64)
    path = tmp_path / "t.csv"
    path.write_text("id,blob\n1,ok\n2," + "y" * 200 + "\n3,ok\n", encoding="utf-8")
    try:
        with caplog.at_level("WARNING"):
            records = await collect(rows_source(path))
    finally:
        import csv  # noqa: PLC0415

        csv.field_size_limit(16 * 1024 * 1024)
    assert [record.locator for record in records] == ["t:row:1", "t:row:3"]
    assert records[-1].commit_cursor == {"file": str(path), "line": 3}
    assert any("skipped=1" in message for message in caplog.messages)
    assert all("y" * 10 not in message for message in caplog.messages)


# ---------------------------------------------------------------------------------------------
# records of the files kind
# ---------------------------------------------------------------------------------------------


async def test_files_kind_emits_file_arrived_with_mtime(tmp_path: Path) -> None:
    outbound = tmp_path / "outbound"
    outbound.mkdir()
    arrival = datetime(2026, 9, 23, 21, 12, tzinfo=UTC)
    first = outbound / "SHIP_20260923_2112.csv"
    first.write_bytes(b"shipment_id\n")
    os.utime(first, (arrival.timestamp(), arrival.timestamp()))
    (outbound / "SHIP_20260924_2115.csv").write_bytes(b"shipment_id\n")
    records = await collect(
        make({"paths": [str(outbound)], "kind": "files", "include": ["SHIP_*.csv"]})
    )
    assert [record.locator for record in records] == [
        "file:SHIP_20260923_2112.csv",
        "file:SHIP_20260924_2115.csv",
    ]
    record = records[0]
    assert record.kind is EventKind.FILE_ARRIVED
    assert record.received_at == arrival
    assert record.template_hint == "file_arrived SHIP_*_*.csv"
    assert record.fields == {
        "name": "SHIP_20260923_2112.csv",
        "size": len("shipment_id\n"),
        "mtime": arrival.isoformat(),
        "directory": "outbound",  # the name only: no machine path in clear
    }
    assert record.commit_cursor == {"file": str(first), "line": 0}
    window = [
        r
        async for r in make({"paths": [str(outbound)], "kind": "files"}).backfill(arrival, arrival)
    ]
    assert [r.locator for r in window] == ["file:SHIP_20260923_2112.csv"]


# ---------------------------------------------------------------------------------------------
# limits (spec 8.1.1)
# ---------------------------------------------------------------------------------------------


def bomb_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.log", b"\n" * (4 * 1024 * 1024))  # compresses far beyond 100:1
    return path


async def test_zip_bomb_ratio_is_refused(tmp_path: Path) -> None:
    path = bomb_zip(tmp_path / "bomb.zip")
    connector = make({"paths": [str(path)]})
    with pytest.raises(UploadLimitError, match="ratio"):
        await collect(connector)
    result = await connector.test()
    assert not result.ok
    assert any("ratio" in problem for problem in result.problems)


def test_zip_traversal_and_absolute_names_are_refused() -> None:
    for name in (
        "../evil.log",
        "logs/../../evil.log",
        "/etc/passwd.log",
        "C:\\x.log",
        "\\\\server\\share.log",
    ):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, "x\n")
        with (
            zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive,
            pytest.raises(UploadLimitError),
        ):
            check_zip(archive)


def test_zip_symlink_entry_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo("link.log")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with (
        zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive,
        pytest.raises(UploadLimitError, match="symlink"),
    ):
        check_zip(archive)


def test_zip_entry_count_limit() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(MAX_ZIP_ENTRIES + 1):
            archive.writestr(f"{index}.log", "")
    with (
        zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive,
        pytest.raises(UploadLimitError, match="entries"),
    ):
        check_zip(archive)


def test_zip_entry_size_limit() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.log", "x" * 2000)
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        assert len(check_zip(archive)) == 1
        with pytest.raises(UploadLimitError, match="uncompressed"):
            check_zip(archive, max_file_bytes=1000)


async def test_zip_entry_over_the_per_file_limit_is_refused_before_streaming(
    tmp_path: Path,
) -> None:
    path = tmp_path / "entry.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("a.log", "x" * 500 + "\n")
    connector = make({"paths": [str(path)], "max_file_bytes": 400})
    with pytest.raises(UploadLimitError, match="uncompressed"):
        await collect(connector)
    assert len(await collect(make({"paths": [str(path)], "max_file_bytes": 2000}))) == 1


async def test_per_file_and_per_upload_size_limits(tmp_path: Path) -> None:
    big = tmp_path / "big.log"
    big.write_bytes(b"x" * 3000 + b"\n")
    small = tmp_path / "small.log"
    small.write_bytes(b"y\n")
    with pytest.raises(UploadLimitError, match="exceeds"):
        await collect(make({"paths": [str(big)], "max_file_bytes": 1000}))
    with pytest.raises(UploadLimitError, match="totals"):
        await collect(make({"paths": [str(tmp_path)], "max_upload_bytes": 3001}))
    assert len(await collect(make({"paths": [str(tmp_path)], "max_upload_bytes": 3003}))) == 2


async def test_gzip_bomb_is_stopped_at_the_limit(tmp_path: Path) -> None:
    path = tmp_path / "bomb.log.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"z" * 200_000)
    with pytest.raises(UploadLimitError, match="uncompressed"):
        await collect(make({"paths": [str(path)], "max_file_bytes": 100_000}))


async def test_symlinks_are_never_followed(tmp_path: Path, logs: Path) -> None:
    link = tmp_path / "link.log"
    try:
        link.symlink_to(logs / "order-svc-2026-09-23.log")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    records = await collect(make({"paths": [str(tmp_path)]}))
    assert all("link.log" not in record.locator for record in records)
    result = await make({"paths": [str(link)]}).test()
    assert not result.ok
    assert any("symlink" in problem for problem in result.problems)


def linked_tree(tmp_path: Path, link: str) -> tuple[Path, Path]:
    """``tree/a.log`` plus ``tree/jump`` -> ``outside/`` (holding ``secret.log``) as a symlink
    or a junction; returns the tree and the link."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.log").write_bytes(b"inside\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.log").write_bytes(b"outside\n")
    jump = tree / "jump"
    if link == "junction":
        if os.name != "nt":
            pytest.skip("junctions exist on Windows only")
        import _winapi  # noqa: PLC0415

        _winapi.CreateJunction(str(outside), str(jump))
    else:
        try:
            jump.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks need privileges on this platform")
    return tree, jump


@pytest.mark.parametrize("link", ["symlink", "junction"])
async def test_links_inside_a_tree_are_not_followed(tmp_path: Path, link: str) -> None:
    """A walk skips the linked directory; a glob that reaches through it loses the file to the
    real-path check and test() reports the count; the link itself is refused as a path."""
    tree, jump = linked_tree(tmp_path, link)
    records = await collect(make({"paths": [str(tree)]}))
    assert [record.locator for record in records] == ["a.log:line:1"]
    through_glob = make({"paths": [str(tree / "**" / "*.log")]})
    assert [r.locator for r in await collect(through_glob)] == ["a.log:line:1"]
    result = await through_glob.test()
    assert result.visible == ("a.log",)
    assert any("1 file(s) resolve outside" in problem for problem in result.problems)
    result = await make({"paths": [str(jump)]}).test()
    assert any(link in problem and "not followed" in problem for problem in result.problems)
    assert result.visible == ()


async def test_base_dir_confines_every_path(tmp_path: Path, logs: Path) -> None:
    """The offline analyzer sets base_dir to --input: nothing outside it is read, however the
    path is spelled."""
    base = tmp_path / "input"
    base.mkdir()
    (base / "a.log").write_bytes(b"inside\n")
    connector = make(
        {
            "paths": ["a.log", str(logs), "../orders/*.log", "../*.log", "sub/../a.log"],
            "base_dir": str(base),
        }
    )
    result = await connector.test()
    assert result.visible == ("a.log",)
    outside = [problem for problem in result.problems if "outside the base directory" in problem]
    assert len(outside) == 3
    assert all(str(logs) not in record.locator for record in await collect(connector))
    assert [record.locator for record in await collect(connector)] == ["a.log:line:1"]


async def test_test_reports_files_and_problems(logs: Path, tmp_path: Path) -> None:
    result = await make({"paths": [str(logs)]}).test()
    assert result.ok
    assert result.read_only is ReadOnlyStatus.VERIFIED
    assert result.visible == ("order-svc-2026-09-23.log", "order-svc-2026-09-24.log")
    missing = await make(
        {"paths": [str(tmp_path / "nope.log"), str(tmp_path / "*.nothing")]}
    ).test()
    assert not missing.ok
    assert any("does not exist" in problem for problem in missing.problems)
    assert any("matched no file" in problem for problem in missing.problems)


# ---------------------------------------------------------------------------------------------
# scenario A files (skipped when the simulator output is absent)
# ---------------------------------------------------------------------------------------------

needs_sim = pytest.mark.skipif(
    not SIM_OUT.is_dir(), reason="sim-out/shop not present; run make sim"
)


@needs_sim
async def test_sim_orders_logs() -> None:
    directory = SIM_OUT / "orders"
    records = await collect(make({"paths": [str(directory / "*.log")]}, "src_orders"))
    expected = 0
    for path in sorted(directory.glob("*.log")):
        with path.open("rb") as handle:
            expected += sum(1 for line in handle if line.strip())
    assert len(records) == expected
    assert records[0].locator == f"{min(directory.glob('*.log')).name}:line:1"
    assert all(record.kind is EventKind.LOG and record.text for record in records)


@needs_sim
async def test_sim_purchase_orders_rows() -> None:
    directory = SIM_OUT / "warehouse"
    connector = make(
        {
            "paths": [
                str(directory / "purchase_orders.csv"),
                str(directory / "purchase_orders.renamed.csv"),
            ],
            "kind": "rows",
            "table": "purchase_orders",
            "primary_key": "id",
        },
        "src_wms_db",
    )
    records = await collect(connector)
    assert records
    assert all(record.locator.startswith("purchase_orders:row:") for record in records)
    assert all(record.template_hint == "row_change purchase_orders" for record in records)
    ids = [record.locator.rsplit(":", 1)[1] for record in records]
    assert len(set(ids)) == len(ids)
    assert "po_num" in (records[0].fields or {})
    assert "po_number" in (records[-1].fields or {})


@needs_sim
async def test_sim_shipping_file_drops() -> None:
    directory = SIM_OUT / "shipping" / "outbound"
    records = await collect(
        make(
            {"paths": [str(directory)], "kind": "files", "include": ["SHIP_*.csv"]}, "src_ship_sftp"
        )
    )
    assert len(records) == len(list(directory.glob("SHIP_*.csv")))
    assert all(record.template_hint == "file_arrived SHIP_*_*.csv" for record in records)
    assert all(record.received_at.tzinfo is UTC for record in records)
    assert records[0].locator == "file:SHIP_20260923_2112.csv"
