"""carto_edge.secrets: secret_ref resolution at the edge (spec 14.3, 8.1; ADR 0013).

``local://`` against the KMS-wrapped SQLite store, ``vault://`` against KV v2, cloud schemes
refused until M6, values cached in memory for at most 15 minutes and never logged.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import threading
from pathlib import Path

import httpx
import pytest

from carto_common.crypto import LocalKms, WrappedKey
from carto_edge.config import EdgeSettings, KmsSettings
from carto_edge.connectors.base import SecretResolver
from carto_edge.secrets import (
    FAILURE_CACHE_SECONDS,
    MAX_CACHE_SECONDS,
    Credentials,
    EdgeSecretResolver,
    LocalSecretStore,
    SecretError,
    parse_credentials,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("CARTO_"):
            monkeypatch.delenv(name)


@pytest.fixture
def kms(tmp_path: Path) -> LocalKms:
    return LocalKms.create(tmp_path / "keys" / "local-kms.key")


@pytest.fixture
def settings(tmp_path: Path) -> EdgeSettings:
    return EdgeSettings(state_dir=tmp_path, tenant_id="acme")


class Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------------------------


def test_parse_credentials_json() -> None:
    creds = parse_credentials('{"username": "carto_ro", "password": "p:w:d"}')
    assert creds == Credentials("carto_ro", "p:w:d")


def test_parse_credentials_text_splits_on_first_colon() -> None:
    assert parse_credentials("carto_ro:p:w:d") == Credentials("carto_ro", "p:w:d")


@pytest.mark.parametrize(
    "value", ["", "nocolon", '{"username": "x"}', '{"username": 1, "password": "x"}', "[]", ":"]
)
def test_parse_credentials_rejects_malformed(value: str) -> None:
    with pytest.raises(SecretError):
        parse_credentials(value)


def test_credentials_repr_hides_the_password() -> None:
    creds = Credentials("carto_ro", "hunter2-very-secret")
    assert "hunter2" not in repr(creds)
    assert "hunter2" not in str(creds)
    assert "carto_ro" in repr(creds)


# ---------------------------------------------------------------------------------------------
# LocalSecretStore
# ---------------------------------------------------------------------------------------------


def test_local_store_round_trip(settings: EdgeSettings, kms: LocalKms) -> None:
    store = LocalSecretStore.open(settings, kms)
    try:
        store.put("splunk-token", "tok-123")
        store.put("db", '{"username": "u", "password": "p"}')
        assert store.get("splunk-token") == "tok-123"
        assert store.get("db") == '{"username": "u", "password": "p"}'
        assert store.names() == ["db", "splunk-token"]
        store.put("splunk-token", "tok-456")
        assert store.get("splunk-token") == "tok-456"
        store.delete("db")
        assert store.names() == ["splunk-token"]
        with pytest.raises(SecretError, match="unknown"):
            store.get("db")
        assert store.delete("db") is False
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_local_store_files_are_owner_only_from_creation(
    settings: EdgeSettings, kms: LocalKms
) -> None:
    """ADR 0070 section 7: the database file is created exclusively with mode 0600 before SQLite
    opens it, and the WAL side files SQLite creates under the umask are restricted right after
    the first connection, so no file of the store is ever readable by others."""
    previous = os.umask(0)
    try:
        store = LocalSecretStore.open(settings, kms)
        try:
            store.put("api", "value")
            database = settings.state_dir / "secrets.sqlite"
            for suffix in ("", "-wal", "-shm"):
                path = database.with_name(database.name + suffix)
                assert path.exists(), suffix
                assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
        finally:
            store.close()
        # Reopening an existing store keeps its mode and restricts fresh side files again.
        again = LocalSecretStore.open(settings, kms)
        try:
            assert again.get("api") == "value"
            for suffix in ("", "-wal", "-shm"):
                path = database.with_name(database.name + suffix)
                if path.exists():
                    assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
        finally:
            again.close()
    finally:
        os.umask(previous)


def test_local_store_values_are_not_in_clear_on_disk(settings: EdgeSettings, kms: LocalKms) -> None:
    store = LocalSecretStore.open(settings, kms)
    store.put("api", "clear-text-marker-xyz")
    store.close()
    raw = (settings.state_dir / "secrets.sqlite").read_bytes()
    assert b"clear-text-marker-xyz" not in raw
    key_file = settings.keys_dir / "secrets-data-key.json"
    assert key_file.exists()
    wrapped = WrappedKey.read(key_file)
    assert wrapped.context == {"tenant_id": "acme", "purpose": "secrets"}
    assert wrapped.provider == "local"


def test_local_store_reopens_with_the_same_kms(settings: EdgeSettings, kms: LocalKms) -> None:
    store = LocalSecretStore.open(settings, kms)
    store.put("x", "1")
    store.close()
    again = LocalSecretStore.open(settings, kms)
    assert again.get("x") == "1"
    again.close()


def test_local_store_refuses_another_kms(
    settings: EdgeSettings, kms: LocalKms, tmp_path: Path
) -> None:
    LocalSecretStore.open(settings, kms).close()
    other = LocalKms.create(tmp_path / "other.key")
    with pytest.raises(SecretError):
        LocalSecretStore.open(settings, other)


def test_local_store_name_is_bound_as_aad(settings: EdgeSettings, kms: LocalKms) -> None:
    store = LocalSecretStore.open(settings, kms)
    store.put("a", "value-a")
    store.close()
    # Re-label the row: the ciphertext of "a" presented as "b" must not decrypt.
    with sqlite3.connect(settings.state_dir / "secrets.sqlite") as db:
        db.execute("UPDATE secrets SET name = 'b' WHERE name = 'a'")
    store = LocalSecretStore.open(settings, kms)
    with pytest.raises(SecretError):
        store.get("b")
    store.close()


@pytest.mark.parametrize("name", ["", "../x", "a b", "x" * 300, "-lead"])
def test_local_store_rejects_bad_names(settings: EdgeSettings, kms: LocalKms, name: str) -> None:
    store = LocalSecretStore.open(settings, kms)
    with pytest.raises(SecretError):
        store.put(name, "v")
    store.close()


# ---------------------------------------------------------------------------------------------
# EdgeSecretResolver
# ---------------------------------------------------------------------------------------------


def test_resolver_is_a_secret_resolver(settings: EdgeSettings, kms: LocalKms) -> None:
    assert isinstance(EdgeSecretResolver(settings, kms), SecretResolver)


def test_resolver_local_scheme(settings: EdgeSettings, kms: LocalKms) -> None:
    store = LocalSecretStore.open(settings, kms)
    store.put("splunk-orders-token", "tok-xyz")
    store.close()
    resolver = EdgeSecretResolver(settings, kms)
    assert resolver.resolve("local://splunk-orders-token") == "tok-xyz"
    with pytest.raises(SecretError, match="unknown"):
        resolver.resolve("local://nope")
    resolver.close()


def test_resolver_caches_for_at_most_fifteen_minutes(settings: EdgeSettings, kms: LocalKms) -> None:
    store = LocalSecretStore.open(settings, kms)
    store.put("s", "v1")
    store.close()
    clock = Clock()
    resolver = EdgeSecretResolver(settings, kms, clock=clock, cache_seconds=10_000)
    assert resolver.cache_seconds == MAX_CACHE_SECONDS == 900
    assert resolver.resolve("local://s") == "v1"
    # A new value in the store is not seen while the cache entry lives...
    store = LocalSecretStore.open(settings, kms)
    store.put("s", "v2")
    store.close()
    clock.now += 899
    assert resolver.resolve("local://s") == "v1"
    # ...and is seen once it expired.
    clock.now += 2
    assert resolver.resolve("local://s") == "v2"
    resolver.invalidate("local://s")
    assert resolver.cached_count == 0
    resolver.close()


@pytest.mark.parametrize(
    "ref", ["aws-sm://carto/wms-readonly", "azure-kv://vault/name", "gcp-sm://p/s"]
)
def test_resolver_cloud_schemes_wait_for_m6(
    settings: EdgeSettings, kms: LocalKms, ref: str
) -> None:
    with pytest.raises(SecretError, match="M6"):
        EdgeSecretResolver(settings, kms).resolve(ref)


@pytest.mark.parametrize("ref", ["", "ftp://x", "vault://", "local://", "vault://kv", "plain-text"])
def test_resolver_rejects_malformed_refs(settings: EdgeSettings, kms: LocalKms, ref: str) -> None:
    with pytest.raises(SecretError):
        EdgeSecretResolver(settings, kms).resolve(ref)


def vault_settings(tmp_path: Path, token: str = "s.token-value") -> EdgeSettings:  # noqa: S107
    token_file = tmp_path / "vault-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    return EdgeSettings(
        state_dir=tmp_path,
        tenant_id="acme",
        kms=KmsSettings(vault_url="https://vault.internal:8200", vault_token_file=token_file),
    )


def test_resolver_vault_kv_v2(tmp_path: Path, kms: LocalKms) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/kv/data/carto/splunk-orders-token":
            return httpx.Response(
                200,
                json={"data": {"data": {"value": "tok-from-vault", "other": "o"}, "metadata": {}}},
            )
        if request.url.path == "/v1/kv/data/carto/sftp-readonly":
            return httpx.Response(200, json={"data": {"data": {"password": "pw", "username": "u"}}})
        if request.url.path == "/v1/kv/data/carto/number":
            return httpx.Response(200, json={"data": {"data": {"value": 42}}})
        return httpx.Response(404, json={"errors": []})

    settings = vault_settings(tmp_path)
    resolver = EdgeSecretResolver(
        settings, kms, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert resolver.resolve("vault://kv/carto/splunk-orders-token") == "tok-from-vault"
    assert resolver.resolve("vault://kv/carto/sftp-readonly#password") == "pw"
    assert seen[0].method == "GET"
    assert seen[0].headers["X-Vault-Token"] == "s.token-value"
    assert seen[0].url.host == "vault.internal"
    with pytest.raises(SecretError, match="404"):
        resolver.resolve("vault://kv/carto/missing")
    with pytest.raises(SecretError, match="string"):
        resolver.resolve("vault://kv/carto/number")
    with pytest.raises(SecretError, match="field"):
        resolver.resolve("vault://kv/carto/splunk-orders-token#absent")
    # Cached: the second resolve of the first reference makes no request.
    count = len(seen)
    assert resolver.resolve("vault://kv/carto/splunk-orders-token") == "tok-from-vault"
    assert len(seen) == count
    resolver.close()


def test_resolver_vault_requires_settings(settings: EdgeSettings, kms: LocalKms) -> None:
    with pytest.raises(SecretError, match="vault_url"):
        EdgeSecretResolver(settings, kms).resolve("vault://kv/carto/x")


def test_resolver_vault_token_file_missing(tmp_path: Path, kms: LocalKms) -> None:
    settings = vault_settings(tmp_path)
    settings.kms.vault_token_file.unlink()  # type: ignore[union-attr]
    resolver = EdgeSecretResolver(
        settings,
        kms,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
    )
    with pytest.raises(SecretError, match="token file"):
        resolver.resolve("vault://kv/carto/x")


def test_resolver_vault_malformed_bodies(tmp_path: Path, kms: LocalKms) -> None:
    bodies = iter([b"not json", json.dumps({"data": "x"}).encode(), b"x" * (2 * 1024 * 1024)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=next(bodies))

    resolver = EdgeSecretResolver(
        vault_settings(tmp_path), kms, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    for ref in ("vault://kv/a", "vault://kv/b", "vault://kv/c"):
        with pytest.raises(SecretError):
            resolver.resolve(ref)


def test_resolver_never_logs_values(
    settings: EdgeSettings, kms: LocalKms, caplog: pytest.LogCaptureFixture
) -> None:
    store = LocalSecretStore.open(settings, kms)
    store.put("s", "the-secret-value-9f8e7d")
    store.close()
    with caplog.at_level(logging.DEBUG):
        EdgeSecretResolver(settings, kms).resolve("local://s")
        with pytest.raises(SecretError):
            EdgeSecretResolver(settings, kms).resolve("local://missing")
    assert "the-secret-value-9f8e7d" not in caplog.text


def test_local_store_and_resolver_work_from_other_threads(tmp_path: Path) -> None:
    """The gateway resolves webhook secrets from a threadpool and the scheduler from worker
    threads; SQLite's default same-thread check must not break either."""
    settings = EdgeSettings(state_dir=tmp_path / "state")
    kms = LocalKms.create(tmp_path / "kms.key")
    with LocalSecretStore.open(settings, kms) as store:
        store.put("hook-secret", "s3cret-value-0123456789")
    resolver = EdgeSecretResolver(settings, kms)
    results: list[str] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.append(resolver.resolve("local://hook-secret"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    closer = threading.Thread(target=resolver.close)
    closer.start()
    closer.join()
    assert not errors
    assert results == ["s3cret-value-0123456789"] * 4


def test_resolver_caches_failures_briefly(tmp_path: Path, kms: LocalKms) -> None:
    """A slow or unreachable Vault costs one attempt per reference per window (review)."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, json={"errors": []})

    now = [1000.0]
    resolver = EdgeSecretResolver(
        vault_settings(tmp_path),
        kms,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    for _ in range(3):
        with pytest.raises(SecretError):
            resolver.resolve("vault://kv/carto/hook")
    assert len(calls) == 1
    now[0] += FAILURE_CACHE_SECONDS + 1
    with pytest.raises(SecretError):
        resolver.resolve("vault://kv/carto/hook")
    assert len(calls) == 2
    resolver.close()
