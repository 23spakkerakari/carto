"""Field classification and policy (spec 8.3; 5.4 steps 4 and 5; 2.3 invariants 2 and 3).

:class:`Classifier` turns the streaming statistics of a field (:mod:`carto_edge.pipeline.stats`)
into a :class:`~carto_edge.pipeline.model.FieldDecision`: class, policy (keep, tokenize, drop),
the forms to tokenize, whether an admin pin decided, and a reason that never carries a value.
The rules of spec 8.3 run in order:

1. ``secret_like``: a name segment hint (``password``, ``token``, ``api_key``, ...) or a secret
   shaped value (JWT, PEM, AWS key id, long high-entropy base64) seen at any time: the
   statistics count those at observation, so one such value marks the field for good and
   the verdict does not depend on what the reservoir kept. **Always dropped**, and before
   pins: a pin cannot keep a secret.
2. PII (``person_name``, ``contact``, ``government_id``, ``financial``, ``health``): name hints,
   segment-aware (``customer_name`` and ``cardholderName`` are names, ``username`` and
   ``file_name`` are not), or the detector hitting at least :data:`PII_HIT_SHARE` of up to
   :data:`PII_SAMPLE_LIMIT` samples with a score at or above ``pii.score_threshold``. Dropped
   by default; a ``person_name`` pinned to tokenize gets the ``phonetic`` family.
3. ``timestamp`` (dropped: the parser already consumed ``observed_at``) and ``date``
   (tokenized as the ``date`` form) from a built-in set of conservative formats; epoch seconds
   and milliseconds only when the name says so.
4. ``amount``: decimals with two fraction digits, or a currency hint in the name with numeric
   values. Tokenized as the ``amount`` form only.
5. ``identifier``: distinct estimate above ``distinct_threshold`` or above ``distinct_ratio``
   of the non-null count, mean length within the identifier range, not mostly whitespace, not
   floats. Tokenized with :data:`IDENTIFIER_FORMS`.
6. ``low_card_attribute``: at or below both thresholds and no detector hit in the samples.
   Kept in clear, except (ADR 0029): when at least :data:`DIGIT_RUN_SHARE` of the samples
   contain a digit run (:func:`has_digit_run`, four or more digits: batch keys such as
   ``MAN-20260923-01``), the field is an ``identifier`` (reason ``identifier:digit_run``) and
   tokenized; in a kept field, a single value with a digit run does not travel in clear
   (:meth:`Classifier.keeps`). Admin pins skip both.
7. ``free_text``: mean length above 32 and most values containing whitespace. Dropped.
8. Quarantine (checked before 5 to 7): fewer than ``quarantine_samples`` non-null values.
   Tokenized with :data:`IDENTIFIER_FORMS` when identifier-shaped, dropped otherwise; class
   ``unknown``, reason ``quarantine``. Anything past quarantine that no rule claims is
   ``unknown`` and dropped (reason ``unclassified``).

Admin pins (``FieldPolicyPin``, matched on the exact ``field_ref`` or ``system/*/path``)
override rules 2 to 8, never rule 1.

**Form families.** ``forms`` names form families, not final form names: ``digits`` stands for
``digits.0`` to ``digits.2`` (one per digit run, spec 8.4) and ``phonetic`` for ``phonetic.0``
to ``phonetic.7`` (one per name token). The tokenizer expands a family per value; an exact form
name from a pin (``digits.0``) is passed through unchanged.

**Caching.** A decision is cached per field and recomputed when the non-null count crosses the
quarantine threshold or every :data:`RECHECK_EVERY` observations. The detector pass over the
samples is amortized further: it reruns when the sample set grew or the non-null count doubled.
A class change after the first decision is logged as ``field_reclassified`` (ref, old and new
class, counts; never a value). ``decisions``, ``field_summary`` and ``bundle_fields`` give the
bundle writer what ``fields.json`` and ``MANIFEST.md`` need; sample values appear only for
``keep`` fields, at most five, truncated and detector-checked.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from carto_common.logging import get_logger, is_secret_key
from carto_edge.config import ClassifySettings, FieldPolicyPin
from carto_edge.pipeline.model import FieldClass, FieldDecision, Policy, split_field_ref
from carto_edge.pipeline.pii import PiiDetector, PiiHit, detect_many
from carto_edge.pipeline.redact import attribute_is_clean, truncate_attribute
from carto_edge.pipeline.stats import FieldStats, FieldStatsStore
from carto_schema.bundle import MAX_SAMPLE_VALUES, MAX_TOP_SHAPES, BundleField
from carto_schema.forms import is_form

__all__ = [
    "AMOUNT_FORMS",
    "DATE_FORMS",
    "DIGIT_RUN_SHARE",
    "FORM_FAMILIES",
    "IDENTIFIER_FORMS",
    "PHONETIC_FORMS",
    "PII_HIT_SHARE",
    "PII_SAMPLE_LIMIT",
    "RECHECK_EVERY",
    "Classifier",
    "has_digit_run",
    "is_amount_value",
    "is_secret_name",
    "name_segments",
    "pii_class_by_name",
    "temporal_kind",
]

IDENTIFIER_FORMS: Final[tuple[str, ...]] = ("raw", "norm", "alnum", "digits")
DATE_FORMS: Final[tuple[str, ...]] = ("date",)
AMOUNT_FORMS: Final[tuple[str, ...]] = ("amount",)
PHONETIC_FORMS: Final[tuple[str, ...]] = ("phonetic",)
FORM_FAMILIES: Final = frozenset({"raw", "norm", "alnum", "digits", "date", "amount", "phonetic"})
"""Names a decision may carry in ``forms``: families; the tokenizer expands ``digits`` and
``phonetic`` to their indexed forms (spec 8.4)."""

PII_SAMPLE_LIMIT: Final = 32
PII_HIT_SHARE: Final = 0.3
RECHECK_EVERY: Final = 1000
MIN_KEPT_VALUE_COUNT: Final = 3
DIGIT_RUN_SHARE: Final = 0.5
"""Share of samples with a digit run that makes a low-cardinality field an identifier."""
"""A kept field's value travels in clear only once this exact value was seen this often."""
MAJORITY: Final = 0.8
"""Share of samples a shape rule (temporal, amount, float) needs to claim a field."""
FREE_TEXT_MIN_MEAN_LEN: Final = 32
FREE_TEXT_MIN_SPACE_RATE: Final = 0.5
SHAPE_CONSISTENCY: Final = 0.8
"""Share of non-null values the four most common shapes must cover for "identifier-shaped"."""

