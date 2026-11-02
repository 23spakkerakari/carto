"""Bundle verification and loading (spec 8.1.1, ADR 0022, ADR 0034, ADR 0036): a synthetic
bundle built exactly as carto_schema.bundle describes verifies and streams; any tampering,
extra file or bad line is refused without echoing content; loads are chunked, derived-id
batches that are idempotent through the ledger's claims and skip events outside the plausible
range; the CLI wraps both."""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import zstandard

import carto_core.bundle as bundle_module
from carto_common.crypto import SigningKey, b64url_encode
from carto_common.ids import derive_ulid
from carto_common.settings import RetentionSettings
from carto_core import cli
from carto_core.bundle import (
    BundleError,
    LoadResult,
    VerifiedBundle,
    bundle_source_id,
    iter_events,
    load_bundle,
    verify_bundle,
)
from carto_core.db.clickhouse import WriterError, WriteResult
from carto_core.ingest.ledger import ClaimState, InMemoryBatchLedger, LedgerEntry, LedgerError
from carto_schema.bundle import (
    DATA_FILES,
    EVENTS_FILE,
    FIELDS_FILE,
    LOCATOR_MAP_FILE,
    MANIFEST_FILE,
    MANIFEST_MD_FILE,
    SIGNATURE_FILE,
    TEMPLATES_FILE,
    BundleCounts,
    BundleManifest,
    BundleSignature,
    BundleSourceSummary,
    FileDigest,
)
from carto_schema.event import CanonicalEvent

CREATED_AT = datetime(2026, 10, 8, 9, 30, 0, 250000, tzinfo=UTC)
BUNDLE_ID = "01K71Y5B2XQ0M4N8P3R6S9T1VW"
BASE_MS = 1_790_000_000_000
LOAD_NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
"""The load clock: the events of BASE_MS (2026-09-21) are inside the plausible range."""


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[list[CanonicalEvent]] = []

    def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
        self.batches.append(list(events))
        return WriteResult(len(events), sum(len(event.identifiers) for event in events))

    def ping(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _fixed_load_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bundle_module, "_utcnow", lambda: LOAD_NOW)


def _events(count: int, tenant_id: str = "default") -> list[CanonicalEvent]:
    base = CanonicalEvent.example().model_dump()
    events: list[CanonicalEvent] = []
    for index in range(count):
        observed = datetime.fromtimestamp((BASE_MS + index) / 1000, tz=UTC)
        data = base | {
            "event_id": derive_ulid(BASE_MS + index, "src_wms_db", str(index)),
            "tenant_id": tenant_id,
            "observed_at": observed,
            "ingested_at": observed + timedelta(seconds=5),
        }
        events.append(CanonicalEvent.model_validate(data))
    return events


def _write_bundle(
    path: Path,
    events: Sequence[CanonicalEvent],
    *,
    tenant_id: str = "default",
    raw_lines: bytes | None = None,
    extra_manifest_files: dict[str, FileDigest] | None = None,
    counted_events: int | None = None,
) -> SigningKey:
    """A bundle exactly as carto_schema.bundle describes it; returns the run's signing key."""
    path.mkdir(parents=True)
    lines = (
        raw_lines
        if raw_lines is not None
        else b"".join(event.model_dump_json().encode("utf-8") + b"\n" for event in events)
    )
    (path / EVENTS_FILE).write_bytes(zstandard.ZstdCompressor(level=3).compress(lines))
    (path / FIELDS_FILE).write_bytes(b"[]\n")
    (path / TEMPLATES_FILE).write_bytes(b"[]\n")
    (path / MANIFEST_MD_FILE).write_bytes(b"# test bundle\n")
    files = {
        name: FileDigest(
            sha256=hashlib.sha256((path / name).read_bytes()).hexdigest(),
            bytes=(path / name).stat().st_size,
        )
        for name in DATA_FILES
    }
    files.update(extra_manifest_files or {})
    count = len(events)
    manifest = BundleManifest(
        bundle_version="1",
        schema_version="1",
        bundle_id=BUNDLE_ID,
        tenant_id=tenant_id,
        created_at=CREATED_AT,
        producer="carto-edge test 0.1.0",
        key_versions=[1],
        policy_version="3",
        sources=[
            BundleSourceSummary(
                source_id="src_wms_db",
                system_id="sys_warehouse",
                connector_type="upload",
                records_read=count,
                events_written=count,
                records_dropped=0,
                parse_errors=0,
            )
        ],
        counts=BundleCounts(
            records_read=count,
            events=counted_events if counted_events is not None else count,
            records_dropped=0,
            parse_errors=0,
            identifiers=4 * count,
            fields_kept=2,
            fields_tokenized=2,
            fields_dropped=2,
        ),
        files=files,
    )
    manifest_bytes = manifest.model_dump_json(indent=2).encode("utf-8")
    (path / MANIFEST_FILE).write_bytes(manifest_bytes)
    key = SigningKey.generate()
    signature = BundleSignature(
        algorithm="ed25519",
        key_id=key.verify_key.key_id,
        public_key=key.verify_key.to_text(),
        signature=b64url_encode(key.sign(manifest_bytes)),
        signed_at=CREATED_AT,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )
    (path / SIGNATURE_FILE).write_bytes(signature.model_dump_json(indent=2).encode("utf-8"))
    return key


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "scenario-a.carto"
    _write_bundle(path, _events(12))
    return path


