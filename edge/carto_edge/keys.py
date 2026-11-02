"""Edge key management: the tenant key, its rotation and the vault data key (spec 8.4).

Key material exists at rest only as a :class:`carto_common.crypto.WrappedKey` written by the
customer's KMS (:class:`carto_common.crypto.LocalKms` for Compose pilots, Vault Transit; ADR
0012) and is unwrapped into memory at startup. The layout under ``<state_dir>/keys/`` is shared
with ``carto-ctl key init``::

    local-kms.key          32 raw bytes, mode 0600 (LocalKms master key; kms.provider=local only)
    tenant-key.v<n>.json   WrappedKey of tenant key version n (the active one and the previous
                           ones still inside the overlap window)
                           context {"tenant_id": ..., "purpose": "tokenization", "version": "<n>"}
    tenant-key.json        Version 1 as ``key init`` writes it (the layout before ADR 0042); read
                           for its version until the first rotation copies it to
                           tenant-key.v<n>.json and removes it
    rotation.json          {"active_version": n, "previous": [{"version": m, "retire_at": iso}],
                            "overlap_days": d}: the one commit point of a rotation
    vault-data-key.json    WrappedKey of the reveal vault data key
                           context {"tenant_id": ..., "purpose": "reveal-vault"}
    assertion-public.key   core's Ed25519 public key (base64url text) for internal assertions

Rotation (spec 8.4): a new version is the active version plus one; the old active key stays in
the keyring as a previous version until ``retire_at`` (now plus the overlap, default 30 days
and at least the event retention) so new events are tokenized under both. Previous keys past
``retire_at`` are ignored with a warning on load and removed on the next rotation.

Crash consistency (ADR 0042): ``rotation.json`` is the single commit point. A rotation first
writes the new key as ``tenant-key.v<n+1>.json`` (and, once, copies a legacy ``tenant-key.json``
to its versioned name), then replaces ``rotation.json``; only then are superseded files removed.
Loads resolve every key file by the version ``rotation.json`` names, so a crash at any step
leaves the old keyring or the new one, never a mismatch: a key file no state names is ignored
and overwritten by the next rotation. Every file is written to a temporary file created 0600,
fsynced, renamed and its directory fsynced (:func:`carto_common.crypto.write_private_file`).

When the KMS key itself rotates, :meth:`KeyManager.rewrap` unwraps every wrapped key file under
``keys/`` (tenant keys of every version, the vault data key, the local secret store data key)
and wraps the same material under the new KMS key with the same context, in two phases: every
file is re-wrapped into a staging file first and the staging files replace the originals only
when all succeeded, so a KMS failure half way leaves the old set intact. Tokens do not change.

Nothing here logs or returns key material; :meth:`KeyManager.status` reports versions,
fingerprints and dates only (spec 2.3 invariant 7).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from carto_common.crypto import (
    CryptoError,
    Keyring,
    KeyWrapper,
    LocalKms,
    TokenKey,
    VaultTransitKms,
    VerifyKey,
    WrappedKey,
    fsync_directory,
    generate_key,
    write_private_file,
)
from carto_common.logging import get_logger
from carto_edge.config import EdgeSettings

__all__ = [
    "ASSERTION_PUBLIC_KEY_FILE",
    "DEFAULT_OVERLAP_DAYS",
    "LOCAL_KMS_KEY_FILE",
    "PURPOSE_REVEAL_VAULT",
    "PURPOSE_TOKENIZATION",
    "ROTATION_FILE",
    "TENANT_KEY_FILE",
    "VAULT_DATA_KEY_FILE",
    "KeyManagementError",
    "KeyManager",
    "PreviousKey",
    "RotationState",
    "build_kms",
    "previous_key_file",
    "versioned_key_file",
]

LOCAL_KMS_KEY_FILE: Final = "local-kms.key"
TENANT_KEY_FILE: Final = "tenant-key.json"
ROTATION_FILE: Final = "rotation.json"
VAULT_DATA_KEY_FILE: Final = "vault-data-key.json"
ASSERTION_PUBLIC_KEY_FILE: Final = "assertion-public.key"
DEFAULT_OVERLAP_DAYS: Final = 30
PURPOSE_TOKENIZATION: Final = "tokenization"
PURPOSE_REVEAL_VAULT: Final = "reveal-vault"
INIT_HINT: Final = "run `carto-ctl key init`"

log = get_logger(component="keys")


class KeyManagementError(Exception):
    """The key state on disk is missing, inconsistent or would be overwritten. The message
    names files and versions, never material."""


class PreviousKey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    version: int = Field(ge=1)
    retire_at: datetime

    @field_validator("retire_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            msg = "retire_at must be timezone-aware"
            raise ValueError(msg)
        return value.astimezone(UTC)


class RotationState(BaseModel):
    """``rotation.json``: which version is active and which previous ones still tokenize."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    active_version: int = Field(ge=1)
    previous: list[PreviousKey] = Field(default_factory=list)
    overlap_days: int = Field(default=DEFAULT_OVERLAP_DAYS, ge=1)

    @classmethod
    def read(cls, path: Path) -> RotationState:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"cannot read key rotation state {path}"
            raise KeyManagementError(msg) from exc
        try:
            return cls.model_validate_json(text)
        except ValueError as exc:
            msg = f"key rotation state {path} is not valid"
            raise KeyManagementError(msg) from exc

    def write(self, path: Path) -> None:
        _write_atomic(path, self.model_dump_json(indent=2) + "\n")


