"""carto_edge.pipeline.tokenize: identifiers per form and key version, the vault entries, the
event cap and the query tokenizer (spec 7.1, 8.4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from carto_common.crypto import Keyring, TokenKey, generate_key, token
from carto_edge.pipeline.model import FieldClass, VaultEntry
from carto_edge.pipeline.tokenize import FieldInput, TokenizedFields, Tokenizer
from carto_schema.event import MAX_IDENTIFIERS_PER_EVENT, CanonicalEvent, Identifier
from carto_schema.forms import TOKEN_PATTERN, shape

KEY_V1 = generate_key()
KEY_V2 = generate_key()
SINGLE = Keyring(active=TokenKey(1, KEY_V1))
DUAL = Keyring(active=TokenKey(2, KEY_V2), previous=(TokenKey(1, KEY_V1),))
EXPIRES = datetime(2026, 11, 5, tzinfo=UTC)
ID_FORMS = ("raw", "norm", "alnum", "digits")


def test_identifiers_for_spec_example_single_key() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    identifiers, entries = tokenizer.identifiers_for(
        "order_ref", FieldClass.IDENTIFIER, ID_FORMS, "SO-0004471", expires_at=EXPIRES
    )
    assert [(i.field, i.form, i.shape, i.len) for i in identifiers] == [
        ("order_ref", "raw", "AA-9999999", 10),
        ("order_ref", "norm", "AA-9999999", 10),
        ("order_ref", "alnum", "AA9999999", 9),
        ("order_ref", "digits.0", "9999", 4),
    ]
    assert all(isinstance(i, Identifier) for i in identifiers)
    assert all(TOKEN_PATTERN.fullmatch(i.token) and i.token.startswith("t1.") for i in identifiers)
    assert identifiers[0].token == token(KEY_V1, 1, "id", "SO-0004471")
    assert identifiers[3].token == token(KEY_V1, 1, "id", "4471")
    assert entries == [VaultEntry(identifiers[0].token, "SO-0004471", EXPIRES)]


def test_raw_value_and_digit_run_share_the_id_domain() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    from_raw, _ = tokenizer.identifiers_for(
        "po_num", FieldClass.IDENTIFIER, ("raw",), "4471", expires_at=EXPIRES
    )
    from_digits, _ = tokenizer.identifiers_for(
        "order_ref", FieldClass.IDENTIFIER, ("digits",), "SO-0004471", expires_at=EXPIRES
    )
    assert from_raw[0].form == "raw"
    assert from_digits[0].form == "digits.0"
    assert from_raw[0].token == from_digits[0].token


def test_domains_separate_date_amount_and_phonetic_tokens() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    as_id, _ = tokenizer.identifiers_for(
        "f", FieldClass.IDENTIFIER, ("raw",), "2026-10-06", expires_at=EXPIRES
    )
    as_date, _ = tokenizer.identifiers_for(
        "f", FieldClass.DATE, ("date",), "2026-10-06", expires_at=EXPIRES
    )
    assert as_id[0].token != as_date[0].token
    assert as_date[0].token == token(KEY_V1, 1, "date", "2026-10-06")
    as_amount, _ = tokenizer.identifiers_for(
        "amt", FieldClass.AMOUNT, ("amount",), "1299.99", expires_at=EXPIRES
    )
    assert as_amount[0].token == token(KEY_V1, 1, "amt", "129999")
    as_name, _ = tokenizer.identifiers_for(
        "name", FieldClass.PERSON_NAME, ("phonetic",), "Jane Smith", expires_at=EXPIRES
    )
    assert [i.form for i in as_name] == ["phonetic.0", "phonetic.1"]
    assert as_name[0].token == token(KEY_V1, 1, "ph", "JN")
    assert as_name[0].shape == shape("JN") and as_name[0].len == 2


def test_dual_tokenization_yields_one_entry_per_live_key_per_field_form() -> None:
    tokenizer = Tokenizer(DUAL, "default")
    identifiers, entries = tokenizer.identifiers_for(
        "order_ref", FieldClass.IDENTIFIER, ID_FORMS, "SO-0004471", expires_at=EXPIRES
    )
    assert len(identifiers) == 8
    # active key first, then the previous key, each in form order
    assert [i.token.partition(".")[0] for i in identifiers] == ["t2"] * 4 + ["t1"] * 4
    assert [i.form for i in identifiers[:4]] == [i.form for i in identifiers[4:]]
    triples = {(i.field, i.form, i.token.partition(".")[0]) for i in identifiers}
    assert len(triples) == 8
    assert identifiers[4].token == token(KEY_V1, 1, "id", "SO-0004471")
    assert [(e.token, e.raw_value) for e in entries] == [
        (identifiers[0].token, "SO-0004471"),
        (identifiers[4].token, "SO-0004471"),
    ]


def test_no_vault_entry_without_a_raw_form() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    identifiers, entries = tokenizer.identifiers_for(
        "order_ref", FieldClass.IDENTIFIER, ("alnum", "digits"), "SO-0004471", expires_at=EXPIRES
    )
    assert [i.form for i in identifiers] == ["alnum", "digits.0"]
    assert entries == []


def test_empty_value_gives_nothing() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    assert tokenizer.identifiers_for(
        "f", FieldClass.IDENTIFIER, ID_FORMS, "  ", expires_at=EXPIRES
    ) == (
        [],
        [],
    )


def test_build_event_identifiers_keeps_field_order_and_dedupes_fields() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    entries: list[FieldInput] = [
        ("b", FieldClass.IDENTIFIER, ("raw",), "B-1001"),
        ("a", FieldClass.IDENTIFIER, ("raw",), "A-2002"),
        ("b", FieldClass.IDENTIFIER, ("raw",), "B-3003"),  # same field again: first wins
    ]
    result = tokenizer.build_event_identifiers(entries, expires_at=EXPIRES)
    assert isinstance(result, TokenizedFields)
    assert [(i.field, i.form) for i in result.identifiers] == [("b", "raw"), ("a", "raw")]
    assert result.truncated == 0
    assert [e.raw_value for e in result.vault_entries] == ["B-1001", "A-2002"]


def test_event_cap_truncates_previous_key_entries_first() -> None:
    tokenizer = Tokenizer(DUAL, "default")
    # 40 fields with one raw form each: 40 active-key entries, 40 previous-key entries.
    entries: list[FieldInput] = [
        (f"f{n:02d}", FieldClass.IDENTIFIER, ("raw",), f"V-{n:04d}") for n in range(40)
    ]
    result = tokenizer.build_event_identifiers(entries, expires_at=EXPIRES)
    assert len(result.identifiers) == MAX_IDENTIFIERS_PER_EVENT
    assert result.truncated == 80 - MAX_IDENTIFIERS_PER_EVENT
    versions = [i.token.partition(".")[0] for i in result.identifiers]
    assert versions[:40] == ["t2"] * 40
    assert versions[40:] == ["t1"] * (MAX_IDENTIFIERS_PER_EVENT - 40)
    assert [i.field for i in result.identifiers[40:]] == [f"f{n:02d}" for n in range(24)]
    # vault entries only for tokens that made it into the event
    assert {e.token for e in result.vault_entries} == {i.token for i in result.identifiers}


def test_event_cap_truncates_active_key_entries_in_field_order() -> None:
    tokenizer = Tokenizer(SINGLE, "default")
    entries: list[FieldInput] = [
        (f"f{n:02d}", FieldClass.IDENTIFIER, ("raw", "norm"), f"AB-{n:04d}") for n in range(50)
    ]
    result = tokenizer.build_event_identifiers(entries, expires_at=EXPIRES)
    assert len(result.identifiers) == MAX_IDENTIFIERS_PER_EVENT
    assert result.truncated == 100 - MAX_IDENTIFIERS_PER_EVENT
    assert result.identifiers[-1].field == "f31"
    assert result.identifiers[-1].form == "norm"


def test_build_event_identifiers_validates_as_canonical_identifiers() -> None:
    tokenizer = Tokenizer(DUAL, "default")
    entries: list[FieldInput] = [
        ("order_ref", FieldClass.IDENTIFIER, ID_FORMS, "SO-0004471"),
        ("amount", FieldClass.AMOUNT, ("amount",), "1299.99"),
        ("customer", FieldClass.PERSON_NAME, ("raw", "phonetic"), "Jane Smith"),
    ]
    result = tokenizer.build_event_identifiers(entries, expires_at=EXPIRES)
    event = CanonicalEvent.model_validate(
        {
            **CanonicalEvent.example().model_dump(mode="json"),
            "identifiers": [i.model_dump() for i in result.identifiers],
        }
    )
    assert len(event.identifiers) == len(result.identifiers)


def test_tokenize_query_returns_tokens_for_all_forms_and_versions() -> None:
    tokenizer = Tokenizer(DUAL, "default")
    tokens = tokenizer.tokenize_query("SO-0004471")
    assert tokens == [
        token(KEY_V2, 2, "id", "SO-0004471"),
        token(KEY_V2, 2, "id", "so-0004471"),
        token(KEY_V2, 2, "id", "so0004471"),
        token(KEY_V2, 2, "id", "4471"),
        token(KEY_V1, 1, "id", "SO-0004471"),
        token(KEY_V1, 1, "id", "so-0004471"),
        token(KEY_V1, 1, "id", "so0004471"),
        token(KEY_V1, 1, "id", "4471"),
    ]
    assert tokenizer.tokenize_query("4471") == [
        token(KEY_V2, 2, "id", "4471"),
        token(KEY_V1, 1, "id", "4471"),
    ]
    assert tokenizer.tokenize_query("2026-10-06", ["date"]) == [
        token(KEY_V2, 2, "date", "2026-10-06"),
        token(KEY_V1, 1, "date", "2026-10-06"),
    ]
    assert tokenizer.key_versions == [2, 1]


def test_tokenize_query_of_nothing_is_empty() -> None:
    assert Tokenizer(DUAL, "default").tokenize_query("  ") == []


def test_actor_token_is_the_raw_id_token_under_the_active_key() -> None:
    tokenizer = Tokenizer(DUAL, "default")
    assert tokenizer.actor_token(" svc-batch ") == token(KEY_V2, 2, "id", "svc-batch")
    with pytest.raises(ValueError, match="empty"):
        tokenizer.actor_token("   ")


def test_vault_entries_carry_the_callers_expiry_and_only_raw_values() -> None:
    tokenizer = Tokenizer(SINGLE, "t1")
    later = EXPIRES + timedelta(days=1)
    _, entries = tokenizer.identifiers_for(
        "f", FieldClass.IDENTIFIER, ID_FORMS, "  SO-0004471 ", expires_at=later
    )
    assert entries == [VaultEntry(token(KEY_V1, 1, "id", "SO-0004471"), "SO-0004471", later)]


def test_repr_never_shows_key_material() -> None:
    tokenizer = Tokenizer(DUAL, "default")
    text = repr(tokenizer)
    assert "default" in text
    assert KEY_V1.hex() not in text and KEY_V2.hex() not in text
    assert "material" not in text
