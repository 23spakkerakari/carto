"""Streaming field statistics (spec 8.3): what the classifier knows about a field.

Spec 8.3: "For every field the edge keeps local streaming stats: count, HyperLogLog distinct
estimate, null rate, shape histogram (...). Stats persist across restarts."

:class:`FieldStats` holds, per field:

- ``count`` and ``null_count`` (``None`` and the empty string count as null);
- a HyperLogLog++ sketch (:data:`HLL_PRECISION` 12, about 1.6% standard error, 4 KB of
  registers updated in plain Python, datasketch's bias-corrected count at decision time) for
  the distinct estimate, capped by the number of non-null values so small sets are exact;
- a shape histogram (:func:`carto_schema.forms.shape`) capped at :data:`MAX_SHAPES` distinct
  shapes, with everything beyond that counted under :data:`OVERFLOW_SHAPE`;
- length minimum, maximum and mean, the share of whitespace characters and the share of values
  containing whitespace (rules 5 and 7);
- the number of secret-shaped values ever seen (rule 1,
  :func:`carto_edge.pipeline.pii.looks_like_secret`), counted at observation so the verdict
  survives reservoir eviction and restarts;
- two sticky flags the classifier sets and nothing unsets (ADR 0068): ``high_cardinality_seen``
  (rule 5's test was true at some decision) and ``detector_hits_seen`` (the detector hit a
  sample at some decision). They persist in the snapshot, so a flood of one constant value
  that drowns the distinct ratio and washes the reservoir cannot turn an identifier or a
  field that carried PII into a kept attribute;
- an exact count per distinct value, for the first :data:`MAX_COUNTED_VALUES` distinct values
  (values longer than :data:`COUNTED_VALUE_MAX_LEN` characters are counted under a BLAKE2b
  digest). The classifier lets a ``keep`` field's value travel in clear only once it was seen
  :data:`carto_edge.pipeline.classify.MIN_KEPT_VALUE_COUNT` times. A field that goes past the
  cap has too many distinct values to be an attribute: its counts are freed and no value of it
  is kept again. Like the reservoir, the counts live in memory only and start empty after a
  restart, so a kept value needs fresh sightings before it travels again;
- a reservoir sample of values (Algorithm R, ``ClassifySettings.sample_values`` entries, each
  cut at :data:`SAMPLE_MAX_LEN` characters). The reservoir lives in memory only: it is **never**
  part of a snapshot, a file or a log line (spec 2.3 invariants 2 and 7). It is what the
  classifier hands to the PII detector and to the secret-pattern check.

:class:`FieldStatsStore` keys the stats by ``field_ref`` and persists everything except the
reservoirs as one JSON document (``field_stats.json`` under the state directory): the sketch
registers travel zlib-compressed and base64-encoded. ``save`` writes a temporary file next to
the target and ``os.replace``s it, so a crash never leaves a half-written file; ``load`` treats a
missing, unreadable, corrupt or foreign-version file as "start empty" and logs a warning,
because stale statistics only delay classification (quarantine rule 8) while a crash loop would
stop ingestion. The snapshot format is versioned (:data:`SNAPSHOT_VERSION`): version 2 added
the two flags; a version 1 snapshot loads with both false.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import threading
import zlib
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from datasketch import HyperLogLogPlusPlus

from carto_common.logging import get_logger
from carto_edge.config import ClassifySettings
from carto_edge.pipeline.pii import looks_like_secret
from carto_schema.forms import shape

__all__ = [
    "COUNTED_VALUE_MAX_LEN",
    "HLL_PRECISION",
    "MAX_COUNTED_VALUES",
    "MAX_SHAPES",
    "OVERFLOW_SHAPE",
    "SAMPLE_MAX_LEN",
    "SNAPSHOT_VERSION",
    "FieldStats",
    "FieldStatsStore",
    "StatsFormatError",
]

SNAPSHOT_VERSION: Final = 2
_READABLE_VERSIONS: Final = frozenset({1, SNAPSHOT_VERSION})
HLL_PRECISION: Final = 12
MAX_SHAPES: Final = 256
OVERFLOW_SHAPE: Final = "<other>"
"""Histogram bucket for shapes beyond the cap. Letters map to ``A`` in a real shape, so no value
can produce this string and the bucket can never collide with a real shape."""
SAMPLE_MAX_LEN: Final = 512
MAX_COUNTED_VALUES: Final = 2048
"""Distinct values counted exactly per field; past this the field is not an attribute."""
COUNTED_VALUE_MAX_LEN: Final = 64
"""Longer values are counted under a 16-byte BLAKE2b digest to bound memory."""
"""Reservoir entries are cut at this many characters; statistics use the full value."""

_HLL_M: Final = 1 << HLL_PRECISION
_HLL_MAX_RANK: Final = 64 - HLL_PRECISION


def _value_key(value: str) -> str | bytes:
    if len(value) <= COUNTED_VALUE_MAX_LEN:
        return value
    return hashlib.blake2b(value.encode("utf-8", errors="surrogatepass"), digest_size=16).digest()


def _hll_add(registers: bytearray, data: bytes) -> None:
    """HyperLogLog++ register update in plain Python (spec 17: the per-value path).

    64-bit blake2b hash; the low ``p`` bits pick the register, the rank is the position of the
    first set bit in the remaining bits, as in datasketch, whose bias-corrected ``count`` and
    serialized layout the sketch reuses at decision time. Three times cheaper per value than
    ``HyperLogLogPlusPlus.update`` (no numpy scalar round trip), which matters at 20,000 field
    values per second.
    """
    hashed = int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "little")
    index = hashed & (_HLL_M - 1)
    rank = _HLL_MAX_RANK - (hashed >> HLL_PRECISION).bit_length() + 1
    if rank > registers[index]:
        registers[index] = rank


class StatsFormatError(ValueError):
    """A snapshot is not in the expected format. The message never carries a value."""


def _logger() -> Any:
    return get_logger(component="carto_edge.pipeline.stats")


def _require_int(data: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        msg = f"snapshot field {key!r} must be an integer >= {minimum}"
        raise StatsFormatError(msg)
    return value


def _require_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key, False)
    if not isinstance(value, bool):
        msg = f"snapshot field {key!r} must be a boolean"
        raise StatsFormatError(msg)
    return value


class FieldStats:
    """Streaming statistics of one field (spec 8.3). Not thread-safe on its own: the store
    serializes access."""

    __slots__ = (
        "_length_max",
        "_length_min",
        "_length_sum",
        "_registers",
        "_rng",
        "_samples",
        "_secret_count",
        "_shapes",
        "_values",
        "_whitespace_chars",
        "_whitespace_values",
        "count",
        "detector_hits_seen",
        "high_cardinality_seen",
        "null_count",
        "sample_size",
    )

    def __init__(self, sample_size: int, *, seed: int | None = None) -> None:
        if sample_size < 1:
            msg = "sample_size must be at least 1"
            raise ValueError(msg)
        self.sample_size = sample_size
        self.count = 0
        self.null_count = 0
        # Sticky (ADR 0068): set by the classifier through FieldStatsStore.note, never unset.
        self.high_cardinality_seen = False
        self.detector_hits_seen = False
        self._registers = bytearray(_HLL_M)
        self._shapes: Counter[str] = Counter()
        # None once the field went past MAX_COUNTED_VALUES distinct values (see the module).
        self._values: Counter[str | bytes] | None = Counter()
        self._length_min = 0
        self._length_max = 0
        self._length_sum = 0
        self._whitespace_chars = 0
        self._whitespace_values = 0
        self._secret_count = 0
        self._samples: list[str] = []
        # Reservoir sampling needs a uniform index, not unpredictability (ruff S311 does not
        # apply: nothing here is a credential, a token or a key).
        self._rng = random.Random(seed)  # noqa: S311

    # -- observation -------------------------------------------------------------------------

    def observe(self, value: str | None) -> None:
        """Account for one value. ``None`` and ``""`` count as null."""
        self.count += 1
        if value is None or value == "":
            self.null_count += 1
            return
        _hll_add(self._registers, value.encode("utf-8", errors="surrogatepass"))
        value_shape = shape(value)
        if value_shape in self._shapes or len(self._shapes) < MAX_SHAPES:
            self._shapes[value_shape] += 1
        else:
            self._shapes[OVERFLOW_SHAPE] += 1
        counts = self._values
        if counts is not None:
            key = _value_key(value)
            if key in counts or len(counts) < MAX_COUNTED_VALUES:
                counts[key] += 1
            else:
                self._values = None
        length = len(value)
        non_null = self.non_null_count
        if non_null == 1:
            self._length_min = length
            self._length_max = length
        else:
            self._length_min = min(self._length_min, length)
            self._length_max = max(self._length_max, length)
        self._length_sum += length
        spaces = sum(1 for char in value if char.isspace())
        self._whitespace_chars += spaces
        if spaces:
            self._whitespace_values += 1
        if looks_like_secret(value):
            self._secret_count += 1
        self._reservoir_add(value[:SAMPLE_MAX_LEN], non_null)

    def _reservoir_add(self, value: str, seen: int) -> None:
        """Algorithm R: the ``seen``-th non-null value replaces a random slot with probability
        ``sample_size / seen`` once the reservoir is full."""
        if len(self._samples) < self.sample_size:
            self._samples.append(value)
            return
        slot = self._rng.randrange(seen)
        if slot < self.sample_size:
            self._samples[slot] = value

    # -- derived figures ---------------------------------------------------------------------

    @property
    def non_null_count(self) -> int:
        return self.count - self.null_count

    @property
    def distinct_estimate(self) -> int:
        """HyperLogLog++ estimate (datasketch's bias correction over the registers), never
        above the number of non-null values."""
        non_null = self.non_null_count
        if non_null == 0:
            return 0
        sketch = HyperLogLogPlusPlus.deserialize(bytes([HLL_PRECISION]) + bytes(self._registers))
        return max(1, min(non_null, round(float(sketch.count()))))

    @property
    def null_rate(self) -> float:
        return self.null_count / self.count if self.count else 0.0

    @property
    def length_min(self) -> int:
        return self._length_min

    @property
    def length_max(self) -> int:
        return self._length_max

    @property
    def length_mean(self) -> float:
        non_null = self.non_null_count
        return self._length_sum / non_null if non_null else 0.0

    @property
    def whitespace_share(self) -> float:
        """Whitespace characters over all characters of non-null values (rule 5: "not mostly
        whitespace")."""
        return self._whitespace_chars / self._length_sum if self._length_sum else 0.0

    @property
    def whitespace_value_rate(self) -> float:
        """Share of non-null values that contain whitespace (rule 7: "strings with spaces")."""
        non_null = self.non_null_count
        return self._whitespace_values / non_null if non_null else 0.0

    @property
    def secret_count(self) -> int:
        """Non-null values that looked like a secret (rule 1). Persisted."""
        return self._secret_count

    @property
    def samples(self) -> tuple[str, ...]:
        """The reservoir, in memory only. Never persist or log what this returns."""
        return tuple(self._samples)

    def value_count(self, value: str) -> int:
        """How many times ``value`` was observed since this process started (0 once the field
        went past :data:`MAX_COUNTED_VALUES` distinct values). In memory only."""
        counts = self._values
        if counts is None:
            return 0
        return counts.get(_value_key(value), 0)

    def shape_count(self, value_shape: str) -> int:
        """How many non-null values had ``value_shape`` (0 when it fell past the cap)."""
        return self._shapes.get(value_shape, 0)

    def top_shapes(self, n: int = 8) -> list[tuple[str, float]]:
        """The ``n`` most common shapes with their share of non-null values, most common first;
        ties break on the shape string so the order is stable."""
        non_null = self.non_null_count
        if non_null == 0 or n <= 0:
            return []
        ranked = sorted(self._shapes.items(), key=lambda item: (-item[1], item[0]))
        return [(value_shape, hits / non_null) for value_shape, hits in ranked[:n]]

    # -- persistence (never the samples) -----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything but the reservoir, JSON-ready."""
        return {
            "count": self.count,
            "null_count": self.null_count,
            "hll": base64.b64encode(zlib.compress(bytes(self._registers))).decode("ascii"),
            "shapes": dict(self._shapes),
            "length_min": self._length_min,
            "length_max": self._length_max,
            "length_sum": self._length_sum,
            "whitespace_chars": self._whitespace_chars,
            "whitespace_values": self._whitespace_values,
            "secret_count": self._secret_count,
            "high_cardinality_seen": self.high_cardinality_seen,
            "detector_hits_seen": self.detector_hits_seen,
        }

    @classmethod
    def restore(cls, data: Any, sample_size: int, *, seed: int | None = None) -> FieldStats:
        """Inverse of :meth:`snapshot`; the reservoir starts empty. Raises
        :class:`StatsFormatError` on anything unexpected."""
        if not isinstance(data, Mapping):
            msg = "field snapshot must be a mapping"
            raise StatsFormatError(msg)
        stats = cls(sample_size, seed=seed)
        stats.count = _require_int(data, "count")
        stats.null_count = _require_int(data, "null_count")
        if stats.null_count > stats.count:
            msg = "null_count exceeds count"
            raise StatsFormatError(msg)
        stats._length_min = _require_int(data, "length_min")
        stats._length_max = _require_int(data, "length_max")
        stats._length_sum = _require_int(data, "length_sum")
        stats._whitespace_chars = _require_int(data, "whitespace_chars")
        stats._whitespace_values = _require_int(data, "whitespace_values")
        stats._secret_count = _require_int(data, "secret_count")
        # Absent in version 1 snapshots: both false.
        stats.high_cardinality_seen = _require_bool(data, "high_cardinality_seen")
        stats.detector_hits_seen = _require_bool(data, "detector_hits_seen")
        shapes = data.get("shapes", {})
        if not isinstance(shapes, Mapping):
            msg = "snapshot field 'shapes' must be a mapping"
            raise StatsFormatError(msg)
        for value_shape, hits in shapes.items():
            if (
                not isinstance(value_shape, str)
                or not value_shape
                or isinstance(hits, bool)
                or not isinstance(hits, int)
                or hits < 0
            ):
                msg = "snapshot field 'shapes' must map shapes to counts"
                raise StatsFormatError(msg)
            stats._shapes[value_shape] = hits
        encoded = data.get("hll")
        if encoded is not None:
            if not isinstance(encoded, str):
                msg = "snapshot field 'hll' must be a string"
                raise StatsFormatError(msg)
            try:
                raw = zlib.decompress(base64.b64decode(encoded, validate=True))
            except (ValueError, zlib.error) as exc:
                msg = "snapshot field 'hll' is not a valid sketch"
                raise StatsFormatError(msg) from exc
            if len(raw) != _HLL_M or max(raw) > _HLL_MAX_RANK + 1:
                msg = f"snapshot field 'hll' must hold {_HLL_M} registers"
                raise StatsFormatError(msg)
            stats._registers = bytearray(raw)
        return stats


def _reservoir_seed(ref: str) -> int:
    """A seed per field, so the same input gives the same samples and the same decisions on
    every run (the PII verdict of rule 6 looks only at the samples)."""
    return int.from_bytes(hashlib.blake2b(ref.encode("utf-8"), digest_size=8).digest(), "big")


class FieldStatsStore:
    """All field statistics of one edge, keyed by ``field_ref`` (spec 7.2), with persistence.

    Thread-safe: every method takes the store lock, so the gateway's poll workers and the OTLP
    and webhook receivers can share one store.
    """

    def __init__(
        self,
        sample_size: int = 64,
        flush_seconds: float = 30.0,
        path: Path | None = None,
    ) -> None:
        self.sample_size = sample_size
        self.flush_seconds = flush_seconds
        self.path = path
        self._fields: dict[str, FieldStats] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self._last_flush = 0.0

    @classmethod
    def from_settings(cls, settings: ClassifySettings, path: Path | None = None) -> FieldStatsStore:
        return cls(
            sample_size=settings.sample_values,
            flush_seconds=settings.stats_flush_seconds,
            path=path,
        )

    # -- access ------------------------------------------------------------------------------

    def get(self, ref: str) -> FieldStats | None:
        with self._lock:
            return self._fields.get(ref)

    def get_or_create(self, ref: str) -> FieldStats:
        with self._lock:
            stats = self._fields.get(ref)
            if stats is None:
                stats = FieldStats(self.sample_size, seed=_reservoir_seed(ref))
                self._fields[ref] = stats
            return stats

    def observe(self, ref: str, value: str | None) -> FieldStats:
        with self._lock:
            stats = self.get_or_create(ref)
            stats.observe(value)
            self._dirty = True
            return stats

    def note(
        self, ref: str, *, high_cardinality: bool = False, detector_hits: bool = False
    ) -> None:
        """Set the sticky flags of ``ref`` (ADR 0068); nothing unsets them. A flag that changes
        marks the store dirty so the next flush persists it. Unknown refs are ignored."""
        with self._lock:
            stats = self._fields.get(ref)
            if stats is None:
                return
            if high_cardinality and not stats.high_cardinality_seen:
                stats.high_cardinality_seen = True
                self._dirty = True
            if detector_hits and not stats.detector_hits_seen:
                stats.detector_hits_seen = True
                self._dirty = True

    def refs(self) -> list[str]:
        with self._lock:
            return list(self._fields)

    def __len__(self) -> int:
        with self._lock:
            return len(self._fields)

    def __contains__(self, ref: object) -> bool:
        with self._lock:
            return ref in self._fields

    # -- persistence -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """The versioned document ``save`` writes. Contains no sample value."""
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "fields": {ref: stats.snapshot() for ref, stats in self._fields.items()},
            }

    def restore(self, data: Any) -> None:
        """Replace the contents with a snapshot. Raises :class:`StatsFormatError` on a bad one
        and leaves the store untouched in that case."""
        if not isinstance(data, Mapping):
            msg = "snapshot must be a mapping"
            raise StatsFormatError(msg)
        if data.get("version") not in _READABLE_VERSIONS:
            msg = f"snapshot version is not {SNAPSHOT_VERSION}"
            raise StatsFormatError(msg)
        fields = data.get("fields", {})
        if not isinstance(fields, Mapping):
            msg = "snapshot field 'fields' must be a mapping"
            raise StatsFormatError(msg)
        restored: dict[str, FieldStats] = {}
        for ref, field_data in fields.items():
            if not isinstance(ref, str) or not ref:
                msg = "snapshot field refs must be non-empty strings"
                raise StatsFormatError(msg)
            restored[ref] = FieldStats.restore(
                field_data, self.sample_size, seed=_reservoir_seed(ref)
            )
        with self._lock:
            self._fields = restored
            self._dirty = False

    def save(self, path: Path | None = None) -> None:
        """Atomic write: temporary file in the same directory, then ``os.replace``."""
        target = path or self.path
        if target is None:
            msg = "no path to save field statistics to"
            raise ValueError(msg)
        with self._lock:
            document = json.dumps(self.snapshot(), separators=(",", ":"), sort_keys=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    handle.write(document)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            self._dirty = False

    def load(self, path: Path | None = None) -> bool:
        """Load a snapshot; on any failure start empty and log a warning. Returns whether a
        snapshot was loaded."""
        target = path or self.path
        if target is None:
            return False
        try:
            text = target.read_text(encoding="utf-8")
            self.restore(json.loads(text))
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError, binascii.Error and StatsFormatError;
            # none of their messages carries a field value.
            with self._lock:
                self._fields = {}
                self._dirty = False
            _logger().warning(
                "field_stats_load_failed",
                path=str(target),
                reason=f"{type(exc).__name__}: {exc}"[:200],
            )
            return False
        return True

    def maybe_flush(self, now: float) -> bool:
        """Save when something changed and ``flush_seconds`` have passed on the caller's clock
        since the last flush (``time.monotonic()`` in the gateway; the first flush is due as
        soon as there is something to write). Returns whether a save happened."""
        with self._lock:
            if self.path is None or not self._dirty:
                return False
            if now - self._last_flush < self.flush_seconds:
                return False
            self.save()
            self._last_flush = now
            return True
