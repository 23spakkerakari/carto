"""carto_edge.keys: the key file layout, init/load/rotate through the local KMS and Vault
Transit, expired previous keys, the vault data key and the status view (spec 8.4)."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from carto_common.crypto import (
    KEY_BYTES,
    CryptoError,
    Keyring,
    LocalKms,
    SigningKey,
    VaultTransitKms,
    WrappedKey,
    canonical_context,
    token,
)
from carto_common.settings import RetentionSettings
from carto_edge.config import EdgeSettings, KmsSettings, RevealSettings
from carto_edge.keys import (
    ASSERTION_PUBLIC_KEY_FILE,
    LOCAL_KMS_KEY_FILE,
    ROTATION_FILE,
    VAULT_DATA_KEY_FILE,
    KeyManagementError,
    KeyManager,
    RotationState,
    build_kms,
    previous_key_file,
    versioned_key_file,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def settings(tmp_path: Path) -> EdgeSettings:
    return EdgeSettings(state_dir=tmp_path / "state", tenant_id="acme")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def manager(settings: EdgeSettings, clock: FakeClock) -> KeyManager:
    manager = KeyManager(settings, clock=clock)
    manager.init(create_local_kms=True)
    return manager


def read_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


# ---------------------------------------------------------------------------------------------
# init: the file layout
# ---------------------------------------------------------------------------------------------


def test_init_creates_the_key_file_layout(settings: EdgeSettings, manager: KeyManager) -> None:
    keys = settings.keys_dir
    assert keys == settings.state_dir / "keys"
    assert (keys / LOCAL_KMS_KEY_FILE) == settings.local_kms_key_file
    assert (keys / LOCAL_KMS_KEY_FILE).read_bytes().__len__() == KEY_BYTES
    if os.name != "nt":
        assert stat.S_IMODE((keys / LOCAL_KMS_KEY_FILE).stat().st_mode) == 0o600

    tenant = WrappedKey.read(settings.tenant_key_file)
    assert settings.tenant_key_file == keys / "tenant-key.json"
    assert tenant.provider == "local"
    assert tenant.context == {"tenant_id": "acme", "purpose": "tokenization", "version": "1"}

    rotation = read_json(keys / ROTATION_FILE)
    assert rotation == {"active_version": 1, "previous": [], "overlap_days": 30}

    vault_key = WrappedKey.read(keys / VAULT_DATA_KEY_FILE)
    assert vault_key.context == {"tenant_id": "acme", "purpose": "reveal-vault"}
    assert not list(keys.glob("tenant-key.v*.json"))


def test_init_refuses_to_overwrite(settings: EdgeSettings, manager: KeyManager) -> None:
    before = settings.tenant_key_file.read_bytes()
    with pytest.raises(KeyManagementError, match="exist"):
        manager.init(create_local_kms=True)
    with pytest.raises(KeyManagementError, match="exist"):
        KeyManager(settings).init(create_local_kms=False)
    assert settings.tenant_key_file.read_bytes() == before


def test_init_without_local_kms_needs_the_master_key_file(settings: EdgeSettings) -> None:
    with pytest.raises(KeyManagementError, match="local KMS"):
        KeyManager(settings).init(create_local_kms=False)


def test_init_with_an_existing_master_key(settings: EdgeSettings) -> None:
    LocalKms.create(settings.local_kms_key_file)
    manager = KeyManager(settings)
    manager.init(create_local_kms=False)
    assert manager.load_keyring().active.version == 1


def test_key_files_never_hold_key_material(settings: EdgeSettings, manager: KeyManager) -> None:
    keyring = manager.load_keyring()
    material = keyring.active.material
    for path in settings.keys_dir.iterdir():
        if path.name == LOCAL_KMS_KEY_FILE:
            continue
        text = path.read_text(encoding="utf-8")
        assert material.hex() not in text
        assert base64.b64encode(material).decode() not in text
        assert base64.urlsafe_b64encode(material).rstrip(b"=").decode() not in text


# ---------------------------------------------------------------------------------------------
# load and rotate
# ---------------------------------------------------------------------------------------------


def test_load_keyring_round_trip(settings: EdgeSettings, manager: KeyManager) -> None:
    keyring = manager.load_keyring()
    assert isinstance(keyring, Keyring)
    assert keyring.active.version == 1
    assert keyring.previous == ()
    wrapped = WrappedKey.read(settings.tenant_key_file)
    assert keyring.active.fingerprint == wrapped.fingerprint
    again = KeyManager(settings).load_keyring()
    assert again.active.material == keyring.active.material


def test_load_keyring_before_init_fails_clearly(settings: EdgeSettings) -> None:
    LocalKms.create(settings.local_kms_key_file)
    with pytest.raises(KeyManagementError, match="key init"):
        KeyManager(settings).load_keyring()


def test_rotate_creates_version_two_and_keeps_one_in_overlap(
    settings: EdgeSettings, manager: KeyManager, clock: FakeClock
) -> None:
    before = manager.load_keyring()
    old_token = before.active.token("id", "SO-0004471")

    keyring = manager.rotate(overlap_days=10)
    assert keyring.active.version == 2
    assert [key.version for key in keyring.previous] == [1]
    assert keyring.previous[0].material == before.active.material
    assert keyring.previous[0].token("id", "SO-0004471") == old_token
    assert keyring.active.token("id", "SO-0004471") != old_token

    active = WrappedKey.read(versioned_key_file(settings.keys_dir, 2))
    assert active.context["version"] == "2"
    assert not settings.tenant_key_file.exists()  # the legacy name is migrated away
    previous = WrappedKey.read(previous_key_file(settings.keys_dir, 1))
    assert previous_key_file(settings.keys_dir, 1) == settings.keys_dir / "tenant-key.v1.json"
    assert previous.context["version"] == "1"
    assert previous.fingerprint == before.active.fingerprint

    rotation = read_json(settings.keys_dir / ROTATION_FILE)
    assert rotation["active_version"] == 2
    assert rotation["overlap_days"] == 10
    previous_entries = rotation["previous"]
    assert isinstance(previous_entries, list) and len(previous_entries) == 1
    entry = previous_entries[0]
    assert isinstance(entry, dict) and entry["version"] == 1
    assert datetime.fromisoformat(str(entry["retire_at"])) == clock.now + timedelta(days=10)

    reloaded = KeyManager(settings, clock=clock).load_keyring()
    assert reloaded == keyring


def test_rotate_twice_keeps_both_previous_versions_until_they_retire(
    settings: EdgeSettings, manager: KeyManager, clock: FakeClock
) -> None:
    manager.rotate(overlap_days=10)
    clock.now += timedelta(days=5)
    keyring = manager.rotate(overlap_days=10)
    assert keyring.active.version == 3
    assert [key.version for key in keyring.previous] == [2, 1]
    assert (settings.keys_dir / "tenant-key.v1.json").exists()
    assert (settings.keys_dir / "tenant-key.v2.json").exists()


def test_expired_previous_keys_are_ignored_on_load(
    settings: EdgeSettings, manager: KeyManager, clock: FakeClock
) -> None:
    manager.rotate(overlap_days=10)
    clock.now += timedelta(days=10, seconds=1)
    keyring = manager.load_keyring()
    assert keyring.active.version == 2
    assert keyring.previous == ()


def test_rotate_prunes_expired_previous_keys(
    settings: EdgeSettings, manager: KeyManager, clock: FakeClock
) -> None:
    manager.rotate(overlap_days=10)
    clock.now += timedelta(days=11)
    keyring = manager.rotate(overlap_days=10)
    assert keyring.active.version == 3
    assert [key.version for key in keyring.previous] == [2]
    rotation = RotationState.model_validate_json(
        (settings.keys_dir / ROTATION_FILE).read_text(encoding="utf-8")
    )
    assert [entry.version for entry in rotation.previous] == [2]
    assert not (settings.keys_dir / "tenant-key.v1.json").exists()


def test_default_overlap_is_at_least_the_event_retention(tmp_path: Path, clock: FakeClock) -> None:
    settings = EdgeSettings(state_dir=tmp_path, retention=RetentionSettings(events_days=45))
    manager = KeyManager(settings, clock=clock)
    manager.init(create_local_kms=True)
    manager.rotate()
    rotation = read_json(settings.keys_dir / ROTATION_FILE)
    assert rotation["overlap_days"] == 45


def test_rotate_before_init_fails(settings: EdgeSettings) -> None:
    LocalKms.create(settings.local_kms_key_file)
    with pytest.raises(KeyManagementError):
        KeyManager(settings).rotate()


def test_active_file_must_match_rotation_state(settings: EdgeSettings, manager: KeyManager) -> None:
    rotation = read_json(settings.keys_dir / ROTATION_FILE)
    rotation["active_version"] = 7
    (settings.keys_dir / ROTATION_FILE).write_text(json.dumps(rotation), encoding="utf-8")
    with pytest.raises(KeyManagementError, match="version"):
        manager.load_keyring()


def test_wrong_master_key_cannot_unwrap(settings: EdgeSettings, manager: KeyManager) -> None:
    settings.local_kms_key_file.write_bytes(os.urandom(KEY_BYTES))
    with pytest.raises(CryptoError):
        KeyManager(settings).load_keyring()


# ---------------------------------------------------------------------------------------------
# vault data key, status, assertion key
# ---------------------------------------------------------------------------------------------


def test_vault_data_key_is_stable_and_created_on_first_use(
    settings: EdgeSettings, clock: FakeClock
) -> None:
    LocalKms.create(settings.local_kms_key_file)
    manager = KeyManager(settings, clock=clock)
    assert not (settings.keys_dir / VAULT_DATA_KEY_FILE).exists()
    first = manager.vault_data_key()
    assert len(first) == KEY_BYTES
    assert (settings.keys_dir / VAULT_DATA_KEY_FILE).exists()
    assert manager.vault_data_key() == first
    assert KeyManager(settings).vault_data_key() == first
    wrapped = WrappedKey.read(settings.keys_dir / VAULT_DATA_KEY_FILE)
    assert wrapped.context == {"tenant_id": "acme", "purpose": "reveal-vault"}


def test_status_reports_versions_and_fingerprints_only(
    settings: EdgeSettings, manager: KeyManager, clock: FakeClock
) -> None:
    keyring = manager.rotate(overlap_days=3)
    status = manager.status()
    text = json.dumps(status)
    assert status["provider"] == "local"
    assert status["kms_key_id"] == manager.kms.key_id
    assert status["active_version"] == 2
    assert status["active_fingerprint"] == keyring.active.fingerprint
    previous = status["previous"]
    assert isinstance(previous, list) and len(previous) == 1
    assert previous[0]["version"] == 1
    assert previous[0]["fingerprint"] == keyring.previous[0].fingerprint
    assert previous[0]["retire_at"] == (clock.now + timedelta(days=3)).isoformat()
    assert status["overlap_days"] == 3
    assert status["assertion_public_key"] is None
    for key in (keyring.active, *keyring.previous):
        assert key.material.hex() not in text
        assert base64.urlsafe_b64encode(key.material).decode().rstrip("=") not in text


def test_status_before_init(settings: EdgeSettings) -> None:
    LocalKms.create(settings.local_kms_key_file)
    status = KeyManager(settings).status()
    assert status["active_version"] is None
    assert status["previous"] == []


def test_load_verify_key_reads_the_public_key_file(
    settings: EdgeSettings, manager: KeyManager
) -> None:
    assert manager.load_verify_key() is None
    signing = SigningKey.generate()
    path = settings.keys_dir / ASSERTION_PUBLIC_KEY_FILE
    path.write_text(signing.verify_key.to_text() + "\n", encoding="utf-8")
    loaded = manager.load_verify_key()
    assert loaded is not None
    assert loaded.key_id == signing.verify_key.key_id
    assert manager.status()["assertion_public_key"] == signing.verify_key.key_id


def test_load_verify_key_honours_the_configured_path(tmp_path: Path) -> None:
    signing = SigningKey.generate()
    path = tmp_path / "core.pub"
    path.write_text(signing.verify_key.to_text(), encoding="utf-8")
    settings = EdgeSettings(
        state_dir=tmp_path / "state", reveal=RevealSettings(assertion_public_key_file=path)
    )
    loaded = KeyManager(settings).load_verify_key()
    assert loaded is not None and loaded.key_id == signing.verify_key.key_id
    path.write_text("not a key", encoding="utf-8")
    with pytest.raises(KeyManagementError, match="public key"):
        KeyManager(settings).load_verify_key()


# ---------------------------------------------------------------------------------------------
# build_kms
# ---------------------------------------------------------------------------------------------


def test_build_kms_local(settings: EdgeSettings) -> None:
    with pytest.raises(KeyManagementError, match="local KMS"):
        build_kms(settings)
    created = LocalKms.create(settings.local_kms_key_file)
    kms = build_kms(settings)
    assert isinstance(kms, LocalKms)
    assert kms.key_id == created.key_id


def test_build_kms_vault_reads_the_token_from_a_file(tmp_path: Path) -> None:
    token_file = tmp_path / "vault.token"
    settings = EdgeSettings(
        state_dir=tmp_path / "state",
        kms=KmsSettings(
            provider="vault",
            vault_url="https://vault.internal:8200",
            vault_transit_key="carto-edge",
            vault_mount="kms",
            vault_token_file=token_file,
        ),
    )
    with pytest.raises(KeyManagementError, match="token file"):
        build_kms(settings)
    token_file.write_text("hvs.secret\n", encoding="utf-8")
    kms = build_kms(settings)
    assert isinstance(kms, VaultTransitKms)
    assert kms.provider == "vault"
    assert kms.key_id == "vault:kms/carto-edge"
    assert "hvs.secret" not in repr(kms)


# ---------------------------------------------------------------------------------------------
# Vault Transit through httpx.MockTransport
# ---------------------------------------------------------------------------------------------


def fake_transit() -> tuple[Callable[[httpx.Request], httpx.Response], dict[str, bytes]]:
    """A Transit engine that keeps plaintexts in memory keyed by a synthetic ciphertext."""
    store: dict[str, bytes] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Vault-Token"] == "hvs.test"
        body = json.loads(request.content)
        if request.url.path == "/v1/transit/encrypt/carto":
            ciphertext = f"vault:v1:{len(store) + 1}"
            store[ciphertext] = (
                base64.b64decode(body["plaintext"]) + b"|" + base64.b64decode(body["context"])
            )
            return httpx.Response(200, json={"data": {"ciphertext": ciphertext}})
        if request.url.path == "/v1/transit/decrypt/carto":
            stored = store.get(body["ciphertext"])
            if stored is None:
                return httpx.Response(400, json={"errors": ["unknown ciphertext"]})
            plaintext, _, context = stored.rpartition(b"|")
            if context != base64.b64decode(body["context"]):
                return httpx.Response(400, json={"errors": ["context mismatch"]})
            return httpx.Response(
                200, json={"data": {"plaintext": base64.b64encode(plaintext).decode()}}
            )
        return httpx.Response(404)

    return handler, store


def test_key_manager_through_vault_transit(tmp_path: Path, clock: FakeClock) -> None:
    handler, store = fake_transit()
    kms = VaultTransitKms(
        "https://vault.internal:8200",
        "carto",
        "hvs.test",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    settings = EdgeSettings(
        state_dir=tmp_path / "state",
        tenant_id="acme",
        kms=KmsSettings(
            provider="vault",
            vault_url="https://vault.internal:8200",
            vault_token_file=tmp_path / "token",
        ),
    )
    manager = KeyManager(settings, kms, clock=clock)
    manager.init()
    assert not settings.local_kms_key_file.exists()
    wrapped = WrappedKey.read(settings.tenant_key_file)
    assert wrapped.provider == "vault"
    assert wrapped.ciphertext.startswith("vault:v1:")
    assert wrapped.ciphertext in store

    keyring = manager.load_keyring()
    first = keyring.active.token("id", "SO-0004471")
    rotated = manager.rotate(overlap_days=30)
    assert rotated.previous[0].token("id", "SO-0004471") == first
    assert rotated.active.token("id", "SO-0004471") == token(
        rotated.active.material, 2, "id", "SO-0004471"
    )
    assert manager.vault_data_key() == manager.vault_data_key()
    # the context is bound: a wrapped key replayed under another context does not unwrap
    replayed = wrapped.model_copy(update={"context": {**wrapped.context, "tenant_id": "other"}})
    with pytest.raises(CryptoError):
        kms.unwrap(replayed)
    assert canonical_context(wrapped.context) in b"".join(store.values())


def test_rewrap_moves_every_wrapped_key_to_the_new_kms_and_keeps_tokens(tmp_path: Path) -> None:
    settings = EdgeSettings(state_dir=tmp_path / "state")
    manager = KeyManager(settings)
    manager.init(create_local_kms=True)
    manager.vault_data_key()
    manager.rotate(overlap_days=30)
    before = manager.load_keyring()
    token = before.active.token("id", "SO-0004471")
    previous_token = before.previous[0].token("id", "SO-0004471")
    vault_key = manager.vault_data_key()
    names = [path.name for path in manager.wrapped_key_files()]
    assert names == ["tenant-key.v1.json", "tenant-key.v2.json", "vault-data-key.json"]

    new_kms = LocalKms.create(tmp_path / "new-master.key")
    rewritten = manager.rewrap(new_kms)
    assert [path.name for path in rewritten] == names
    assert not list(settings.keys_dir.glob("*.rewrap"))
    for path in rewritten:
        assert WrappedKey.read(path).key_id == new_kms.key_id
    reopened = KeyManager(settings, new_kms)
    after = reopened.load_keyring()
    assert after.active.token("id", "SO-0004471") == token
    assert after.previous[0].token("id", "SO-0004471") == previous_token
    assert reopened.vault_data_key() == vault_key
    with pytest.raises(CryptoError):
        KeyManager(settings, LocalKms.open(settings.local_kms_key_file)).load_keyring()


def test_rewrap_failure_leaves_the_old_files_untouched(tmp_path: Path) -> None:
    settings = EdgeSettings(state_dir=tmp_path / "state")
    manager = KeyManager(settings)
    manager.init(create_local_kms=True)
    manager.vault_data_key()
    originals = {path.name: path.read_bytes() for path in manager.wrapped_key_files()}

    class Failing:
        provider = "local"
        key_id = "local:failing"
        calls = 0

        def wrap(self, material: bytes, context: dict[str, str]) -> WrappedKey:
            self.calls += 1
            if self.calls == 2:
                raise CryptoError("KMS unavailable")
            return LocalKms.create(tmp_path / f"k{self.calls}.key").wrap(material, context)

        def unwrap(self, wrapped: WrappedKey) -> bytes:
            raise AssertionError("never called")

    with pytest.raises(KeyManagementError, match="unchanged"):
        manager.rewrap(Failing())  # type: ignore[arg-type]
    assert {path.name: path.read_bytes() for path in manager.wrapped_key_files()} == originals
    assert not list(settings.keys_dir.glob("*.rewrap"))
    assert manager.load_keyring().active.version == 1


# ---------------------------------------------------------------------------------------------
# Crash consistency: rotation.json is the one commit point (ADR 0042)
# ---------------------------------------------------------------------------------------------


class _PowerCutError(Exception):
    pass


def _cut_power_at(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Fail the atomic replace that would install ``name``."""
    real_replace = os.replace

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if Path(dst).name == name:
            raise _PowerCutError(name)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)


