"""Field names travel only once they repeat; maps turn their keys into values (ADR 0030).

Field names leave the edge in clear (attribute keys, identifier and dropped field names, the
``keys:`` template), so a name must never carry a value (spec 2.3 invariant 2). ADR 0027 masks
segments that look like values (:func:`~carto_edge.pipeline.parse.common.safe_segment`); a key
without digits (a person's name keying a JSON map) passes that test. :class:`FieldNameStore`
adds two rules, per system:

- **Name gate.** A name read from the input is used in clear only once it has been seen in
  :data:`MIN_NAME_RECORDS` records under the same parent and the PII detector, run once when it
  gets there, does not flag it. Until then the segment is :data:`MASKED_SEGMENT`. Parents are
  keyed by the path so far: trusted names as written (value-like ones as ``*``), array indexes
  as ``#``, and a name not yet trusted as ``~<digest>``, so its children are counted from the
  first record; when the name is trusted its subtree moves under its name. Paths the
  configuration names (and their prefixes) are trusted from the first record. A record
  counts once however often it is delivered: :meth:`FieldNameStore.apply` takes the
  record's id (the parser passes its source and locator) and the register remembers,
  under a name's digest, the ids that already counted for it.
- **Map detection.** A parent with ``map_min_keys`` distinct names becomes a map for good. Its
  entries are written like array elements: ``<parent>.<i>.key`` holds the key as a value and
  the entry's content is under ``<parent>.<i>.value``; only the first :data:`MAX_MAP_ENTRIES`
  entries of a record are kept.

Untrusted names and record ids are counted under a 16-byte BLAKE2b digest, so the register
never stores a raw value; trusted names are stored in clear (they travel anyway). Memory is
bounded by :data:`MAX_PARENTS` per system; past it, every name under a new parent is masked.
The gateway persists the register per system as plain JSON (``<system>.json``, written
atomically); the offline analyzer keeps it in memory and calls :meth:`FieldNameStore.freeze`
before its second pass, which then counts nothing (ADR 0017, ADR 0027 addendum). Not
thread-safe: the pipeline calls it under its own lock.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from carto_common.logging import get_logger
from carto_edge.pipeline.classify import is_secret_name, pii_class_by_name
from carto_edge.pipeline.model import FieldClass
from carto_edge.pipeline.parse.common import (
    MASKED_SEGMENT,
    SegmentPath,
    clean_text,
    safe_path,
    safe_segment,
)
from carto_edge.pipeline.pii import PiiDetector
from carto_schema.event import MAX_FIELD_NAME_LEN, SOURCE_ID_PATTERN

__all__ = [
    "DEFAULT_MAP_MIN_KEYS",
    "MAP_KEY",
    "MAP_VALUE",
    "MAX_MAP_ENTRIES",
    "MAX_PARENTS",
    "MIN_NAME_RECORDS",
    "STATE_VERSION",
    "FieldNameStore",
    "GatedRecord",
    "exempt_prefixes",
]

MIN_NAME_RECORDS: Final = 3
"""Records a name must be seen in, under one parent, before it travels in clear."""
DEFAULT_MAP_MIN_KEYS: Final = 256
MAX_MAP_ENTRIES: Final = 20
"""Entries kept per map and record, as for arrays (spec 8.2 item 1)."""
MAX_PARENTS: Final = 4096
"""Parents tracked per system; names under a parent past the limit are masked."""
MAP_KEY: Final = "key"
MAP_VALUE: Final = "value"
INDEX: Final = "#"
_UNTRUSTED: Final = "~"
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS: Final = str.maketrans("_-.", "   ")
STATE_VERSION: Final = 2
"""Version 1 had no ``records``; it is still read."""
MAX_STATE_BYTES: Final = 32 * 1024 * 1024
DEFAULT_FLUSH_INTERVAL: Final = 60.0
_DIGEST_BYTES: Final = 16
_MAX_CACHE: Final = 65_536


def _digest(name: str) -> str:
    # surrogatepass: the parsers replace lone surrogates (clean_text); a caller that skipped
    # that must still not make the gate raise.
    data = name.encode("utf-8", errors="surrogatepass")
    return hashlib.blake2b(data, digest_size=_DIGEST_BYTES).hexdigest()


def _join(prefix: str, segment: str) -> str:
    return f"{prefix}.{segment}" if prefix else segment


def exempt_prefixes(paths: Iterable[str | None]) -> frozenset[str]:
    """Every path in ``paths`` with each of its prefixes (``meta.ts`` -> ``meta``, ``meta.ts``):
    the segments the configuration names, trusted from the first record."""
    out: set[str] = set()
    for path in paths:
        if not path:
            continue
        segments = path.split(".")
        for end in range(1, len(segments) + 1):
            out.add(".".join(segments[:end]))
    return frozenset(out)


@dataclass(slots=True)
class _Parent:
    is_map: bool = False
    trusted: set[str] = field(default_factory=set)
    flagged: set[str] = field(default_factory=set)  # digests the detector flagged
    counts: dict[str, int] = field(default_factory=dict)  # digest -> records seen
    records: dict[str, set[str]] = field(default_factory=dict)  # digest -> record id digests

    @property
    def distinct(self) -> int:
        return len(self.trusted) + len(self.flagged) + len(self.counts)


@dataclass(slots=True)
class _System:
    parents: dict[str, _Parent] = field(default_factory=dict)
    cache: dict[tuple[str | SegmentPath, frozenset[str]], str] = field(default_factory=dict)
    dirty: bool = False


@dataclass(frozen=True, slots=True)
class GatedRecord:
    """A record's fields and top-level keys after the gate. ``name_classes`` holds, for a
    gated name that hides its raw name (masked, or a map entry), the spec 8.3 rule 2 class the
    raw name implies (``customer_email`` -> ``contact``), so the classifier keeps applying the
    name rule to ``*``."""

    fields: dict[str, str]
    top_keys: tuple[str, ...]
    name_classes: dict[str, FieldClass] = field(default_factory=dict)


@dataclass(slots=True)
class _Record:
    """Per-record state of :meth:`FieldNameStore.apply`. ``record_id`` is the digest of the
    id the caller gave, or ``None`` when every call counts as a new record."""

    record_id: str | None = None
    out: dict[str, str] = field(default_factory=dict)
    seen: set[tuple[str, str]] = field(default_factory=set)
    entries: dict[str, dict[str, int]] = field(default_factory=dict)  # map entries by prefix
    masks: dict[tuple[str, str], str] = field(default_factory=dict)  # (prefix, raw) -> label
    mask_counts: dict[str, int] = field(default_factory=dict)  # labels used per prefix

    def mask(self, out_segments: Sequence[str], raw: str) -> str:
        """The masked label of ``raw`` under the gated prefix ``out_segments``: ``*`` for the
        first distinct raw name of the record there, then ``*~1``, ``*~2``, ... so unrelated
        values never share a field (and its statistics) within a record."""
        prefix = ".".join(out_segments)
        label = self.masks.get((prefix, raw))
        if label is None:
            count = self.mask_counts.get(prefix, 0)
            label = MASKED_SEGMENT if count == 0 else f"{MASKED_SEGMENT}~{count}"
            self.mask_counts[prefix] = count + 1
            self.masks[(prefix, raw)] = label
        return label


class FieldNameStore:
    """The per-system register of field names (see the module docstring).

    ``directory`` is ``EdgeSettings.names_dir``; ``None`` keeps everything in memory (the
    analyzer, tests). ``trusted_paths`` are trusted in every system (field policy pin paths).
    """

    def __init__(
        self,
        detector: PiiDetector,
        directory: Path | None = None,
        *,
        map_min_keys: int = DEFAULT_MAP_MIN_KEYS,
        trusted_paths: Iterable[str] = (),
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        self._detector = detector
        self._directory = directory
        self._map_min_keys = max(2, map_min_keys)
        self._trusted = exempt_prefixes(trusted_paths)
        self._flush_interval = max(0.0, flush_interval_seconds)
        self._systems: dict[str, _System] = {}
        self._frozen = False
        self._last_flush = time.monotonic()
        self._threshold = _score_threshold(detector)

    def trusted_set(self, paths: Iterable[str | None]) -> frozenset[str]:
        """The trusted prefixes for a parser: ``paths`` (its configured fields) with their
        prefixes, plus the store's own. Built once per parser and passed to :meth:`apply`."""
        return exempt_prefixes(paths) | self._trusted

    @property
    def frozen(self) -> bool:
        return self._frozen

    def freeze(self) -> None:
        """Stop counting: names trusted so far stay trusted, nothing else changes. The analyzer
        calls it before its second pass. Irreversible."""
        self._frozen = True

    # -- the gate -------------------------------------------------------------------------------

    def apply(
        self,
        system_id: str,
        fields: Mapping[str, str],
        top_keys: Sequence[str],
        trusted: frozenset[str] = frozenset(),
        *,
        structure: Mapping[str, SegmentPath] | None = None,
        record_id: str | None = None,
    ) -> GatedRecord:
        """Gate one record's flattened ``fields`` and its ``top_keys``. ``trusted`` comes from
        :meth:`trusted_set`; a path in it is used in clear from the first record. ``structure``
        gives the segments of nested paths (:class:`~carto_edge.pipeline.parse.common.Flattened`);
        a path without an entry is one segment. Value-like segments are masked as before (ADR
        0027); when two paths end up with the same name the first value is kept; a name longer
        than the canonical event allows is dropped. ``record_id`` identifies the record across
        deliveries (the parser passes ``<source_id>:<locator>``): its names count once, however
        often it is read; without it every call counts."""
        system = self._system(system_id)
        record = _Record(None if record_id is None else _digest(record_id))
        out = record.out
        name_classes: dict[str, FieldClass] = {}
        for path, value in fields.items():
            segments = structure.get(path) if structure else None
            key: str | SegmentPath = path if segments is None else segments
            cached = system.cache.get((key, trusted))
            if cached is not None:
                if cached not in out:
                    out[cached] = value
                continue
            if segments is None:
                segments = ((path, False),)
            name, cacheable, hint = self._gate_path(system, segments, trusted, record)
            if name is None:
                continue
            if cacheable:
                if len(system.cache) >= _MAX_CACHE:
                    system.cache.clear()
                system.cache[(key, trusted)] = name
            if name not in out:
                out[name] = value
                if hint is not None:
                    name_classes[name] = hint
        gated_keys: list[str] = []
        for key in top_keys:
            parent = system.parents.get("")
            if (parent is not None and parent.is_map) or not (
                key in trusted or self._name_trusted(system, "", key, record)
            ):
                gated_keys.append(MASKED_SEGMENT)
            else:
                gated_keys.append(safe_segment(key))
        return GatedRecord(fields=out, top_keys=tuple(gated_keys), name_classes=name_classes)

    def _gate_path(
        self,
        system: _System,
        segments: SegmentPath,
        trusted: frozenset[str],
        record: _Record,
    ) -> tuple[str | None, bool, FieldClass | None]:
        """The gated name of one path, whether it may be cached (every segment trusted and in
        clear, no map on the way: a trusted name stays trusted, so the answer cannot change
        until a parent becomes a map, which clears the cache), and the name class its raw path
        implies when the gated name hides it.

        A path whose raw name is secret-like (spec 8.3 rule 1: ``user_password``) is dropped
        here when its gated name hides that: a masked name would otherwise reach the classifier
        as ``*`` and an identifier-shaped secret could be tokenized with its ``raw`` form."""
        out = record.out
        out_segments: list[str] = []
        raw_segments: list[str] = []
        parent_key = ""
        cacheable = True
        hidden = False
        for segment, is_index in segments:
            raw_segments.append(segment)
            parent = system.parents.get(parent_key)
            if parent is not None and parent.is_map:
                cacheable = False
                hidden = True
                prefix = ".".join(out_segments)
                numbered = record.entries.setdefault(prefix, {})
                index = numbered.get(segment)
                if index is None:
                    if len(numbered) >= MAX_MAP_ENTRIES:
                        return None, False, None
                    index = len(numbered)
                    numbered[segment] = index
                    key_name = safe_path(_join(prefix, f"{index}.{MAP_KEY}"))
                    if key_name not in out:
                        out[key_name] = segment
                out_segments += [str(index), MAP_VALUE]
                parent_key = _join(_join(parent_key, INDEX), MAP_VALUE)
                continue
            if is_index:  # an array index from the flattener, never a key
                out_segments.append(segment)
                parent_key = _join(parent_key, INDEX)
                continue
            raw_prefix = ".".join(raw_segments)
            if raw_prefix in trusted or self._name_trusted(system, parent_key, segment, record):
                masked = safe_segment(segment)
                parent_key = _join(parent_key, masked)
                if masked != segment:  # trusted, but value-like (ADR 0027)
                    cacheable = False
                    hidden = True
                    masked = record.mask(out_segments, segment)
                out_segments.append(masked)
            else:
                cacheable = False
                hidden = True
                parent_key = _join(parent_key, _UNTRUSTED + _digest(segment))
                out_segments.append(record.mask(out_segments, segment))
        hint: FieldClass | None = None
        if hidden:
            raw_path = ".".join(raw_segments)
            if is_secret_name(raw_path):
                return None, False, None
            hint = pii_class_by_name(raw_path)
        name = safe_path(".".join(out_segments))
        if not name or len(name) > MAX_FIELD_NAME_LEN:
            return None, False, None
        return name, cacheable, hint

    def _name_trusted(self, system: _System, parent_key: str, name: str, record: _Record) -> bool:
        parent = system.parents.get(parent_key)
        if parent is None:
            if self._frozen or len(system.parents) >= MAX_PARENTS:
                return False
            parent = _Parent()
            system.parents[parent_key] = parent
            system.dirty = True
        if name in parent.trusted:
            return True
        if self._frozen or parent.is_map:
            return False
        digest = _digest(name)
        if digest in parent.flagged:
            return False
        if (parent_key, digest) in record.seen:
            return False
        record.seen.add((parent_key, digest))
        counted = parent.records.get(digest)
        if record.record_id is not None and counted is not None and record.record_id in counted:
            return False  # the same record delivered again counts no further
        count = parent.counts.get(digest)
        if count is None:
            if parent.distinct + 1 >= self._map_min_keys:
                parent.is_map = True
                parent.trusted.clear()
                parent.flagged.clear()
                parent.counts.clear()
                parent.records.clear()
                system.cache.clear()
                system.dirty = True
                _log().info("field_map_detected", parent=parent_key or "<root>")
                return False
            count = 0
        count += 1
        system.dirty = True
        if count < MIN_NAME_RECORDS:
            parent.counts[digest] = count
            if record.record_id is not None:
                parent.records.setdefault(digest, set()).add(record.record_id)
            self._maybe_flush()
            return False
        del parent.counts[digest]
        parent.records.pop(digest, None)
        if self._flags(name):
            parent.flagged.add(digest)
            return False
        parent.trusted.add(name)
        self._promote(system, parent_key, digest, name)
        self._maybe_flush()
        return True

    @staticmethod
    def _promote(system: _System, parent_key: str, digest: str, name: str) -> None:
        """Move the subtree counted under a name's digest (``a.~<digest>``) under the name now
        that it is trusted, so its children keep their counts."""
        old = _join(parent_key, _UNTRUSTED + digest)
        new = _join(parent_key, safe_segment(name))
        moved = [key for key in system.parents if key == old or key.startswith(old + ".")]
        for key in moved:
            system.parents.setdefault(new + key[len(old) :], system.parents.pop(key))
        if moved:
            system.cache.clear()

    def _flags(self, name: str) -> bool:
        """Whether the detector sees PII in the name read as words (``alice_smith`` and
        ``aliceSmith`` -> ``alice smith``). The words, not the name as written: spaCy's
        recogniser reads a bare ``order_id`` as a person and ``warehouse_code`` as a location,
        while the spaced forms of field names are clean and person names are still found."""
        words = clean_text(_CAMEL.sub(" ", name).translate(_SEPARATORS))
        return any(hit.score >= self._threshold for hit in self._detector.detect(words))

    # -- persistence ----------------------------------------------------------------------------

    def flush(self) -> None:
        """Write every changed system's register; no-op without a directory."""
        self._last_flush = time.monotonic()
        if self._directory is None:
            return
        for system_id, system in self._systems.items():
            if system.dirty:
                self._write(system_id, system)
                system.dirty = False

    def close(self) -> None:
        self.flush()

    def _maybe_flush(self) -> None:
        if self._directory is not None and (
            time.monotonic() - self._last_flush >= self._flush_interval
        ):
            self.flush()

    def _system(self, system_id: str) -> _System:
        system = self._systems.get(system_id)
        if system is None:
            if not SOURCE_ID_PATTERN.match(system_id):
                msg = "system_id must match the source id pattern"
                raise ValueError(msg)
            system = _System()
            if self._directory is not None:
                self._load(system_id, system)
            self._systems[system_id] = system
        return system

    def _path(self, system_id: str) -> Path:
        if self._directory is None:  # callers check
            msg = "the name store has no directory"
            raise RuntimeError(msg)
        return self._directory / f"{system_id}.json"

    def _write(self, system_id: str, system: _System) -> None:
        path = self._path(system_id)
        payload = {
            "version": STATE_VERSION,
            "system_id": system_id,
            "parents": {
                key: {
                    "map": parent.is_map,
                    "trusted": sorted(parent.trusted),
                    "flagged": sorted(parent.flagged),
                    "counts": dict(sorted(parent.counts.items())),
                    "records": {
                        digest: sorted(ids) for digest, ids in sorted(parent.records.items())
                    },
                }
                for key, parent in system.parents.items()
            },
        }
        # ASCII escapes: a name holding a lone surrogate (from a caller that did not clean it)
        # still serializes instead of failing the flush.
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        temporary = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(data)
            temporary.replace(path)
        except OSError as exc:
            _log().warning("name_state_write_failed", system_id=system_id, error=type(exc).__name__)
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)

    def _load(self, system_id: str, system: _System) -> None:
        path = self._path(system_id)
        try:
            if not path.is_file():
                return
            if path.stat().st_size > MAX_STATE_BYTES:
                _log().warning("name_state_too_large", system_id=system_id, path=str(path))
                return
            data = json.loads(path.read_bytes())
            system.parents = _parents_from_json(data)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            _log().warning(
                "name_state_unreadable",
                system_id=system_id,
                path=str(path),
                error=type(exc).__name__,
            )
            system.parents = {}


