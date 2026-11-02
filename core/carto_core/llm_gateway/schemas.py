"""LLM gateway task schemas and the provider seam (spec 9.9; plan M2 decision 12).

Every task has a fixed payload model built only from metadata tables (field paths, shapes,
template text, low-cardinality attribute values) and a fixed output model with enumerated label
types and length limits. The gateway validates the payload before it leaves and the output
before anyone sees it; anything else is discarded and counted. Nothing in a payload is a token,
a raw value, reveal output, actor data, connector configuration or a secret.

A :class:`Provider` is one module with ``complete`` and nothing else: the gateway owns fencing,
schema validation, the egress allowlist, audit and retries, so an adapter cannot weaken them.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Final, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "LABEL_DICTIONARY",
    "LABEL_PATTERN",
    "MAX_LABEL_LEN",
    "RATIONALE_PATTERN",
    "AttributeSummary",
    "FieldSummary",
    "LabelFieldRequest",
    "LabelType",
    "NameEntityRequest",
    "NameNodeRequest",
    "Provider",
    "ProviderError",
    "Suggestion",
    "SuggestionList",
    "TaskKind",
    "rationale_is_clean",
]

MAX_LABEL_LEN: Final = 48
MAX_RATIONALE_LEN: Final = 200
MAX_TEMPLATE_TEXT_LEN: Final = 512

LABEL_PATTERN: Final = r"^[A-Za-z0-9 _:/().-]+$"
"""Letters, digits, space and a little punctuation: no markup, no braces, no control
characters, nothing a template engine or a shell would act on."""
RATIONALE_PATTERN: Final = r"^[^\x00-\x1f\x7f<>{}]*$"
"""A rationale is any printable text, quotes, ampersands and accents included, without a
control character, ``<``, ``>``, ``{`` or ``}``; :func:`rationale_is_clean` adds
``str.isprintable`` (no format or line-separator characters either), the length and the URL
rule. A rationale that fails is blanked, not refused (review of 2026-11-02): the label is what
is used, and a tightened character class dropped whole answers over an apostrophe."""
_RATIONALE_RE: Final = re.compile(RATIONALE_PATTERN)
_LINK_SHAPED: Final = re.compile(
    r"://|(?:^|[^A-Za-z0-9])www\.|\.[A-Za-z]{2,6}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
"""A URL scheme separator, a ``www.`` host, or a dot followed by two to six letters that end a
word (``evil.co``, ``orders.com``): a label a reviewer might take for a link and click. A dot
before a digit or a space (``no. 1``, ``v1.2``) is ordinary text."""
_URL_SHAPED: Final = re.compile(r"://|(?:^|[^A-Za-z0-9])www\.", re.IGNORECASE)
"""For the rationale, which may legitimately quote a dotted field path (``orders.order_id``):
only a scheme separator or a ``www.`` host is refused."""
MAX_ATTRIBUTE_VALUES: Final = 8
MAX_FIELDS_PER_REQUEST: Final = 64

LABEL_DICTIONARY: Final[tuple[str, ...]] = (
    "order",
    "po",
    "invoice",
    "claim",
    "member",
    "subscriber",
    "employee",
    "worker",
    "sku",
    "item",
    "shipment",
    "tracking",
    "file",
    "batch",
)
"""Spec 9.9: the heuristic dictionary of common field names, used with or without an LLM."""


def rationale_is_clean(text: str) -> bool:
    """Whether ``text`` may be shown as a suggestion's rationale (see :data:`RATIONALE_PATTERN`):
    at most :data:`MAX_RATIONALE_LEN` printable characters, none of ``<>{}``, no control
    character, and neither a URL scheme separator nor a ``www.`` host (a dotted field path is
    fine)."""
    return (
        len(text) <= MAX_RATIONALE_LEN
        and text.isprintable()
        and _RATIONALE_RE.match(text) is not None
        and not _URL_SHAPED.search(text)
    )


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_max_length=4096)


class TaskKind(StrEnum):
    LABEL_FIELD = "label_field"
    NAME_ENTITY = "name_entity"
    NAME_NODE = "name_node"


class LabelType(StrEnum):
    """What a suggested label names. Enumerated so the output schema is closed (spec 9.9)."""

    ORDER = "order"
    PURCHASE_ORDER = "purchase_order"
    INVOICE = "invoice"
    CLAIM = "claim"
    MEMBER = "member"
    SUBSCRIBER = "subscriber"
    EMPLOYEE = "employee"
    WORKER = "worker"
    CANDIDATE = "candidate"
    CUSTOMER = "customer"
    PRODUCT = "product"
    SKU = "sku"
    ITEM = "item"
    SHIPMENT = "shipment"
    TRACKING = "tracking"
    FILE = "file"
    BATCH = "batch"
    PAYMENT = "payment"
    CART = "cart"
    ACCOUNT = "account"
    USER = "user"
    OTHER = "other"


class AttributeSummary(_Strict):
    key: Annotated[str, Field(max_length=128)]
    values: Annotated[
        list[Annotated[str, Field(max_length=64)]], Field(max_length=MAX_ATTRIBUTE_VALUES)
    ]


class FieldSummary(_Strict):
    """One field as metadata only: path, shapes, template text; never a token or a value."""

    field_id: Annotated[str, Field(max_length=512)]
    path: Annotated[str, Field(max_length=256)]
    system_name: Annotated[str, Field(max_length=128)]
    top_shapes: Annotated[list[Annotated[str, Field(max_length=64)]], Field(max_length=8)]
    template_text: Annotated[str, Field(max_length=MAX_TEMPLATE_TEXT_LEN)] = ""
    attributes: Annotated[list[AttributeSummary], Field(max_length=16)] = Field(
        default_factory=list
    )


class LabelFieldRequest(_Strict):
    task: Annotated[TaskKind, Field(default=TaskKind.LABEL_FIELD)] = TaskKind.LABEL_FIELD
    fields: Annotated[list[FieldSummary], Field(min_length=1, max_length=MAX_FIELDS_PER_REQUEST)]


class NameEntityRequest(_Strict):
    task: Annotated[TaskKind, Field(default=TaskKind.NAME_ENTITY)] = TaskKind.NAME_ENTITY
    entity_id: Annotated[str, Field(max_length=64)]
    fields: Annotated[list[FieldSummary], Field(min_length=1, max_length=MAX_FIELDS_PER_REQUEST)]


class NameNodeRequest(_Strict):
    """M3 uses it (node naming); the schema exists so the gateway's task table is complete."""

    task: Annotated[TaskKind, Field(default=TaskKind.NAME_NODE)] = TaskKind.NAME_NODE
    node_id: Annotated[str, Field(max_length=64)]
    system_name: Annotated[str, Field(max_length=128)]
    template_text: Annotated[str, Field(max_length=MAX_TEMPLATE_TEXT_LEN)]


