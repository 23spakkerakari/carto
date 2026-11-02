"""Heuristic names for entity families and fields without an LLM (spec 9.9 "Without the LLM").

The dictionary is the spec's (``order, po, invoice, claim, member, subscriber, employee, worker,
sku, item, shipment, tracking, file, batch``) plus the labels the gateway's closed
:class:`~carto_core.llm_gateway.schemas.LabelType` already allows (candidate, customer,
product, payment) and a few everyday synonyms and abbreviations (``purchase_order`` is a PO,
``emp`` an employee, ``cand_ref`` a candidate). Paths are tokenized on ``_``, ``.``, ``-``,
``/`` and camelCase; every label found in a path votes once with the path's weight (the
field's event count when known), template texts vote with weight one, and a file-like shape
(``AAAA_99999999_9999.AAA``) votes for File. The majority wins; ties fall to the dictionary
order, which puts the spec's own words first. Output is Title Case, at most
:data:`MAX_NAME_LEN` characters, and None when nothing in the inputs names a thing: the
reviewer sees an empty name field rather than a guess.

Everything here reads field names, template text and shapes: metadata the edge already masked
(ADR 0027), never a token or a value.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from typing import Final

__all__ = [
    "LABEL_ORDER",
    "MAX_NAME_LEN",
    "labels_in",
    "noun_for",
    "noun_for_path",
    "plural",
    "suggest_entity_name",
    "suggest_field_label",
    "tokenize",
]

MAX_NAME_LEN: Final = 48

LABEL_ORDER: Final[tuple[str, ...]] = (
    "Order",
    "PO",
    "Invoice",
    "Claim",
    "Member",
    "Subscriber",
    "Employee",
    "Worker",
    "SKU",
    "Item",
    "Shipment",
    "Tracking",
    "File",
    "Batch",
    "Candidate",
    "Customer",
    "Product",
    "Payment",
    "Cart",
    "Manifest",
)
"""Tie-break order: the spec 9.9 dictionary first, then the gateway's extra label types."""

_TOKEN_LABELS: Final[dict[str, str]] = {
    "order": "Order",
    "ord": "Order",
    "po": "PO",
    "purchaseorder": "PO",
    "invoice": "Invoice",
    "inv": "Invoice",
    "claim": "Claim",
    "clm": "Claim",
    "member": "Member",
    "mbr": "Member",
    "subscriber": "Subscriber",
    "employee": "Employee",
    "emp": "Employee",
    "worker": "Worker",
    "sku": "SKU",
    "item": "Item",
    "shipment": "Shipment",
    "ship": "Shipment",
    "shp": "Shipment",
    "tracking": "Tracking",
    "trk": "Tracking",
    "file": "File",
    "filename": "File",
    "batch": "Batch",
    "candidate": "Candidate",
    "cand": "Candidate",
    "customer": "Customer",
    "cust": "Customer",
    "product": "Product",
    "prod": "Product",
    "payment": "Payment",
    "cart": "Cart",
    "manifest": "Manifest",
}
_BIGRAM_LABELS: Final[dict[tuple[str, str], str]] = {
    ("purchase", "order"): "PO",
    ("sales", "order"): "Order",
    ("work", "order"): "Order",
    ("stock", "keeping"): "SKU",
}
_NOUNS: Final[dict[str, str]] = {"PO": "purchase order", "SKU": "SKU"}
_NUMBER_SUFFIX: Final[frozenset[str]] = frozenset({"num", "number", "no", "nr"})
_SPLIT: Final = re.compile(r"[^0-9A-Za-z]+")
_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_FILE_SHAPE: Final = re.compile(r"\.[Aa]{2,4}$")
_RANK: Final[dict[str, int]] = {label: index for index, label in enumerate(LABEL_ORDER)}


def tokenize(text: str) -> list[str]:
    """Lower-case word tokens: split on punctuation and camelCase."""
    tokens: list[str] = []
    for piece in _SPLIT.split(text):
        if not piece:
            continue
        tokens.extend(part.lower() for part in _CAMEL.split(piece) if part)
    return tokens


def labels_in(tokens: Sequence[str]) -> list[str]:
    """The labels named by a token sequence, in order of appearance, each once."""
    found: list[str] = []
    index = 0
    while index < len(tokens):
        if index + 1 < len(tokens):
            label = _BIGRAM_LABELS.get((tokens[index], tokens[index + 1]))
            if label is not None:
                if label not in found:
                    found.append(label)
                index += 2
                continue
        label = _TOKEN_LABELS.get(tokens[index])
        if label is not None and label not in found:
            found.append(label)
        index += 1
    return found


def _cap(name: str) -> str:
    return name if len(name) <= MAX_NAME_LEN else name[:MAX_NAME_LEN].rstrip()


def suggest_entity_name(
    paths: Sequence[str],
    template_texts: Sequence[str] = (),
    shapes: Sequence[str] = (),
    *,
    weights: Sequence[int] | None = None,
) -> str | None:
    """The majority label over the family's field paths (weighted), template texts and shapes,
    or None when none of them names a thing."""
    votes: Counter[str] = Counter()
    for index, path in enumerate(paths):
        weight = weights[index] if weights is not None and index < len(weights) else 1
        for label in labels_in(tokenize(path)):
            votes[label] += max(1, weight)
    for text in template_texts:
        for label in labels_in(tokenize(text)):
            votes[label] += 1
    for shape in shapes:
        if _FILE_SHAPE.search(shape) is not None:
            votes["File"] += 1
    if not votes:
        return None
    best = min(votes.items(), key=lambda item: (-item[1], _RANK.get(item[0], len(_RANK))))
    return _cap(best[0])


def noun_for(label: str) -> str:
    """The business noun of a label, singular and lower case (``PO`` is ``purchase order``)."""
    return _NOUNS.get(label, label.lower())


def noun_for_path(path: str) -> str | None:
    """The noun a single field path names, or None."""
    labels = labels_in(tokenize(path))
    return noun_for(labels[0]) if labels else None


def plural(noun: str) -> str:
    if noun.isupper():
        return f"{noun}s"
    if noun.endswith(("s", "x", "ch", "sh")):
        return f"{noun}es"
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in "aeiou":
        return f"{noun[:-1]}ies"
    return f"{noun}s"


def suggest_field_label(path: str, shape: str | None = None) -> str | None:
    """A label for one field: its noun plus what kind of identifier it is (``PO number`` for
    ``po_num`` with shape ``99-999``, ``Order reference`` for ``order_ref``), or None."""
    tokens = tokenize(path)
    labels = labels_in(tokens)
    if not labels:
        return None
    label = labels[0]
    digits_only = bool(shape) and set(shape or "") <= set("9+")
    numeric_suffix = bool(tokens) and tokens[-1] in _NUMBER_SUFFIX
    kind = "number" if digits_only or numeric_suffix else "reference"
    return _cap(f"{label} {kind}")
