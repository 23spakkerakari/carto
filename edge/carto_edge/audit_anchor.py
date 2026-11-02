"""Publishing the edge audit anchor outside the edge (ADR 0033, spec 14.9).

Every hour, and at gateway shutdown, :meth:`AnchorPublisher.publish` reads the last row of
``audit.ndjson`` (:meth:`carto_edge.audit.EdgeAudit.anchor`) and sends ``seq``, ``row_hash`` and
``ts`` to two places:

- **The product log**, as one JSON line with ``"event": "audit.anchor"`` on the gateway's log
  stream, which customers already ship off the host with their container logs. The line is
  written here, not through :func:`carto_common.logging.redact`: that processor masks a 64-hex
  digest (it cannot tell a chain hash from a content hash) and would make the anchor useless.
  What bypasses it is fixed: the line carries an integer, a hash that matched
  ``[0-9a-f]{64}`` and the row's ISO timestamp, nothing from a record or a request (ADR 0042).
- **Optionally the customer's syslog or SIEM** (``audit.anchor_syslog``): RFC 5424 messages over
  TCP with RFC 5425 octet-counting framing (``<length> <message>``), TLS 1.2 or later by
  default, verified against the system trust store or ``ca_file``. A send that fails is logged
  by exception type and the next anchor is sent as usual (each anchor supersedes the previous
  one); it never blocks auditing, because publishing runs on the housekeeping worker thread
  with a socket timeout, never inside :meth:`~carto_edge.audit.EdgeAudit.record`.

Verify with ``carto-edge audit verify --file audit.ndjson --anchor <seq>:<row_hash>``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import ssl
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, Protocol, TextIO

from carto_common.logging import get_logger
from carto_edge.audit import AuditAnchor, EdgeAudit
from carto_edge.config import AnchorSyslogSettings

__all__ = [
    "ANCHOR_EVENT",
    "SYSLOG_PRI",
    "AnchorPublisher",
    "AnchorSender",
    "SyslogAnchorSender",
    "anchor_log_line",
    "frame_rfc5425",
    "syslog_message",
]

ANCHOR_EVENT: Final = "audit.anchor"
SYSLOG_FACILITY_LOG_AUDIT: Final = 13
SYSLOG_SEVERITY_INFO: Final = 6
SYSLOG_PRI: Final = SYSLOG_FACILITY_LOG_AUDIT * 8 + SYSLOG_SEVERITY_INFO
APP_NAME: Final = "carto-edge"
MSG_ID: Final = "audit.anchor"
MAX_HOSTNAME: Final = 255
CLOSE_WAIT_SECONDS: Final = 1.0
_ROW_HASH: Final = re.compile(r"[0-9a-f]{64}")
_ISO_TS: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})")
_PRINTUSASCII: Final = re.compile(r"[\x21-\x7e]+")

log = get_logger(component="carto_edge.audit_anchor")


class AnchorSender(Protocol):
    def send(self, anchor: AuditAnchor) -> None: ...


def _checked(anchor: AuditAnchor) -> AuditAnchor:
    """Refuse anything that is not an integer, a 64-hex hash and an ISO timestamp, so nothing
    else can ride on the line that bypasses the log processor."""
    if isinstance(anchor.seq, bool) or anchor.seq < 0:
        msg = "anchor seq must be a non-negative integer"
        raise ValueError(msg)
    if not _ROW_HASH.fullmatch(anchor.row_hash):
        msg = "anchor row_hash must be 64 lower-case hex characters"
        raise ValueError(msg)
    if anchor.ts and not _ISO_TS.fullmatch(anchor.ts):
        msg = "anchor ts must be an ISO 8601 timestamp"
        raise ValueError(msg)
    return anchor


def _utc_now() -> datetime:
    return datetime.now(UTC)


def anchor_log_line(anchor: AuditAnchor, service: str, now: datetime) -> str:
    """The product log line of an anchor (same keys as the structlog renderer)."""
    checked = _checked(anchor)
    return json.dumps(
        {
            "event": ANCHOR_EVENT,
            "seq": checked.seq,
            "row_hash": checked.row_hash,
            "ts": checked.ts,
            "service": service,
            "level": "info",
            "timestamp": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        },
        separators=(", ", ": "),
    )


def _hostname() -> str:
    found = _PRINTUSASCII.findall(socket.gethostname())
    name = "".join(found)[:MAX_HOSTNAME]
    return name or "-"


def syslog_message(anchor: AuditAnchor, now: datetime, hostname: str | None = None) -> bytes:
    """One RFC 5424 message: no structured data, the anchor as ``key=value`` text."""
    checked = _checked(anchor)
    timestamp = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    host = hostname if hostname is not None else _hostname()
    body = f"event={ANCHOR_EVENT} seq={checked.seq} row_hash={checked.row_hash} ts={checked.ts}"
    header = f"<{SYSLOG_PRI}>1 {timestamp} {host} {APP_NAME} {os.getpid()} {MSG_ID} -"
    return f"{header} {body}".encode("ascii")


def frame_rfc5425(message: bytes) -> bytes:
    """RFC 5425 section 4.3 (and RFC 6587 octet counting): ``MSG-LEN SP SYSLOG-MSG``."""
    return str(len(message)).encode("ascii") + b" " + message


class SyslogAnchorSender:
    """Sends each anchor on a fresh TCP (default TLS) connection to the configured target."""

    __slots__ = ("_clock", "_context", "_hostname", "_settings")

    def __init__(
        self,
        settings: AnchorSyslogSettings,
        *,
        context: ssl.SSLContext | None = None,
        clock: Callable[[], datetime] = _utc_now,
        hostname: str | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._hostname = hostname
        self._context: ssl.SSLContext | None = None
        if settings.tls:
            if context is None:
                ca_file = str(settings.ca_file) if settings.ca_file is not None else None
                context = ssl.create_default_context(cafile=ca_file)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self._context = context

    def __repr__(self) -> str:
        return (
            f"SyslogAnchorSender(host={self._settings.host!r}, port={self._settings.port}, "
            f"tls={self._settings.tls})"
        )

    def send(self, anchor: AuditAnchor) -> None:
        frame = frame_rfc5425(syslog_message(anchor, self._clock(), self._hostname))
        target = (self._settings.host, self._settings.port)
        with socket.create_connection(target, timeout=self._settings.timeout_seconds) as raw:
            if self._context is None:
                raw.sendall(frame)
                return
            with self._context.wrap_socket(raw, server_hostname=self._settings.host) as tls:
                tls.sendall(frame)
                _close_tls(tls, min(CLOSE_WAIT_SECONDS, self._settings.timeout_seconds))


def _close_tls(tls: ssl.SSLSocket, wait_seconds: float) -> None:
    """Send close_notify and read what the server sent meanwhile (TLS 1.3 session tickets):
    closing a socket with unread data resets the connection, and a server may then discard
    the frame it has not read yet. The frame is already sent, so a failure here is ignored."""
    tls.settimeout(wait_seconds)
    with contextlib.suppress(OSError, ssl.SSLError, ValueError):
        tls.unwrap()


class AnchorPublisher:
    """Reads the anchor and publishes it (module docstring). Never raises."""

    __slots__ = ("_audit", "_clock", "_sender", "_service", "_stream")

    def __init__(
        self,
        audit: EdgeAudit,
        *,
        sender: AnchorSender | None = None,
        stream: TextIO | None = None,
        service: str = "edge-gateway",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._audit = audit
        self._sender = sender
        self._stream = stream
        self._service = service
        self._clock = clock

    def __repr__(self) -> str:
        return f"AnchorPublisher(syslog={self._sender is not None})"

    def publish(self) -> AuditAnchor | None:
        """Write the anchor line and send it to syslog when configured; returns the anchor,
        or ``None`` when it could not be read."""
        try:
            anchor = _checked(self._audit.anchor())
        except Exception as exc:  # the chain or the file is unreadable; the next tick retries
            log.error("audit.anchor_failed", error=type(exc).__name__)
            return None
        stream = self._stream if self._stream is not None else sys.stdout
        try:
            stream.write(anchor_log_line(anchor, self._service, self._clock()) + "\n")
            stream.flush()
        except (OSError, ValueError) as exc:
            log.error("audit.anchor_log_failed", error=type(exc).__name__)
        if self._sender is not None:
            try:
                self._sender.send(anchor)
            except (OSError, ssl.SSLError, ValueError) as exc:
                log.warning("audit.anchor_syslog_failed", error=type(exc).__name__)
        return anchor