def _flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))


# --- verification -------------------------------------------------------------------------------


def test_round_trip_verifies_and_streams_every_event(bundle: Path) -> None:
    verified = verify_bundle(bundle)
    assert isinstance(verified, VerifiedBundle)
    assert verified.manifest.bundle_id == BUNDLE_ID
    assert verified.manifest.counts.events == 12
    assert verified.signature.key_id == verified.signature.key_id
    assert verified.path == bundle
    events = list(iter_events(verified))
    assert events == _events(12)


def test_flipped_byte_in_the_events_file_fails_the_digest(bundle: Path) -> None:
    _flip_byte(bundle / EVENTS_FILE)
    with pytest.raises(BundleError, match=f"{EVENTS_FILE} digest"):
        verify_bundle(bundle)


def test_tampered_manifest_fails_the_digest_or_the_signature(bundle: Path) -> None:
    manifest_path = bundle / MANIFEST_FILE
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(original.replace(b'"policy_version": "3"', b'"policy_version": "4"'))
    with pytest.raises(BundleError, match="does not match the digest"):
        verify_bundle(bundle)
    # An attacker who also rewrites manifest_sha256 still fails on the signature.
    tampered = manifest_path.read_bytes()
    signature_path = bundle / SIGNATURE_FILE
    signature = json.loads(signature_path.read_text(encoding="utf-8"))
    signature["manifest_sha256"] = hashlib.sha256(tampered).hexdigest()
    signature_path.write_text(json.dumps(signature), encoding="utf-8")
    with pytest.raises(BundleError, match="signature is invalid"):
        verify_bundle(bundle)


def test_key_id_must_match_the_embedded_public_key(bundle: Path) -> None:
    signature_path = bundle / SIGNATURE_FILE
    signature = json.loads(signature_path.read_text(encoding="utf-8"))
    signature["key_id"] = "0123456789abcdef"
    signature_path.write_text(json.dumps(signature), encoding="utf-8")
    with pytest.raises(BundleError, match="key id"):
        verify_bundle(bundle)


def test_extra_files_in_the_directory_are_refused(bundle: Path) -> None:
    (bundle / LOCATOR_MAP_FILE).write_text("{}\n", encoding="utf-8")
    with pytest.raises(BundleError, match=f"unexpected entry.*{LOCATOR_MAP_FILE}"):
        verify_bundle(bundle)
    (bundle / LOCATOR_MAP_FILE).unlink()
    (bundle / "nested").mkdir()
    with pytest.raises(BundleError, match=r"unexpected entry.*nested"):
        verify_bundle(bundle)


def test_manifest_listing_a_file_outside_data_files_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "odd.carto"
    extra = {"secrets.json": FileDigest(sha256="0" * 64, bytes=0)}
    _write_bundle(path, _events(1), extra_manifest_files=extra)
    with pytest.raises(BundleError, match=r"not bundle data files: secrets\.json"):
        verify_bundle(path)


def test_missing_and_resized_listed_files_are_refused(bundle: Path) -> None:
    with (bundle / TEMPLATES_FILE).open("ab") as handle:
        handle.write(b"\n")
    with pytest.raises(BundleError, match=f"{TEMPLATES_FILE} has"):
        verify_bundle(bundle)
    (bundle / TEMPLATES_FILE).unlink()
    with pytest.raises(BundleError, match=f"{TEMPLATES_FILE} is missing"):
        verify_bundle(bundle)


def test_not_a_bundle(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="not a bundle directory"):
        verify_bundle(tmp_path / "absent.carto")
    empty = tmp_path / "empty.carto"
    empty.mkdir()
    with pytest.raises(BundleError, match=SIGNATURE_FILE):
        verify_bundle(empty)


# --- streaming events -------------------------------------------------------------------------


