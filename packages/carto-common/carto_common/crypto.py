"""Cryptographic wrappers shared by edge, core and carto-ctl (spec 8.4, 14.3, 14.4).

Everything here is a thin, typed layer over the ``cryptography`` library (spec 14.4: "Use only
the ``cryptography`` library primitives. No custom crypto"):

- :func:`token`: the spec 8.4 token, ``t<key_version>.`` + the first 22 base64url characters of
  HMAC-SHA256(K_tenant_v<version>, domain || 0x00 || form_value).
- :class:`TokenKey` and :class:`Keyring`: the tenant key with its version, and the active key
  plus the previous versions still inside the rotation overlap window (spec 8.4 "Rotation").
- :class:`AesGcmBox`: AES-256-GCM with a random 96-bit nonce and caller-supplied AAD (spec
  14.4: the reveal vault and local secrets, AAD = tenant + token or secret id).
- :class:`KeyWrapper`, :class:`LocalKms`, :class:`VaultTransitKms`: envelope encryption of key
  material. The tenant key, vault data keys and the core assertion key exist at rest only as a
  :class:`WrappedKey` (spec 8.4 "stored only wrapped by the customer's KMS or Vault Transit").
  ``LocalKms`` is the Compose pilot fallback (spec 8.1, 14.3 ``local://``); cloud KMS wrappers
  arrive with the first partner that needs them (ADR 0012).
- :class:`SigningKey` and :class:`VerifyKey`: Ed25519 for bundle signatures (spec 8.1.1) and
  the signed internal assertion the core API presents to the edge reveal endpoint (spec 8.4
  "Reveal vault").

Key material never appears in ``repr`` or ``str`` of any object here, never in exceptions, and
is never logged (spec 2.3 invariant 7, 14.12).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, Self, runtime_checkable

import httpx
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AES_GCM_NONCE_BYTES",
    "KEY_BYTES",
    "MAX_ASSERTION_TTL",
    "TOKEN_BODY_LEN",
    "AesGcmBox",
    "CryptoError",
    "InternalAssertion",
    "InvalidAssertionError",
    "KeyWrapper",
    "Keyring",
    "KmsProvider",
    "LocalKms",
    "SigningKey",
    "TokenDomain",
    "TokenKey",
    "VaultTransitKms",
    "VerifyKey",
    "WrappedKey",
    "b64url_decode",
    "b64url_encode",
    "canonical_context",
    "fsync_directory",
    "generate_key",
    "key_fingerprint",
    "sign_assertion",
    "token",
    "verify_assertion",
    "write_private_file",
]

KEY_BYTES: Final = 32
"""Length of the tenant key, vault data keys and the local KMS master key: 256 bits."""

AES_GCM_NONCE_BYTES: Final = 12
"""96-bit nonces (spec 14.4)."""

AES_GCM_TAG_BYTES: Final = 16

TOKEN_BODY_LEN: Final = 22
"""Characters of base64url HMAC output in a token (spec 8.4)."""

TOKEN_SEPARATOR: Final = b"\x00"

MAX_KEY_VERSION: Final = 9999
"""Matches ``carto_schema.forms.TOKEN_PATTERN`` (one to four digits, no leading zero)."""

MAX_ASSERTION_TTL: Final = timedelta(minutes=5)
"""Longest lifetime an internal assertion may claim (spec 8.4: "short-lived")."""

ED25519_KEY_BYTES: Final = 32

TokenDomain = Literal["id", "date", "amt", "ph"]
"""Same values as :data:`carto_schema.forms.TokenDomain`; repeated so this package does not
depend on carto-schema."""

KmsProvider = Literal["local", "vault"]

PRIVATE_FILE_MODE: Final = 0o600
_O_BINARY: Final = getattr(os, "O_BINARY", 0)
"""Windows opens descriptors in text mode unless told otherwise; key bytes must not be
translated."""


class CryptoError(Exception):
    """A cryptographic operation failed. The message never carries key material or plaintext."""


class InvalidAssertionError(CryptoError):
    """An internal assertion failed signature, audience or time checks."""


# ---------------------------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------------------------


def b64url_encode(data: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Inverse of :func:`b64url_encode`; raises :class:`CryptoError` on malformed input."""
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode((text + padding).encode("ascii"))
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        msg = "malformed base64url input"
        raise CryptoError(msg) from exc