_PII_CLASSES: Final = frozenset(
    {
        FieldClass.PERSON_NAME,
        FieldClass.CONTACT,
        FieldClass.GOVERNMENT_ID,
        FieldClass.FINANCIAL,
        FieldClass.HEALTH,
    }
)

_ENTITY_CLASS: Final[dict[str, FieldClass]] = {
    "PERSON": FieldClass.PERSON_NAME,
    "EMAIL_ADDRESS": FieldClass.CONTACT,
    "PHONE_NUMBER": FieldClass.CONTACT,
    "LOCATION": FieldClass.CONTACT,
    "US_SSN": FieldClass.GOVERNMENT_ID,
    "US_PASSPORT": FieldClass.GOVERNMENT_ID,
    "US_DRIVER_LICENSE": FieldClass.GOVERNMENT_ID,
    "CREDIT_CARD": FieldClass.FINANCIAL,
    "IBAN_CODE": FieldClass.FINANCIAL,
    "MEDICAL_LICENSE": FieldClass.HEALTH,
}

# ---------------------------------------------------------------------------------------------
# Name hints
# ---------------------------------------------------------------------------------------------

_CAMEL: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_DIGIT_BOUNDARY: Final = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")
_SEPARATORS: Final = re.compile(r"[^a-z0-9]+")

_SECRET_WORDS: Final = frozenset(
    {
        "password",
        "passwords",
        "passwd",
        "secret",
        "secrets",
        "token",
        "tokens",
        "apikey",
        "apikeys",
        "authorization",
        "cookie",
        "cookies",
        "privatekey",
        "bearer",
        "credential",
        "credentials",
    }
)
_SECRET_PAIRS: Final = frozenset(
    {
        ("api", "key"),
        ("api", "keys"),
        ("private", "key"),
        ("private", "keys"),
        ("client", "secret"),
        ("access", "key"),
        ("secret", "key"),
        ("auth", "token"),
        ("session", "token"),
        ("refresh", "token"),
    }
)
_SECRET_QUALIFIERS: Final = frozenset(
    {
        "count",
        "ref",
        "name",
        "type",
        "ttl",
        "len",
        "length",
        "size",
        "kind",
        "enabled",
        "required",
        "expires",
        "expiry",
        "expiration",
        "age",
        "scope",
        "scopes",
        "issuer",
        "audience",
        "algorithm",
        "alg",
        "url",
        "uri",
        "endpoint",
    }
)

