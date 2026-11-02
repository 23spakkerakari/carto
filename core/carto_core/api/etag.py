"""``ETag`` and ``If-Match`` on mutable resources (spec 12: "to prevent lost updates").

A resource's entity tag is its decision version as a weak tag, ``W/"<version>"``: two readers
who saw different versions cannot both write, because every mutation carries the version it
was based on. A mutation without ``If-Match`` is a 428 (RFC 6585), one with a stale tag a 412.
``*`` is accepted (the client takes the lost-update risk knowingly, as RFC 9110 allows).
"""

from __future__ import annotations

import re
from typing import Final

from fastapi import Request

from carto_core.api.problems import Problem

__all__ = ["etag_of", "parse_if_match", "require_if_match", "version_of_etag"]

_TAG: Final = re.compile(r'^(?:W/)?"([0-9]{1,18})"$')
_MAX_HEADER_LEN: Final = 512


def etag_of(version: int) -> str:
    return f'W/"{version}"'


def version_of_etag(tag: str) -> int | None:
    match = _TAG.fullmatch(tag.strip())
    return int(match.group(1)) if match else None


def parse_if_match(header: str | None) -> tuple[int, ...] | None:
    """The versions an ``If-Match`` names, ``()`` for ``*``, ``None`` when absent or garbage."""
    if header is None:
        return None
    header = header.strip()
    if not header or len(header) > _MAX_HEADER_LEN:
        return None
    if header == "*":
        return ()
    versions: list[int] = []
    for part in header.split(","):
        version = version_of_etag(part)
        if version is None:
            return None
        versions.append(version)
    return tuple(versions)


def require_if_match(request: Request, current_version: int) -> int:
    """Check the header against the version the port holds; return the version the client saw
    (the port checks it again under its own lock)."""
    versions = parse_if_match(request.headers.get("if-match"))
    if versions is None:
        raise Problem(428, "precondition required", "send If-Match with the ETag you last read")
    if versions == ():
        return current_version
    if current_version not in versions:
        raise Problem(412, "precondition failed", "the resource changed; read it again")
    return current_version
