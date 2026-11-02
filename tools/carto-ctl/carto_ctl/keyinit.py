"""``carto-ctl key init``: the tenant tokenization key, wrapped by the customer KMS (spec 8.4).

Creates, under ``<state-dir>/keys/``, exactly what the edge ``KeyManager`` reads at startup:

- ``local-kms.key`` (``--kms local`` only): the 32-byte local KMS master key, owner-only. It is
  the one secret the operator must protect and back up (spec 14.11).
- ``tenant-key.json``: a :class:`~carto_common.crypto.WrappedKey` of the version-1 tenant key,
  bound to ``{"tenant_id", "purpose": "tokenization", "version": "1"}``.
- ``rotation.json``: ``{"active_version": 1, "previous": [], "overlap_days": 30}`` (spec 8.4
  "Rotation", default overlap 30 days).

``--kms vault`` wraps through Vault Transit; the token is read from ``--vault-token-file``,
never from an option or the environment (spec 8.4 "No key material in environment variables"),
and Vault's certificate is verified against ``--vault-ca-file`` alone, never the system trust
store or ``SSL_CERT_FILE`` (ADR 0070 section 7).
Existing key files are never overwritten. With ``--if-missing`` a complete earlier run (the
tenant key, the rotation state and, for ``--kms local``, the master key all present) is a
success that writes nothing, which is what the Compose ``key-init`` job needs on its second
start; a partial one is still refused. The output names key ids and fingerprints only: no key
material, in any encoding, reaches a terminal or a log.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from carto_common.crypto import (
    CryptoError,
    KeyWrapper,
    LocalKms,
    VaultTransitKms,
    generate_key,
)
from carto_common.settings import TENANT_ID_REGEX
from carto_ctl.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE

if TYPE_CHECKING:
    from carto_ctl.registry import Invocation

__all__ = [
    "DEFAULT_OVERLAP_DAYS",
    "DEFAULT_STATE_DIR",
    "KEYS_DIR_NAME",
    "LOCAL_KMS_FILE",
    "ROTATION_FILE",
    "TENANT_KEY_FILE",
    "TOKENIZATION_PURPOSE",
    "KeyInitOptions",
    "KeyInitResult",
    "configure_key_init",
    "initialize_keys",
    "key_init",
    "keys_complete",
]

DEFAULT_STATE_DIR: Final = Path("/var/lib/carto-edge")
"""Matches ``carto_edge.config.EdgeSettings.state_dir``."""
KEYS_DIR_NAME: Final = "keys"
LOCAL_KMS_FILE: Final = "local-kms.key"
TENANT_KEY_FILE: Final = "tenant-key.json"
ROTATION_FILE: Final = "rotation.json"
DEFAULT_OVERLAP_DAYS: Final = 30
TOKENIZATION_PURPOSE: Final = "tokenization"
MAX_TOKEN_FILE_BYTES: Final = 4096
_TENANT_PATTERN: Final = re.compile(TENANT_ID_REGEX)

KmsName = Literal["local", "vault"]


@dataclass(frozen=True, slots=True)
class KeyInitOptions:
    state_dir: Path
    tenant_id: str
    kms: KmsName
    vault_url: str | None = None
    vault_transit_key: str = "carto"
    vault_mount: str = "transit"
    vault_token_file: Path | None = None
    vault_ca_file: Path | None = None

    @property
    def keys_dir(self) -> Path:
        return self.state_dir / KEYS_DIR_NAME

    @property
    def local_kms_file(self) -> Path:
        return self.keys_dir / LOCAL_KMS_FILE

    @property
    def tenant_key_file(self) -> Path:
        return self.keys_dir / TENANT_KEY_FILE

    @property
    def rotation_file(self) -> Path:
        return self.keys_dir / ROTATION_FILE


@dataclass(frozen=True, slots=True)
class KeyInitResult:
    kms_provider: str
    kms_key_id: str
    tenant_id: str
    version: int
    fingerprint: str
    tenant_key_file: Path
    rotation_file: Path
    local_kms_file: Path | None


def _tenant_id(text: str) -> str:
    if not _TENANT_PATTERN.match(text):
        msg = f"tenant id must match {TENANT_ID_REGEX}"
        raise argparse.ArgumentTypeError(msg)
    return text


def configure_key_init(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Edge state directory; keys go under <state-dir>/keys (default {DEFAULT_STATE_DIR})",
    )
    parser.add_argument("--tenant-id", type=_tenant_id, default="default")
    parser.add_argument(
        "--kms",
        choices=("local", "vault"),
        default="local",
        help="Which KMS wraps the key: a local master key file or Vault Transit (ADR 0012)",
    )
    parser.add_argument(
        "--if-missing",
        action="store_true",
        help="Succeed without writing when a complete earlier run is present (Compose jobs)",
    )
    parser.add_argument("--vault-url", metavar="URL", help="Vault address (https)")
    parser.add_argument("--vault-transit-key", default="carto", metavar="NAME")
    parser.add_argument("--vault-mount", default="transit", metavar="PATH")
    parser.add_argument(
        "--vault-token-file",
        type=Path,
        metavar="FILE",
        help="File holding the Vault token (never an option value or an environment variable)",
    )
    parser.add_argument(
        "--vault-ca-file",
        type=Path,
        metavar="FILE",
        help="CA bundle that alone verifies Vault's certificate (required with --kms vault)",
    )


def _read_token_file(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_TOKEN_FILE_BYTES + 1)
    except OSError as exc:
        msg = f"cannot read Vault token file {path}"
        raise CryptoError(msg) from exc
    if len(data) > MAX_TOKEN_FILE_BYTES:
        msg = f"Vault token file {path} is larger than {MAX_TOKEN_FILE_BYTES} bytes"
        raise CryptoError(msg)
    token_value = data.decode("utf-8", errors="strict").strip()
    if not token_value:
        msg = f"Vault token file {path} is empty"
        raise CryptoError(msg)
    return token_value


def _build_kms(options: KeyInitOptions) -> tuple[KeyWrapper, Path | None]:
    if options.kms == "local":
        return LocalKms.create(options.local_kms_file), options.local_kms_file
    if (
        options.vault_url is None
        or options.vault_token_file is None
        or options.vault_ca_file is None
    ):
        msg = "the vault KMS needs --vault-url, --vault-token-file and --vault-ca-file"
        raise CryptoError(msg)
    return (
        VaultTransitKms(
            options.vault_url,
            options.vault_transit_key,
            _read_token_file(options.vault_token_file),
            mount=options.vault_mount,
            ca_file=options.vault_ca_file,
        ),
        None,
    )


def keys_complete(options: KeyInitOptions) -> bool:
    """True when every file :func:`initialize_keys` would write for ``options`` exists."""
    planned = [options.tenant_key_file, options.rotation_file]
    if options.kms == "local":
        planned.append(options.local_kms_file)
    return all(path.is_file() for path in planned)


def initialize_keys(options: KeyInitOptions, *, kms: KeyWrapper | None = None) -> KeyInitResult:
    """Create the key files; refuse when any of them exists. ``kms`` overrides the options."""
    planned = [options.tenant_key_file, options.rotation_file]
    if options.kms == "local" and kms is None:
        planned.append(options.local_kms_file)
    existing = [str(path) for path in planned if path.exists()]
    if existing:
        msg = f"refusing to overwrite existing key files: {', '.join(existing)}"
        raise CryptoError(msg)
    options.keys_dir.mkdir(parents=True, exist_ok=True)
    local_file: Path | None = None
    if kms is None:
        kms, local_file = _build_kms(options)
    context = {"tenant_id": options.tenant_id, "purpose": TOKENIZATION_PURPOSE, "version": "1"}
    wrapped = kms.wrap(generate_key(), context)
    wrapped.write(options.tenant_key_file)
    options.tenant_key_file.chmod(0o600)
    rotation = {"active_version": 1, "previous": [], "overlap_days": DEFAULT_OVERLAP_DAYS}
    options.rotation_file.write_text(
        json.dumps(rotation, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return KeyInitResult(
        kms_provider=kms.provider,
        kms_key_id=kms.key_id,
        tenant_id=options.tenant_id,
        version=1,
        fingerprint=wrapped.fingerprint,
        tenant_key_file=options.tenant_key_file,
        rotation_file=options.rotation_file,
        local_kms_file=local_file,
    )


def key_init(invocation: Invocation) -> int:
    args = invocation.args
    if args.kms == "vault" and (
        args.vault_url is None or args.vault_token_file is None or args.vault_ca_file is None
    ):
        invocation.err.write(
            "carto-ctl key init: --vault-url, --vault-token-file and --vault-ca-file are "
            "required with --kms vault\n"
        )
        return EXIT_USAGE
    options = KeyInitOptions(
        state_dir=args.state_dir,
        tenant_id=args.tenant_id,
        kms=args.kms,
        vault_url=args.vault_url,
        vault_transit_key=args.vault_transit_key,
        vault_mount=args.vault_mount,
        vault_token_file=args.vault_token_file,
        vault_ca_file=args.vault_ca_file,
    )
    if args.if_missing and keys_complete(options):
        invocation.out.write(
            f"tenant key already present in {options.keys_dir}; nothing written.\n"
        )
        return EXIT_OK
    try:
        result = initialize_keys(options)
    except (CryptoError, OSError) as exc:
        invocation.err.write(f"carto-ctl key init: {exc}\n")
        return EXIT_FAILURE
    out = invocation.out
    out.write(f"kms: {result.kms_provider} (key id {result.kms_key_id})\n")
    if result.local_kms_file is not None:
        out.write(f"master key: {result.local_kms_file} (owner-only; back it up, spec 14.11)\n")
    out.write(
        f"tenant key: tenant {result.tenant_id}, version {result.version}, fingerprint "
        f"{result.fingerprint}, wrapped at {result.tenant_key_file}\n"
    )
    out.write(
        f"rotation: active version {result.version}, overlap {DEFAULT_OVERLAP_DAYS} days, "
        f"{result.rotation_file}\n"
    )
    return EXIT_OK