@pytest.mark.parametrize("cut_at", ["tenant-key.v1.json", "tenant-key.v2.json", ROTATION_FILE])
def test_a_crash_during_rotation_leaves_the_old_keyring_and_rotation_retries(
    settings: EdgeSettings,
    manager: KeyManager,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    cut_at: str,
) -> None:
    before = manager.load_keyring()
    with monkeypatch.context() as patched:
        _cut_power_at(patched, cut_at)
        with pytest.raises(_PowerCutError):
            manager.rotate(overlap_days=10)
    after_crash = KeyManager(settings, clock=clock).load_keyring()
    assert after_crash == before
    assert not list(settings.keys_dir.glob("*.tmp"))
    rotated = KeyManager(settings, clock=clock).rotate(overlap_days=10)
    assert rotated.active.version == 2
    assert rotated.previous[0].material == before.active.material
    assert KeyManager(settings, clock=clock).load_keyring() == rotated


def test_a_crash_after_the_commit_loads_the_new_keyring(
    settings: EdgeSettings, manager: KeyManager, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patched:  # the power goes right after the commit
        patched.setattr(KeyManager, "_remove_superseded", lambda _self, _retired: None)
        rotated = manager.rotate(overlap_days=10)
    assert settings.tenant_key_file.exists()  # the legacy file survived the cut
    assert KeyManager(settings, clock=clock).load_keyring() == rotated
    status = KeyManager(settings, clock=clock).status()
    assert status["active_version"] == 2
    again = KeyManager(settings, clock=clock).rotate(overlap_days=10)
    assert again.active.version == 3
    assert not settings.tenant_key_file.exists()


def test_the_legacy_single_file_layout_still_loads(
    settings: EdgeSettings, manager: KeyManager
) -> None:
    assert settings.tenant_key_file.exists()
    assert not versioned_key_file(settings.keys_dir, 1).exists()
    assert manager.load_keyring().active.version == 1
    assert manager.status()["active_fingerprint"] is not None


def test_a_key_file_with_the_wrong_version_is_refused(
    settings: EdgeSettings, manager: KeyManager
) -> None:
    manager.rotate(overlap_days=10)
    one = versioned_key_file(settings.keys_dir, 1)
    two = versioned_key_file(settings.keys_dir, 2)
    one.write_bytes(two.read_bytes())
    with pytest.raises(KeyManagementError, match="version"):
        manager.load_keyring()


def test_key_files_are_fsynced_with_their_directory(
    settings: EdgeSettings, manager: KeyManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[int] = []
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        synced.append(descriptor)
        real_fsync(descriptor)

    directories: list[Path] = []
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr("carto_common.crypto.fsync_directory", directories.append)
    monkeypatch.setattr("carto_edge.keys.fsync_directory", directories.append)
    manager.rotate(overlap_days=10)
    # the migrated v1, the new v2 and rotation.json: one file fsync and one directory sync each
    assert len(synced) >= 3
    assert directories.count(settings.keys_dir) >= 3


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_rotated_key_files_are_owner_only(settings: EdgeSettings, manager: KeyManager) -> None:
    manager.rotate(overlap_days=10)
    for path in settings.keys_dir.glob("*.json"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name