def versioned_key_file(keys_dir: Path, version: int) -> Path:
    """``tenant-key.v<n>.json``: tenant key version ``n``, active or previous."""
    return keys_dir / f"tenant-key.v{version}.json"


previous_key_file = versioned_key_file
"""The name before every version, the active one included, had a versioned file."""


def _write_atomic(path: Path, text: str) -> None:
    write_private_file(path, text.encode("utf-8"))


def _write_wrapped(path: Path, wrapped: WrappedKey) -> None:
    _write_atomic(path, wrapped.model_dump_json(indent=2) + "\n")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def build_kms(settings: EdgeSettings) -> KeyWrapper:
    """The configured KMS: the local master key file or Vault Transit (ADR 0012).

    The Vault token is read from ``kms.vault_token_file``, never from the environment (spec
    8.4). The message of a failure names the file, never its content.
    """
    kms = settings.kms
    if kms.provider == "local":
        path = settings.local_kms_key_file
        if not path.is_file():
            msg = f"local KMS master key {path} not found; {INIT_HINT}"
            raise KeyManagementError(msg)
        try:
            return LocalKms.open(path)
        except CryptoError as exc:
            msg = f"local KMS master key {path} cannot be opened"
            raise KeyManagementError(msg) from exc
    token_file = kms.vault_token_file
    if kms.vault_url is None or token_file is None:  # KmsSettings validates; keep mypy honest
        msg = "kms.vault_url and kms.vault_token_file are required for the vault provider"
        raise KeyManagementError(msg)
    try:
        token_value = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        msg = f"cannot read the Vault token file {token_file}"
        raise KeyManagementError(msg) from exc
    if not token_value:
        msg = f"the Vault token file {token_file} is empty"
        raise KeyManagementError(msg)
    try:
        return VaultTransitKms(
            kms.vault_url, kms.vault_transit_key, token_value, mount=kms.vault_mount
        )
    except CryptoError as exc:
        msg = "Vault Transit settings are invalid"
        raise KeyManagementError(msg) from exc