def canonical_context(context: Mapping[str, str]) -> bytes:
    """Deterministic bytes of a wrapping context, used as AAD and as the Vault ``context``."""
    return json.dumps(dict(context), sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_key() -> bytes:
    """Fresh 256-bit key material from the OS CSPRNG."""
    return secrets.token_bytes(KEY_BYTES)


def key_fingerprint(material: bytes) -> str:
    """Short, non-reversible id of key material for status screens and wrapped-key files."""
    return hashlib.sha256(material).hexdigest()[:16]


def _require_key(material: bytes, what: str) -> bytes:
    if len(material) != KEY_BYTES:
        msg = f"{what} must be exactly {KEY_BYTES} bytes"
        raise CryptoError(msg)
    return bytes(material)


def _require_version(version: int) -> int:
    if isinstance(version, bool):
        msg = "key version must be an integer"
        raise CryptoError(msg)
    if version < 1 or version > MAX_KEY_VERSION:
        msg = f"key version must be between 1 and {MAX_KEY_VERSION}"
        raise CryptoError(msg)
    return version


# ---------------------------------------------------------------------------------------------
# Tokens (spec 8.4)
# ---------------------------------------------------------------------------------------------


def token(material: bytes, key_version: int, domain: TokenDomain, form_value: str) -> str:
    """Compute the spec 8.4 token of ``form_value`` under ``domain`` with key ``key_version``.

    ``t<version>.`` + base64url(HMAC-SHA256(K, domain || 0x00 || form_value))[:22]. The result
    matches :data:`carto_schema.forms.TOKEN_PATTERN`.
    """
    version = _require_version(key_version)
    key = _require_key(material, "token key")
    message = domain.encode("ascii") + TOKEN_SEPARATOR + form_value.encode("utf-8")
    # hmac.digest is the one-shot C path of the same HMAC-SHA256 (no HMAC object per token).
    digest = hmac.digest(key, message, "sha256")
    return f"t{version}.{b64url_encode(digest)[:TOKEN_BODY_LEN]}"


@dataclass(frozen=True, slots=True)
class TokenKey:
    """One version of the tenant tokenization key. ``repr`` shows the version only."""

    version: int
    material: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_version(self.version)
        _require_key(self.material, "token key")

    def token(self, domain: TokenDomain, form_value: str) -> str:
        """The token of ``form_value`` under this key version."""
        return token(self.material, self.version, domain, form_value)

    @property
    def fingerprint(self) -> str:
        return key_fingerprint(self.material)


@dataclass(frozen=True, slots=True)
class Keyring:
    """The active tenant key plus previous versions still inside the rotation overlap.

    Tokenization uses :meth:`tokenization_keys` (every key, newest first) so events carry one
    token per live version during the overlap window (spec 8.4 "dual-tokenize"). Lookups of
    stored tokens (vault, search) use whichever version the token's prefix names.
    """

    active: TokenKey
    previous: tuple[TokenKey, ...] = ()

    def __post_init__(self) -> None:
        versions = [self.active.version, *(key.version for key in self.previous)]
        if len(set(versions)) != len(versions):
            msg = "keyring versions must be unique"
            raise CryptoError(msg)
        if any(key.version >= self.active.version for key in self.previous):
            msg = "previous key versions must be lower than the active version"
            raise CryptoError(msg)

    def tokenization_keys(self) -> tuple[TokenKey, ...]:
        return (self.active, *self.previous)

    def key(self, version: int) -> TokenKey:
        for key in self.tokenization_keys():
            if key.version == version:
                return key
        msg = f"no key with version {version} in the keyring"
        raise CryptoError(msg)

    @staticmethod
    def version_of(token_text: str) -> int:
        """The key version a token names (``t3.…`` gives 3); raises on malformed input."""
        head, separator, _body = token_text.partition(".")
        digits = head[1:]
        if (
            not separator
            or not head.startswith("t")
            or not digits.isdigit()
            or not digits.isascii()
            or digits.startswith("0")
        ):
            msg = "malformed token"
            raise CryptoError(msg)
        return _require_version(int(digits))


# ---------------------------------------------------------------------------------------------
# AES-256-GCM (spec 14.4)
# ---------------------------------------------------------------------------------------------


class AesGcmBox:
    """AES-256-GCM with random 96-bit nonces; ciphertexts are ``nonce || ciphertext+tag``."""

    __slots__ = ("_aead", "_fingerprint")

    def __init__(self, material: bytes) -> None:
        key = _require_key(material, "AES-GCM key")
        self._aead = AESGCM(key)
        self._fingerprint = key_fingerprint(key)

    def __repr__(self) -> str:
        return f"AesGcmBox(fingerprint={self._fingerprint!r})"

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(AES_GCM_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if len(blob) < AES_GCM_NONCE_BYTES + AES_GCM_TAG_BYTES:
            msg = "ciphertext too short"
            raise CryptoError(msg)
        nonce, ciphertext = blob[:AES_GCM_NONCE_BYTES], blob[AES_GCM_NONCE_BYTES:]
        try:
            return self._aead.decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            msg = "authentication failed (wrong key, wrong AAD or tampered ciphertext)"
            raise CryptoError(msg) from exc


# ---------------------------------------------------------------------------------------------
# Key wrapping (spec 8.4 key management, 14.4 envelope encryption)
# ---------------------------------------------------------------------------------------------


def fsync_directory(directory: Path) -> None:
    """Make a create, rename or delete in ``directory`` durable. Windows has no directory
    handle to sync; NTFS journals the rename itself."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_private(path: Path, data: bytes) -> None:
    """Create ``path`` (it must not exist) owner-only from the first instant, write and fsync."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def write_private_file(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically and durably, owner-only (0600) from creation:
    a temporary file is created exclusively with mode 0600, written and fsynced, renamed over
    ``path``, and the directory is fsynced. A crash leaves the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)  # left by a crash; never another writer's (one writer)
    try:
        _create_private(temporary, data)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


class WrappedKey(BaseModel):
    """Key material at rest: the KMS ciphertext plus what is needed to unwrap it.

    ``context`` is bound into the wrapping (AAD for the local KMS, derived-key context for
    Vault Transit) so a wrapped key cannot be replayed under another purpose or tenant.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    provider: KmsProvider
    key_id: str = Field(min_length=1, max_length=256)
    algorithm: str = Field(min_length=1, max_length=64)
    ciphertext: str = Field(min_length=1, max_length=8192)
    context: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    fingerprint: str = Field(
        min_length=16, max_length=16, description="sha256 of the plaintext key, first 16 hex."
    )

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "created_at must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)

    def write(self, path: Path) -> None:
        """Atomically, durably and owner-only (:func:`write_private_file`)."""
        write_private_file(path, (self.model_dump_json(indent=2) + "\n").encode("utf-8"))

    @classmethod
    def read(cls, path: Path) -> Self:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"cannot read wrapped key file {path}"
            raise CryptoError(msg) from exc
        try:
            return cls.model_validate_json(text)
        except ValueError as exc:
            msg = f"wrapped key file {path} is not valid"
            raise CryptoError(msg) from exc


@runtime_checkable
class KeyWrapper(Protocol):
    """Wraps and unwraps key material through the customer's KMS."""

    @property
    def provider(self) -> KmsProvider: ...

    @property
    def key_id(self) -> str: ...

    def wrap(self, material: bytes, context: Mapping[str, str]) -> WrappedKey: ...

    def unwrap(self, wrapped: WrappedKey) -> bytes: ...


def _check_unwrapped(wrapped: WrappedKey, provider: str, key_id: str, material: bytes) -> bytes:
    if wrapped.provider != provider:
        msg = f"wrapped key provider {wrapped.provider!r} does not match {provider!r}"
        raise CryptoError(msg)
    if wrapped.key_id != key_id:
        msg = "wrapped key was wrapped by a different KMS key"
        raise CryptoError(msg)
    if not hmac.compare_digest(key_fingerprint(material), wrapped.fingerprint):
        msg = "unwrapped key does not match the recorded fingerprint"
        raise CryptoError(msg)
    return material


class LocalKms:
    """A master key in a file, for Compose pilots and development (spec 8.1, 14.3 ``local://``).

    The file holds 32 raw bytes and is created with owner-only permissions. Wrapping is
    AES-256-GCM under the master key with the canonical context as AAD. The master key itself
    is the one secret an operator must protect and back up (spec 14.11, runbook).
    """

    ALGORITHM: Final = "A256GCM"

    __slots__ = ("_box", "_key_id")

    def __init__(self, material: bytes) -> None:
        self._box = AesGcmBox(material)
        self._key_id = f"local:{key_fingerprint(material)}"

    def __repr__(self) -> str:
        return f"LocalKms(key_id={self._key_id!r})"

    @property
    def provider(self) -> KmsProvider:
        return "local"

    @property
    def key_id(self) -> str:
        return self._key_id

    @classmethod
    def create(cls, path: Path) -> Self:
        """Create a new master key file; refuses to overwrite an existing one. The file is
        created exclusively with mode 0600 (never readable by others, not even for an instant)
        and fsynced with its directory."""
        path.parent.mkdir(parents=True, exist_ok=True)
        material = generate_key()
        try:
            _create_private(path, material)
        except FileExistsError as exc:
            msg = f"refusing to overwrite existing key file {path}"
            raise CryptoError(msg) from exc
        fsync_directory(path.parent)
        return cls(material)

    @classmethod
    def open(cls, path: Path) -> Self:
        try:
            material = path.read_bytes()
        except OSError as exc:
            msg = f"cannot read local KMS key file {path}"
            raise CryptoError(msg) from exc
        if len(material) != KEY_BYTES:
            msg = f"local KMS key file {path} is not a {KEY_BYTES}-byte key"
            raise CryptoError(msg)
        return cls(material)

    def wrap(self, material: bytes, context: Mapping[str, str]) -> WrappedKey:
        key = _require_key(material, "key to wrap")
        blob = self._box.encrypt(key, canonical_context(context))
        return WrappedKey(
            provider=self.provider,
            key_id=self._key_id,
            algorithm=self.ALGORITHM,
            ciphertext=b64url_encode(blob),
            context=dict(context),
            created_at=datetime.now(UTC),
            fingerprint=key_fingerprint(key),
        )

    def unwrap(self, wrapped: WrappedKey) -> bytes:
        if wrapped.provider != self.provider or wrapped.key_id != self._key_id:
            msg = "wrapped key was wrapped by a different KMS key"
            raise CryptoError(msg)
        material = self._box.decrypt(
            b64url_decode(wrapped.ciphertext), canonical_context(wrapped.context)
        )
        return _check_unwrapped(wrapped, self.provider, self._key_id, material)


class VaultTransitKms:
    """HashiCorp Vault Transit as the KMS (spec 8.4 "Vault Transit (envelope encryption)").

    Uses ``POST /v1/<mount>/encrypt/<key>`` and ``POST /v1/<mount>/decrypt/<key>`` with the
    canonical context as the Transit ``context`` (create the key with ``derived=true`` so Vault
    enforces it). The Vault token is read by the caller from the configured token file, never
    from the environment or a config value (spec 8.4). Vault is the customer's secret store,
    not a source system, so these POSTs are outside the read-only rule of spec 8.1.
    """

    ALGORITHM: Final = "vault-transit"

    __slots__ = ("_base_url", "_client", "_headers", "_key_id", "_key_name", "_mount")

    def __init__(
        self,
        base_url: str,
        key_name: str,
        token_value: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
        mount: str = "transit",
    ) -> None:
        if not base_url.startswith(("https://", "http://127.0.0.1")):
            msg = "Vault URL must use https (plain http is allowed for 127.0.0.1 only)"
            raise CryptoError(msg)
        if not key_name or "/" in key_name:
            msg = "Vault Transit key name must be a single path segment"
            raise CryptoError(msg)
        self._base_url = base_url.rstrip("/")
        self._key_name = key_name
        self._mount = mount.strip("/")
        self._headers = {"X-Vault-Token": token_value, "X-Vault-Request": "true"}
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)
        self._key_id = f"vault:{self._mount}/{key_name}"

    def __repr__(self) -> str:
        return f"VaultTransitKms(key_id={self._key_id!r})"

    @property
    def provider(self) -> KmsProvider:
        return "vault"

    @property
    def key_id(self) -> str:
        return self._key_id

    def _post(self, operation: str, payload: Mapping[str, str]) -> dict[str, object]:
        url = f"{self._base_url}/v1/{self._mount}/{operation}/{self._key_name}"
        try:
            response = self._client.post(url, json=dict(payload), headers=self._headers)
        except httpx.HTTPError as exc:
            msg = f"Vault Transit {operation} request failed: {type(exc).__name__}"
            raise CryptoError(msg) from exc
        if response.status_code != 200:
            msg = f"Vault Transit {operation} returned HTTP {response.status_code}"
            raise CryptoError(msg)
        try:
            body = response.json()
        except ValueError as exc:
            msg = f"Vault Transit {operation} returned a non-JSON body"
            raise CryptoError(msg) from exc
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            msg = f"Vault Transit {operation} response has no data object"
            raise CryptoError(msg)
        return data

    def wrap(self, material: bytes, context: Mapping[str, str]) -> WrappedKey:
        key = _require_key(material, "key to wrap")
        data = self._post(
            "encrypt",
            {
                "plaintext": base64.b64encode(key).decode("ascii"),
                "context": base64.b64encode(canonical_context(context)).decode("ascii"),
            },
        )
        ciphertext = data.get("ciphertext")
        if not isinstance(ciphertext, str) or not ciphertext.startswith("vault:"):
            msg = "Vault Transit encrypt returned no ciphertext"
            raise CryptoError(msg)
        return WrappedKey(
            provider=self.provider,
            key_id=self._key_id,
            algorithm=self.ALGORITHM,
            ciphertext=ciphertext,
            context=dict(context),
            created_at=datetime.now(UTC),
            fingerprint=key_fingerprint(key),
        )

    def unwrap(self, wrapped: WrappedKey) -> bytes:
        if wrapped.provider != self.provider or wrapped.key_id != self._key_id:
            msg = "wrapped key was wrapped by a different KMS key"
            raise CryptoError(msg)
        data = self._post(
            "decrypt",
            {
                "ciphertext": wrapped.ciphertext,
                "context": base64.b64encode(canonical_context(wrapped.context)).decode("ascii"),
            },
        )
        plaintext = data.get("plaintext")
        if not isinstance(plaintext, str):
            msg = "Vault Transit decrypt returned no plaintext"
            raise CryptoError(msg)
        try:
            material = base64.b64decode(plaintext, validate=True)
        except (ValueError, TypeError) as exc:
            msg = "Vault Transit decrypt returned malformed plaintext"
            raise CryptoError(msg) from exc
        return _check_unwrapped(
            wrapped, self.provider, self._key_id, _require_key(material, "unwrapped key")
        )