_NAME_WORDS: Final = frozenset(
    {
        "firstname",
        "lastname",
        "fullname",
        "surname",
        "givenname",
        "familyname",
        "forename",
        "middlename",
        "cardholder",
    }
)
_TECHNICAL_BEFORE_NAME: Final = frozenset(
    {
        "user",
        "host",
        "service",
        "file",
        "app",
        "application",
        "table",
        "column",
        "col",
        "db",
        "database",
        "schema",
        "queue",
        "topic",
        "job",
        "process",
        "proc",
        "thread",
        "class",
        "method",
        "function",
        "func",
        "module",
        "template",
        "event",
        "env",
        "environment",
        "pod",
        "container",
        "node",
        "cluster",
        "region",
        "bucket",
        "role",
        "field",
        "source",
        "system",
        "connector",
        "product",
        "item",
        "sku",
        "warehouse",
        "carrier",
        "plan",
        "package",
        "rule",
        "policy",
        "pipeline",
        "stage",
        "step",
        "task",
        "workflow",
        "channel",
        "device",
        "model",
        "version",
        "release",
        "branch",
        "repo",
        "repository",
        "project",
        "domain",
        "zone",
        "site",
        "page",
        "screen",
        "report",
        "logger",
        "index",
        "key",
        "metric",
        "tag",
        "label",
        "attribute",
        "attr",
        "param",
        "parameter",
        "header",
        "dns",
        "server",
        "machine",
        "vm",
        "instance",
        "image",
        "volume",
        "disk",
        "network",
        "interface",
        "port",
        "protocol",
        "config",
        "setting",
        "option",
        "flag",
        "feature",
        "test",
        "suite",
        "scenario",
        "batch",
        "partition",
        "shard",
        "exception",
        "error",
        "operation",
        "op",
        "action",
        "command",
        "cmd",
        "script",
        "plugin",
        "extension",
        "library",
        "lib",
        "framework",
        "language",
        "lang",
        "os",
        "platform",
        "vendor",
        "brand",
        "company",
        "org",
        "organization",
        "team",
        "dept",
        "department",
        "store",
        "shop",
        "merchant",
        "supplier",
        "provider",
        "processor",
        "gateway",
        "bank",
        "group",
        "workspace",
        "tenant",
        "namespace",
        "realm",
        "sheet",
        "tab",
        "doc",
        "document",
        "folder",
        "dir",
        "directory",
        "path",
        "resource",
        "endpoint",
        "route",
        "api",
        "span",
        "trace",
        "segment",
        "collection",
        "dataset",
        "entity",
        "object",
        "element",
        "component",
        "widget",
        "form",
        "cron",
        "scheduler",
        "exchange",
        "stream",
        "kernel",
        "driver",
        "firmware",
        "printer",
        "camera",
        "sensor",
        "unit",
        "location",
        "facility",
        "building",
        "room",
        "dc",
    }
)
_CONTACT_WORDS: Final = frozenset(
    {
        "email",
        "emails",
        "phone",
        "phones",
        "mobile",
        "telephone",
        "tel",
        "fax",
        "address",
        "addr",
        "street",
        "zip",
        "zipcode",
        "postcode",
        "postal",
    }
)
_NOT_CONTACT_PAIRS: Final = frozenset(
    {
        ("ip", "address"),
        ("ip", "addr"),
        ("mac", "address"),
        ("mac", "addr"),
        ("host", "address"),
        ("server", "address"),
        ("memory", "address"),
        ("return", "address"),
        ("contract", "address"),
        ("wallet", "address"),
    }
)
_GOVERNMENT_WORDS: Final = frozenset(
    {
        "ssn",
        "sin",
        "nino",
        "passport",
        "taxid",
        "tin",
        "itin",
        "nationalid",
        "aadhaar",
        "pesel",
        "dob",
        "birth",
        "birthdate",
        "birthday",
    }
)
_GOVERNMENT_PAIRS: Final = frozenset(
    {
        ("driver", "license"),
        ("drivers", "license"),
        ("driver", "licence"),
        ("drivers", "licence"),
        ("driving", "licence"),
        ("driving", "license"),
        ("tax", "id"),
        ("national", "id"),
        ("social", "security"),
    }
)
_FINANCIAL_WORDS: Final = frozenset(
    {"iban", "bic", "swift", "creditcard", "cardnumber", "ccnum", "ccnumber", "cvv", "cvc", "cvv2"}
)
_FINANCIAL_PAIRS: Final = frozenset(
    {
        ("credit", "card"),
        ("card", "number"),
        ("card", "num"),
        ("cc", "num"),
        ("cc", "number"),
        ("routing", "number"),
        ("bank", "account"),
    }
)
_HEALTH_WORDS: Final = frozenset(
    {
        "diagnosis",
        "diagnoses",
        "icd",
        "icd9",
        "icd10",
        "npi",
        "patient",
        "mrn",
        "medical",
        "cpt",
        "ndc",
        "drg",
        "prescription",
        "dx",
    }
)
_AMOUNT_WORDS: Final = frozenset(
    {
        "amount",
        "amt",
        "total",
        "subtotal",
        "price",
        "cost",
        "costs",
        "fee",
        "fees",
        "balance",
        "salary",
        "pay",
        "wage",
        "wages",
        "revenue",
        "charge",
        "charges",
        "tax",
        "discount",
        "payment",
    }
)
_TIME_WORDS: Final = frozenset(
    {"ts", "timestamp", "time", "datetime", "epoch", "at", "date", "created", "updated", "modified"}
)


def name_segments(path: str) -> list[str]:
    """Lower-cased segments of a field path split on separators, camelCase and letter-digit
    boundaries: ``cardholderName`` gives ``["cardholder", "name"]``, ``icd10_code`` gives
    ``["icd", "10", "code"]``, ``username`` stays one segment."""
    spaced = _DIGIT_BOUNDARY.sub("_", _CAMEL.sub("_", path))
    return [segment for segment in _SEPARATORS.split(spaced.lower()) if segment]


