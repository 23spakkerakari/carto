"""carto_common.crypto: tokens (spec 8.4), AES-GCM, key wrapping, Ed25519 and internal
assertions (spec 14.4). Key material never shows in reprs or errors (spec 2.3 invariant 7)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from carto_common.crypto import (
    KEY_BYTES,
    TOKEN_BODY_LEN,
    AesGcmBox,
    CryptoError,
    InternalAssertion,
    InvalidAssertionError,
    Keyring,
    LocalKms,
    SigningKey,
    TokenDomain,
    TokenKey,
    VaultTransitKms,
    VerifyKey,
    WrappedKey,
    b64url_decode,
    b64url_encode,
    canonical_context,
    generate_key,
    key_fingerprint,
    sign_assertion,
    token,
    vault_ssl_context,
    verify_assertion,
)

TOKEN_PATTERN = re.compile(r"^t[1-9][0-9]{0,3}\.[A-Za-z0-9_-]{22}$")
KEY_A = bytes(range(32))
KEY_B = bytes(range(1, 33))
DOMAINS: tuple[TokenDomain, ...] = ("id", "date", "amt", "ph")


# -- tokens ---------------------------------------------------------------------------------------


def test_token_matches_the_spec_construction() -> None:
    expected_mac = hmac.new(KEY_A, b"id\x00SO-0004471", hashlib.sha256).digest()
    expected = "t1." + base64.urlsafe_b64encode(expected_mac).decode()[:TOKEN_BODY_LEN]
    assert token(KEY_A, 1, "id", "SO-0004471") == expected
    assert TOKEN_PATTERN.fullmatch(expected)


def test_token_key_version_prefix_and_range() -> None:
    assert token(KEY_A, 9999, "id", "x").startswith("t9999.")
    with pytest.raises(CryptoError):
        token(KEY_A, 0, "id", "x")
    with pytest.raises(CryptoError):
        token(KEY_A, 10000, "id", "x")
    with pytest.raises(CryptoError):
        token(KEY_A, True, "id", "x")


def test_token_rejects_wrong_key_length() -> None:
    with pytest.raises(CryptoError, match="32 bytes"):
        token(b"short", 1, "id", "x")


@settings(max_examples=200)
@given(st.text(min_size=1, max_size=64), st.sampled_from(DOMAINS))
def test_tokens_are_deterministic_and_differ_across_versions_and_domains(
    value: str, domain: TokenDomain
) -> None:
    first = token(KEY_A, 1, domain, value)
    assert first == token(KEY_A, 1, domain, value)
    assert TOKEN_PATTERN.fullmatch(first)
    assert first != token(KEY_A, 2, domain, value)
    assert first.partition(".")[2] != token(KEY_B, 1, domain, value).partition(".")[2]
    other_domain: TokenDomain = "amt" if domain != "amt" else "id"
    assert first != token(KEY_A, 1, other_domain, value)


def test_same_form_value_from_different_forms_share_the_id_domain() -> None:
    # 4471 as a raw value and 4471 as digits.0 of SO-0004471 tokenize identically (spec 8.4).
    assert token(KEY_A, 1, "id", "4471") == token(KEY_A, 1, "id", "4471")


def test_token_key_hides_material() -> None:
    key = TokenKey(1, KEY_A)
    assert KEY_A.hex() not in repr(key)
    assert "material" not in repr(key)
    assert key.token("id", "4471") == token(KEY_A, 1, "id", "4471")
    assert key.fingerprint == hashlib.sha256(KEY_A).hexdigest()[:16]


def test_keyring_orders_active_first_and_validates_versions() -> None:
    ring = Keyring(TokenKey(3, KEY_A), (TokenKey(2, KEY_B),))
    assert [k.version for k in ring.tokenization_keys()] == [3, 2]
    assert ring.key(2).material == KEY_B
    with pytest.raises(CryptoError):
        ring.key(1)
    with pytest.raises(CryptoError, match="lower"):
        Keyring(TokenKey(2, KEY_A), (TokenKey(3, KEY_B),))
    with pytest.raises(CryptoError, match="unique"):
        Keyring(TokenKey(2, KEY_A), (TokenKey(2, KEY_B),))


def test_keyring_version_of_token() -> None:
    assert Keyring.version_of("t12.abc") == 12
    for bad in ("12.abc", "t.abc", "t01.abc", "tx.abc", "t1", "t٣.abc"):
        with pytest.raises(CryptoError):
            Keyring.version_of(bad)


# -- AES-GCM --------------------------------------------------------------------------------------


def test_aes_gcm_round_trip_and_aad_binding() -> None:
    box = AesGcmBox(KEY_A)
    blob = box.encrypt(b"secret value", b"tenant\x00t1.abc")
    assert box.decrypt(blob, b"tenant\x00t1.abc") == b"secret value"
    with pytest.raises(CryptoError, match="authentication failed"):
        box.decrypt(blob, b"tenant\x00t1.other")
    with pytest.raises(CryptoError):
        AesGcmBox(KEY_B).decrypt(blob, b"tenant\x00t1.abc")
    tampered = bytes([blob[0] ^ 0x01]) + blob[1:]
    with pytest.raises(CryptoError):
        box.decrypt(tampered, b"tenant\x00t1.abc")
    with pytest.raises(CryptoError, match="too short"):
        box.decrypt(b"x" * 10, b"")


def test_aes_gcm_nonces_are_random_and_repr_hides_key() -> None:
    box = AesGcmBox(KEY_A)
    assert box.encrypt(b"v", b"a") != box.encrypt(b"v", b"a")
    assert KEY_A.hex() not in repr(box)
    assert box.fingerprint in repr(box)


# -- key wrapping ---------------------------------------------------------------------------------


def test_local_kms_create_open_wrap_unwrap(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "local-kms.key"
    kms = LocalKms.create(path)
    assert path.read_bytes().__len__() == KEY_BYTES
    reopened = LocalKms.open(path)
    assert reopened.key_id == kms.key_id
    material = generate_key()
    wrapped = kms.wrap(material, {"tenant_id": "default", "purpose": "tokenization"})
    assert wrapped.provider == "local"
    assert wrapped.fingerprint == key_fingerprint(material)
    assert base64.b64encode(material).decode() not in wrapped.model_dump_json()
    assert reopened.unwrap(wrapped) == material
    with pytest.raises(CryptoError, match="refusing to overwrite"):
        LocalKms.create(path)


def test_local_kms_context_and_key_binding(tmp_path: Path) -> None:
    kms = LocalKms.create(tmp_path / "a.key")
    other = LocalKms.create(tmp_path / "b.key")
    wrapped = kms.wrap(generate_key(), {"purpose": "tokenization"})
    retargeted = wrapped.model_copy(update={"context": {"purpose": "reveal-vault"}})
    with pytest.raises(CryptoError):
        kms.unwrap(retargeted)
    with pytest.raises(CryptoError, match="different KMS key"):
        other.unwrap(wrapped)
    with pytest.raises(CryptoError, match="fingerprint"):
        kms.unwrap(wrapped.model_copy(update={"fingerprint": "0" * 16}))


def test_local_kms_open_rejects_bad_files(tmp_path: Path) -> None:
    with pytest.raises(CryptoError, match="cannot read"):
        LocalKms.open(tmp_path / "missing.key")
    bad = tmp_path / "bad.key"
    bad.touch(mode=0o600)
    bad.write_bytes(b"short")
    with pytest.raises(CryptoError, match="32-byte"):
        LocalKms.open(bad)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660])
def test_local_kms_open_refuses_a_master_key_readable_by_others(tmp_path: Path, mode: int) -> None:
    """ADR 0070 section 7: a master key file that group or others can read is refused, and the
    message names the path and its mode so the operator can fix it."""
    path = tmp_path / "local-kms.key"
    LocalKms.create(path)
    LocalKms.open(path)  # 0600 as created
    path.chmod(mode)
    with pytest.raises(CryptoError, match=f"{mode:04o}") as info:
        LocalKms.open(path)
    assert str(path) in str(info.value) and "chmod 600" in str(info.value)
    path.chmod(0o400)
    LocalKms.open(path)  # read-only for the owner is fine


def test_wrapped_key_file_round_trip(tmp_path: Path) -> None:
    kms = LocalKms.create(tmp_path / "kms.key")
    wrapped = kms.wrap(generate_key(), {"tenant_id": "t"})
    path = tmp_path / "keys" / "tenant-key.json"
    wrapped.write(path)
    assert WrappedKey.read(path) == wrapped
    with pytest.raises(CryptoError, match="cannot read"):
        WrappedKey.read(tmp_path / "nope.json")
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CryptoError, match="not valid"):
        WrappedKey.read(path)


def _fake_vault(
    material_store: dict[str, bytes],
    token_value: str = "s.token",  # noqa: S107
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Vault-Token"] == token_value
        body = json.loads(request.content)
        if request.url.path == "/v1/transit/encrypt/carto":
            plaintext = base64.b64decode(body["plaintext"])
            ciphertext = (
                "vault:v1:" + hashlib.sha256(plaintext + body["context"].encode()).hexdigest()
            )
            material_store[ciphertext + body["context"]] = plaintext
            return httpx.Response(200, json={"data": {"ciphertext": ciphertext}})
        if request.url.path == "/v1/transit/decrypt/carto":
            stored = material_store.get(body["ciphertext"] + body["context"])
            if stored is None:
                return httpx.Response(400, json={"errors": ["invalid ciphertext"]})
            return httpx.Response(
                200, json={"data": {"plaintext": base64.b64encode(stored).decode()}}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_vault_transit_wrap_unwrap_through_mock_transport() -> None:
    store: dict[str, bytes] = {}
    client = httpx.Client(transport=_fake_vault(store))
    kms = VaultTransitKms("https://vault.internal:8200", "carto", "s.token", client=client)
    material = generate_key()
    wrapped = kms.wrap(material, {"tenant_id": "default"})
    assert wrapped.provider == "vault"
    assert wrapped.ciphertext.startswith("vault:v1:")
    assert kms.unwrap(wrapped) == material
    # A different context is a different derived key: Vault refuses, we raise.
    with pytest.raises(CryptoError):
        kms.unwrap(wrapped.model_copy(update={"context": {"tenant_id": "other"}}))
    assert "s.token" not in repr(kms)


def _ca_der(pem: bytes) -> bytes:
    from cryptography import x509  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import Encoding  # noqa: PLC0415

    return x509.load_pem_x509_certificate(pem).public_bytes(Encoding.DER)


def test_vault_ssl_context_trusts_the_configured_bundle_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of 2026-11-02 (Medium): ``create_default_context`` loads OpenSSL's default paths
    and honours ``SSL_CERT_FILE`` and ``SSL_CERT_DIR``; the Vault context must trust the
    configured bundle and nothing else, with the hostname checked and TLS 1.2 at least."""
    from carto_common.pki import create_ca  # noqa: PLC0415

    ours = create_ca("ours").certificate_pem
    hostile = create_ca("hostile").certificate_pem
    bundle = tmp_path / "vault-ca.pem"
    bundle.write_bytes(ours)
    hostile_dir = tmp_path / "hostile"
    hostile_dir.mkdir()
    (hostile_dir / "attacker-ca.pem").write_bytes(hostile)
    monkeypatch.setenv("SSL_CERT_FILE", str(hostile_dir / "attacker-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(hostile_dir))
    context = vault_ssl_context(bundle)
    assert context.get_ca_certs(binary_form=True) == [_ca_der(ours)]
    assert _ca_der(hostile) not in context.get_ca_certs(binary_form=True)
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version is ssl.TLSVersion.TLSv1_2
    # Against the default context, which is what the fix replaces.
    polluted = ssl.create_default_context()
    assert _ca_der(hostile) in polluted.get_ca_certs(binary_form=True)
    empty = tmp_path / "empty.pem"
    empty.write_bytes(b"")
    with pytest.raises(CryptoError, match=r"no CA certificate|cannot be loaded"):
        vault_ssl_context(empty)
    with pytest.raises(CryptoError, match="ca_file"):
        vault_ssl_context(tmp_path / "missing.pem")
    # The KMS refuses to build its own client without a bundle: the system store is never used.
    with pytest.raises(CryptoError, match="ca_file"):
        VaultTransitKms("https://vault.internal:8200", "carto", "s.token")
    kms = VaultTransitKms("https://vault.internal:8200", "carto", "s.token", ca_file=bundle)
    assert kms._client.trust_env is False


def test_vault_transit_client_ignores_the_environment_and_never_follows_redirects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ADR 0070 section 7: ``HTTPS_PROXY`` or ``SSL_CERT_FILE`` in the environment must not
    redirect the Vault token and the key material to a proxy with an attacker CA, and a 3xx
    from Vault must not send them to another host."""
    from carto_common.pki import create_ca  # noqa: PLC0415

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "attacker-ca.pem"))
    bundle = tmp_path / "vault-ca.pem"
    bundle.write_bytes(create_ca().certificate_pem)
    kms = VaultTransitKms("https://vault.internal:8200", "carto", "s.token", ca_file=bundle)
    client = kms._client
    assert client.trust_env is False
    assert client.follow_redirects is False
    assert not client._mounts, "no proxy mounts from the environment"

    calls: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.example/v1/transit"})

    # Even a caller-supplied client that would follow redirects is told not to per request.
    following = httpx.Client(transport=httpx.MockTransport(redirecting), follow_redirects=True)
    kms = VaultTransitKms("https://vault.internal:8200", "carto", "s.token", client=following)
    with pytest.raises(CryptoError, match="HTTP 302"):
        kms.wrap(generate_key(), {"tenant_id": "default"})
    assert calls == ["https://vault.internal:8200/v1/transit/encrypt/carto"]


def test_vault_transit_takes_an_explicit_ca_bundle(tmp_path: Path) -> None:
    from carto_common.pki import create_ca  # noqa: PLC0415

    bundle = tmp_path / "vault-ca.pem"
    bundle.write_bytes(create_ca().certificate_pem)
    kms = VaultTransitKms("https://vault.internal:8200", "carto", "s.token", ca_file=bundle)
    assert kms._client.trust_env is False
    with pytest.raises(CryptoError, match="ca_file"):
        VaultTransitKms(
            "https://vault.internal:8200", "carto", "s.token", ca_file=tmp_path / "missing.pem"
        )


def test_vault_transit_rejects_plain_http_and_bad_key_names() -> None:
    with pytest.raises(CryptoError, match="https"):
        VaultTransitKms("http://vault.internal:8200", "carto", "t")
    with pytest.raises(CryptoError, match="single path segment"):
        VaultTransitKms("https://vault.internal", "a/b", "t")
    # Dev Vault over plain http on the loopback is allowed; a bundle is still required for a
    # client the KMS builds itself, so the dev path hands one in (or, here, a client).
    with pytest.raises(CryptoError, match="ca_file"):
        VaultTransitKms("http://127.0.0.1:8200", "carto", "t")
    VaultTransitKms("http://127.0.0.1:8200", "carto", "t", client=httpx.Client())


def test_vault_transit_errors_never_include_material() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": ["permission denied"]})

    kms = VaultTransitKms(
        "https://vault.internal", "carto", "t", client=httpx.Client(transport=_fake_vault({}, "t"))
    )
    kms2 = VaultTransitKms(
        "https://vault.internal",
        "carto",
        "t",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    material = generate_key()
    with pytest.raises(CryptoError, match="HTTP 403") as info:
        kms2.wrap(material, {})
    assert base64.b64encode(material).decode() not in str(info.value)
    wrapped = kms.wrap(material, {})
    with pytest.raises(CryptoError, match="different KMS key"):
        VaultTransitKms(
            "https://vault.internal",
            "other",
            "t",
            client=httpx.Client(transport=_fake_vault({}, "t")),
        ).unwrap(wrapped)


def test_canonical_context_is_sorted_and_compact() -> None:
    assert canonical_context({"b": "2", "a": "1"}) == b'{"a":"1","b":"2"}'


# -- base64url ------------------------------------------------------------------------------------


@given(st.binary(max_size=64))
def test_b64url_round_trip(data: bytes) -> None:
    text = b64url_encode(data)
    assert "=" not in text
    assert b64url_decode(text) == data


def test_b64url_decode_rejects_garbage() -> None:
    with pytest.raises(CryptoError):
        b64url_decode("not base64 !!")
    with pytest.raises(CryptoError):
        b64url_decode("日本")


# -- Ed25519 and assertions -----------------------------------------------------------------------


def test_signing_round_trip_and_key_encoding() -> None:
    key = SigningKey.generate()
    signature = key.sign(b"manifest bytes")
    key.verify_key.verify(signature, b"manifest bytes")
    with pytest.raises(CryptoError, match="verification failed"):
        key.verify_key.verify(signature, b"other bytes")
    restored = SigningKey.from_bytes(key.to_bytes())
    assert restored.verify_key.to_text() == key.verify_key.to_text()
    public = VerifyKey.from_text(key.verify_key.to_text())
    public.verify(signature, b"manifest bytes")
    assert len(key.verify_key.to_text()) == 43
    assert key.to_bytes().hex() not in repr(key)
    with pytest.raises(CryptoError):
        VerifyKey.from_bytes(b"short")
    with pytest.raises(CryptoError):
        SigningKey.from_bytes(b"short")


def _assertion(**overrides: object) -> InternalAssertion:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    data: dict[str, object] = {
        "subject": "user_42",
        "permission": "reveal",
        "purpose": "investigating alert alr_1",
        "audience": "edge-reveal",
        "issued_at": now,
        "expires_at": now + timedelta(minutes=2),
        "nonce": "n" * 16,
    }
    data.update(overrides)
    return InternalAssertion.model_validate(data)


def test_assertion_sign_and_verify() -> None:
    key = SigningKey.generate()
    text = sign_assertion(key, _assertion())
    now = datetime(2026, 10, 8, 12, 1, tzinfo=UTC)
    verified = verify_assertion(key.verify_key, text, audience="edge-reveal", now=now)
    assert verified.subject == "user_42"
    assert verified.permission == "reveal"


@pytest.mark.parametrize(
    ("overrides", "now", "match"),
    [
        ({}, datetime(2026, 10, 8, 12, 3, tzinfo=UTC), "expired"),
        ({}, datetime(2026, 10, 8, 11, 0, tzinfo=UTC), "future"),
        (
            {"expires_at": datetime(2026, 10, 8, 12, 10, tzinfo=UTC)},
            datetime(2026, 10, 8, 12, 1, tzinfo=UTC),
            "lifetime",
        ),
        (
            {"expires_at": datetime(2026, 10, 8, 11, 59, tzinfo=UTC)},
            datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
            "before it was issued",
        ),
        ({"audience": "edge-tokenize"}, datetime(2026, 10, 8, 12, 1, tzinfo=UTC), "audience"),
    ],
)
def test_assertion_time_and_audience_checks(
    overrides: dict[str, object], now: datetime, match: str
) -> None:
    key = SigningKey.generate()
    text = sign_assertion(key, _assertion(**overrides))
    with pytest.raises(InvalidAssertionError, match=match):
        verify_assertion(key.verify_key, text, audience="edge-reveal", now=now)


def test_assertion_signature_and_shape_checks() -> None:
    key = SigningKey.generate()
    other = SigningKey.generate()
    now = datetime(2026, 10, 8, 12, 1, tzinfo=UTC)
    text = sign_assertion(key, _assertion())
    with pytest.raises(InvalidAssertionError, match="signature"):
        verify_assertion(other.verify_key, text, audience="edge-reveal", now=now)
    payload, _, signature = text.partition(".")
    forged_payload = b64url_encode(
        json.dumps({**json.loads(b64url_decode(payload)), "subject": "admin"}).encode()
    )
    with pytest.raises(InvalidAssertionError, match="signature"):
        verify_assertion(
            key.verify_key, f"{forged_payload}.{signature}", audience="edge-reveal", now=now
        )
    for malformed in ("", "abc", ".", "a.b.c", "!!.??"):
        with pytest.raises(InvalidAssertionError):
            verify_assertion(key.verify_key, malformed, audience="edge-reveal", now=now)


def test_assertion_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _assertion(issued_at=datetime(2026, 10, 8, 12, 0))


def test_local_kms_master_key_is_created_exclusively_and_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple[int, int]] = []
    real_open = os.open

    def recording_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        opened.append((flags, mode))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", recording_open)
    path = tmp_path / "keys" / "local-kms.key"
    LocalKms.create(path)
    creates = [(flags, mode) for flags, mode in opened if flags & os.O_CREAT]
    assert creates, "the master key must be created through os.open with an explicit mode"
    flags, mode = creates[0]
    assert flags & os.O_EXCL
    assert mode == 0o600
    assert len(path.read_bytes()) == KEY_BYTES  # no newline translation on Windows
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_wrapped_key_write_is_atomic_and_owner_only(tmp_path: Path) -> None:
    kms = LocalKms.create(tmp_path / "kms.key")
    path = tmp_path / "keys" / "tenant-key.v1.json"
    wrapped = kms.wrap(generate_key(), {"purpose": "test"})
    wrapped.write(path)
    wrapped.write(path)  # replaces
    assert WrappedKey.read(path) == wrapped
    assert not list(path.parent.glob("*.tmp"))
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