# ---------------------------------------------------------------------------------------------
# Ed25519 signatures (bundle signature, internal assertions)
# ---------------------------------------------------------------------------------------------


class VerifyKey:
    """An Ed25519 public key."""

    __slots__ = ("_key",)

    def __init__(self, key: ed25519.Ed25519PublicKey) -> None:
        self._key = key

    def __repr__(self) -> str:
        return f"VerifyKey({self.key_id!r})"

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        if len(raw) != ED25519_KEY_BYTES:
            msg = f"an Ed25519 public key is {ED25519_KEY_BYTES} bytes"
            raise CryptoError(msg)
        try:
            return cls(ed25519.Ed25519PublicKey.from_public_bytes(raw))
        except ValueError as exc:
            msg = "not an Ed25519 public key"
            raise CryptoError(msg) from exc

    @classmethod
    def from_text(cls, text: str) -> Self:
        return cls.from_bytes(b64url_decode(text))

    def to_bytes(self) -> bytes:
        return self._key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def to_text(self) -> str:
        return b64url_encode(self.to_bytes())

    @property
    def key_id(self) -> str:
        return key_fingerprint(self.to_bytes())

    def verify(self, signature: bytes, data: bytes) -> None:
        try:
            self._key.verify(signature, data)
        except InvalidSignature as exc:
            msg = "signature verification failed"
            raise CryptoError(msg) from exc