class Suggestion(_Strict):
    """One suggested label or name; shown as "Suggested" until a person confirms it."""

    target_id: Annotated[str, Field(max_length=512)]
    label_type: LabelType
    label: Annotated[str, Field(min_length=1, max_length=MAX_LABEL_LEN, pattern=LABEL_PATTERN)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    rationale: Annotated[str, Field(max_length=MAX_RATIONALE_LEN)] = ""

    @field_validator("label")
    @classmethod
    def _label_is_not_link_shaped(cls, value: str) -> str:
        """A label must not look like a URL or a domain (ADR 0070 section 7): the console shows
        it to a reviewer, and a name shaped like a link invites a click."""
        if _LINK_SHAPED.search(value):
            msg = "must not look like a URL or a domain name"
            raise ValueError(msg)
        return value

    @field_validator("rationale", mode="before")
    @classmethod
    def _rationale_or_nothing(cls, value: object) -> str:
        """A rationale that is not clean text (:func:`rationale_is_clean`) becomes ``""``
        instead of failing the suggestion: the label carries the decision, the rationale is
        a courtesy, and a reviewer is shown no markup, brace, control character or link."""
        return value if isinstance(value, str) and rationale_is_clean(value) else ""


class SuggestionList(_Strict):
    suggestions: Annotated[list[Suggestion], Field(max_length=MAX_FIELDS_PER_REQUEST)]


class ProviderError(Exception):
    """The provider did not answer usably. The message names the cause, never the payload."""


@runtime_checkable
class Provider(Protocol):
    """One adapter module (plan M2 decision 12): Anthropic, OpenAI-compatible, the fake."""

    name: str

    def complete(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> str:
        """Return the model's raw text for the fenced prompt; raise :class:`ProviderError`."""
        ...
