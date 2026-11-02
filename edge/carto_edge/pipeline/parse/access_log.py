"""HTTP access logs: Apache/nginx combined and common formats, custom RE2 patterns (spec 8.2
item 5, ADR 0016).

The built-in pattern covers the common format and its combined extension (referer and user
agent), with trailing extra fields tolerated. A per-source ``access_log_pattern`` is compiled
with ``google-re2`` (linear time whatever the customer writes, spec 2.3 invariant 8) and must
use named groups; its ``groupdict`` becomes the fields. Either way the template is
``<METHOD> <route> <status class>`` (ADR 0016). The request line is attacker text and the
template leaves the edge in clear (spec 2.3 invariant 2), so (ADR 0039 amendment of
2026-11-02) the method is a constant only when it is a registered HTTP method
(:data:`HTTP_METHODS`; anything else is ``<*>``) and the route is the path without its query
string with every segment that looks like a value masked whole as ``<*>``: a UUID, a run of
16 or more hex characters, or any digit, ``@`` or more than 64 characters
(:func:`~carto_edge.pipeline.parse.common.is_value_like`), so ``GET /orders/4471/items 2xx``
and ``GET /orders/9910/items 2xx`` are one node. The segments that pass are words by shape;
whether each may travel is the parser's name-gate question. Dash placeholders (``-``) of the
standard formats are absent fields.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

import re2

from carto_edge.pipeline.parse.common import TEMPLATE_MASK, is_value_like

__all__ = [
    "HTTP_METHODS",
    "AccessLogParser",
    "AccessLogRecord",
    "generalize_path",
    "looks_like_access_log",
    "normalize_method",
    "status_class",
]

MAX_PATH_LEN: Final = 512
HTTP_METHODS: Final = frozenset(
    {
        # RFC 9110 and PATCH (RFC 5789)
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "DELETE",
        "CONNECT",
        "OPTIONS",
        "TRACE",
        "PATCH",
        # WebDAV (RFC 4918), versioning (RFC 3253), ACL (RFC 3744), ordered collections
        # (RFC 3648), redirect references (RFC 4437), CalDAV (RFC 4791), binding (RFC 5842),
        # search (RFC 5323), linking (RFC 2068), HTTP/2 preface (RFC 9113), QUERY (draft)
        "PROPFIND",
        "PROPPATCH",
        "MKCOL",
        "COPY",
        "MOVE",
        "LOCK",
        "UNLOCK",
        "VERSION-CONTROL",
        "REPORT",
        "CHECKOUT",
        "CHECKIN",
        "UNCHECKOUT",
        "MKWORKSPACE",
        "UPDATE",
        "LABEL",
        "MERGE",
        "BASELINE-CONTROL",
        "MKACTIVITY",
        "ACL",
        "ORDERPATCH",
        "MKREDIRECTREF",
        "UPDATEREDIRECTREF",
        "MKCALENDAR",
        "BIND",
        "UNBIND",
        "REBIND",
        "SEARCH",
        "LINK",
        "UNLINK",
        "PRI",
        "QUERY",
    }
)
"""The request methods that may be a template constant, as the IANA HTTP method registry
spells them (case-sensitive, so ``get`` is masked). Everything else is ``<*>``."""

_STANDARD: Final = re.compile(
    r"^(?P<remote_addr>\S+) (?P<remote_ident>\S+) (?P<remote_user>\S+) "
    r"\[(?P<time>[^\]]{1,64})\] \"(?P<request>[^\"]{0,4096})\" (?P<status>\d{3}|-) "
    r"(?P<bytes>\d{1,20}|-)"
    r"(?: \"(?P<referer>[^\"]{0,4096})\" \"(?P<user_agent>[^\"]{0,4096})\")?(?: .{0,4096})?$"
)
_PREFIX: Final = re.compile(r"^\S+ \S+ \S+ \[")
_UUID: Final = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX: Final = re.compile(r"^[0-9a-fA-F]{16,}$")
_DASH_IS_ABSENT: Final = frozenset(
    {"remote_ident", "remote_user", "bytes", "referer", "user_agent"}
)


@dataclass(frozen=True, slots=True)
class AccessLogRecord:
    """The fields of one access-log line and its template text, with the template's three
    parts: ``method`` (from :data:`HTTP_METHODS`, ``-`` when absent, else ``<*>``), ``route``
    (:func:`generalize_path`) and ``status`` (:func:`status_class`)."""

    fields: dict[str, str]
    template: str
    method: str
    route: str
    status: str


def looks_like_access_log(text: str) -> bool:
    """Cheap sniff for the auto-detector: ``host ident user [`` at the start."""
    return _PREFIX.match(text) is not None


def status_class(status: str) -> str:
    """``200`` -> ``2xx``; anything that is not a three-digit code -> ``-``."""
    text = status.strip()
    if len(text) == 3 and text.isdigit():
        return f"{text[0]}xx"
    return "-"


def normalize_method(method: str | None) -> str:
    """``method`` when it is in :data:`HTTP_METHODS`, ``-`` when absent, else ``<*>``."""
    if not method:
        return "-"
    return method if method in HTTP_METHODS else TEMPLATE_MASK


def generalize_path(path: str) -> str:
    """The route of a request path (ADR 0016, ADR 0039 amendment): no query or fragment,
    every segment that is a UUID, a hex run of 16 or more characters or value-like (any
    digit, ``@``, over 64 characters) replaced whole by ``<*>``; ``-`` for an empty path."""
    if not path:
        return "-"
    cut = len(path)
    for stop in ("?", "#"):
        index = path.find(stop)
        if 0 <= index < cut:
            cut = index
    route = path[:cut][:MAX_PATH_LEN]
    if not route:
        return "-"
    return "/".join(
        TEMPLATE_MASK
        if segment and (_UUID.match(segment) or _HEX.match(segment) or is_value_like(segment))
        else segment
        for segment in route.split("/")
    )


def _record(fields: dict[str, str]) -> AccessLogRecord:
    method = normalize_method(fields.get("method"))
    route = generalize_path(fields.get("path") or "")
    status = status_class(fields.get("status", ""))
    return AccessLogRecord(
        fields=fields,
        template=f"{method} {route} {status}",
        method=method,
        route=route,
        status=status,
    )


def _split_request(request: str, fields: dict[str, str]) -> None:
    parts = request.split(" ")
    if len(parts) in (2, 3) and parts[0] and parts[1]:
        fields["method"] = parts[0]
        fields["path"] = parts[1]
        if len(parts) == 3 and parts[2]:
            fields["protocol"] = parts[2]
        return
    fields["request"] = request


class AccessLogParser:
    """One per source: the built-in standard formats, or the configured RE2 pattern."""

    def __init__(self, pattern: str | None = None) -> None:
        self._custom: Any | None = None
        if pattern is not None:
            options = re2.Options()
            options.log_errors = False
            options.max_mem = 16 * 1024 * 1024
            try:
                compiled = re2.compile(pattern, options=options)
            except re2.error as exc:
                msg = "invalid access_log_pattern (RE2 syntax; no backreferences or lookarounds)"
                raise ValueError(msg) from exc
            if not compiled.groupindex:
                msg = "access_log_pattern needs at least one named group, e.g. (?P<path>\\S+)"
                raise ValueError(msg)
            self._custom = compiled

    def parse(self, text: str) -> AccessLogRecord | None:
        """Fields and template of one line, or ``None`` when it does not match."""
        line = text.rstrip("\r\n")
        if not line:
            return None
        if self._custom is not None:
            return self._parse_custom(line)
        match = _STANDARD.match(line)
        if match is None:
            return None
        fields: dict[str, str] = {}
        for name, value in match.groupdict().items():
            if value is None or name == "request":
                continue
            if value == "-" and name in _DASH_IS_ABSENT:
                continue
            fields[name] = value
        _split_request(match.group("request"), fields)
        return _record(fields)

    def _parse_custom(self, line: str) -> AccessLogRecord | None:
        if self._custom is None:  # established by the constructor
            return None
        try:
            match = self._custom.search(line)
        except (re2.error, UnicodeError, ValueError, TypeError):
            return None
        if match is None:
            return None
        fields = {name: value for name, value in match.groupdict().items() if value is not None}
        if not fields:
            return None
        return _record(fields)
