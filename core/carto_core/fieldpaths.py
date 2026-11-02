"""Pure helpers over engine field references (``system_id/template_id/path``) and the names a
person or a ground truth calls a field by (plan M2 decision 6; ADR 0030, 0049).

The engine names a field by the path the edge flattened it to: a top-level key or CSV column
as written (``merchantRef``, ``startDt``), a dotted path for nested JSON (``order.cart_id``,
``msg.param_0``), ``*`` or ``*~n`` for a segment the name gate masked (ADR 0030), and
``<parent>.<i>.key`` / ``<parent>.<i>.value`` for the entries of a detected map. A partner's
confirmed-link CSV (spec 21 gate) and the simulator's truth name the same field by its leaf
name, or by its dotted path when the data itself is nested. Both the gate report
(:mod:`carto_core.gate`) and the eval harness's field mapping use the rules here, so core never
imports the simulator and the two cannot drift:

- :func:`name_candidates` lists the names an engine path may be known by, most specific first:
  the path itself, then the parent of a map entry or a masked tail, then the leaf. A name that
  is masked is never a candidate.
- :func:`matches_name` says whether a name (case-insensitive) is one of those candidates or a
  suffix of the path on a segment boundary.

Nothing here reads data; paths and names are metadata.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MASK",
    "SEPARATOR",
    "is_masked_segment",
    "leaf_name",
    "matches_name",
    "name_candidates",
    "split_field_ref",
]

MASK: Final = "*"
SEPARATOR: Final = "."
_MAP_ENTRY_TAILS: Final = frozenset({"key", "value"})
_ARRAY_INDEX: Final = "#"


def split_field_ref(field_ref: str) -> tuple[str, str, str]:
    """``(system_id, template_id, path)`` of a ``system_id/template_id/path`` reference. A
    reference with fewer parts leaves the missing ones empty; the path keeps any further
    slashes."""
    system_id, _sep, rest = field_ref.partition("/")
    template_id, _sep, path = rest.partition("/")
    return system_id, template_id, path


def is_masked_segment(segment: str) -> bool:
    """``*`` and ``*~n`` (ADR 0030) hide the name; the segment carries no usable name."""
    return segment.startswith(MASK)


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split(SEPARATOR) if segment != ""]


def leaf_name(path: str) -> str | None:
    """The last segment of a path, or None when it is masked or the path is empty."""
    segments = _segments(path)
    if not segments or is_masked_segment(segments[-1]):
        return None
    return segments[-1]


def _is_entry_tail(segment: str) -> bool:
    return segment in _MAP_ENTRY_TAILS or segment == _ARRAY_INDEX or segment.isdigit()


def name_candidates(path: str) -> tuple[str, ...]:
    """The names a path may be known by, most specific first, without repeats:

    1. the path itself when no segment is masked;
    2. the parent path of a map entry or array element (``stock.0.key`` and ``stock.*`` name
       ``stock``), stripping trailing index, ``key``/``value`` and masked segments;
    3. the leaf name of each of the above when it is dotted.

    A path whose every segment is masked has no candidates.
    """
    segments = _segments(path)
    candidates: list[str] = []

    def offer(parts: list[str]) -> None:
        if not parts or any(is_masked_segment(part) for part in parts):
            return
        joined = SEPARATOR.join(parts)
        if joined not in candidates:
            candidates.append(joined)
        if len(parts) > 1 and parts[-1] not in candidates:
            candidates.append(parts[-1])

    offer(segments)
    trimmed = list(segments)
    while trimmed and (is_masked_segment(trimmed[-1]) or _is_entry_tail(trimmed[-1])):
        trimmed.pop()
    if trimmed != segments:
        offer(trimmed)
    return tuple(candidates)


def matches_name(path: str, name: str) -> bool:
    """Whether ``name`` (a partner's or a truth's field name, case-insensitive) names ``path``:
    equal to one of its :func:`name_candidates`, or a suffix of the path on a segment
    boundary (``cart_id`` matches ``order.cart_id``; ``art_id`` does not)."""
    wanted = name.strip().lower()
    if not wanted:
        return False
    if any(candidate.lower() == wanted for candidate in name_candidates(path)):
        return True
    lowered = path.lower()
    return lowered.endswith(SEPARATOR + wanted) and not is_masked_segment(wanted)