def test_an_invalid_line_fails_fast_and_names_the_line_not_its_content(tmp_path: Path) -> None:
    good = _events(3)
    lines = [event.model_dump_json() for event in good]
    bad = dict(json.loads(lines[1]))
    bad["identifiers"][0]["token"] = "RAWLEAKVALUE-4471"  # noqa: S105 - a planted raw value
    lines[1] = json.dumps(bad)
    path = tmp_path / "bad.carto"
    _write_bundle(path, good, raw_lines="\n".join(lines).encode("utf-8") + b"\n")
    verified = verify_bundle(path)
    seen: list[CanonicalEvent] = []
    with pytest.raises(BundleError, match=f"{EVENTS_FILE} line 2") as info:
        seen.extend(iter_events(verified))
    assert "RAWLEAKVALUE" not in str(info.value)
    assert seen == good[:1], "stopped at the first bad line"


def test_events_of_another_tenant_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "tenant.carto"
    _write_bundle(path, _events(2, tenant_id="other"), tenant_id="default")
    with pytest.raises(BundleError, match="line 1 belongs to another tenant"):
        list(iter_events(verify_bundle(path)))


def test_manifest_count_must_match_the_file(tmp_path: Path) -> None:
    path = tmp_path / "count.carto"
    _write_bundle(path, _events(2), counted_events=3)
    with pytest.raises(BundleError, match=r"counts 3 events, .* holds 2"):
        list(iter_events(verify_bundle(path)))


def test_blank_lines_and_crlf_are_tolerated(tmp_path: Path) -> None:
    events = _events(2)
    raw = b"\r\n".join(event.model_dump_json().encode() for event in events) + b"\r\n\r\n"
    path = tmp_path / "crlf.carto"
    _write_bundle(path, events, raw_lines=raw)
    assert list(iter_events(verify_bundle(path))) == events


# --- loading (ADR 0034, 0036) ---------------------------------------------------------------


def test_load_writes_chunks_with_derived_batch_ids(bundle: Path) -> None:
    verified = verify_bundle(bundle)
    writer, ledger = FakeWriter(), InMemoryBatchLedger()
    now = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    result = load_bundle(verified, writer, ledger, chunk_size=5, clock=lambda: now)
    assert result == LoadResult(events_written=12, chunks=3, duplicates=0)
    assert [len(batch) for batch in writer.batches] == [5, 5, 2]
    assert [event.event_id for batch in writer.batches for event in batch] == [
        event.event_id for event in _events(12)
    ]
    created_at_ms = int(CREATED_AT.timestamp() * 1000)
    expected_ids = [derive_ulid(created_at_ms, BUNDLE_ID, str(index)) for index in range(3)]
    assert [entry.batch_id for entry in ledger.entries] == expected_ids
    assert ledger.entries[0] == LedgerEntry(
        "default", expected_ids[0], bundle_source_id(verified.manifest), 5, now
    )
    assert bundle_source_id(verified.manifest) == f"bundle_{BUNDLE_ID.lower()}"


def test_a_second_load_is_all_duplicates_and_writes_nothing(bundle: Path) -> None:
    verified = verify_bundle(bundle)
    writer, ledger = FakeWriter(), InMemoryBatchLedger()
    load_bundle(verified, writer, ledger, chunk_size=5)
    again = load_bundle(verified, writer, ledger, chunk_size=5)
    assert again == LoadResult(events_written=0, chunks=3, duplicates=3)
    assert len(writer.batches) == 3
    assert len(ledger.entries) == 3


def test_an_interrupted_load_resumes_at_the_first_missing_chunk(bundle: Path) -> None:
    verified = verify_bundle(bundle)
    writer, ledger = FakeWriter(), InMemoryBatchLedger()
    created_at_ms = int(CREATED_AT.timestamp() * 1000)
    first = derive_ulid(created_at_ms, BUNDLE_ID, "0")
    ledger.complete(LedgerEntry("default", first, "bundle_x", 5, LOAD_NOW), LOAD_NOW)
    result = load_bundle(verified, writer, ledger, chunk_size=5)
    assert result == LoadResult(events_written=7, chunks=3, duplicates=1)
    assert [len(batch) for batch in writer.batches] == [5, 2]


def test_default_chunk_size_is_the_ingest_batch_cap(bundle: Path) -> None:
    verified = verify_bundle(bundle)
    writer = FakeWriter()
    assert load_bundle(verified, writer, InMemoryBatchLedger()).chunks == 1
    assert len(writer.batches[0]) == 12