def is_secret_name(path: str) -> bool:
    """Spec 8.3 rule 1 name hints, unless the last segment is a qualifier (``token_count``,
    ``secret_ref`` describe a secret and hold none). Also defers to the log redactor's own
    key rule so the two can never disagree about what is a secret."""
    segments = name_segments(path)
    if not segments:
        return False
    if segments[-1] not in _SECRET_QUALIFIERS and (
        any(segment in _SECRET_WORDS for segment in segments)
        or any(pair in _SECRET_PAIRS for pair in itertools.pairwise(segments))
    ):
        return True
    return is_secret_key(path)


def pii_class_by_name(path: str) -> FieldClass | None:
    """Spec 8.3 rule 2 name hints, segment-aware. ``name`` counts only when the segment before
    it is not a technical qualifier (``user``, ``host``, ``file``, ``table``, ...)."""
    segments = name_segments(path)
    pairs = set(itertools.pairwise(segments))
    for index, segment in enumerate(segments):
        if segment in _NAME_WORDS:
            return FieldClass.PERSON_NAME
        if segment == "name" and (index == 0 or segments[index - 1] not in _TECHNICAL_BEFORE_NAME):
            return FieldClass.PERSON_NAME
    if any(segment in _GOVERNMENT_WORDS for segment in segments) or pairs & _GOVERNMENT_PAIRS:
        return FieldClass.GOVERNMENT_ID
    if any(segment in _FINANCIAL_WORDS for segment in segments) or pairs & _FINANCIAL_PAIRS:
        return FieldClass.FINANCIAL
    if any(segment in _HEALTH_WORDS for segment in segments):
        return FieldClass.HEALTH
    if any(segment in _CONTACT_WORDS for segment in segments) and not pairs & _NOT_CONTACT_PAIRS:
        return FieldClass.CONTACT
    return None


# ---------------------------------------------------------------------------------------------
# Value shapes
# ---------------------------------------------------------------------------------------------

_MONTHS: Final = frozenset(
    {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}
)
_TZ: Final = r"(?:Z|[+-]\d{2}:?\d{2})?"
_CLOCK: Final = r"(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?"
_DATE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "ymd"),
    (re.compile(r"(\d{4})/(\d{2})/(\d{2})"), "ymd"),
    (re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})"), "dmy_or_mdy"),
    (re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})"), "d_mon_y"),
    (re.compile(r"([A-Za-z]{3})[a-z]* (\d{1,2}),? (\d{4})"), "mon_d_y"),
)
_DATETIME_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(rf"(\d{{4}})-(\d{{2}})-(\d{{2}})[T ]{_CLOCK}{_TZ}"), "ymd_clock"),
    (re.compile(rf"(\d{{4}})/(\d{{2}})/(\d{{2}}) {_CLOCK}{_TZ}"), "ymd_clock"),
    (re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(?:\.\d{1,9})?Z?"), "compact"),
    (re.compile(r"(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) [+-]\d{4}"), "apache"),
    (
        re.compile(
            r"(?:[A-Za-z]{3}, )?(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2}):(\d{2})"
            r"(?: [+-]\d{4}| [A-Z]{1,4})?"
        ),
        "rfc2822",
    ),
    (re.compile(r"([A-Za-z]{3}) +(\d{1,2}) (\d{2}):(\d{2}):(\d{2})"), "syslog"),
    (re.compile(r"(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?"), "clock"),
)
_EPOCH_SECONDS: Final = re.compile(r"[12]\d{9}")
_EPOCH_MILLIS: Final = re.compile(r"[12]\d{12}")


def _valid_ymd(year: str, month: str, day: str) -> bool:
    if not year or not month or not day:
        return False
    return 1 <= int(month) <= 12 and 1 <= int(day) <= 31 and 1000 <= int(year) <= 9999


def _valid_clock(hour: str, minute: str, second: str) -> bool:
    """``second`` may be empty (an optional group that did not match)."""
    if not hour or not minute:
        return False
    return int(hour) < 24 and int(minute) < 60 and (not second or int(second) < 61)


def temporal_kind(value: str) -> str | None:
    """``"date"`` or ``"datetime"`` when ``value`` matches one of the built-in formats with
    plausible fields, else ``None``. Conservative on purpose: eight bare digits and epoch
    numbers are not temporal here (see the name-hinted epoch rule)."""
    text = value.strip()
    if not text:
        return None
    for pattern, kind in _DATETIME_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            groups = tuple(group or "" for group in match.groups())
            return "datetime" if _datetime_fields_valid(kind, groups) else None
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.fullmatch(text)
        if match:
            groups = tuple(group or "" for group in match.groups())
            return "date" if _date_fields_valid(kind, groups) else None
    return None


