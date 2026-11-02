"""Secret resolution at the edge (spec 14.3, 8.1 "Credentials are referenced, never stored";
ADR 0013).

A ``secret_ref`` is resolved at use through :class:`EdgeSecretResolver`, the edge's
:class:`carto_edge.connectors.base.SecretResolver`:

- ``local://<name>``: the Compose-pilot fallback. Values live in :class:`LocalSecretStore`, a
  SQLite file under the state directory, each encrypted with AES-256-GCM under a data key
  that exists at rest only wrapped by the KMS (``<state_dir>/keys/secrets-data-key.json``,
  context ``{"tenant_id": ..., "purpose": "secrets"}``). The secret name is the AAD, so a
  ciphertext moved to another name does not decrypt (spec 14.4 "AAD = tenant + token (or
  secret ID)"). ``put`` and ``delete`` exist for the CLI only; no connector calls them.
- ``vault://<mount>/<path>[#<field>]``: HashiCorp Vault KV v2, ``GET /v1/<mount>/data/<path>``
  with the token read from ``kms.vault_token_file`` (never from the environment). The field
  defaults to ``value``.
- ``aws-sm://``, ``azure-kv://``, ``gcp-sm://``: refused with a clear message until M6.

Resolved values are cached in memory for at most 15 minutes (spec 14.3), never written to disk
in clear, never logged (spec 2.3 invariant 7) and never part of an exception message. A
credential pair is one secret value: a JSON object ``{"username": ..., "password": ...}`` or
``user:password`` text; :func:`parse_credentials` splits it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Self

import httpx

from carto_common.crypto import (
    AesGcmBox,
    CryptoError,
    KeyWrapper,
    WrappedKey,
    _create_private,
    generate_key,
)
from carto_common.settings import validate_secret_ref
from carto_edge.config import EdgeSettings
from carto_edge.net.http import build_ssl_context

__all__ = [
    "MAX_CACHE_SECONDS",
    "Credentials",
    "EdgeSecretResolver",
    "LocalSecretStore",
    "SecretError",
    "parse_credentials",
]

logger = logging.getLogger(__name__)

MAX_CACHE_SECONDS: Final = 900
"""Spec 14.3: cached in memory at most 15 minutes."""

FAILURE_CACHE_SECONDS: Final = 15.0
"""A failed resolution is not retried for this long: a slow or unreachable Vault costs one
timeout per reference per window, not one per request."""

MAX_VAULT_BODY_BYTES: Final = 1024 * 1024
VAULT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_VAULT_FIELD: Final = "value"
SECRET_NAME_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
SECRETS_DB_NAME: Final = "secrets.sqlite"
_SQLITE_SIDE_FILES: Final = ("-wal", "-shm", "-journal")
"""Files SQLite creates next to the database under the process umask; restricted after the
first connection (ADR 0070 section 7)."""
DATA_KEY_FILE_NAME: Final = "secrets-data-key.json"
DATA_KEY_PURPOSE: Final = "secrets"

_CLOUD_SCHEMES: Final = ("aws-sm", "azure-kv", "gcp-sm")


class SecretError(Exception):
    """A secret could not be resolved. The message names the reference, never a value."""


# ---------------------------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Credentials:
    """A username and password; the password is hidden from ``repr`` and ``str``."""

    username: str
    password: str = field(repr=False)

    def __str__(self) -> str:
        return f"Credentials(username={self.username!r})"


def parse_credentials(value: str) -> Credentials:
    """Split a credential secret: JSON ``{"username", "password"}`` or ``user:password``."""
    text = value.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError as exc:
            msg = "credential secret is not valid JSON"
            raise SecretError(msg) from exc
        if not isinstance(data, dict):
            msg = "credential secret must be a JSON object"
            raise SecretError(msg)
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str) or not username:
            msg = "credential secret needs string 'username' and 'password' fields"
            raise SecretError(msg)
        return Credentials(username, password)
    username, separator, password = text.partition(":")
    if not separator or not username:
        msg = "credential secret must be a JSON object or 'user:password' text"
        raise SecretError(msg)
    return Credentials(username, password)


# ---------------------------------------------------------------------------------------------
# Local store
# ---------------------------------------------------------------------------------------------


def _check_name(name: str) -> str:
    if not SECRET_NAME_RE.match(name) or ".." in name:
        msg = "secret name must be 1 to 256 characters of letters, digits, '.', '_', '/' or '-'"
        raise SecretError(msg)
    return name


class LocalSecretStore:
    """``local://`` secrets: SQLite rows of AES-256-GCM ciphertext under a KMS-wrapped data key.

    One connection shared across threads behind a lock (ADR 0014), so the gateway's threadpool
    and the poll scheduler's worker threads can resolve through the same store.

    The database file is created exclusively with mode 0600 before SQLite opens it (an empty
    file is an empty database to SQLite), and the ``-wal``, ``-shm`` and ``-journal`` files
    SQLite creates under the process umask are restricted to the owner right after the first
    connection, so no file of the store is readable by others at any instant (ADR 0070
    section 7). The rows are ciphertext either way; the modes are defence in depth."""

    __slots__ = ("_box", "_db", "_lock", "_path")

    def __init__(self, path: Path, data_key: bytes) -> None:
        self._path = path
        self._box = AesGcmBox(data_key)
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not path.exists():
                # A FileExistsError means another process got there first; its file is 0600 too.
                with contextlib.suppress(FileExistsError):
                    _create_private(path, b"")
            self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS secrets ("
                "name TEXT PRIMARY KEY, blob BLOB NOT NULL, updated_at TEXT NOT NULL)"
            )
        except (sqlite3.Error, OSError) as exc:
            msg = f"cannot open the local secret store at {path}"
            raise SecretError(msg) from exc
        _restrict_store_files(path)

    def __repr__(self) -> str:
        return f"LocalSecretStore(path={str(self._path)!r})"

    @classmethod
    def open(cls, settings: EdgeSettings, kms: KeyWrapper) -> Self:
        """Open ``<state_dir>/secrets.sqlite``, creating the wrapped data key on first use."""
        context = {"tenant_id": settings.tenant_id, "purpose": DATA_KEY_PURPOSE}
        key_file = settings.keys_dir / DATA_KEY_FILE_NAME
        try:
            if key_file.exists():
                wrapped = WrappedKey.read(key_file)
                if wrapped.context != context:
                    msg = f"wrapped data key {key_file} was made for another tenant or purpose"
                    raise SecretError(msg)
                data_key = kms.unwrap(wrapped)
            else:
                data_key = generate_key()
                kms.wrap(data_key, context).write(key_file)
        except CryptoError as exc:
            msg = f"cannot unwrap the local secret store data key {key_file}: {exc}"
            raise SecretError(msg) from exc
        except OSError as exc:
            msg = f"cannot read or write the wrapped data key {key_file}"
            raise SecretError(msg) from exc
        return cls(settings.state_dir / SECRETS_DB_NAME, data_key)

    def put(self, name: str, value: str) -> None:
        """Store or replace ``value`` under ``name`` (CLI use)."""
        _check_name(name)
        blob = self._box.encrypt(value.encode("utf-8"), name.encode("utf-8"))
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO secrets (name, blob, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET blob = excluded.blob, "
                "updated_at = excluded.updated_at",
                (name, blob, now),
            )

    def get(self, name: str) -> str:
        _check_name(name)
        with self._lock:
            row = self._db.execute("SELECT blob FROM secrets WHERE name = ?", (name,)).fetchone()
        if row is None:
            msg = f"unknown local secret {name!r}"
            raise SecretError(msg)
        try:
            return self._box.decrypt(bytes(row[0]), name.encode("utf-8")).decode("utf-8")
        except (CryptoError, UnicodeDecodeError) as exc:
            msg = f"local secret {name!r} cannot be decrypted (wrong key or tampered row)"
            raise SecretError(msg) from exc

    def delete(self, name: str) -> bool:
        """Remove ``name``; returns whether it existed (CLI use)."""
        _check_name(name)
        with self._lock:
            cursor = self._db.execute("DELETE FROM secrets WHERE name = ?", (name,))
            return cursor.rowcount > 0

    def names(self) -> list[str]:
        with self._lock:
            return [row[0] for row in self._db.execute("SELECT name FROM secrets ORDER BY name")]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _restrict_store_files(path: Path) -> None:
    """Owner-only mode on the database and every SQLite side file that exists."""
    for candidate in (path, *(path.with_name(path.name + s) for s in _SQLITE_SIDE_FILES)):
        if not candidate.exists():
            continue
        try:
            candidate.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            logger.warning("could not restrict permissions of %s", candidate)


# ---------------------------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _CacheEntry:
    value: str = field(repr=False)
    expires_at: float


class EdgeSecretResolver:
    """Resolves ``secret_ref`` values for connectors (spec 14.3)."""

    def __init__(
        self,
        settings: EdgeSettings,
        kms: KeyWrapper,
        client: httpx.Client | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        cache_seconds: float = MAX_CACHE_SECONDS,
    ) -> None:
        self._settings = settings
        self._kms = kms
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._cache_seconds = max(0.0, min(float(cache_seconds), float(MAX_CACHE_SECONDS)))
        self._cache: dict[str, _CacheEntry] = {}
        self._store: LocalSecretStore | None = None
        self._lock = threading.RLock()
        self._failures: dict[str, float] = {}

    def __repr__(self) -> str:
        return f"EdgeSecretResolver(cached={len(self._cache)})"

    @property
    def cache_seconds(self) -> float:
        return self._cache_seconds

    @property
    def cached_count(self) -> int:
        return len(self._cache)

    def invalidate(self, secret_ref: str) -> None:
        self._cache.pop(secret_ref, None)

    def clear_cache(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        self.clear_cache()
        if self._store is not None:
            self._store.close()
            self._store = None
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    # -- resolution ---------------------------------------------------------------------------

    def resolve(self, secret_ref: str) -> str:
        """The secret's value; thread-safe (one resolution at a time, cached per reference)."""
        try:
            validate_secret_ref(secret_ref)
        except ValueError as exc:
            msg = f"invalid secret_ref: {exc}"
            raise SecretError(msg) from exc
        with self._lock:
            return self._resolve_locked(secret_ref)

    def _resolve_locked(self, secret_ref: str) -> str:
        now = self._clock()
        entry = self._cache.get(secret_ref)
        if entry is not None and entry.expires_at > now:
            return entry.value
        failed_until = self._failures.get(secret_ref)
        if failed_until is not None and failed_until > now:
            msg = "the secret could not be resolved a moment ago; retrying shortly"
            raise SecretError(msg)
        try:
            value = self._fetch(secret_ref)
        except SecretError:
            self._failures[secret_ref] = now + FAILURE_CACHE_SECONDS
            raise
        self._failures.pop(secret_ref, None)
        if self._cache_seconds > 0:
            self._cache[secret_ref] = _CacheEntry(value, now + self._cache_seconds)
        return value

    def _fetch(self, secret_ref: str) -> str:
        scheme, _, rest = secret_ref.partition("://")
        if scheme == "local":
            value = self._resolve_local(rest)
        elif scheme == "vault":
            value = self._resolve_vault(rest)
        elif scheme in _CLOUD_SCHEMES:
            msg = f"{scheme}:// secret references arrive in M6 (ADR 0013)"
            raise SecretError(msg)
        else:  # pragma: no cover - validate_secret_ref admits only the schemes above
            msg = f"unsupported secret_ref scheme {scheme!r}"
            raise SecretError(msg)
        logger.debug("reference resolved, scheme=%s", scheme)
        return value

    def _resolve_local(self, name: str) -> str:
        if self._store is None:
            self._store = LocalSecretStore.open(self._settings, self._kms)
        return self._store.get(name)

    def _vault_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=VAULT_TIMEOUT_SECONDS,
                trust_env=False,
                verify=build_ssl_context(ca_file=None, client_cert=None, verify=True),
            )
        return self._client

    def _vault_token(self) -> str:
        token_file = self._settings.kms.vault_token_file
        if token_file is None:  # pragma: no cover - checked by the caller
            msg = "kms.vault_token_file is not set"
            raise SecretError(msg)
        try:
            token_value = token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            msg = f"cannot read the Vault token file {token_file}"
            raise SecretError(msg) from exc
        if not token_value:
            msg = f"the Vault token file {token_file} is empty"
            raise SecretError(msg)
        return token_value

    def _resolve_vault(self, rest: str) -> str:
        kms = self._settings.kms
        if kms.vault_url is None or kms.vault_token_file is None:
            msg = "vault:// secret references need kms.vault_url and kms.vault_token_file"
            raise SecretError(msg)
        location, _, field_name = rest.partition("#")
        mount, _, path = location.partition("/")
        field_name = field_name or DEFAULT_VAULT_FIELD
        if not mount or not path or path.startswith("/") or ".." in path.split("/"):
            msg = "vault:// references are vault://<mount>/<path>[#<field>]"
            raise SecretError(msg)
        url = f"{kms.vault_url.rstrip('/')}/v1/{mount}/data/{path}"
        headers = {"X-Vault-Token": self._vault_token(), "X-Vault-Request": "true"}
        body = self._vault_get(url, headers)
        try:
            document = json.loads(body)
        except ValueError as exc:
            msg = "Vault returned a body that is not JSON"
            raise SecretError(msg) from exc
        data = document.get("data") if isinstance(document, dict) else None
        inner = data.get("data") if isinstance(data, dict) else None
        if not isinstance(inner, dict):
            msg = "Vault response has no data.data object (is the mount KV v2?)"
            raise SecretError(msg)
        if field_name not in inner:
            msg = f"Vault secret has no field {field_name!r}"
            raise SecretError(msg)
        value = inner[field_name]
        if not isinstance(value, str):
            msg = f"Vault secret field {field_name!r} is not a string"
            raise SecretError(msg)
        return value

    def _vault_get(self, url: str, headers: dict[str, str]) -> bytes:
        client = self._vault_client()
        try:
            with client.stream("GET", url, headers=headers) as response:
                if response.status_code != 200:
                    msg = f"Vault returned HTTP {response.status_code}"
                    raise SecretError(msg)
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_VAULT_BODY_BYTES:
                        msg = "Vault response exceeds the size limit"
                        raise SecretError(msg)
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            msg = f"Vault request failed: {type(exc).__name__}"
            raise SecretError(msg) from exc
        return b"".join(chunks)
