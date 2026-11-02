"""carto_edge.pipeline.parse.names: the field name gate and map detection (ADR 0030).

Every value is synthetic. The detector is a stub that flags the names it is given.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from carto_edge.pipeline.parse.common import SegmentPath, flatten
from carto_edge.pipeline.parse.names import (
    MAX_MAP_ENTRIES,
    MIN_NAME_RECORDS,
    FieldNameStore,
    GatedRecord,
    exempt_prefixes,
)
from carto_edge.pipeline.pii import PiiHit

SYS = "sys_web"
DIGEST = b"0" * 32  # the shape of a persisted name or record id digest


class FlagDetector:
    """Flags a text when it contains one of ``names`` (as a person, score 0.9)."""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self.names = set(names)
        self.calls: list[str] = []

    def detect(self, text: str) -> list[PiiHit]:
        self.calls.append(text)
        for name in self.names:
            start = text.find(name)
            if start >= 0:
                return [PiiHit("PERSON", start, start + len(name), 0.9)]
        return []


def store(**kwargs: object) -> FieldNameStore:
    detector = kwargs.pop("detector", FlagDetector())
    return FieldNameStore(detector, **kwargs)  # type: ignore[arg-type]


def segments_of(path: str) -> SegmentPath:
    """A dotted test path as the flattener would report it: every part a key, except one or
    two ASCII digits, which stand for an array index."""
    return tuple(
        (part, len(part) <= 2 and part.isascii() and part.isdigit()) for part in path.split(".")
    )


def gate(
    names: FieldNameStore, fields: dict[str, str], trusted: frozenset[str] = frozenset()
) -> GatedRecord:
    top = tuple(dict.fromkeys(path.split(".")[0] for path in fields))
    structure = {path: segments_of(path) for path in fields if "." in path}
    return names.apply(SYS, fields, top, trusted, structure=structure)


def test_a_name_travels_once_seen_in_three_records() -> None:
    names = store()
    record = {"cart_id": "c-1", "region": "us-east"}
    for _ in range(MIN_NAME_RECORDS - 1):
        gated = gate(names, record)
        assert gated.fields == {"*": "c-1", "*~1": "us-east"}  # both masked, each on its own
        assert gated.top_keys == ("*", "*")
    gated = gate(names, record)
    assert gated.fields == record
    assert gated.top_keys == ("cart_id", "region")


def test_configured_names_are_trusted_from_the_first_record() -> None:
    names = store()
    trusted = names.trusted_set(["ts", "meta.actor"])
    gated = gate(names, {"ts": "t", "meta.actor": "a", "meta.other": "o"}, trusted)
    assert gated.fields == {"ts": "t", "meta.actor": "a", "meta.*": "o"}


def test_pin_paths_are_trusted_in_every_system() -> None:
    names = store(trusted_paths=["order.ref"])
    gated = gate(names, {"order.ref": "x"}, names.trusted_set([]))
    assert gated.fields == {"order.ref": "x"}


def test_a_name_counts_once_per_record() -> None:
    names = store()
    for _ in range(MIN_NAME_RECORDS - 1):
        assert "a.0.sku" not in gate(names, {"a.0.sku": "1", "a.1.sku": "2"}).fields
    assert gate(names, {"a.0.sku": "1", "a.1.sku": "2"}).fields == {"a.0.sku": "1", "a.1.sku": "2"}


def test_a_name_the_detector_flags_never_travels_and_is_checked_once() -> None:
    detector = FlagDetector(["Alice Smith"])
    names = store(detector=detector)
    for _ in range(10):
        gated = gate(names, {"stock.Alice Smith": "3", "stock.total": "9"})
        assert "Alice Smith" not in json.dumps(gated.fields)
    assert sum(1 for call in detector.calls if "Alice" in call) == 1
    assert gate(names, {"stock.total": "9"}).fields == {"stock.total": "9"}


@pytest.mark.parametrize("name", ["alice_smith", "aliceSmith", "alice-smith"])
def test_names_are_checked_as_words(name: str) -> None:
    detector = FlagDetector(["alice smith", "alice Smith"])
    names = store(detector=detector)
    for _ in range(5):
        gated = gate(names, {f"owner.{name}": "3"})
    assert gated.fields == {"owner.*": "3"}  # owner is trusted, the person's name never is


def test_value_like_segments_stay_masked_when_trusted() -> None:
    names = store()
    for _ in range(MIN_NAME_RECORDS):
        gated = gate(names, {"stock.SKU-12345": "1"})
    assert gated.fields == {"stock.*": "1"}


@pytest.mark.parametrize("segment", ["١٢", "12", "123", "007x"])
def test_a_numeric_key_is_a_name_not_an_array_index(segment: str) -> None:
    names = store()
    flat = flatten({"items": {segment: 1}})
    gated = names.apply(SYS, flat.fields, flat.top_keys, structure=flat.structure)
    assert gated.fields == {"*.*": "1"}


def test_a_parent_with_many_names_becomes_a_map_whose_keys_are_values() -> None:
    names = store(map_min_keys=16)
    customers = [f"Customer Name {chr(65 + i)}{chr(97 + i)}" for i in range(20)]
    for customer in customers[:16]:
        gate(names, {f"stock.{customer}.qty": "1", "stock_total": "9"})
    gated = gate(names, {"stock.Zed Quinn.qty": "4", "stock.Ann Lee": "5", "stock_total": "9"})
    assert gated.fields["stock.0.key"] == "Zed Quinn"
    assert gated.fields["stock.1.key"] == "Ann Lee"
    assert gated.fields["stock.1.value"] == "5"
    assert "Zed Quinn" not in "".join(gated.fields)  # never in a name
    # Names below an entry go through the gate under "stock.#.value".
    for _ in range(MIN_NAME_RECORDS):
        gated = gate(names, {"stock.Bo Day.qty": "4"})
    assert gated.fields == {"stock.0.key": "Bo Day", "stock.0.value.qty": "4"}


def test_map_entries_are_capped_like_arrays() -> None:
    names = store(map_min_keys=16)
    for i in range(16):
        gate(names, {f"m.k{chr(97 + i)}": "1"})
    record = {f"m.key{chr(97 + i % 26)}{chr(97 + i // 26)}": str(i) for i in range(40)}
    gated = gate(names, record)
    keys = [name for name in gated.fields if name.endswith(".key")]
    assert len(keys) == MAX_MAP_ENTRIES


def test_a_root_map_masks_the_signature_keys() -> None:
    names = store(map_min_keys=16)
    for i in range(16):
        gate(names, {f"name{chr(97 + i)}": "1"})
    gated = gate(names, {"Olive Stone": "3"})
    assert gated.top_keys == ("*",)
    assert gated.fields == {"0.key": "Olive Stone", "0.value": "3"}


def test_a_frozen_store_counts_nothing() -> None:
    names = store()
    gate(names, {"seen_once": "1", "seen_thrice": "1"})
    gate(names, {"seen_thrice": "1"})
    gate(names, {"seen_thrice": "1"})
    names.freeze()
    for _ in range(5):
        gated = gate(names, {"seen_once": "1", "seen_thrice": "2"})
    assert gated.fields == {"*": "1", "seen_thrice": "2"}


def test_parents_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("carto_edge.pipeline.parse.names.MAX_PARENTS", 1)
    names = store()
    for _ in range(MIN_NAME_RECORDS):
        gated = gate(names, {"a.x": "1", "b": "2"})
    # Only the root fits: top-level names are trusted, names under "a" stay masked.
    assert gated.fields == {"a.*": "1", "b": "2"}


def test_nested_names_count_from_the_first_record() -> None:
    names = store()
    for _ in range(MIN_NAME_RECORDS - 1):
        assert gate(names, {"order.lines.0.sku": "1"}).fields == {"*.*.0.*": "1"}
    assert gate(names, {"order.lines.0.sku": "1"}).fields == {"order.lines.0.sku": "1"}


def test_the_register_persists_without_raw_untrusted_names(tmp_path: Path) -> None:
    names = store(directory=tmp_path, flush_interval_seconds=3600)
    for _ in range(MIN_NAME_RECORDS):
        gate(names, {"cart_id": "1"})
    gate(names, {"Maria Lopez": "2"})
    names.close()
    text = (tmp_path / f"{SYS}.json").read_text(encoding="utf-8")
    assert "cart_id" in text
    assert "Maria" not in text
    again = store(directory=tmp_path)
    assert gate(again, {"cart_id": "1"}).fields == {"cart_id": "1"}


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b'{"version": 99, "parents": {}}',
        (
            b'{"version": 1, "parents": {"": {"map": false, "trusted": [], "flagged": [], '
            b'"counts": {"Maria Lopez": 1}}}}'
        ),
        (
            b'{"version": 1, "parents": {"": {"map": "no", "trusted": [], "flagged": [], '
            b'"counts": {}}}}'
        ),
        (  # a record id in clear
            b'{"version": 2, "parents": {"": {"map": false, "trusted": [], "flagged": [], '
            b'"counts": {"' + DIGEST + b'": 1}, '
            b'"records": {"' + DIGEST + b'": ["app.log:line:7"]}}}}'
        ),
        (  # more record ids than the count
            b'{"version": 2, "parents": {"": {"map": false, "trusted": [], "flagged": [], '
            b'"counts": {"' + DIGEST + b'": 1}, '
            b'"records": {"' + DIGEST + b'": ["' + DIGEST + b'", "' + DIGEST[:-1] + b'1"]}}}}'
        ),
    ],
)
def test_an_unreadable_register_is_ignored(tmp_path: Path, content: bytes) -> None:
    (tmp_path / f"{SYS}.json").write_bytes(content)
    names = store(directory=tmp_path)
    assert gate(names, {"cart_id": "1"}).fields == {"*": "1"}


def test_record_ids_persist_as_digests_and_survive_a_restart(tmp_path: Path) -> None:
    names = store(directory=tmp_path, flush_interval_seconds=3600)
    names.apply(SYS, {"cart_id": "1"}, ("cart_id",), record_id="app.log:line:7")
    names.close()
    text = (tmp_path / f"{SYS}.json").read_text(encoding="utf-8")
    assert "app.log" not in text
    assert '"records"' in text
    again = store(directory=tmp_path)
    for _ in range(3):  # the same record again, after a restart: no further count
        again.apply(SYS, {"cart_id": "1"}, ("cart_id",), record_id="app.log:line:7")
    masked = again.apply(SYS, {"cart_id": "1"}, ("cart_id",), record_id="app.log:line:8")
    assert masked.fields == {"*": "1"}
    clear = again.apply(SYS, {"cart_id": "1"}, ("cart_id",), record_id="app.log:line:9")
    assert clear.fields == {"cart_id": "1"}


def test_a_version_1_register_is_still_read(tmp_path: Path) -> None:
    (tmp_path / f"{SYS}.json").write_bytes(
        b'{"version": 1, "parents": {"": {"map": false, "trusted": ["cart_id"], "flagged": [], '
        b'"counts": {}}}}'
    )
    assert gate(store(directory=tmp_path), {"cart_id": "1"}).fields == {"cart_id": "1"}


def test_exempt_prefixes() -> None:
    assert exempt_prefixes(["meta.ts", None, "", "level"]) == {"meta", "meta.ts", "level"}


def test_an_invalid_system_id_is_refused() -> None:
    with pytest.raises(ValueError, match="system_id"):
        store().apply("../x", {"a": "1"}, ("a",))