class KeyManager:
    """Creates, loads and rotates the key files of one edge installation."""

    __slots__ = ("_clock", "_kms", "_settings")

    def __init__(
        self,
        settings: EdgeSettings,
        kms: KeyWrapper | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._kms = kms
        self._clock = clock if clock is not None else _utc_now

    def __repr__(self) -> str:
        return f"KeyManager(keys_dir={str(self._settings.keys_dir)!r})"

    @property
    def kms(self) -> KeyWrapper:
        if self._kms is None:
            self._kms = build_kms(self._settings)
        return self._kms

    @property
    def keys_dir(self) -> Path:
        return self._settings.keys_dir

    @property
    def _rotation_file(self) -> Path:
        return self.keys_dir / ROTATION_FILE

    @property
    def _tenant_key_file(self) -> Path:
        return self._settings.tenant_key_file

    @property
    def _vault_data_key_file(self) -> Path:
        return self.keys_dir / VAULT_DATA_KEY_FILE

    def _tokenization_context(self, version: int) -> dict[str, str]:
        return {
            "tenant_id": self._settings.tenant_id,
            "purpose": PURPOSE_TOKENIZATION,
            "version": str(version),
        }

    def _default_overlap_days(self) -> int:
        return max(DEFAULT_OVERLAP_DAYS, self._settings.retention.events_days)

    # -----------------------------------------------------------------------------------------
    # init
    # -----------------------------------------------------------------------------------------

    def init(self, *, create_local_kms: bool = False) -> dict[str, object]:
        """Create version 1 of the tenant key, the rotation state and the vault data key.

        With ``create_local_kms`` the local master key file is created first (local provider
        only). Refuses to overwrite any existing key file. Returns :meth:`status`.
        """
        for path in (self._tenant_key_file, self._rotation_file):
            if path.exists():
                msg = f"key file {path} already exists; refusing to overwrite"
                raise KeyManagementError(msg)
        if create_local_kms:
            if self._settings.kms.provider != "local":
                msg = "create_local_kms applies to the local KMS provider only"
                raise KeyManagementError(msg)
            try:
                self._kms = LocalKms.create(self._settings.local_kms_key_file)
            except CryptoError as exc:
                msg = f"local KMS master key {self._settings.local_kms_key_file} already exists"
                raise KeyManagementError(msg) from exc
        kms = self.kms
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        wrapped = kms.wrap(generate_key(), self._tokenization_context(1))
        _write_wrapped(self._tenant_key_file, wrapped)
        RotationState(
            active_version=1, previous=[], overlap_days=self._default_overlap_days()
        ).write(self._rotation_file)
        self._ensure_vault_data_key()
        log.info(
            "keys.initialized",
            provider=kms.provider,
            kms_key_id=kms.key_id,
            active_version=1,
            fingerprint=wrapped.fingerprint,
        )
        return self.status()

    # -----------------------------------------------------------------------------------------
    # load
    # -----------------------------------------------------------------------------------------

    def _read_rotation(self) -> RotationState:
        if not self._rotation_file.is_file():
            msg = f"no key state in {self.keys_dir}; {INIT_HINT}"
            raise KeyManagementError(msg)
        return RotationState.read(self._rotation_file)

    def _key_path(self, version: int) -> Path:
        """The file holding ``version``: its versioned file, else the legacy
        ``tenant-key.json`` when that is the version it holds."""
        versioned = versioned_key_file(self.keys_dir, version)
        if versioned.is_file() or not self._tenant_key_file.is_file():
            return versioned
        return self._tenant_key_file

    def _read_version(self, version: int) -> WrappedKey:
        path = self._key_path(version)
        if not path.is_file():
            msg = f"key file for version {version} is missing ({path.name}); restore keys/"
            raise KeyManagementError(msg)
        wrapped = WrappedKey.read(path)
        if wrapped.context.get("version") != str(version):
            msg = (
                f"key file {path} carries version {wrapped.context.get('version')!r} but "
                f"{ROTATION_FILE} names version {version}"
            )
            raise KeyManagementError(msg)
        return wrapped

    def _read_active(self, rotation: RotationState) -> WrappedKey:
        return self._read_version(rotation.active_version)

    def load_keyring(self) -> Keyring:
        """The active key plus every previous version whose ``retire_at`` is still ahead.

        Expired previous keys are skipped with a warning; their files are left for the next
        :meth:`rotate` to remove.
        """
        rotation = self._read_rotation()
        kms = self.kms
        active = TokenKey(rotation.active_version, kms.unwrap(self._read_active(rotation)))
        now = self._clock()
        previous: list[TokenKey] = []
        for entry in rotation.previous:
            if entry.retire_at <= now:
                log.warning(
                    "keys.previous_expired",
                    version=entry.version,
                    retire_at=entry.retire_at.isoformat(),
                )
                continue
            wrapped = self._read_version(entry.version)
            previous.append(TokenKey(entry.version, kms.unwrap(wrapped)))
        return Keyring(active=active, previous=tuple(previous))

    # -----------------------------------------------------------------------------------------
    # rotate
    # -----------------------------------------------------------------------------------------

    def rotate(self, overlap_days: int | None = None) -> Keyring:
        """Create the next key version; the old active key stays live for ``overlap_days``.

        Order (module docstring): a legacy ``tenant-key.json`` copied to its versioned name
        once, the new key as ``tenant-key.v<n+1>.json``, then ``rotation.json`` (the commit),
        then the files nothing names any more are removed: previous versions past
        ``retire_at`` and the legacy file.
        """
        rotation = self._read_rotation()
        old_wrapped = self._read_active(rotation)
        kms = self.kms
        overlap = overlap_days if overlap_days is not None else self._default_overlap_days()
        if overlap < 1:
            msg = "overlap_days must be at least 1"
            raise KeyManagementError(msg)
        now = self._clock()
        new_version = rotation.active_version + 1
        new_wrapped = kms.wrap(generate_key(), self._tokenization_context(new_version))

        self._migrate_legacy(rotation)
        _write_wrapped(versioned_key_file(self.keys_dir, new_version), new_wrapped)
        kept = [entry for entry in rotation.previous if entry.retire_at > now]
        retired = [entry.version for entry in rotation.previous if entry.retire_at <= now]
        previous = [
            PreviousKey(version=rotation.active_version, retire_at=now + timedelta(days=overlap)),
            *kept,
        ]
        RotationState(active_version=new_version, previous=previous, overlap_days=overlap).write(
            self._rotation_file
        )
        self._remove_superseded(retired)
        del old_wrapped
        log.info(
            "keys.rotated",
            active_version=new_version,
            previous_versions=[entry.version for entry in previous],
            overlap_days=overlap,
            fingerprint=new_wrapped.fingerprint,
        )
        return self.load_keyring()

    def _migrate_legacy(self, rotation: RotationState) -> None:
        """Copy a legacy ``tenant-key.json`` to the versioned name of the version it holds,
        for every version the state still names, before the commit."""
        legacy = self._tenant_key_file
        if not legacy.is_file():
            return
        wrapped = WrappedKey.read(legacy)
        version_text = wrapped.context.get("version", "")
        named = {rotation.active_version, *(entry.version for entry in rotation.previous)}
        if not version_text.isdecimal() or int(version_text) not in named:
            return
        target = versioned_key_file(self.keys_dir, int(version_text))
        if not target.is_file():
            _write_wrapped(target, wrapped)

    def _remove_superseded(self, retired: list[int]) -> None:
        """After the commit: files of retired versions and a legacy ``tenant-key.json`` whose
        version now has its own file. Failures leave harmless files behind."""
        doomed = [versioned_key_file(self.keys_dir, version) for version in retired]
        legacy = self._tenant_key_file
        if legacy.is_file():
            with contextlib.suppress(CryptoError):
                version_text = WrappedKey.read(legacy).context.get("version", "")
                if (
                    version_text.isdecimal()
                    and versioned_key_file(self.keys_dir, int(version_text)).is_file()
                ):
                    doomed.append(legacy)
        removed = False
        for path in doomed:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
                removed = True
        if removed:
            with contextlib.suppress(OSError):
                fsync_directory(self.keys_dir)

    # -----------------------------------------------------------------------------------------
    # rewrap (the KMS key rotates; the tenant key does not)
    # -----------------------------------------------------------------------------------------

    def wrapped_key_files(self) -> list[Path]:
        """Every :class:`WrappedKey` file under ``keys/``, sorted (``rotation.json`` excluded)."""
        if not self.keys_dir.is_dir():
            return []
        return sorted(
            path
            for path in self.keys_dir.glob("*.json")
            if path.name != ROTATION_FILE and path.is_file()
        )

    def rewrap(self, new_kms: KeyWrapper) -> list[Path]:
        """Re-wrap every wrapped key file with ``new_kms``; returns the files rewritten.

        The material and the context of each key stay the same, which the fingerprint check
        confirms before anything is replaced. ``new_kms`` may be the current KMS object when the
        KMS rotated its own key version (Vault Transit wraps with the latest version)."""
        old = self.kms
        files = self.wrapped_key_files()
        if not files:
            msg = f"no wrapped key files in {self.keys_dir}; {INIT_HINT}"
            raise KeyManagementError(msg)
        staged: list[tuple[Path, Path]] = []
        try:
            for path in files:
                wrapped = WrappedKey.read(path)
                fresh = new_kms.wrap(old.unwrap(wrapped), wrapped.context)
                if fresh.fingerprint != wrapped.fingerprint:
                    msg = f"re-wrapping {path.name} changed its fingerprint"
                    raise KeyManagementError(msg)
                staging = path.with_name(path.name + ".rewrap")
                _write_wrapped(staging, fresh)
                staged.append((staging, path))
        except (CryptoError, KeyManagementError, OSError) as exc:
            for staging, _path in staged:
                staging.unlink(missing_ok=True)
            if isinstance(exc, KeyManagementError):
                raise
            msg = "re-wrapping failed; the existing key files are unchanged"
            raise KeyManagementError(msg) from exc
        for staging, path in staged:
            staging.replace(path)
        fsync_directory(self.keys_dir)
        self._kms = new_kms
        log.info(
            "keys.rewrapped",
            files=[path.name for _staging, path in staged],
            old_kms_key_id=old.key_id,
            new_kms_key_id=new_kms.key_id,
        )
        return [path for _staging, path in staged]

    # -----------------------------------------------------------------------------------------
    # vault data key, assertion key, status
    # -----------------------------------------------------------------------------------------

    def _ensure_vault_data_key(self) -> WrappedKey:
        path = self._vault_data_key_file
        if path.is_file():
            return WrappedKey.read(path)
        wrapped = self.kms.wrap(
            generate_key(),
            {"tenant_id": self._settings.tenant_id, "purpose": PURPOSE_REVEAL_VAULT},
        )
        _write_wrapped(path, wrapped)
        log.info("keys.vault_data_key_created", fingerprint=wrapped.fingerprint)
        return wrapped

    def vault_data_key(self) -> bytes:
        """The reveal vault data key (spec 8.4), created and wrapped on first use."""
        return self.kms.unwrap(self._ensure_vault_data_key())

    def _assertion_key_file(self) -> Path:
        configured = self._settings.reveal.assertion_public_key_file
        return configured if configured is not None else self.keys_dir / ASSERTION_PUBLIC_KEY_FILE

    def load_verify_key(self) -> VerifyKey | None:
        """Core's Ed25519 public key for internal assertions, or ``None`` when not installed
        (the reveal and tokenize endpoints then answer 503)."""
        path = self._assertion_key_file()
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            msg = f"cannot read the assertion public key file {path}"
            raise KeyManagementError(msg) from exc
        try:
            return VerifyKey.from_text(text)
        except CryptoError as exc:
            msg = f"assertion public key file {path} is not an Ed25519 public key"
            raise KeyManagementError(msg) from exc

    def status(self) -> dict[str, object]:
        """Versions, fingerprints, KMS key id and retire dates. Never key material."""
        kms = self.kms
        active_version: int | None = None
        active_fingerprint: str | None = None
        overlap_days: int | None = None
        previous: list[dict[str, object]] = []
        if self._rotation_file.is_file():
            rotation = self._read_rotation()
            active_version = rotation.active_version
            overlap_days = rotation.overlap_days
            active_path = self._key_path(rotation.active_version)
            if active_path.is_file():
                active_fingerprint = WrappedKey.read(active_path).fingerprint
            for entry in rotation.previous:
                path = self._key_path(entry.version)
                previous.append(
                    {
                        "version": entry.version,
                        "fingerprint": WrappedKey.read(path).fingerprint
                        if path.is_file()
                        else None,
                        "retire_at": entry.retire_at.isoformat(),
                    }
                )
        vault_fingerprint = (
            WrappedKey.read(self._vault_data_key_file).fingerprint
            if self._vault_data_key_file.is_file()
            else None
        )
        verify_key = self.load_verify_key()
        return {
            "keys_dir": str(self.keys_dir),
            "provider": kms.provider,
            "kms_key_id": kms.key_id,
            "active_version": active_version,
            "active_fingerprint": active_fingerprint,
            "previous": previous,
            "overlap_days": overlap_days,
            "vault_data_key": vault_fingerprint,
            "assertion_public_key": verify_key.key_id if verify_key is not None else None,
        }
