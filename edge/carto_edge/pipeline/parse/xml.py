"""XML records through defusedxml (spec 8.2 item 2, 2.3 invariant 8).

One record is one XML document (the payments source writes one per line). The parser forbids
DTDs, entity declarations and external references, so billion-laughs and XXE documents are
rejected before any expansion. The tree is flattened to paths like the JSON flattener: child
elements by local name (namespaces stripped), attributes as ``@attr``, repeated siblings indexed
``tag.0``, ``tag.1`` up to :data:`~carto_edge.pipeline.parse.common.MAX_ARRAY_ITEMS`, nesting
up to :data:`~carto_edge.pipeline.parse.common.MAX_DEPTH`. An element with text and nothing
else maps to its path; one that also has attributes or children keeps its text under
``#text``. The root element name is returned separately: it is the template of XML records
(ADR 0016).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, ParseError  # nosec B405 - parsing is defused below

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from carto_edge.pipeline.parse.common import (
    MAX_ARRAY_ITEMS,
    MAX_DEPTH,
    MAX_FIELDS,
    MAX_KEY_LEN,
    NOTE_ARRAY_LIMIT,
    NOTE_DEPTH_LIMIT,
    NOTE_FIELD_LIMIT,
    NOTE_KEY_LIMIT,
    SegmentPath,
)

__all__ = ["XmlRecord", "looks_like_xml", "parse_xml"]


@dataclass(frozen=True, slots=True)
class XmlRecord:
    """The root element's local name, the flattened fields and the limit notes; ``structure``
    as in :class:`~carto_edge.pipeline.parse.common.Flattened` (XML names may contain dots)."""

    root: str
    fields: dict[str, str]
    notes: tuple[str, ...]
    structure: dict[str, SegmentPath] = field(default_factory=dict)


def looks_like_xml(text: str) -> bool:
    """Cheap sniff for the auto-detector: ``<`` followed by a name or a prolog."""
    stripped = text.lstrip()
    return (
        len(stripped) > 1 and stripped[0] == "<" and (stripped[1].isalpha() or stripped[1] in "?_")
    )


def _local_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _join(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


@dataclass(slots=True)
class _Out:
    fields: dict[str, str]
    structure: dict[str, SegmentPath]
    notes: set[str]

    def put(self, path: str, segments: SegmentPath, value: str) -> None:
        if len(path) > MAX_KEY_LEN:
            self.notes.add(NOTE_KEY_LIMIT)
            return
        if len(self.fields) >= MAX_FIELDS and path not in self.fields:
            self.notes.add(NOTE_FIELD_LIMIT)
            return
        self.fields[path] = value
        if len(segments) > 1:
            self.structure[path] = segments
        else:
            self.structure.pop(path, None)


def _flatten_element(root: Element) -> _Out:
    """Iterative walk of the tree with the common depth, array and field limits."""
    out = _Out(fields={}, structure={}, notes=set())
    notes = out.notes
    stack: list[tuple[str, SegmentPath, int, Element]] = [("", (), 0, root)]
    while stack:
        prefix, segments, depth, element = stack.pop()
        for name, value in element.attrib.items():
            attribute = f"@{_local_name(name)}"
            out.put(_join(prefix, attribute), (*segments, (attribute, False)), value)
        children = list(element)
        text = (element.text or "").strip()
        if text:
            if children or element.attrib or not prefix:
                out.put(_join(prefix, "#text"), (*segments, ("#text", False)), text)
            else:
                out.put(prefix, segments, text)
        if not children:
            continue
        if depth >= MAX_DEPTH:
            notes.add(NOTE_DEPTH_LIMIT)
            continue
        counts: dict[str, int] = {}
        for child in children:
            name = _local_name(child.tag)
            counts[name] = counts.get(name, 0) + 1
        seen: dict[str, int] = {}
        pending: list[tuple[str, SegmentPath, int, Element]] = []
        for child in children:
            name = _local_name(child.tag)
            index = seen.get(name, 0)
            seen[name] = index + 1
            if counts[name] > 1:
                if index >= MAX_ARRAY_ITEMS:
                    notes.add(NOTE_ARRAY_LIMIT)
                    continue
                path = _join(prefix, f"{name}.{index}")
                child_segments: SegmentPath = (*segments, (name, False), (str(index), True))
            else:
                path = _join(prefix, name)
                child_segments = (*segments, (name, False))
            pending.append((path, child_segments, depth + 1, child))
        stack.extend(reversed(pending))
    return out


def parse_xml(text: str) -> XmlRecord | None:
    """Parse one defused XML document; ``None`` for anything malformed or forbidden."""
    if not looks_like_xml(text):
        return None
    try:
        root = fromstring(text, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ParseError, DefusedXmlException, ValueError, RecursionError, MemoryError):
        return None
    if root is None:
        return None
    out = _flatten_element(root)
    return XmlRecord(
        root=_local_name(root.tag),
        fields=out.fields,
        notes=tuple(sorted(out.notes)),
        structure=out.structure,
    )
