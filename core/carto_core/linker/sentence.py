"""The plain-language evidence sentence of a review item (spec 9.6; ADR 0057).

"92% of Warehouse purchase orders (po_num, 99-999) match an order (order_ref, AA-9999999)
within about 2 hours; the match uses the digits inside the order reference." The sentence is
built from the link's features, the field paths, the shapes and the system names only: counts,
shares, lags and metadata the edge already masked, never a token or a value. It follows the
link's direction (the end whose events come first is the subject and carries its system's
name), names the form when it is not ``raw`` ("the digits inside", "the letters and digits
of", "how the name sounds"), says what a bridge or a composite link is in one clause, adds the
role when it is not a transaction link, and is bounded at :data:`MAX_SENTENCE_LEN` characters.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from carto_core.linker.model import Direction, LinkRole, LinkType
from carto_core.linker.naming import noun_for_path, plural, suggest_field_label

if TYPE_CHECKING:
    from carto_core.linker.model import KeyLink
    from carto_core.store.model import FieldForm

__all__ = [
    "MAX_SENTENCE_LEN",
    "describe_lag",
    "evidence_sentence",
    "field_noun",
    "form_phrase",
    "system_label",
]

MAX_SENTENCE_LEN: Final = 1024

_MINUTE: Final = 60.0
_HOUR: Final = 3600.0
_DAY: Final = 86400.0


def system_label(system_id: str, system_names: Mapping[str, str] | None) -> str:
    """The display name of a system, or a readable form of its id (``sys_warehouse`` is
    ``Warehouse``)."""
    if system_names is not None and system_names.get(system_id):
        return system_names[system_id]
    text = system_id.removeprefix("sys_").replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else system_id


def describe_lag(seconds: float | None) -> str:
    """``within about 2 hours``; ``at about the same time`` under a minute; empty when unknown."""
    if seconds is None or math.isnan(seconds):
        return ""
    gap = abs(seconds)
    if gap < _MINUTE:
        return "at about the same time"
    if gap < _HOUR:
        return f"within about {max(1, math.ceil(gap / _MINUTE))} minutes"
    if gap < _DAY:
        hours = max(1, math.ceil(gap / _HOUR))
        return f"within about {hours} hour{'s' if hours != 1 else ''}"
    days = max(1, math.ceil(gap / _DAY))
    return f"within about {days} day{'s' if days != 1 else ''}"


def _path(end: FieldForm) -> str:
    parts = end.field_ref.split("/", 2)
    return parts[2] if len(parts) == 3 else end.field_ref


def _humanize(path: str) -> str:
    return " ".join(part for part in path.replace(".", " ").replace("_", " ").split()) or "value"


def field_noun(end: FieldForm, shape: str | None = None) -> str:
    """What one field is called in the sentence: ``order reference``, ``PO number``, or the
    field's own name when the dictionary has no word for it (``merchant ref``)."""
    label = suggest_field_label(_path(end), shape)
    if label is None:
        return _humanize(_path(end)).lower()
    first, _space, rest = label.partition(" ")
    head = first if first.isupper() else first.lower()
    return f"{head} {rest}".strip()


def _entity_noun(end: FieldForm) -> str:
    """The thing the field identifies (``purchase order``), else its own name."""
    return noun_for_path(_path(end)) or _humanize(_path(end)).lower()


def form_phrase(form: str, noun: str) -> str | None:
    """How a non-raw form is matched, in business language; None for ``raw``."""
    base = form.partition(".")[0]
    if base == "raw":
        return None
    if base == "digits":
        return f"the digits inside the {noun}"
    if base == "alnum":
        return f"the letters and digits of the {noun}"
    if base == "norm":
        return f"the {noun} ignoring case and spacing"
    if base == "phonetic":
        return "how the name sounds"
    if base == "date":
        return f"the date in the {noun}"
    if base == "amount":
        return f"the amount in the {noun}"
    return f"the {form} form of the {noun}"


def _article(noun: str) -> str:
    return "an" if noun[:1].lower() in "aeiou" else "a"


