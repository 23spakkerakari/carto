"""carto-ctl key init (spec 8.4): the key layout the edge KeyManager reads, wrapped by the local
KMS or Vault Transit, no byte of key material in the output in any encoding, and refusal to
overwrite."""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from carto_common.crypto import (
    CryptoError,
    LocalKms,
    VaultTransitKms,
    WrappedKey,
    key_fingerprint,
)
from carto_ctl.cli import main
from carto_ctl.exit_codes import EXIT_FAILURE, EXIT_OK, EXIT_USAGE
from carto_ctl.keyinit import DEFAULT_STATE_DIR, KeyInitOptions, initialize_keys

CONTEXT = {"tenant_id": "acme", "purpose": "tokenization", "version": "1"}


def _encodings(material: bytes) -> list[str]:
    standard = base64.b64encode(material).decode()
    url = base64.urlsafe_b64encode(material).decode()
    return [material.hex(), standard, standard.rstrip("="), url, url.rstrip("=")]


def _run(state: Path, *extra: str) -> int:
    return main(["key", "init", "--state-dir", str(state), "--tenant-id", "acme", *extra])


def test_local_kms_creates_exactly_the_edge_layout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _run(state, "--kms", "local") == EXIT_OK
    captured = capsys.readouterr()
    assert captured.err == ""
    keys = state / "keys"
    assert sorted(path.name for path in keys.iterdir()) == [
        "local-kms.key",
        "rotation.json",
        "tenant-key.json",
    ]
    master = (keys / "local-kms.key").read_bytes()
    assert len(master) == 32
    wrapped = WrappedKey.read(keys / "tenant-key.json")
    assert wrapped.provider == "local"
    assert wrapped.algorithm == "A256GCM"
    assert wrapped.context == CONTEXT
    kms = LocalKms.open(keys / "local-kms.key")
    assert wrapped.key_id == kms.key_id
    material = kms.unwrap(wrapped)
    assert len(material) == 32
    assert key_fingerprint(material) == wrapped.fingerprint
    assert json.loads((keys / "rotation.json").read_text(encoding="utf-8")) == {
        "active_version": 1,
        "previous": [],
        "overlap_days": 30,
    }
    assert wrapped.fingerprint in captured.out
    assert kms.key_id in captured.out
    assert "tenant acme" in captured.out and "version 1" in captured.out
    assert str(keys / "tenant-key.json") in captured.out
    for secret in (material, master):
        for encoded in _encodings(secret):
            assert encoded not in captured.out
    assert wrapped.ciphertext not in captured.out