def test_events_outside_the_plausible_range_are_skipped_and_counted(tmp_path: Path) -> None:
    """ADR 0036: an event older than retention would be deleted on arrival, a future one is
    refused by ingest-api; a bundle loads its recent part and reports the rest."""
    events = _events(6)
    old = LOAD_NOW - timedelta(days=40)
    ahead = LOAD_NOW + timedelta(days=2)
    events[1] = events[1].model_copy(update={"observed_at": old})
    events[2] = events[2].model_copy(update={"observed_at": old})
    events[4] = events[4].model_copy(update={"observed_at": ahead})
    path = tmp_path / "ranged.carto"
    _write_bundle(path, events)
    writer, ledger = FakeWriter(), InMemoryBatchLedger()
    result = load_bundle(verify_bundle(path), writer, ledger, chunk_size=4)
    assert result == LoadResult(events_written=3, chunks=2, duplicates=0, too_old=2, future=1)
    assert [[event.event_id for event in batch] for batch in writer.batches] == [
        [events[0].event_id, events[3].event_id],
        [events[5].event_id],
    ]
    assert [entry.event_count for entry in ledger.entries] == [4, 2], "chunks cut before"


def test_the_load_range_follows_the_configured_retention(bundle: Path) -> None:
    writer = FakeWriter()
    result = load_bundle(
        verify_bundle(bundle),
        writer,
        InMemoryBatchLedger(),
        retention=RetentionSettings(events_days=7),
    )
    assert result.too_old == 12 and result.events_written == 0
    assert writer.batches == [[]]


def test_a_chunk_another_load_is_writing_stops_the_load(bundle: Path) -> None:
    verified = verify_bundle(bundle)
    ledger = InMemoryBatchLedger()
    created_at_ms = int(CREATED_AT.timestamp() * 1000)
    first = derive_ulid(created_at_ms, BUNDLE_ID, "0")
    entry = LedgerEntry("default", first, "bundle_x", 5, LOAD_NOW - timedelta(seconds=10))
    assert ledger.claim(entry, LOAD_NOW - timedelta(days=1)).state is ClaimState.CLAIMED
    with pytest.raises(LedgerError, match="chunk 0 is being loaded by another run"):
        load_bundle(verified, FakeWriter(), ledger, chunk_size=5)


def test_a_failed_chunk_write_releases_its_claim(bundle: Path) -> None:
    class Failing(FakeWriter):
        def write_events(self, events: Sequence[CanonicalEvent]) -> WriteResult:
            raise WriterError("ClickHouse insert failed: DatabaseError")

    ledger = InMemoryBatchLedger()
    with pytest.raises(WriterError):
        load_bundle(verify_bundle(bundle), Failing(), ledger, chunk_size=5)
    assert ledger.rows == ()
    assert load_bundle(verify_bundle(bundle), FakeWriter(), ledger, chunk_size=5).chunks == 3


@pytest.mark.parametrize("size", [0, -1, 5001])
def test_chunk_size_is_bounded(bundle: Path, size: int) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        load_bundle(verify_bundle(bundle), FakeWriter(), InMemoryBatchLedger(), chunk_size=size)


# --- carto-core verify-bundle / load-bundle ----------------------------------------------------


def test_cli_verify_bundle(bundle: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["verify-bundle", "--bundle", str(bundle)]) == 0
    out = capsys.readouterr().out
    assert BUNDLE_ID in out and "12 events" in out
    _flip_byte(bundle / FIELDS_FILE)
    assert cli.main(["verify-bundle", "--bundle", str(bundle)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "rejected" in captured.err and FIELDS_FILE in captured.err


def test_cli_load_bundle_is_idempotent(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    writer, ledger = FakeWriter(), InMemoryBatchLedger()
    monkeypatch.setattr(
        cli, "_open_stores", lambda _settings: contextlib.nullcontext((writer, ledger))
    )
    assert cli.main(["load-bundle", "--bundle", str(bundle), "--chunk-size", "5"]) == 0
    first = capsys.readouterr().out
    assert "12 events written" in first and "3 chunks" in first
    assert cli.main(["load-bundle", "--bundle", str(bundle), "--chunk-size", "5"]) == 0
    second = capsys.readouterr().out
    assert "0 events written" in second and "3 chunks already" in second
    assert len(writer.batches) == 3


def test_cli_load_bundle_rejects_a_tampered_bundle_before_opening_stores(
    bundle: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(_settings: object) -> tuple[FakeWriter, InMemoryBatchLedger]:
        raise AssertionError("stores must not be opened for a rejected bundle")

    monkeypatch.setattr(cli, "_open_stores", explode)
    _flip_byte(bundle / MANIFEST_MD_FILE)
    assert cli.main(["load-bundle", "--bundle", str(bundle)]) == 1
    assert "rejected" in capsys.readouterr().err
