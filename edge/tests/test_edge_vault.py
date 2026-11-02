"""carto_edge.vault: put, reveal, expiry, delete, purge, AAD binding and thread safety of the
reveal vault (spec 8.4 "Reveal vault", 14.4, 14.10)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from carto_common.crypto import Keyring, TokenKey, generate_key
from carto_edge.pipeline.model import VaultEntry
from carto_edge.vault import RevealVault

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)
KEYRING = Keyring(active=TokenKey(1, generate_key()))
DATA_KEY = generate_key()


def raw_token(value: str) -> str:
    return KEYRING.active.token("id", value)


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[RevealVault]:
    vault = RevealVault(tmp_path / "vault.sqlite", DATA_KEY, "acme")
    yield vault
    vault.close()


def entries(*values: str, expires_at: datetime = LATER) -> list[VaultEntry]:
    return [VaultEntry(raw_token(value), value, expires_at) for value in values]


def test_put_and_reveal(vault: RevealVault) -> None:
    vault.put_many(entries("SO-0004471", "PO-77"))
    assert vault.count() == 2
    revealed = vault.reveal([raw_token("SO-0004471"), raw_token("PO-77"), raw_token("nope")])
    assert revealed == {raw_token("SO-0004471"): "SO-0004471", raw_token("PO-77"): "PO-77"}


def test_misses_and_malformed_tokens_are_omitted(vault: RevealVault) -> None:
    vault.put_many(entries("SO-0004471"))
    assert vault.reveal(["not-a-token", "", raw_token("missing")]) == {}
    assert vault.reveal([]) == {}


def test_put_is_insert_or_ignore(vault: RevealVault) -> None:
    vault.put_many(entries("SO-0004471"))
    vault.put_many([VaultEntry(raw_token("SO-0004471"), "tampered", LATER)])
    assert vault.count() == 1
    assert vault.reveal([raw_token("SO-0004471")]) == {raw_token("SO-0004471"): "SO-0004471"}


def test_put_many_is_one_transaction_and_tolerates_duplicates_in_the_batch(
    vault: RevealVault,
) -> None:
    batch = entries("A-1001", "A-1001", "B-2002")
    vault.put_many(batch)
    assert vault.count() == 2
    vault.put_many([])
    assert vault.count() == 2


def test_expired_entries_are_not_revealed_and_can_be_purged(vault: RevealVault) -> None:
    vault.put_many(entries("OLD-1", expires_at=NOW - timedelta(seconds=1)))
    vault.put_many(entries("NEW-1", expires_at=NOW + timedelta(seconds=1)))
    assert vault.reveal([raw_token("OLD-1"), raw_token("NEW-1")], now=NOW) == {
        raw_token("NEW-1"): "NEW-1"
    }
    assert vault.count() == 2
    assert vault.purge_expired(NOW) == 1
    assert vault.count() == 1
    assert vault.purge_expired(NOW + timedelta(seconds=2)) == 1
    assert vault.count() == 0


def test_expiry_at_the_boundary_is_expired(vault: RevealVault) -> None:
    vault.put_many(entries("X-1", expires_at=NOW))
    assert vault.reveal([raw_token("X-1")], now=NOW) == {}
    assert vault.reveal([raw_token("X-1")], now=NOW - timedelta(seconds=1)) == {
        raw_token("X-1"): "X-1"
    }


def test_delete_removes_entries_and_reports_the_count(vault: RevealVault) -> None:
    vault.put_many(entries("A-1001", "B-2002", "C-3003"))
    assert vault.delete([raw_token("A-1001"), raw_token("B-2002"), raw_token("zzz")]) == 2
    assert vault.count() == 1
    assert vault.reveal([raw_token("A-1001")]) == {}
    assert vault.delete([]) == 0


def test_aad_binds_the_ciphertext_to_its_token(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite"
    vault = RevealVault(path, DATA_KEY, "acme")
    vault.put_many(entries("A-1001", "B-2002"))
    vault.close()
    # Move A's ciphertext under B's token: the AAD (tenant + token) no longer matches.
    conn = sqlite3.connect(path)
    (blob,) = conn.execute(
        "SELECT ciphertext FROM vault WHERE token = ?", (raw_token("A-1001"),)
    ).fetchone()
    conn.execute("UPDATE vault SET ciphertext = ? WHERE token = ?", (blob, raw_token("B-2002")))
    conn.commit()
    conn.close()
    vault = RevealVault(path, DATA_KEY, "acme")
    try:
        assert vault.reveal([raw_token("A-1001"), raw_token("B-2002")]) == {
            raw_token("A-1001"): "A-1001"
        }
    finally:
        vault.close()


def test_aad_binds_the_tenant_and_the_data_key(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite"
    vault = RevealVault(path, DATA_KEY, "acme")
    vault.put_many(entries("A-1001"))
    vault.close()
    other_tenant = RevealVault(path, DATA_KEY, "other")
    try:
        assert other_tenant.reveal([raw_token("A-1001")]) == {}
    finally:
        other_tenant.close()
    other_key = RevealVault(path, generate_key(), "acme")
    try:
        assert other_key.reveal([raw_token("A-1001")]) == {}
    finally:
        other_key.close()


def test_values_are_encrypted_at_rest(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite"
    vault = RevealVault(path, DATA_KEY, "acme")
    vault.put_many(entries("SO-0004471-SECRET-VALUE"))
    vault.close()
    for candidate in (path, path.with_name("vault.sqlite-wal")):
        if candidate.exists():
            assert b"SO-0004471-SECRET-VALUE" not in candidate.read_bytes()


def test_schema_and_wal_mode(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite"
    vault = RevealVault(path, DATA_KEY, "acme")
    vault.put_many(entries("A-1001"))
    vault.close()
    conn = sqlite3.connect(path)
    try:
        (mode,) = conn.execute("PRAGMA journal_mode").fetchone()
        assert mode == "wal"
        columns = [row[1] for row in conn.execute("PRAGMA table_info(vault)")]
        assert columns == ["token", "key_version", "ciphertext", "expires_at"]
        (version,) = conn.execute("SELECT key_version FROM vault").fetchone()
        assert version == 1
        indexes = [row[1] for row in conn.execute("PRAGMA index_list(vault)")]
        assert any("expires" in name for name in indexes)
    finally:
        conn.close()


def test_entries_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite"
    vault = RevealVault(path, DATA_KEY, "acme")
    vault.put_many(entries("A-1001"))
    vault.close()
    reopened = RevealVault(path, DATA_KEY, "acme")
    try:
        assert reopened.reveal([raw_token("A-1001")]) == {raw_token("A-1001"): "A-1001"}
    finally:
        reopened.close()


def test_concurrent_writers_and_readers(vault: RevealVault) -> None:
    def writer(prefix: str) -> None:
        for n in range(50):
            vault.put_many(entries(f"{prefix}-{n:04d}"))

    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert vault.count() == 200
    assert vault.reveal([raw_token("T3-0049")]) == {raw_token("T3-0049"): "T3-0049"}


def test_context_manager_closes(tmp_path: Path) -> None:
    with RevealVault(tmp_path / "vault.sqlite", DATA_KEY, "acme") as vault:
        vault.put_many(entries("A-1001"))
        assert vault.count() == 1
    with pytest.raises(sqlite3.ProgrammingError):
        vault.count()


def test_repr_shows_no_key_material(vault: RevealVault) -> None:
    text = repr(vault)
    assert DATA_KEY.hex() not in text
    assert "acme" in text


def test_a_later_sighting_extends_the_expiry_of_a_backfilled_entry(vault: RevealVault) -> None:
    # A backfilled record (already past retention) arrives first, then a live one for the
    # same value: the entry must follow the live record's retention, not stay expired.
    token_text = raw_token("CUST-88210")
    vault.put_many(entries("CUST-88210", expires_at=NOW - timedelta(days=10)))
    assert vault.reveal([token_text], now=NOW) == {}
    assert vault.put_many(entries("CUST-88210", expires_at=LATER)) == 1
    assert vault.reveal([token_text], now=NOW) == {token_text: "CUST-88210"}
    assert vault.count() == 1


def test_an_earlier_expiry_never_shortens_an_entry(vault: RevealVault) -> None:
    token_text = raw_token("CUST-88210")
    vault.put_many(entries("CUST-88210", expires_at=LATER))
    assert vault.put_many(entries("CUST-88210", expires_at=NOW - timedelta(days=10))) == 0
    assert vault.reveal([token_text], now=NOW + timedelta(days=29)) == {token_text: "CUST-88210"}
