"""Prometheus text-format metrics for edge-gateway (spec 16), without a client library.

One :class:`EdgeMetrics` registry per process holds counters and gauges, optionally labelled,
and renders them in the exposition format ``GET /metrics`` serves on the internal network. Names
and label names are validated when declared; label values are escaped on output. Values are
numbers only: no metric ever carries a record value (spec 2.3 invariant 7).
:func:`default_metrics` declares the series the gateway, the ingestor and the forwarder share.
"""

from __future__ import annotations

import re
import threading
from typing import Final, Literal

__all__ = ["EDGE_SERIES", "MAX_SERIES_PER_METRIC", "EdgeMetrics", "default_metrics"]

_NAME: Final = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL: Final = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
MAX_LABEL_VALUE_LEN: Final = 128
MAX_SERIES_PER_METRIC: Final = 4096
"""Label combinations kept per metric; beyond that new combinations are dropped, so an
attacker-controlled label (a source id from a request path) cannot grow memory without bound."""

Kind = Literal["counter", "gauge"]
LabelKey = tuple[tuple[str, str], ...]

EDGE_SERIES: Final[tuple[tuple[str, Kind, str], ...]] = (
    ("carto_edge_records_total", "counter", "Raw records handed to the pipeline, by source."),
    ("carto_edge_events_total", "counter", "Canonical events produced, by source."),
    ("carto_edge_dropped_total", "counter", "Records dropped, by source and reason."),
    (
        "carto_edge_events_out_of_range_total",
        "counter",
        "Records whose time lay outside the plausible range, by source and reason (ADR 0036).",
    ),
    ("carto_edge_vault_entries_total", "counter", "Reveal vault entries written."),
    ("carto_edge_vault_purged_total", "counter", "Expired reveal vault entries purged."),
    ("carto_edge_batches_sealed_total", "counter", "Batches appended to the disk buffer."),
    ("carto_edge_batches_sent_total", "counter", "Batches acknowledged by ingest-api."),
    ("carto_edge_batches_parked_total", "counter", "Batches ingest-api refused for good."),
    ("carto_edge_send_errors_total", "counter", "Failed delivery attempts (spec 8.5)."),
    ("carto_edge_heartbeats_total", "counter", "Source heartbeats accepted by core."),
    ("carto_edge_requests_total", "counter", "HTTP requests answered, by route and status."),
    ("carto_edge_connector_errors_total", "counter", "Connector read failures, by source."),
    ("carto_edge_buffer_bytes", "gauge", "Bytes in the disk buffer (pending and parked)."),
    ("carto_edge_buffer_depth", "gauge", "Pending batches in the disk buffer (spec 8.5)."),
    ("carto_edge_buffer_oldest_age_seconds", "gauge", "Age of the oldest pending batch."),
    ("carto_edge_backpressure", "gauge", "0 ok, 1 above the backpressure ratio, 2 full."),
    ("carto_edge_connector_lag_seconds", "gauge", "Cursor lag behind the source clock."),
    ("carto_edge_key_versions", "gauge", "Tokenization key versions in use."),
)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class EdgeMetrics:
    """Thread-safe registry of counters and gauges. ``repr`` shows the metric count only."""

    __slots__ = ("_help", "_kinds", "_lock", "_values")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kinds: dict[str, Kind] = {}
        self._help: dict[str, str] = {}
        self._values: dict[str, dict[LabelKey, float]] = {}

    def __repr__(self) -> str:
        return f"EdgeMetrics(metrics={len(self._kinds)})"

    def declare(self, name: str, kind: Kind, help_text: str) -> None:
        """Register a metric; declaring the same name with the same kind again is a no-op."""
        if not _NAME.fullmatch(name):
            msg = f"invalid metric name {name!r}"
            raise ValueError(msg)
        with self._lock:
            existing = self._kinds.get(name)
            if existing is not None and existing != kind:
                msg = f"metric {name!r} is already declared as a {existing}"
                raise ValueError(msg)
            self._kinds[name] = kind
            self._help[name] = help_text
            self._values.setdefault(name, {})

    def counter(self, name: str, help_text: str) -> None:
        self.declare(name, "counter", help_text)

    def gauge(self, name: str, help_text: str) -> None:
        self.declare(name, "gauge", help_text)

    @staticmethod
    def _key(labels: dict[str, str]) -> LabelKey:
        out: list[tuple[str, str]] = []
        for label, value in sorted(labels.items()):
            if not _LABEL.fullmatch(label):
                msg = f"invalid label name {label!r}"
                raise ValueError(msg)
            out.append((label, str(value)[:MAX_LABEL_VALUE_LEN]))
        return tuple(out)

    def _series(self, name: str, labels: dict[str, str]) -> tuple[dict[LabelKey, float], LabelKey]:
        if name not in self._kinds:
            msg = f"metric {name!r} is not declared"
            raise KeyError(msg)
        return self._values[name], self._key(labels)

    def inc(self, name: str, amount: float = 1, **labels: str) -> None:
        """Add to a counter (or move a gauge); negative amounts are refused for counters."""
        with self._lock:
            series, key = self._series(name, labels)
            if self._kinds[name] == "counter" and amount < 0:
                msg = "a counter cannot decrease"
                raise ValueError(msg)
            if key not in series and len(series) >= MAX_SERIES_PER_METRIC:
                return
            series[key] = series.get(key, 0.0) + amount

    def set(self, name: str, value: float, **labels: str) -> None:
        """Set a gauge."""
        with self._lock:
            series, key = self._series(name, labels)
            if self._kinds[name] != "gauge":
                msg = f"metric {name!r} is a counter; use inc"
                raise ValueError(msg)
            if key not in series and len(series) >= MAX_SERIES_PER_METRIC:
                return
            series[key] = float(value)

    def get(self, name: str, **labels: str) -> float:
        with self._lock:
            series, key = self._series(name, labels)
            return series.get(key, 0.0)

    def render(self) -> str:
        """The Prometheus text exposition (``text/plain; version=0.0.4``)."""
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._kinds):
                lines.append(f"# HELP {name} {_escape(self._help[name])}")
                lines.append(f"# TYPE {name} {self._kinds[name]}")
                series = self._values[name]
                if not series:
                    lines.append(f"{name} 0")
                    continue
                for key in sorted(series):
                    value = series[key]
                    text = str(int(value)) if value == int(value) else repr(value)
                    if key:
                        labels = ",".join(f'{label}="{_escape(v)}"' for label, v in key)
                        lines.append(f"{name}{{{labels}}} {text}")
                    else:
                        lines.append(f"{name} {text}")
            return "\n".join(lines) + "\n"


def default_metrics() -> EdgeMetrics:
    """A registry with every series of :data:`EDGE_SERIES` declared."""
    metrics = EdgeMetrics()
    for name, kind, help_text in EDGE_SERIES:
        metrics.declare(name, kind, help_text)
    return metrics