def test_if_missing_succeeds_on_a_complete_run_and_refuses_a_partial_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _run(state, "--kms", "local", "--if-missing") == EXIT_OK
    keys = state / "keys"
    before = {path.name: path.read_bytes() for path in keys.iterdir()}
    capsys.readouterr()
    assert _run(state, "--kms", "local", "--if-missing") == EXIT_OK
    assert "nothing written" in capsys.readouterr().out
    assert {path.name: path.read_bytes() for path in keys.iterdir()} == before
    (keys / "rotation.json").unlink()
    assert _run(state, "--kms", "local", "--if-missing") == EXIT_FAILURE
    assert "refusing to overwrite" in capsys.readouterr().err
    assert (keys / "tenant-key.json").read_bytes() == before["tenant-key.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_key_files_are_owner_only(tmp_path: Path) -> None:
    state = tmp_path / "state"
    assert _run(state) == EXIT_OK
    for name in ("local-kms.key", "tenant-key.json"):
        assert stat.S_IMODE((state / "keys" / name).stat().st_mode) == 0o600, name


def test_refuses_to_overwrite_existing_key_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _run(state) == EXIT_OK
    keys = state / "keys"
    before = {path.name: path.read_bytes() for path in keys.iterdir()}
    capsys.readouterr()
    assert _run(state) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing to overwrite" in captured.err
    assert {path.name: path.read_bytes() for path in keys.iterdir()} == before
    # A lone leftover file is enough, and no master key is created alongside it.
    other = tmp_path / "other"
    (other / "keys").mkdir(parents=True)
    (other / "keys" / "rotation.json").write_text("{}", encoding="utf-8")
    assert _run(other) == EXIT_FAILURE
    assert "rotation.json" in capsys.readouterr().err
    assert sorted(path.name for path in (other / "keys").iterdir()) == ["rotation.json"]


def test_vault_requires_url_and_token_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    assert _run(state, "--kms", "vault") == EXIT_USAGE
    err = capsys.readouterr().err
    assert "--vault-url" in err and "--vault-token-file" in err and "--vault-ca-file" in err
    assert not state.exists()
    assert _run(state, "--kms", "vault", "--vault-url", "https://vault:8200") == EXIT_USAGE
    assert not state.exists()
    token_file = tmp_path / "vault-token"
    token_file.write_text("s.test-token\n", encoding="utf-8")
    url_and_token = ("--vault-url", "https://vault:8200", "--vault-token-file", str(token_file))
    assert _run(state, "--kms", "vault", *url_and_token) == EXIT_USAGE
    assert "--vault-ca-file" in capsys.readouterr().err
    assert not state.exists()


def test_vault_ca_file_reaches_the_transit_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of 2026-11-02 (Medium): ``key init --kms vault`` verifies Vault against the
    bundle it is given, never the system trust store."""
    token_file = tmp_path / "vault-token"
    token_file.write_text("s.test-token\n", encoding="utf-8")
    bundle = tmp_path / "vault-ca.pem"
    bundle.write_bytes(b"not read by the fake")
    seen: dict[str, object] = {}

    class Recording:
        provider = "vault"
        key_id = "vault:transit/carto"

        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.update(kwargs)
            msg = "stop here"
            raise CryptoError(msg)

    monkeypatch.setattr("carto_ctl.keyinit.VaultTransitKms", Recording)
    code = _run(
        tmp_path / "state",
        "--kms",
        "vault",
        "--vault-url",
        "https://vault.internal:8200",
        "--vault-token-file",
        str(token_file),
        "--vault-ca-file",
        str(bundle),
    )
    assert code == EXIT_FAILURE
    assert seen["ca_file"] == bundle
    assert "s.test-token" not in capsys.readouterr().err


def test_vault_token_is_never_an_option_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path / "state", "--kms", "vault", "--vault-token", "s.secret") == EXIT_USAGE
    assert "unrecognized arguments: --vault-token" in capsys.readouterr().err


def test_vault_transit_wraps_through_the_transit_api(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["X-Vault-Token"] == "s.test-token"
        body = json.loads(request.content)
        if request.url.path == "/v1/transit/encrypt/carto":
            ciphertext = f"vault:v1:{body['plaintext']}:{body['context']}"
            return httpx.Response(200, json={"data": {"ciphertext": ciphertext}})
        if request.url.path == "/v1/transit/decrypt/carto":
            _vault, _v1, plaintext, context = body["ciphertext"].split(":", 3)
            assert context == body["context"]
            return httpx.Response(200, json={"data": {"plaintext": plaintext}})
        return httpx.Response(404)

    token_file = tmp_path / "vault-token"
    token_file.write_text("s.test-token\n", encoding="utf-8")
    kms = VaultTransitKms(
        "https://vault.internal:8200",
        "carto",
        token_file.read_text(encoding="utf-8").strip(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    options = KeyInitOptions(
        state_dir=tmp_path / "state",
        tenant_id="acme",
        kms="vault",
        vault_url="https://vault.internal:8200",
        vault_token_file=token_file,
    )
    result = initialize_keys(options, kms=kms)
    assert result.kms_provider == "vault"
    assert result.kms_key_id == "vault:transit/carto"
    assert result.local_kms_file is None
    keys = tmp_path / "state" / "keys"
    assert sorted(path.name for path in keys.iterdir()) == ["rotation.json", "tenant-key.json"]
    wrapped = WrappedKey.read(keys / "tenant-key.json")
    assert wrapped.provider == "vault"
    assert wrapped.ciphertext.startswith("vault:v1:")
    assert wrapped.context == CONTEXT
    material = kms.unwrap(wrapped)
    assert key_fingerprint(material) == wrapped.fingerprint == result.fingerprint
    assert calls == ["/v1/transit/encrypt/carto", "/v1/transit/decrypt/carto"]


def test_unreachable_vault_fails_cleanly_without_writing_a_tenant_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from carto_common.pki import create_ca  # noqa: PLC0415

    token_file = tmp_path / "vault-token"
    token_file.write_text("s.test-token\n", encoding="utf-8")
    bundle = tmp_path / "vault-ca.pem"
    bundle.write_bytes(create_ca().certificate_pem)
    state = tmp_path / "state"
    code = _run(
        state,
        "--kms",
        "vault",
        "--vault-url",
        "https://127.0.0.1:1",
        "--vault-token-file",
        str(token_file),
        "--vault-ca-file",
        str(bundle),
    )
    assert code == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "Vault Transit" in err
    assert "s.test-token" not in err
    assert not (state / "keys" / "tenant-key.json").exists()
    assert not (state / "keys" / "local-kms.key").exists()


def test_missing_vault_token_file_is_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run(
        tmp_path / "state",
        "--kms",
        "vault",
        "--vault-url",
        "https://vault:8200",
        "--vault-token-file",
        str(tmp_path / "absent"),
        "--vault-ca-file",
        str(tmp_path / "vault-ca.pem"),
    )
    assert code == EXIT_FAILURE
    assert "cannot read Vault token file" in capsys.readouterr().err


@pytest.mark.parametrize("tenant", ["Acme", "a b", "", "-x", "a" * 65])
def test_bad_tenant_id_is_a_usage_error(
    tmp_path: Path, tenant: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["key", "init", "--state-dir", str(tmp_path), "--tenant-id", tenant]) == EXIT_USAGE
    assert "usage:" in capsys.readouterr().err


def test_defaults_match_the_edge_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Path("/var/lib/carto-edge") == DEFAULT_STATE_DIR
    monkeypatch.setenv("COLUMNS", "200")
    assert main(["key", "init", "--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for option in ("--state-dir", "--tenant-id", "--kms", "--vault-url", "--vault-token-file"):
        assert option in out
    assert "Arrives in" not in out
