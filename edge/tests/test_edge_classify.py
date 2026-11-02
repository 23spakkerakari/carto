"""carto_edge.pipeline.classify: spec 8.3 rules 1 to 8, admin pins, caching, reclassification.

Every value is synthetic and modelled on simulator scenario A (spec 19). The detector is the
regex fallback or a stub so these tests stay fast; the real Presidio path is exercised once in
``test_edge_pii.py``.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Callable, Iterable, Sequence

import pytest
import structlog

from carto_edge.config import ClassifySettings, FieldPolicyPin
from carto_edge.pipeline.classify import (
    AMOUNT_FORMS,
    DATE_FORMS,
    IDENTIFIER_FORMS,
    PHONETIC_FORMS,
    RECHECK_EVERY,
    Classifier,
    has_digit_run,
    is_amount_value,
    is_secret_name,
    name_segments,
    pii_class_by_name,
    temporal_kind,
)
from carto_edge.pipeline.model import FieldClass, FieldDecision, Policy, field_ref
from carto_edge.pipeline.pii import PiiHit, RegexDetector
from carto_edge.pipeline.stats import FieldStatsStore
from carto_schema.bundle import BundleField

SYS = "sys_orders"
TPL = "tpl_0000000000ab"

FIRSTS = ["Maria", "Alex", "Priya", "Jonas", "Amara", "Luis", "Hannah", "Omar", "Mei", "Tariq"]
LASTS = ["Lopez", "Chen", "Patel", "Weber", "Okafor", "Garcia", "Schmidt", "Haddad", "Ng", "Rossi"]
STATUSES = ["CREATED", "RELEASED", "SHIPPED", "ERROR"]
WORDS = ["the", "quick", "order", "was", "released", "from", "warehouse", "after", "payment"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(i: int) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"sub": f"svc-{i}", "iat": 1_700_000_000 + i}).encode())
    return f"{header}.{claims}.{_b64url(bytes(range(i % 7, i % 7 + 32)))}"


def names(n: int) -> list[str]:
    return [f"{FIRSTS[i % 10]} {LASTS[(i // 10) % 10]}" for i in range(n)]


def emails(n: int) -> list[str]:
    return [f"user{i}@example.org" for i in range(n)]


def sentences(n: int) -> list[str]:
    return [
        " ".join(WORDS[(i + j) % len(WORDS)] for j in range(6 + i % 5)) + f" #{i}" for i in range(n)
    ]


def ints(n: int, start: int = 4471) -> list[str]:
    return [str(start + i) for i in range(n)]


def so_refs(n: int) -> list[str]:
    return [f"SO-{4471 + i:07d}" for i in range(n)]


def po_nums(n: int) -> list[str]:
    return [f"{88 + i // 1000:02d}-{210 + i % 1000:03d}" for i in range(n)]


def amounts(n: int) -> list[str]:
    return [f"{(i * 37) % 900 + 10}.{(i * 13) % 100:02d}" for i in range(n)]


def iso_dates(n: int) -> list[str]:
    return [f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}" for i in range(n)]


def iso_timestamps(n: int) -> list[str]:
    return [
        f"2026-10-{(i % 28) + 1:02d}T{i % 24:02d}:{i % 60:02d}:{(i * 7) % 60:02d}.412Z"
        for i in range(n)
    ]


class StubDetector:
    """Flags exactly the values a predicate accepts; counts calls."""

    def __init__(self, predicate: Callable[[str], bool], entity: str = "PERSON") -> None:
        self.predicate = predicate
        self.entity = entity
        self.calls = 0

    def detect(self, text: str) -> list[PiiHit]:
        self.calls += 1
        hits: list[PiiHit] = []
        offset = 0
        for line in text.split("\n"):
            if self.predicate(line):
                hits.append(PiiHit(self.entity, offset, offset + len(line), 0.85))
            offset += len(line) + 1
        return hits


def make(
    pins: Sequence[FieldPolicyPin] = (),
    detector: StubDetector | RegexDetector | None = None,
    **overrides: object,
) -> Classifier:
    settings = ClassifySettings(**overrides)  # type: ignore[arg-type]
    stats = FieldStatsStore.from_settings(settings)
    return Classifier(settings, pins, detector or RegexDetector(), stats, policy_version="v7")


def feed(classifier: Classifier, path: str, values: Iterable[str | None]) -> str:
    ref = field_ref(SYS, TPL, path)
    for value in values:
        classifier.observe(ref, path, value)
    return ref


def decide(classifier: Classifier, path: str, values: Iterable[str | None]) -> FieldDecision:
    ref = feed(classifier, path, values)
    return classifier.decide(ref, path)


# ---------------------------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------------------------


def test_name_segments_split_snake_camel_dots_and_digits() -> None:
    assert name_segments("cardholderName") == ["cardholder", "name"]
    assert name_segments("payload.auth.api_key") == ["payload", "auth", "api", "key"]
    assert name_segments("icd10_code") == ["icd", "10", "code"]
    assert name_segments("msg.param_0") == ["msg", "param", "0"]
    assert name_segments("username") == ["username"]
    assert name_segments("X-API-Key") == ["x", "api", "key"]


@pytest.mark.parametrize(
    "path",
    [
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apiKey",
        "x-api-key",
        "Authorization",
        "cookie",
        "private_key",
        "bearer",
        "credential",
        "credentials",
        "payload.auth.token",
        "db.password_hash",
        "client_secret",
    ],
)
def test_secret_names(path: str) -> None:
    assert is_secret_name(path)


@pytest.mark.parametrize("path", ["order_id", "token_count", "secret_ref", "status", "username"])
def test_non_secret_names(path: str) -> None:
    assert not is_secret_name(path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("customer_name", FieldClass.PERSON_NAME),
        ("cardholderName", FieldClass.PERSON_NAME),
        ("first_name", FieldClass.PERSON_NAME),
        ("lastName", FieldClass.PERSON_NAME),
        ("member_name", FieldClass.PERSON_NAME),
        ("patient_name", FieldClass.PERSON_NAME),
        ("name", FieldClass.PERSON_NAME),
        ("customer_email", FieldClass.CONTACT),
        ("email", FieldClass.CONTACT),
        ("phone_number", FieldClass.CONTACT),
        ("ship_to_address", FieldClass.CONTACT),
        ("street", FieldClass.CONTACT),
        ("zip", FieldClass.CONTACT),
        ("ssn", FieldClass.GOVERNMENT_ID),
        ("dob", FieldClass.GOVERNMENT_ID),
        ("birth_date", FieldClass.GOVERNMENT_ID),
        ("passport_no", FieldClass.GOVERNMENT_ID),
        ("driver_license", FieldClass.GOVERNMENT_ID),
        ("iban", FieldClass.FINANCIAL),
        ("card_number", FieldClass.FINANCIAL),
        ("diagnosis", FieldClass.HEALTH),
        ("icd10_code", FieldClass.HEALTH),
        ("npi", FieldClass.HEALTH),
        ("patient_id", FieldClass.HEALTH),
    ],
)
def test_pii_class_by_name(path: str, expected: FieldClass) -> None:
    assert pii_class_by_name(path) is expected


@pytest.mark.parametrize(
    "path",
    [
        "username",
        "hostname",
        "user_name",
        "service_name",
        "file_name",
        "fileName",
        "table_name",
        "order_id",
        "status",
        "ip_address",
        "mac_address",
        "msg.param_0",
        "warehouse_code",
    ],
)
def test_pii_class_by_name_ignores_technical_names(path: str) -> None:
    assert pii_class_by_name(path) is None


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("2026-10-06", "date"),
        ("2026/10/06", "date"),
        ("06/10/2026", "date"),
        ("10/06/2026", "date"),
        ("06-Oct-2026", "date"),
        ("Oct 6, 2026", "date"),
        ("2026-10-06T21:12:03.412Z", "datetime"),
        ("2026-10-06T21:12:03+02:00", "datetime"),
        ("2026-10-06 21:12:03", "datetime"),
        ("2026-10-06T21:12", "datetime"),
        ("20261006T211203Z", "datetime"),
        ("06/Oct/2026:21:12:03 +0000", "datetime"),
        ("Tue, 06 Oct 2026 21:12:03 +0000", "datetime"),
        ("Oct  6 21:12:03", "datetime"),
        ("21:12:03", "datetime"),
        ("4471", None),
        ("SO-0004471", None),
        ("2026-13-45", None),
        ("20261006", None),
        ("1728248123", None),
        ("129.99", None),
        ("", None),
    ],
)
def test_temporal_kind(value: str, kind: str | None) -> None:
    assert temporal_kind(value) == kind


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("129.99", True),
        ("-12.50", True),
        ("$1,299.00", True),
        ("129.99 USD", True),
        ("EUR 10.00", True),
        ("129.9", False),
        ("4471", False),
        ("1.2.3", False),
        ("12,50", False),
        ("abc", False),
    ],
)
def test_is_amount_value(value: str, expected: bool) -> None:
    assert is_amount_value(value) is expected


# ---------------------------------------------------------------------------------------------
# Rule 1: secret_like
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["password", "api_key", "payload.auth.token", "Authorization"])
def test_secret_names_are_dropped_whatever_the_values(path: str) -> None:
    decision = decide(make(), path, ["CREATED"] * 300)
    assert decision.field_class is FieldClass.SECRET_LIKE
    assert decision.policy is Policy.DROP
    assert decision.forms == ()
    assert decision.reason == "secret_like:name"


def test_secret_values_are_dropped_under_a_benign_name() -> None:
    values = ["note " + _jwt(i) for i in range(5)] + ["plain note"] * 300
    decision = decide(make(), "note", values)
    assert decision.field_class is FieldClass.SECRET_LIKE
    assert decision.policy is Policy.DROP
    assert decision.reason == "secret_like:value"


def test_pins_cannot_keep_a_secret() -> None:
    pins = [
        FieldPolicyPin(field=f"{SYS}/*/api_key", field_class="identifier", policy="keep"),
        FieldPolicyPin(field=f"{SYS}/*/note", field_class="low_card_attribute", policy="keep"),
    ]
    classifier = make(pins)
    by_name = decide(classifier, "api_key", ["abc"] * 300)
    assert (by_name.field_class, by_name.policy, by_name.pinned) == (
        FieldClass.SECRET_LIKE,
        Policy.DROP,
        False,
    )
    by_value = decide(classifier, "note", [_jwt(1)] * 300)
    assert (by_value.field_class, by_value.policy, by_value.pinned) == (
        FieldClass.SECRET_LIKE,
        Policy.DROP,
        False,
    )


def test_uuids_are_identifiers_not_secrets() -> None:
    values = [str(uuid.UUID(int=i * 7919)) for i in range(1_500)]
    decision = decide(make(), "request_id", values)
    assert decision.field_class is FieldClass.IDENTIFIER
    assert decision.policy is Policy.TOKENIZE


# ---------------------------------------------------------------------------------------------
# Rule 2: PII
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "values", "expected"),
    [
        ("customer_name", names(300), FieldClass.PERSON_NAME),
        ("cardholderName", names(300), FieldClass.PERSON_NAME),
        ("customer_email", emails(300), FieldClass.CONTACT),
        ("ship_to_address", [f"{i} Main St" for i in range(300)], FieldClass.CONTACT),
        ("ssn", ["123-45-6789"] * 300, FieldClass.GOVERNMENT_ID),
        ("iban", ["GB82WEST12345698765432"] * 300, FieldClass.FINANCIAL),
        ("diagnosis", ["J45.909"] * 300, FieldClass.HEALTH),
    ],
)
def test_scenario_a_pii_fields_are_dropped(
    path: str, values: list[str], expected: FieldClass
) -> None:
    decision = decide(make(), path, values)
    assert decision.field_class is expected
    assert decision.policy is Policy.DROP
    assert decision.forms == ()
    assert decision.reason == "pii:name"


def test_pii_by_name_applies_even_in_quarantine() -> None:
    decision = decide(make(), "customer_name", names(5))
    assert decision.field_class is FieldClass.PERSON_NAME
    assert decision.policy is Policy.DROP


def test_pii_by_detector_on_unnamed_field() -> None:
    detector = StubDetector(lambda line: line in set(names(300)))
    decision = decide(make(detector=detector), "msg.param_0", names(300))
    assert decision.field_class is FieldClass.PERSON_NAME
    assert decision.policy is Policy.DROP
    assert decision.reason == "pii:detector:PERSON"
    assert detector.calls >= 1


def test_pii_by_detector_needs_thirty_percent_of_samples() -> None:
    flagged = set(names(2_000))
    detector = StubDetector(lambda line: line in flagged)
    # One flagged value in ten: below the 30% share, so the field is an identifier.
    values = [names(2_000)[i] if i % 10 == 0 else f"SO-{i:07d}" for i in range(2_000)]
    decision = decide(make(detector=detector), "msg.param_0", values)
    assert decision.field_class is FieldClass.IDENTIFIER


@pytest.mark.parametrize(
    "entity_class",
    [
        ("EMAIL_ADDRESS", FieldClass.CONTACT),
        ("PHONE_NUMBER", FieldClass.CONTACT),
        ("LOCATION", FieldClass.CONTACT),
        ("US_SSN", FieldClass.GOVERNMENT_ID),
        ("US_PASSPORT", FieldClass.GOVERNMENT_ID),
        ("US_DRIVER_LICENSE", FieldClass.GOVERNMENT_ID),
        ("CREDIT_CARD", FieldClass.FINANCIAL),
        ("IBAN_CODE", FieldClass.FINANCIAL),
        ("MEDICAL_LICENSE", FieldClass.HEALTH),
    ],
)
def test_detector_entities_map_to_classes(entity_class: tuple[str, FieldClass]) -> None:
    entity, expected = entity_class
    detector = StubDetector(lambda _line: True, entity=entity)
    decision = decide(make(detector=detector), "msg.param_1", so_refs(300))
    assert decision.field_class is expected
    assert decision.policy is Policy.DROP


def test_person_name_pinned_with_phonetic_is_tokenized_per_token() -> None:
    pins = [
        FieldPolicyPin(
            field=f"{SYS}/*/customer_name",
            field_class="person_name",
            policy="tokenize",
            forms=["phonetic"],
        )
    ]
    decision = decide(make(pins), "customer_name", names(300))
    assert decision.field_class is FieldClass.PERSON_NAME
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == PHONETIC_FORMS == ("phonetic",)
    assert decision.pinned is True
    assert decision.reason == "pinned"


# ---------------------------------------------------------------------------------------------
# Rules 3 and 4: timestamp, date, amount
# ---------------------------------------------------------------------------------------------


def test_timestamps_are_dropped() -> None:
    decision = decide(make(), "ts", iso_timestamps(300))
    assert decision.field_class is FieldClass.TIMESTAMP
    assert decision.policy is Policy.DROP
    assert decision.reason == "timestamp"


def test_epoch_timestamps_need_a_name_hint() -> None:
    epochs = [str(1_728_248_123 + i * 60) for i in range(300)]
    hinted = decide(make(), "created_at", epochs)
    assert hinted.field_class is FieldClass.TIMESTAMP
    unhinted = decide(make(), "msg.param_2", epochs)
    assert unhinted.field_class is FieldClass.IDENTIFIER


def test_dates_are_tokenized_as_date_form() -> None:
    decision = decide(make(), "order_date", iso_dates(300))
    assert decision.field_class is FieldClass.DATE
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == DATE_FORMS == ("date",)


def test_date_detection_in_quarantine_is_safe() -> None:
    decision = decide(make(), "msg.param_3", iso_dates(10))
    assert decision.field_class is FieldClass.DATE
    assert decision.policy is Policy.TOKENIZE


@pytest.mark.parametrize("path", ["total", "amount", "order_total"])
def test_amounts_are_tokenized_as_amount_form(path: str) -> None:
    decision = decide(make(), path, amounts(300))
    assert decision.field_class is FieldClass.AMOUNT
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == AMOUNT_FORMS == ("amount",)


def test_amount_by_shape_without_a_name_hint() -> None:
    decision = decide(make(), "msg.param_4", amounts(300))
    assert decision.field_class is FieldClass.AMOUNT
    assert decision.reason == "amount:shape"


def test_amount_name_hint_with_integer_values() -> None:
    decision = decide(make(), "order_total", ints(300, start=120))
    assert decision.field_class is FieldClass.AMOUNT
    assert decision.reason == "amount:name"


def test_amount_name_hint_needs_numeric_values() -> None:
    decision = decide(
        make(), "price_tier", [["GOLD", "SILVER", "BRONZE"][i % 3] for i in range(300)]
    )
    assert decision.field_class is FieldClass.LOW_CARD_ATTRIBUTE
    assert decision.policy is Policy.KEEP


# ---------------------------------------------------------------------------------------------
# Rules 5 to 7: identifier, low-cardinality attribute, free text
# ---------------------------------------------------------------------------------------------


def test_order_id_is_tokenized_with_identifier_forms() -> None:
    decision = decide(make(), "order_id", ints(9_600))
    assert decision.field_class is FieldClass.IDENTIFIER
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == IDENTIFIER_FORMS == ("raw", "norm", "alnum", "digits")
    assert decision.reason == "identifier"
    assert decision.samples_seen == 9_600


@pytest.mark.parametrize(
    ("path", "values"),
    [
        ("po_num", po_nums(3_000)),
        ("order_ref", so_refs(3_000)),
        ("cart_id", [f"c-{88213 + i}" for i in range(3_000)]),
        ("merchant_ref", [f"X9-{i:04d}" for i in range(3_000)]),
    ],
)
def test_scenario_a_identifiers(path: str, values: list[str]) -> None:
    decision = decide(make(), path, values)
    assert decision.field_class is FieldClass.IDENTIFIER
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == IDENTIFIER_FORMS


def test_identifier_by_ratio_below_absolute_threshold() -> None:
    # 400 distinct of 1,000 observations: above 20%, below 1,000 -> identifier.
    decision = decide(make(), "batch_ref", [f"B{i % 400:05d}" for i in range(1_000)])
    assert decision.field_class is FieldClass.IDENTIFIER
    # 150 distinct of 1,000: 15%, below both -> low-cardinality attribute.
    decision = decide(make(), "route_code", [f"R{i % 150:03d}" for i in range(1_000)])
    assert decision.field_class is FieldClass.LOW_CARD_ATTRIBUTE


def test_identifier_above_absolute_threshold_even_at_low_ratio() -> None:
    values = [f"K{i % 1_500:05d}" for i in range(20_000)]  # 7.5% of count, 1,500 distinct
    decision = decide(make(), "sku_ref", values)
    assert decision.field_class is FieldClass.IDENTIFIER


@pytest.mark.parametrize(
    ("path", "values"),
    [
        ("status", [STATUSES[i % 4] for i in range(1_000)]),
        ("warehouse_code", [f"DC-{(i % 5) + 1:02d}" for i in range(1_000)]),
        ("region", [["us-east", "us-west", "eu-central"][i % 3] for i in range(1_000)]),
        ("channel", [["web", "mobile", "pos"][i % 3] for i in range(1_000)]),
        ("carrier", [["UPS", "DHL", "FedEx"][i % 3] for i in range(1_000)]),
        ("httpStatus", [["200", "503"][i % 2] for i in range(1_000)]),
        ("currency", ["USD"] * 1_000),
    ],
)
def test_scenario_a_attributes_are_kept(path: str, values: list[str]) -> None:
    decision = decide(make(), path, values)
    assert decision.field_class is FieldClass.LOW_CARD_ATTRIBUTE
    assert decision.policy is Policy.KEEP
    assert decision.forms == ()
    assert decision.reason == "low_card"


# ADR 0029: a run of four or more digits marks an identifier whatever the cardinality.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("MAN-20260923-01", True),
        ("SHIP_20260923_2112.csv", True),
        ("2026", True),
        ("DC-03", False),
        ("200", False),
        ("v12", False),
        ("us-east", False),
        ("1.2.3", False),
        ("٣٤٥٦", False),  # non-ASCII digits are not a run
    ],
)
def test_has_digit_run(value: str, expected: bool) -> None:
    assert has_digit_run(value) is expected


@pytest.mark.parametrize(
    ("path", "values"),
    [
        ("manifest_id", [f"MAN-202609{20 + i % 4:02d}-01" for i in range(1_000)]),
        ("file", [f"SHIP_202609{20 + i % 4:02d}_2112.csv" for i in range(1_000)]),
        ("store", [["1042", "1043", "DC-03"][i % 3] for i in range(1_000)]),
    ],
)
def test_low_card_field_with_digit_runs_is_a_tokenized_identifier(
    path: str, values: list[str]
) -> None:
    decision = decide(make(), path, values)
    assert decision.field_class is FieldClass.IDENTIFIER
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == IDENTIFIER_FORMS
    assert decision.reason == "identifier:digit_run"


def test_kept_field_does_not_keep_a_value_with_a_digit_run() -> None:
    classifier = make()
    values = [["CREATED", "RELEASED", "SHIPPED", "HOLD-20260923"][i % 4] for i in range(1_000)]
    ref = feed(classifier, "status", values)
    decision = classifier.decide(ref, "status")
    assert decision.policy is Policy.KEEP
    assert classifier.keeps(ref, "CREATED")
    assert not classifier.keeps(ref, "HOLD-20260923")
    summary = classifier.field_summary(ref)
    assert "HOLD-20260923" not in summary["sample_values"]
    assert "CREATED" in summary["sample_values"]


def test_pinned_keep_field_keeps_digit_runs() -> None:
    pins = [FieldPolicyPin(field=f"{SYS}/*/year", field_class="low_card_attribute", policy="keep")]
    classifier = make(pins=pins)
    ref = feed(classifier, "year", [["2025", "2026"][i % 2] for i in range(1_000)])
    decision = classifier.decide(ref, "year")
    assert decision.policy is Policy.KEEP
    assert decision.pinned
    assert classifier.keeps(ref, "2026", pinned=True)
    assert classifier.field_summary(ref)["sample_values"] == ["2025", "2026"]


def test_unpinned_year_field_is_tokenized() -> None:
    decision = decide(make(), "year", [["2025", "2026"][i % 2] for i in range(1_000)])
    assert decision.policy is Policy.TOKENIZE
    assert decision.reason == "identifier:digit_run"


def test_low_card_with_detector_hits_is_not_kept() -> None:
    detector = StubDetector(lambda line: line == "Maria Lopez")
    values = [["CREATED", "RELEASED", "Maria Lopez"][i % 3] for i in range(900)]
    decision = decide(make(detector=detector), "note", values)
    assert decision.policy is not Policy.KEEP
    assert decision.field_class in {FieldClass.LOW_CARD_ATTRIBUTE, FieldClass.PERSON_NAME}


def test_free_text_is_dropped() -> None:
    decision = decide(make(), "message", sentences(2_000))
    assert decision.field_class is FieldClass.FREE_TEXT
    assert decision.policy is Policy.DROP
    assert decision.reason == "free_text"


def test_high_cardinality_floats_are_unknown_and_dropped() -> None:
    decision = decide(make(), "latency", [f"0.{i:06d}" for i in range(3_000)])
    assert decision.field_class is FieldClass.UNKNOWN
    assert decision.policy is Policy.DROP
    assert decision.reason == "unclassified"


def test_mostly_whitespace_values_are_not_identifiers() -> None:
    values = [" " * 40 + str(i) for i in range(3_000)]
    decision = decide(make(), "padded", values)
    assert decision.field_class is not FieldClass.IDENTIFIER
    assert decision.policy is not Policy.KEEP


# ---------------------------------------------------------------------------------------------
# Rule 8: quarantine
# ---------------------------------------------------------------------------------------------


def test_quarantined_identifier_shaped_field_is_tokenized() -> None:
    decision = decide(make(), "po_num", po_nums(50))
    assert decision.field_class is FieldClass.UNKNOWN
    assert decision.policy is Policy.TOKENIZE
    assert decision.forms == IDENTIFIER_FORMS
    assert decision.reason == "quarantine"
    assert decision.samples_seen == 50


def test_quarantined_free_text_shaped_field_is_dropped() -> None:
    decision = decide(make(), "message", sentences(50))
    assert decision.field_class is FieldClass.UNKNOWN
    assert decision.policy is Policy.DROP
    assert decision.forms == ()
    assert decision.reason == "quarantine"


def test_quarantined_emails_are_not_tokenized_by_shape() -> None:
    detector = StubDetector(lambda _line: False)  # a detector that misses everything
    decision = decide(make(detector=detector), "msg.param_5", emails(50))
    assert decision.policy is Policy.DROP


def test_never_observed_field_is_quarantined_and_dropped() -> None:
    classifier = make()
    decision = classifier.decide(field_ref(SYS, TPL, "ghost"), "ghost")
    assert decision.field_class is FieldClass.UNKNOWN
    assert decision.policy is Policy.DROP
    assert decision.samples_seen == 0
    assert decision.reason == "quarantine"


def test_quarantine_threshold_is_configurable() -> None:
    decision = decide(make(quarantine_samples=20), "po_num", po_nums(50))
    assert decision.field_class is FieldClass.IDENTIFIER


# ---------------------------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------------------------


def test_wildcard_pin_keeps_an_identifier_in_clear() -> None:
    pins = [FieldPolicyPin(field=f"{SYS}/*/order_id", field_class="identifier", policy="keep")]
    decision = decide(make(pins), "order_id", ints(2_000))
    assert decision == FieldDecision(
        FieldClass.IDENTIFIER, Policy.KEEP, (), pinned=True, reason="pinned", samples_seen=2_000
    )


def test_exact_pin_only_matches_its_template() -> None:
    pins = [
        FieldPolicyPin(
            field=field_ref(SYS, TPL, "status"), field_class="identifier", policy="tokenize"
        )
    ]
    classifier = make(pins)
    pinned = decide(classifier, "status", [STATUSES[i % 4] for i in range(1_000)])
    assert pinned.pinned is True
    assert pinned.policy is Policy.TOKENIZE
    assert pinned.forms == IDENTIFIER_FORMS
    other_ref = field_ref(SYS, "tpl_0000000000cd", "status")
    for value in [STATUSES[i % 4] for i in range(1_000)]:
        classifier.observe(other_ref, "status", value)
    assert classifier.decide(other_ref, "status").pinned is False


def test_pin_forms_default_by_class() -> None:
    pins = [
        FieldPolicyPin(field=f"{SYS}/*/order_date", field_class="date", policy="tokenize"),
        FieldPolicyPin(field=f"{SYS}/*/total", field_class="amount", policy="tokenize"),
        FieldPolicyPin(
            field=f"{SYS}/*/customer_name", field_class="person_name", policy="tokenize"
        ),
        FieldPolicyPin(field=f"{SYS}/*/ssn", field_class="government_id", policy="tokenize"),
        FieldPolicyPin(field=f"{SYS}/*/note", field_class="free_text", policy="drop"),
    ]
    classifier = make(pins)
    assert decide(classifier, "order_date", iso_dates(300)).forms == DATE_FORMS
    assert decide(classifier, "total", amounts(300)).forms == AMOUNT_FORMS
    assert decide(classifier, "customer_name", names(300)).forms == PHONETIC_FORMS
    assert decide(classifier, "ssn", ["123-45-6789"] * 300).forms == IDENTIFIER_FORMS
    dropped = decide(classifier, "note", sentences(300))
    assert (dropped.policy, dropped.forms) == (Policy.DROP, ())


def test_pin_with_explicit_forms_is_honoured_and_validated() -> None:
    pins = [
        FieldPolicyPin(
            field=f"{SYS}/*/order_id",
            field_class="identifier",
            policy="tokenize",
            forms=["raw", "digits.0"],
        )
    ]
    assert decide(make(pins), "order_id", ints(2_000)).forms == ("raw", "digits.0")
    bad = [
        FieldPolicyPin(
            field=f"{SYS}/*/order_id",
            field_class="identifier",
            policy="tokenize",
            forms=["soundex"],
        )
    ]
    with pytest.raises(ValueError, match="soundex"):
        make(bad)
    malformed = [FieldPolicyPin(field="not-a-ref", field_class="identifier", policy="drop")]
    with pytest.raises(ValueError, match="field_ref"):
        make(malformed)


# ---------------------------------------------------------------------------------------------
# Caching, invalidation, reclassification
# ---------------------------------------------------------------------------------------------


def test_reclassification_after_quarantine_is_logged_without_values() -> None:
    classifier = make()
    ref = feed(classifier, "po_num", po_nums(50))
    first = classifier.decide(ref, "po_num")
    assert first.field_class is FieldClass.UNKNOWN
    feed(classifier, "po_num", po_nums(3_000))
    with structlog.testing.capture_logs() as logs:
        second = classifier.decide(ref, "po_num")
    assert second.field_class is FieldClass.IDENTIFIER
    events = [entry for entry in logs if entry["event"] == "field_reclassified"]
    assert len(events) == 1
    entry = events[0]
    assert entry["log_level"] == "info"
    assert entry["field_ref"] == ref
    assert entry["old_class"] == "unknown"
    assert entry["new_class"] == "identifier"
    rendered = json.dumps(entry, default=str)
    assert not any(value in rendered for value in po_nums(3_000))


def test_decision_is_cached_between_observations() -> None:
    detector = StubDetector(lambda _line: False)
    classifier = make(detector=detector)
    ref = feed(classifier, "status", [STATUSES[i % 4] for i in range(1_000)])
    first = classifier.decide(ref, "status")
    calls = detector.calls
    for _ in range(10):
        classifier.observe(ref, "status", "CREATED")
        assert classifier.decide(ref, "status") is first
    assert detector.calls == calls


def test_decision_is_recomputed_every_thousand_observations() -> None:
    classifier = make()
    ref = feed(classifier, "code", [f"C{i % 50:03d}" for i in range(1_000)])
    assert classifier.decide(ref, "code").field_class is FieldClass.LOW_CARD_ATTRIBUTE
    # The field turns high-cardinality; the cached decision survives until RECHECK_EVERY.
    feed(classifier, "code", [f"C{i:06d}" for i in range(RECHECK_EVERY - 1)])
    assert classifier.decide(ref, "code").field_class is FieldClass.LOW_CARD_ATTRIBUTE
    feed(classifier, "code", [f"C{i:06d}" for i in range(RECHECK_EVERY, RECHECK_EVERY + 2_000)])
    with structlog.testing.capture_logs() as logs:
        assert classifier.decide(ref, "code").field_class is FieldClass.IDENTIFIER
    assert any(entry["event"] == "field_reclassified" for entry in logs)


def test_detector_runs_are_amortized() -> None:
    detector = StubDetector(lambda _line: False)
    classifier = make(detector=detector)
    ref = feed(classifier, "msg.param_6", so_refs(1_000))
    classifier.decide(ref, "msg.param_6")
    after_first = detector.calls
    assert after_first >= 1
    for _ in range(20):
        feed(classifier, "msg.param_6", so_refs(1_000))
        classifier.decide(ref, "msg.param_6")
    # 20 re-decisions, but the detector ran only a handful of times (log2 growth).
    assert detector.calls - after_first <= 6


# ---------------------------------------------------------------------------------------------
# Summaries for the bundle writer
# ---------------------------------------------------------------------------------------------


def test_decisions_and_policy_version() -> None:
    classifier = make()
    assert classifier.policy_version == "v7"
    ref = feed(classifier, "status", STATUSES * 100)
    assert classifier.decisions() == {}
    decision = classifier.decide(ref, "status")
    assert classifier.decisions() == {ref: decision}


def test_field_summary_exposes_samples_only_for_kept_fields() -> None:
    classifier = make()
    kept = feed(classifier, "status", [STATUSES[i % 4] for i in range(1_000)])
    tokenized = feed(classifier, "order_id", ints(3_000))
    dropped = feed(classifier, "customer_name", names(300))
    quarantined = feed(classifier, "po_num", po_nums(20))
    summaries = {
        ref: classifier.field_summary(ref) for ref in (kept, tokenized, dropped, quarantined)
    }
    assert set(summaries[kept]["sample_values"]) <= set(STATUSES)
    assert 1 <= len(summaries[kept]["sample_values"]) <= 5
    assert summaries[kept]["policy"] == "keep"
    for ref in (tokenized, dropped, quarantined):
        assert summaries[ref]["sample_values"] == []
    assert summaries[tokenized]["forms"] == list(IDENTIFIER_FORMS)
    assert summaries[tokenized]["count"] == 3_000
    assert abs(summaries[tokenized]["distinct_estimate"] - 3_000) <= 150
    assert summaries[tokenized]["top_shapes"][0]["shape"] == "9999"
    assert summaries[dropped]["field_class"] == "person_name"
    assert summaries[quarantined]["reason"] == "quarantine"
    text = json.dumps(summaries)
    assert not any(value in text for value in names(300))
    assert not any(value in text for value in ints(3_000))


def test_bundle_fields_validate_against_the_contract() -> None:
    classifier = make()
    feed(classifier, "status", [STATUSES[i % 4] for i in range(1_000)])
    feed(classifier, "order_id", ints(3_000))
    feed(classifier, "customer_name", names(300))
    fields = classifier.bundle_fields()
    assert [field.path for field in fields] == ["customer_name", "order_id", "status"]
    for field in fields:
        assert isinstance(field, BundleField)
        assert field.system_id == SYS
        assert field.template_id == TPL
        assert field.field_ref == field_ref(SYS, TPL, field.path)
        assert (field.policy == "keep") == bool(field.sample_values)
        BundleField.model_validate(field.model_dump(mode="json"))


def test_kept_sample_values_are_truncated_and_clean() -> None:
    detector = StubDetector(lambda line: line.startswith("Maria"))
    classifier = make(detector=detector)
    long_value = "L" * 300
    values = [long_value, "RELEASED", "Maria Lopez"]
    # Two low-cardinality clean values plus one tainted value: the detector hit makes the
    # field unkept; without the tainted value the long one is truncated for the summary.
    ref = feed(classifier, "note", [values[i % 2] for i in range(600)])
    summary = classifier.field_summary(ref)
    assert summary["policy"] == "keep"
    assert all(len(value) <= 256 for value in summary["sample_values"])
    assert "RELEASED" in summary["sample_values"]


# ---------------------------------------------------------------------------------------------
# Ratchet (ADR 0068): cardinality and detector hits never fall back to keep
# ---------------------------------------------------------------------------------------------


def _alphabetic(n: int) -> list[str]:
    """``n`` distinct 11-character alphabetic values (no digit run, no whitespace)."""
    return [
        f"customer{chr(97 + i % 26)}{chr(97 + (i // 26) % 26)}{chr(97 + (i // 676) % 26)}"
        for i in range(n)
    ]


def test_flooded_identifier_stays_tokenized() -> None:
    """The review's flood: 900 distinct values over 1,000 rows make an identifier; 4,000 rows
    of ``ok`` then drown the distinct ratio and fill the reservoir. The field stays tokenized."""
    classifier = make()
    distinct = _alphabetic(900)
    ref = feed(classifier, "note", [distinct[i % 900] for i in range(1_000)])
    first = classifier.decide(ref, "note")
    assert first.field_class is FieldClass.IDENTIFIER
    assert first.reason == "identifier"
    feed(classifier, "note", ["ok"] * 4_000)
    stats = classifier._stats.get(ref)  # the live figures the ratchet overrides
    assert stats is not None
    settings = classifier.settings
    assert stats.distinct_estimate <= settings.distinct_ratio * stats.non_null_count
    assert stats.distinct_estimate <= settings.distinct_threshold
    assert stats.value_count("ok") >= 3  # rule 6 would keep it without the ratchet
    assert stats.high_cardinality_seen
    with structlog.testing.capture_logs() as logs:
        after = classifier.decide(ref, "note")
    assert after.field_class is FieldClass.IDENTIFIER
    assert after.policy is Policy.TOKENIZE
    assert after.forms == IDENTIFIER_FORMS
    assert after.reason == "identifier:ratchet"
    assert not any(entry["event"] == "field_reclassified" for entry in logs)


def test_genuine_enum_still_becomes_keep() -> None:
    values = [f"STATE_{chr(65 + i % 20)}" for i in range(2_000)]
    classifier = make()
    ref = feed(classifier, "state", values)
    decision = classifier.decide(ref, "state")
    assert decision.field_class is FieldClass.LOW_CARD_ATTRIBUTE
    assert decision.policy is Policy.KEEP
    assert decision.reason == "low_card"
    stats = classifier._stats.get(ref)
    assert stats is not None
    assert not stats.high_cardinality_seen
    assert not stats.detector_hits_seen
    feed(classifier, "state", values)
    assert classifier.decide(ref, "state").policy is Policy.KEEP


def test_detector_hits_ratchet_survives_a_restart_and_a_flood() -> None:
    """A name seen early sets ``detector_hits_seen``; after a restart the reservoir starts
    empty and a flood of ``ok`` fills it, yet rule 6 still drops the field."""
    detector = StubDetector(lambda line: line == "Maria Lopez")
    settings = ClassifySettings()
    store = FieldStatsStore.from_settings(settings)
    classifier = Classifier(settings, (), detector, store)
    ref = feed(classifier, "note", [["ok", "pending", "Maria Lopez"][i % 3] for i in range(300)])
    assert classifier.decide(ref, "note").policy is Policy.DROP
    stats = store.get(ref)
    assert stats is not None
    assert stats.detector_hits_seen
    snapshot = store.snapshot()
    restarted = FieldStatsStore.from_settings(settings)
    restarted.restore(snapshot)
    again = Classifier(settings, (), detector, restarted)
    feed(again, "note", ["ok", "pending"] * 500)
    restored = restarted.get(ref)
    assert restored is not None
    assert "Maria Lopez" not in restored.samples  # the reservoir holds only the flood
    decision = again.decide(ref, "note")
    assert decision.field_class is FieldClass.LOW_CARD_ATTRIBUTE
    assert decision.policy is Policy.DROP
    assert decision.reason == "low_card:pii_hits"


def test_ratchet_flags_persist_across_a_restart_for_identifiers() -> None:
    settings = ClassifySettings()
    store = FieldStatsStore.from_settings(settings)
    classifier = Classifier(settings, (), RegexDetector(), store)
    ref = feed(classifier, "ref", _alphabetic(900))
    assert classifier.decide(ref, "ref").field_class is FieldClass.IDENTIFIER
    restarted = FieldStatsStore.from_settings(settings)
    restarted.restore(store.snapshot())
    again = Classifier(settings, (), RegexDetector(), restarted)
    feed(again, "ref", ["ok"] * 4_000)
    decision = again.decide(ref, "ref")
    assert decision.policy is Policy.TOKENIZE
    assert decision.reason == "identifier:ratchet"


def test_pin_to_keep_still_wins_over_the_ratchet() -> None:
    pin = FieldPolicyPin(
        field=f"{SYS}/*/code", field_class="low_card_attribute", policy="keep", reason="admin"
    )
    classifier = make(pins=[pin])
    ref = feed(classifier, "code", _alphabetic(900))
    assert classifier.decide(ref, "code").policy is Policy.KEEP
    feed(classifier, "code", ["ok"] * 4_000)
    decision = classifier.decide(ref, "code")
    assert decision.policy is Policy.KEEP
    assert decision.pinned