def _string_list(value: Any, what: str) -> set[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{what} must be a list of strings"
        raise ValueError(msg)
    return set(value)


def _is_digest(value: str) -> bool:
    return len(value) == 2 * _DIGEST_BYTES and all(c in "0123456789abcdef" for c in value)


def _parents_from_json(data: Any) -> dict[str, _Parent]:
    """Validate a persisted register; raises ``ValueError`` on anything unexpected."""
    if not isinstance(data, dict) or data.get("version") not in (1, STATE_VERSION):
        msg = "unknown name state version"
        raise ValueError(msg)
    raw = data.get("parents")
    if not isinstance(raw, dict) or len(raw) > MAX_PARENTS:
        msg = "parents must be a mapping within the limit"
        raise ValueError(msg)
    parents: dict[str, _Parent] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            msg = "a parent must be a mapping under a string key"
            raise ValueError(msg)
        is_map = item.get("map")
        counts = item.get("counts")
        if not isinstance(is_map, bool) or not isinstance(counts, dict):
            msg = "a parent needs a map flag and counts"
            raise ValueError(msg)
        flagged = _string_list(item.get("flagged"), "flagged")
        if not all(_is_digest(value) for value in flagged) or not all(
            isinstance(digest, str)
            and _is_digest(digest)
            and isinstance(count, int)
            and 0 < count < MIN_NAME_RECORDS
            for digest, count in counts.items()
        ):
            msg = "counts and flagged names must be digests"
            raise ValueError(msg)
        parents[key] = _Parent(
            is_map=is_map,
            trusted=_string_list(item.get("trusted"), "trusted"),
            flagged=flagged,
            counts=dict(counts),
            records=_records_from_json(item.get("records", {}), counts),
        )
    return parents


def _records_from_json(raw: Any, counts: Mapping[str, int]) -> dict[str, set[str]]:
    """The record ids (digests) counted under each pending name; every entry must belong to
    a counted name and hold no more ids than the count."""
    if not isinstance(raw, dict):
        msg = "records must be a mapping"
        raise ValueError(msg)
    records: dict[str, set[str]] = {}
    for digest, ids in raw.items():
        if not isinstance(digest, str) or digest not in counts:
            msg = "records must belong to counted names"
            raise ValueError(msg)
        found = _string_list(ids, "records")
        if len(found) > counts[digest] or not all(_is_digest(value) for value in found):
            msg = "record ids must be digests within the count"
            raise ValueError(msg)
        records[digest] = found
    return records


def _score_threshold(detector: PiiDetector) -> float:
    """The configured threshold when the detector carries settings, else 0.5 (spec default)."""
    settings = getattr(detector, "settings", None)
    threshold = getattr(settings, "score_threshold", None)
    return float(threshold) if isinstance(threshold, int | float) else 0.5


def _log() -> Any:
    return get_logger(component="field_names")