def _detail(end: FieldForm, shape: str | None) -> str:
    return _path(end) if shape is None else f"{_path(end)}, {shape}"


def _subject(end: FieldForm, shape: str | None, system_names: Mapping[str, str] | None) -> str:
    system = system_label(end.system_id, system_names)
    return f"{system} {plural(_entity_noun(end))} ({_detail(end, shape)})"


def _object(end: FieldForm, shape: str | None) -> str:
    noun = _entity_noun(end)
    return f"{_article(noun)} {noun} ({_detail(end, shape)})"


def _percent(share: float) -> int:
    return max(0, min(100, round(share * 100)))


def _shape_of(
    link: KeyLink, end: FieldForm, which: str, shapes: Mapping[FieldForm, str] | None
) -> str | None:
    if shapes is not None and end in shapes:
        return shapes[end]
    value = link.extra.get(f"shape_{which}")
    return value if isinstance(value, str) and value else None


def evidence_sentence(
    link: KeyLink,
    *,
    system_names: Mapping[str, str] | None = None,
    shapes: Mapping[FieldForm, str] | None = None,
) -> str:
    """See the module docstring."""
    features = link.features
    shape_a = _shape_of(link, link.a, "a", shapes)
    shape_b = _shape_of(link, link.b, "b", shapes)
    lag = describe_lag(features.lag_median)
    clauses: list[str] = []
    if link.link_type == LinkType.BRIDGE:
        system = system_label(link.a.system_id, system_names)
        clauses.append(
            f"{_subject(link.a, shape_a, system_names)} and "
            f"{plural(_entity_noun(link.b))} ({_detail(link.b, shape_b)}) appear together in "
            f"the same {system} events"
        )
        if features.pairing_consistency is not None:
            clauses.append(
                f"{_percent(features.pairing_consistency)}% of them always carry the same "
                f"{field_noun(link.b, shape_b)} for one {field_noun(link.a, shape_a)}"
            )
    elif link.link_type == LinkType.COMPOSITE:
        clauses.append(
            f"{_subject(link.a, shape_a, system_names)} and {_object(link.b, shape_b)} share "
            "no identifier; they match through dates, amounts and names in a composite rule"
        )
    elif link.direction == Direction.B_TO_A:
        tail = f" {lag}" if lag else ""
        clauses.append(
            f"{_percent(features.c_ba)}% of {_subject(link.b, shape_b, system_names)} match "
            f"{_object(link.a, shape_a)}{tail}"
        )
    elif link.direction == Direction.A_TO_B:
        tail = f" {lag}" if lag else ""
        clauses.append(
            f"{_percent(features.c_ab)}% of {_subject(link.a, shape_a, system_names)} match "
            f"{_object(link.b, shape_b)}{tail}"
        )
    else:
        clauses.append(
            f"{_percent(features.c_ab)}% of {_subject(link.a, shape_a, system_names)} share "
            f"their value with {_object(link.b, shape_b)}"
        )
        if lag:
            clauses.append(f"the two appear {lag} in no fixed order")
    forms = [
        phrase
        for phrase in (
            form_phrase(link.a.form, field_noun(link.a, shape_a)),
            form_phrase(link.b.form, field_noun(link.b, shape_b)),
        )
        if phrase is not None
    ]
    if forms:
        clauses.append(f"the match uses {' and '.join(forms)}")
    if link.role == LinkRole.ASSOCIATION:
        clauses.append(
            f"one {_entity_noun(link.a)} relates to several {plural(_entity_noun(link.b))} or "
            "the reverse, so the link groups records rather than identifying one"
        )
    elif link.role == LinkRole.BATCH:
        clauses.append(f"one value covers many {plural(_entity_noun(link.b))}: a batch")
    text = "; ".join(clauses)
    text = f"{text[:1].upper()}{text[1:]}."
    if features.n_match:
        text = f"{text} {features.n_match:,} shared values in the window."
    if len(text) > MAX_SENTENCE_LEN:
        text = text[: MAX_SENTENCE_LEN - 1].rstrip() + "."
    return text