def _datetime_fields_valid(kind: str, groups: tuple[str, ...]) -> bool:
    if kind in ("ymd_clock", "compact"):
        return _valid_ymd(groups[0], groups[1], groups[2]) and _valid_clock(
            groups[3], groups[4], groups[5]
        )
    if kind in ("apache", "rfc2822"):
        return groups[1].lower() in _MONTHS and _valid_clock(groups[3], groups[4], groups[5])
    if kind == "syslog":
        return groups[0].lower() in _MONTHS and _valid_clock(groups[2], groups[3], groups[4])
    return _valid_clock(groups[0], groups[1], groups[2])


def _date_fields_valid(kind: str, groups: tuple[str, ...]) -> bool:
    if kind == "ymd":
        return _valid_ymd(groups[0], groups[1], groups[2])
    if kind == "dmy_or_mdy":
        first, second = int(groups[0]), int(groups[1])
        return 1 <= first <= 31 and 1 <= second <= 31 and min(first, second) <= 12
    if kind == "d_mon_y":
        return groups[1].lower() in _MONTHS and 1 <= int(groups[0]) <= 31
    return groups[0].lower() in _MONTHS and 1 <= int(groups[1]) <= 31


_CURRENCY_PREFIX: Final = r"(?:[$€£¥]\s?|[A-Z]{3}\s)?"
_CURRENCY_SUFFIX: Final = r"(?:\s?[A-Z]{3})?"
_AMOUNT: Final = re.compile(
    rf"[-+]?{_CURRENCY_PREFIX}[-+]?(?:\d{{1,3}}(?:,\d{{3}})+|\d+)\.\d{{2}}{_CURRENCY_SUFFIX}"
)
_NUMERIC: Final = re.compile(
    rf"[-+]?{_CURRENCY_PREFIX}[-+]?(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?{_CURRENCY_SUFFIX}"
)
_FLOAT: Final = re.compile(r"[-+]?\d+\.\d+")
_DIGIT_RUN: Final = re.compile(r"[0-9]{4}")
_IDENTIFIER_SHAPED: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:/#+=-]*[A-Za-z0-9])?")


def is_amount_value(value: str) -> bool:
    """A decimal with exactly two fraction digits, optionally signed, grouped and with a
    currency symbol or code (``129.99``, ``$1,299.00``, ``EUR 10.00``)."""
    return _AMOUNT.fullmatch(value.strip()) is not None


def has_digit_run(value: str) -> bool:
    """Whether ``value`` holds four or more consecutive ASCII digits (ADR 0029): the mark of
    an identifier even in a low-cardinality field (``MAN-20260923-01``, ``SHIP_20260923.csv``).
    Short codes (``DC-03``, ``200``) do not have one."""
    return _DIGIT_RUN.search(value) is not None


def _is_numeric(value: str) -> bool:
    return _NUMERIC.fullmatch(value.strip()) is not None


def _share(samples: Sequence[str], predicate: Any) -> float:
    return sum(1 for sample in samples if predicate(sample)) / len(samples) if samples else 0.0


# ---------------------------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pin:
    system_id: str
    template_id: str
    path: str
    pin: FieldPolicyPin

    def matches(self, system_id: str, template_id: str) -> bool:
        return self.system_id in ("*", system_id) and self.template_id in ("*", template_id)


def _default_forms(field_class: FieldClass) -> tuple[str, ...]:
    if field_class in (FieldClass.DATE, FieldClass.TIMESTAMP):
        return DATE_FORMS
    if field_class is FieldClass.AMOUNT:
        return AMOUNT_FORMS
    if field_class is FieldClass.PERSON_NAME:
        return PHONETIC_FORMS
    return IDENTIFIER_FORMS


def _parse_pins(pins: Sequence[FieldPolicyPin]) -> tuple[dict[str, _Pin], dict[str, list[_Pin]]]:
    exact: dict[str, _Pin] = {}
    wild: dict[str, list[_Pin]] = {}
    for pin in pins:
        try:
            system_id, template_id, path = split_field_ref(pin.field)
        except ValueError as exc:
            msg = f"field policy pin {pin.field!r} is not a field_ref or a system/*/path pattern"
            raise ValueError(msg) from exc
        for form in pin.forms or ():
            if form not in FORM_FAMILIES and not is_form(form):
                msg = f"field policy pin {pin.field!r} names an unknown form {form!r}"
                raise ValueError(msg)
        parsed = _Pin(system_id, template_id, path, pin)
        if "*" in (system_id, template_id):
            wild.setdefault(path, []).append(parsed)
        else:
            exact[pin.field] = parsed
    return exact, wild


# ---------------------------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Verdict:
    """What the detector said about the samples of a field, and when."""

    checked_at: int
    samples: int
    hit_samples: int
    entity: str | None


@dataclass(slots=True)
class _Cached:
    decision: FieldDecision
    count_at: int
    quarantined: bool


def _logger() -> Any:
    return get_logger(component="carto_edge.pipeline.classify")