class SigningKey:
    """An Ed25519 private key. ``repr`` shows the public key id only."""

    __slots__ = ("_key", "_verify")

    def __init__(self, key: ed25519.Ed25519PrivateKey) -> None:
        self._key = key
        self._verify = VerifyKey(key.public_key())

    def __repr__(self) -> str:
        return f"SigningKey(public={self._verify.key_id!r})"

    @classmethod
    def generate(cls) -> Self:
        return cls(ed25519.Ed25519PrivateKey.generate())

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        if len(raw) != ED25519_KEY_BYTES:
            msg = f"an Ed25519 private key is {ED25519_KEY_BYTES} bytes"
            raise CryptoError(msg)
        return cls(ed25519.Ed25519PrivateKey.from_private_bytes(raw))

    def to_bytes(self) -> bytes:
        return self._key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )

    @property
    def verify_key(self) -> VerifyKey:
        return self._verify

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


# ---------------------------------------------------------------------------------------------
# Internal assertions (spec 8.4 reveal: "signed short-lived internal assertion carrying the
# user, permission and purpose")
# ---------------------------------------------------------------------------------------------


class InternalAssertion(BaseModel):
    """What core asserts to the edge when it asks for a reveal or a tokenization."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    subject: str = Field(min_length=1, max_length=256, description="User id or API key id.")
    permission: str = Field(min_length=1, max_length=64, description="e.g. reveal, search.")
    purpose: str = Field(max_length=512, description="Reason given by the user (audited).")
    audience: str = Field(min_length=1, max_length=64, description="e.g. edge-reveal.")
    issued_at: datetime
    expires_at: datetime
    nonce: str = Field(min_length=16, max_length=64)
    request_id: str = Field(default="", max_length=128)

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "assertion timestamps must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)


def sign_assertion(key: SigningKey, assertion: InternalAssertion) -> str:
    """``<b64url payload>.<b64url signature>`` over the JSON payload."""
    payload = assertion.model_dump_json().encode("utf-8")
    return f"{b64url_encode(payload)}.{b64url_encode(key.sign(payload))}"


def verify_assertion(
    key: VerifyKey,
    text: str,
    *,
    audience: str,
    now: datetime | None = None,
    max_ttl: timedelta = MAX_ASSERTION_TTL,
    clock_skew: timedelta = timedelta(seconds=30),
) -> InternalAssertion:
    """Verify signature, audience and validity window; return the assertion.

    Replay protection (one use per nonce) is the caller's job; this function is pure.
    """
    encoded_payload, separator, encoded_signature = text.partition(".")
    if not separator or not encoded_payload or not encoded_signature or "." in encoded_signature:
        msg = "malformed assertion"
        raise InvalidAssertionError(msg)
    try:
        payload = b64url_decode(encoded_payload)
        signature = b64url_decode(encoded_signature)
    except CryptoError as exc:
        msg = "malformed assertion"
        raise InvalidAssertionError(msg) from exc
    try:
        key.verify(signature, payload)
    except CryptoError as exc:
        msg = "assertion signature is invalid"
        raise InvalidAssertionError(msg) from exc
    try:
        assertion = InternalAssertion.model_validate_json(payload)
    except ValueError as exc:
        msg = "assertion payload is invalid"
        raise InvalidAssertionError(msg) from exc
    if assertion.audience != audience:
        msg = "assertion audience mismatch"
        raise InvalidAssertionError(msg)
    moment = now if now is not None else datetime.now(UTC)
    if assertion.expires_at <= assertion.issued_at:
        msg = "assertion expires before it was issued"
        raise InvalidAssertionError(msg)
    if assertion.expires_at - assertion.issued_at > max_ttl:
        msg = "assertion lifetime exceeds the maximum"
        raise InvalidAssertionError(msg)
    if assertion.issued_at - clock_skew > moment:
        msg = "assertion issued in the future"
        raise InvalidAssertionError(msg)
    if assertion.expires_at + clock_skew < moment:
        msg = "assertion has expired"
        raise InvalidAssertionError(msg)
    return assertion