class Classifier:
    """Spec 8.3 classification over :class:`FieldStatsStore` statistics. Thread-safe."""

    def __init__(
        self,
        settings: ClassifySettings,
        pins: Sequence[FieldPolicyPin],
        detector: PiiDetector,
        stats: FieldStatsStore,
        *,
        policy_version: str = "",
    ) -> None:
        self._settings = settings
        self._detector = detector
        self._stats = stats
        self._policy_version = policy_version
        self._pins_exact, self._pins_wild = _parse_pins(pins)
        self._paths: dict[str, str] = {}
        self._cache: dict[str, _Cached] = {}
        self._verdicts: dict[str, _Verdict] = {}
        self._lock = threading.RLock()

    @property
    def policy_version(self) -> str:
        """Spec 8.3 ``policy_version``: passed through to every event the edge emits."""
        return self._policy_version

    @property
    def settings(self) -> ClassifySettings:
        return self._settings

    # -- observation and decision ------------------------------------------------------------

    def observe(self, ref: str, path: str, value: str | None) -> None:
        """Feed one value into the field's statistics. ``path`` is remembered for the ref."""
        self._paths.setdefault(ref, path)
        self._stats.observe(ref, value)

    def decide(self, ref: str, path: str, *, name_class: FieldClass | None = None) -> FieldDecision:
        """The current decision for a field; cached, see the module docstring.

        ``name_class`` is the class the name gate read from a raw name it masked (ADR 0030
        amendment): rules 1 and 2 apply it as if the path carried that name. Such a decision
        belongs to one record, so it is neither cached nor cached from."""
        if name_class is not None:
            with self._lock:
                self._paths.setdefault(ref, path)
                return self._classify(ref, path, self._stats.get(ref), name_class)
        with self._lock:
            self._paths.setdefault(ref, path)
            stats = self._stats.get(ref)
            count = stats.count if stats is not None else 0
            non_null = stats.non_null_count if stats is not None else 0
            quarantined = non_null < self._settings.quarantine_samples
            cached = self._cache.get(ref)
            if (
                cached is not None
                and cached.quarantined == quarantined
                and count - cached.count_at < RECHECK_EVERY
            ):
                return cached.decision
            decision = self._classify(ref, path, stats)
            if cached is not None and cached.decision.field_class is not decision.field_class:
                _logger().info(
                    "field_reclassified",
                    field_ref=ref,
                    old_class=cached.decision.field_class.value,
                    new_class=decision.field_class.value,
                    old_policy=cached.decision.policy.value,
                    new_policy=decision.policy.value,
                    samples_seen=decision.samples_seen,
                )
            self._cache[ref] = _Cached(decision, count, quarantined)
            return decision

    def keeps(self, ref: str, value: str, *, pinned: bool = False) -> bool:
        """Whether a value of a ``keep`` field may travel in clear: the exact value must have
        been seen at least :data:`MIN_KEPT_VALUE_COUNT` times in the field. A low-cardinality
        field can still carry a rare value (``yes`` and ``no`` 290 times, then a customer's
        name once); a value seen once or twice stays at the edge (spec 2.3 invariant 2). Unless
        the field is ``pinned`` to keep, a value with a digit run stays at the edge too (ADR
        0029)."""
        if not pinned and has_digit_run(value):
            return False
        stats = self._stats.get(ref)
        if stats is None:
            return False
        return stats.value_count(value) >= MIN_KEPT_VALUE_COUNT

    def decisions(self) -> dict[str, FieldDecision]:
        """Every cached decision by ``field_ref``."""
        with self._lock:
            return {ref: cached.decision for ref, cached in self._cache.items()}

    # -- the rules ---------------------------------------------------------------------------

    def _classify(
        self,
        ref: str,
        path: str,
        stats: FieldStats | None,
        name_class: FieldClass | None = None,
    ) -> FieldDecision:
        samples: tuple[str, ...] = stats.samples if stats is not None else ()
        non_null = stats.non_null_count if stats is not None else 0
        settings = self._settings

        # Rule 1: secrets, before pins.
        if name_class is FieldClass.SECRET_LIKE or is_secret_name(path):
            return FieldDecision(
                FieldClass.SECRET_LIKE,
                Policy.DROP,
                reason="secret_like:name",
                samples_seen=non_null,
            )
        if stats is not None and stats.secret_count > 0:
            return FieldDecision(
                FieldClass.SECRET_LIKE,
                Policy.DROP,
                reason="secret_like:value",
                samples_seen=non_null,
            )

        # Admin pins override rules 2 to 8.
        pin = self._pin_for(ref, path)
        if pin is not None:
            return self._pinned(pin, non_null)

        # Rule 2: PII by name (of the path, or of the raw name the gate masked), then by
        # detector.
        pii_class = name_class if name_class is not None else pii_class_by_name(path)
        if pii_class is not None:
            return FieldDecision(pii_class, Policy.DROP, reason="pii:name", samples_seen=non_null)
        verdict = self._verdict(ref, samples, non_null)
        if (
            verdict.samples
            and verdict.hit_samples / verdict.samples >= PII_HIT_SHARE
            and verdict.entity
        ):
            detected = _ENTITY_CLASS.get(verdict.entity, FieldClass.CONTACT)
            return FieldDecision(
                detected,
                Policy.DROP,
                reason=f"pii:detector:{verdict.entity}",
                samples_seen=non_null,
            )

        # Rule 3: timestamps and dates.
        temporal = self._temporal(path, samples)
        if temporal is FieldClass.TIMESTAMP:
            return FieldDecision(
                FieldClass.TIMESTAMP, Policy.DROP, reason="timestamp", samples_seen=non_null
            )
        if temporal is FieldClass.DATE:
            return FieldDecision(
                FieldClass.DATE, Policy.TOKENIZE, DATE_FORMS, reason="date", samples_seen=non_null
            )

        # Rule 4: amounts.
        if samples:
            segments = set(name_segments(path))
            if segments & _AMOUNT_WORDS and _share(samples, _is_numeric) >= MAJORITY:
                return FieldDecision(
                    FieldClass.AMOUNT,
                    Policy.TOKENIZE,
                    AMOUNT_FORMS,
                    reason="amount:name",
                    samples_seen=non_null,
                )
            if _share(samples, is_amount_value) >= MAJORITY:
                return FieldDecision(
                    FieldClass.AMOUNT,
                    Policy.TOKENIZE,
                    AMOUNT_FORMS,
                    reason="amount:shape",
                    samples_seen=non_null,
                )

        # Rule 8: quarantine, before the statistical rules.
        if stats is None or non_null < settings.quarantine_samples:
            if self._identifier_shaped(stats, samples):
                return FieldDecision(
                    FieldClass.UNKNOWN,
                    Policy.TOKENIZE,
                    IDENTIFIER_FORMS,
                    reason="quarantine",
                    samples_seen=non_null,
                )
            return FieldDecision(
                FieldClass.UNKNOWN, Policy.DROP, reason="quarantine", samples_seen=non_null
            )

        # Rule 5: identifiers.
        distinct = stats.distinct_estimate
        high_cardinality = (
            distinct > settings.distinct_threshold or distinct > settings.distinct_ratio * non_null
        )
        if (
            high_cardinality
            and settings.identifier_min_len <= stats.length_mean <= settings.identifier_max_len
            and stats.whitespace_share < 0.5
            and stats.whitespace_value_rate < 0.5
            and _share(samples, lambda sample: _FLOAT.fullmatch(sample.strip()) is not None)
            < MAJORITY
        ):
            return FieldDecision(
                FieldClass.IDENTIFIER,
                Policy.TOKENIZE,
                IDENTIFIER_FORMS,
                reason="identifier",
                samples_seen=non_null,
            )

        # Rule 6: low-cardinality attributes, kept only when the samples passed the detector.
        if not high_cardinality:
            if verdict.samples == 0:
                return FieldDecision(
                    FieldClass.LOW_CARD_ATTRIBUTE,
                    Policy.DROP,
                    reason="low_card:no_samples",
                    samples_seen=non_null,
                )
            if verdict.hit_samples:
                return FieldDecision(
                    FieldClass.LOW_CARD_ATTRIBUTE,
                    Policy.DROP,
                    reason="low_card:pii_hits",
                    samples_seen=non_null,
                )
            if _share(samples, has_digit_run) >= DIGIT_RUN_SHARE:
                return FieldDecision(
                    FieldClass.IDENTIFIER,
                    Policy.TOKENIZE,
                    IDENTIFIER_FORMS,
                    reason="identifier:digit_run",
                    samples_seen=non_null,
                )
            return FieldDecision(
                FieldClass.LOW_CARD_ATTRIBUTE, Policy.KEEP, reason="low_card", samples_seen=non_null
            )

        # Rule 7: free text.
        if (
            stats.length_mean > FREE_TEXT_MIN_MEAN_LEN
            and stats.whitespace_value_rate > FREE_TEXT_MIN_SPACE_RATE
        ):
            return FieldDecision(
                FieldClass.FREE_TEXT, Policy.DROP, reason="free_text", samples_seen=non_null
            )

        return FieldDecision(
            FieldClass.UNKNOWN, Policy.DROP, reason="unclassified", samples_seen=non_null
        )

    def _pin_for(self, ref: str, path: str) -> FieldPolicyPin | None:
        exact = self._pins_exact.get(ref)
        if exact is not None:
            return exact.pin
        candidates = self._pins_wild.get(path)
        if not candidates:
            return None
        try:
            system_id, template_id, _path = split_field_ref(ref)
        except ValueError:
            return None
        for candidate in candidates:
            if candidate.matches(system_id, template_id):
                return candidate.pin
        return None

    @staticmethod
    def _pinned(pin: FieldPolicyPin, non_null: int) -> FieldDecision:
        field_class = FieldClass(pin.field_class)
        policy = Policy(pin.policy)
        forms: tuple[str, ...] = ()
        if policy is Policy.TOKENIZE:
            forms = tuple(pin.forms) if pin.forms else _default_forms(field_class)
        return FieldDecision(
            field_class, policy, forms, pinned=True, reason="pinned", samples_seen=non_null
        )

    def _verdict(self, ref: str, samples: Sequence[str], non_null: int) -> _Verdict:
        """The detector's view of up to :data:`PII_SAMPLE_LIMIT` samples, rerun only when the
        sample set grew or the non-null count doubled since the last run."""
        checked = samples[:PII_SAMPLE_LIMIT]
        previous = self._verdicts.get(ref)
        if (
            previous is not None
            and len(checked) <= previous.samples
            and non_null < 2 * max(previous.checked_at, 1)
        ):
            return previous
        hit_samples = 0
        entities: Counter[str] = Counter()
        threshold = _score_threshold(self._detector)
        per_sample: list[list[PiiHit]] = detect_many(self._detector, checked) if checked else []
        for hits in per_sample:
            strong = [hit for hit in hits if hit.score >= threshold]
            if strong:
                hit_samples += 1
                entities.update(hit.entity_type for hit in strong)
        entity = entities.most_common(1)[0][0] if entities else None
        verdict = _Verdict(non_null, len(checked), hit_samples, entity)
        self._verdicts[ref] = verdict
        return verdict

    @staticmethod
    def _temporal(path: str, samples: Sequence[str]) -> FieldClass | None:
        if not samples:
            return None
        kinds = Counter(temporal_kind(sample) for sample in samples)
        datetimes = kinds.get("datetime", 0)
        dates = kinds.get("date", 0)
        if (datetimes + dates) / len(samples) >= MAJORITY:
            return FieldClass.TIMESTAMP if datetimes >= dates else FieldClass.DATE
        if set(name_segments(path)) & _TIME_WORDS:
            epochs = _share(
                samples,
                lambda sample: (
                    _EPOCH_SECONDS.fullmatch(sample.strip()) is not None
                    or _EPOCH_MILLIS.fullmatch(sample.strip()) is not None
                ),
            )
            if epochs >= MAJORITY:
                return FieldClass.TIMESTAMP
        return None

    def _identifier_shaped(self, stats: FieldStats | None, samples: Sequence[str]) -> bool:
        """Quarantine rule: no whitespace, identifier characters only, length in range and a
        consistent shape (the top four shapes cover most values)."""
        if stats is None or not samples:
            return False
        settings = self._settings
        shaped = _share(
            samples,
            lambda sample: (
                settings.identifier_min_len <= len(sample) <= settings.identifier_max_len
                and _IDENTIFIER_SHAPED.fullmatch(sample) is not None
            ),
        )
        if shaped < 0.9:
            return False
        coverage = sum(share for _shape, share in stats.top_shapes(4))
        return coverage >= SHAPE_CONSISTENCY

    # -- summaries for the bundle writer -----------------------------------------------------

    def field_summary(self, ref: str) -> dict[str, Any]:
        """A ``BundleField``-shaped mapping for one field. ``sample_values`` is non-empty only
        for ``keep`` fields: at most five distinct, truncated, detector-checked values."""
        with self._lock:
            system_id, template_id, ref_path = split_field_ref(ref)
            path = self._paths.get(ref, ref_path)
            decision = self.decide(ref, path)
            stats = self._stats.get(ref)
            sample_values: list[str] = []
            if decision.policy is Policy.KEEP and stats is not None:
                clean: set[str] = set()
                for sample in stats.samples:
                    text = truncate_attribute(sample)
                    if (
                        text
                        and text not in clean
                        and self.keeps(ref, sample, pinned=decision.pinned)
                        and attribute_is_clean(text, self._detector)
                    ):
                        clean.add(text)
                sample_values = sorted(clean)[:MAX_SAMPLE_VALUES]
            top_shapes = stats.top_shapes(MAX_TOP_SHAPES) if stats is not None else []
            return {
                "field_ref": ref,
                "system_id": system_id,
                "template_id": template_id,
                "path": path,
                "field_class": decision.field_class.value,
                "policy": decision.policy.value,
                "pinned": decision.pinned,
                "reason": decision.reason,
                "count": stats.count if stats is not None else 0,
                "distinct_estimate": stats.distinct_estimate if stats is not None else 0,
                "null_rate": stats.null_rate if stats is not None else 0.0,
                "top_shapes": [{"shape": shape, "share": share} for shape, share in top_shapes],
                "forms": list(decision.forms),
                "sample_values": sample_values,
            }

    def bundle_fields(self) -> list[BundleField]:
        """``fields.json``: every observed field, sorted by ``field_ref``."""
        with self._lock:
            return [
                BundleField.model_validate(self.field_summary(ref)) for ref in sorted(self._paths)
            ]


def _score_threshold(detector: PiiDetector) -> float:
    """The configured threshold when the detector carries settings, else 0.5 (spec default)."""
    settings = getattr(detector, "settings", None)
    threshold = getattr(settings, "score_threshold", None)
    return float(threshold) if isinstance(threshold, int | float) else 0.5
