"""OIDC login, sessions and CSRF (spec 14.1; ADR 0052) against a mock identity provider:
the authorization request carries state, nonce and an S256 PKCE challenge; the callback
exchanges the code with the client secret from its file, verifies the ID token and issues a
fresh session behind the ``__Host-`` cookie with the right flags; a wrong nonce, issuer,
audience or an expired token is refused; groups without a binding grant no role; idle and
absolute timeouts end the session; logout needs the CSRF token and invalidates; login starts
are rate-limited per address and in total, callbacks per attempt (ADR 0070 section 5)."""

from __future__ import annotations

import base64
import hashlib
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from authlib.jose import JsonWebKey, jwt
    from fastapi.testclient import TestClient

from carto_core.api.app import API_PREFIX, create_app
from carto_core.api.context import ApiContext
from carto_core.api.memory_ports import (
    InMemoryAuditLog,
    InMemoryLoginAttemptStore,
    InMemorySessionStore,
    seeded_ports,
)
from carto_core.api.oidc import (
    OidcClient,
    OidcError,
    backchannel_ssl_context,
    build_backchannel_client,
)
from carto_core.api.ports import ApiPorts
from carto_core.api.routes_auth import LOGIN_ATTEMPT_TTL, LOGIN_COOKIE_NAME
from carto_core.api.sessions import hash_session_id
from carto_core.audit import ANONYMOUS_ACTOR
from carto_core.settings import (
    SESSION_COOKIE_NAME,
    ApiSettings,
    CoreConfigError,
    CoreSettings,
    OidcSettings,
)

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
ISSUER = "https://idp.test"
CLIENT_ID = "carto-console"
CLIENT_SECRET = "s3cret-from-file"  # noqa: S105 - test fixture
LOGIN = f"{API_PREFIX}/auth/login"
CALLBACK = f"{API_PREFIX}/auth/callback"
LOGOUT = f"{API_PREFIX}/auth/logout"
ME = f"{API_PREFIX}/me"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: int) -> None:
        self.now += timedelta(**kwargs)


@dataclass
class Provider:
    """A mock identity provider behind ``httpx.MockTransport``."""

    clock: Clock
    issuer: str = ISSUER
    issuer_in_discovery: str = ISSUER
    urls: list[str] = field(default_factory=list)
    nonce: str | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    groups: list[str] = field(default_factory=lambda: ["engineers"])
    token_requests: list[httpx.Request] = field(default_factory=list)
    discovery_calls: int = 0
    jwks_calls: int = 0

    def __post_init__(self) -> None:
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        public_pem = private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        self.jwk = JsonWebKey.import_key(public_pem, {"kty": "RSA", "kid": "k1", "use": "sig"})

    def claims(self) -> dict[str, Any]:
        now = int(self.clock().timestamp())
        base: dict[str, Any] = {
            "iss": self.issuer,
            "sub": "alice",
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "auth_time": now - 5,
            "nonce": self.nonce,
            "name": "Alice Example",
            "email": "alice@example.test",
            "groups": self.groups,
        }
        base.update(self.overrides)
        return {key: value for key, value in base.items() if value is not None}

    def id_token(self) -> str:
        token = jwt.encode({"alg": "RS256", "kid": "k1"}, self.claims(), self.private_pem)
        return token.decode("ascii") if isinstance(token, bytes) else str(token)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            self.discovery_calls += 1
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer_in_discovery,
                    "authorization_endpoint": f"{self.issuer}/auth",
                    "token_endpoint": f"{self.issuer}/token",
                    "jwks_uri": f"{self.issuer}/keys",
                },
            )
        if path.endswith("/keys"):
            self.jwks_calls += 1
            return httpx.Response(200, json={"keys": [self.jwk.as_dict()]})
        if path.endswith("/token"):
            self.token_requests.append(request)
            return httpx.Response(
                200, json={"id_token": self.id_token(), "access_token": "x", "token_type": "Bearer"}
            )
        return httpx.Response(404)

    def http(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


@dataclass
class World:
    client: TestClient
    app: FastAPI
    ports: ApiPorts
    clock: Clock
    provider: Provider
    settings: CoreSettings

    @property
    def context(self) -> ApiContext:
        context = self.app.state.context
        assert isinstance(context, ApiContext)
        return context

    @property
    def sessions(self) -> InMemorySessionStore:
        store = self.ports.sessions
        assert isinstance(store, InMemorySessionStore)
        return store

    @property
    def audit(self) -> InMemoryAuditLog:
        log = self.ports.audit
        assert isinstance(log, InMemoryAuditLog)
        return log

    def start_login(self) -> dict[str, str]:
        response = self.client.get(LOGIN)
        assert response.status_code == 302, response.text
        query = {k: v[0] for k, v in parse_qs(urlsplit(response.headers["location"]).query).items()}
        self.provider.nonce = query["nonce"]
        return query

    def finish_login(self, query: dict[str, str], code: str = "code-1") -> httpx.Response:
        response: httpx.Response = self.client.get(
            CALLBACK, params={"code": code, "state": query["state"]}
        )
        return response

    def login(self) -> httpx.Response:
        return self.finish_login(self.start_login())

    def csrf(self) -> str:
        me = self.client.get(ME)
        assert me.status_code == 200, me.text
        return str(me.json()["csrf_token"])


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "oidc-client-secret"
    path.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    return path


def _world(secret_file: Path, oidc: dict[str, Any] | None = None, **api: Any) -> World:
    clock = Clock()
    oidc_settings = OidcSettings(
        **{
            "issuer": ISSUER,
            "client_id": CLIENT_ID,
            "client_secret_file": secret_file,
            **(oidc or {}),
        }
    )
    settings = CoreSettings(
        _env_file=None,
        api=ApiSettings(
            **{
                "insecure_http": True,
                "oidc": oidc_settings,
                "bootstrap_group_roles": {"engineers": "integration_engineer", "admins": "admin"},
                **api,
            }
        ),
    )
    issuer = oidc_settings.issuer or ISSUER
    provider = Provider(clock, issuer=issuer, issuer_in_discovery=issuer)
    oidc_client = OidcClient(
        settings.api.oidc, settings.api.redirect_url, http=provider.http(), clock=clock
    )
    ports = seeded_ports()
    app = create_app(settings, ports, clock=clock, oidc=oidc_client)
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    return World(client, app, ports, clock, provider, settings)


# --- login start -------------------------------------------------------------------------------


def test_login_redirects_to_the_provider_with_pkce_state_and_nonce(secret_file: Path) -> None:
    world = _world(secret_file)
    response = world.client.get(LOGIN)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/auth?")
    query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == "https://localhost/api/v1/auth/callback"
    assert query["code_challenge_method"] == "S256"
    assert "openid" in query["scope"].split()
    assert len(query["state"]) >= 32 and len(query["nonce"]) >= 32
    attempts = world.ports.login_attempts
    assert isinstance(attempts, InMemoryLoginAttemptStore)
    assert len(attempts) == 1
    assert world.provider.discovery_calls == 1
    world.client.get(LOGIN)
    assert world.provider.discovery_calls == 1, "discovery is fetched once per process"


def test_login_is_503_without_a_configured_provider() -> None:
    settings = CoreSettings(_env_file=None, api=ApiSettings(insecure_http=True))
    app = create_app(settings, seeded_ports())
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    response = client.get(LOGIN)
    assert response.status_code == 503
    assert response.json()["title"] == "login unavailable"


def test_discovery_naming_another_issuer_is_refused(secret_file: Path) -> None:
    world = _world(secret_file)
    world.provider.issuer_in_discovery = "https://evil.test"
    response = world.client.get(LOGIN)
    assert response.status_code == 503
    with pytest.raises(OidcError) as info:
        world.context.oidc.discovery()
    assert info.value.kind == "discovery"


def test_login_is_rate_limited_per_address(secret_file: Path) -> None:
    world = _world(secret_file, login_starts_per_minute_per_ip=3)
    for _ in range(3):
        assert world.client.get(LOGIN).status_code == 302
    limited = world.client.get(LOGIN)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.json()["title"] == "too many requests"
    assert world.context.metrics.get("carto_api_rate_limited_total") == 1
    assert world.context.metrics.get("carto_api_login_starts_rate_limited_total") == 1
    assert world.context.metrics.get("carto_api_login_callbacks_rate_limited_total") == 0
    world.clock.advance(minutes=1)
    assert world.client.get(LOGIN).status_code == 302


def test_callbacks_with_a_valid_cookie_are_not_limited_per_address(secret_file: Path) -> None:
    """A whole office behind one NAT address signs in: the callback is limited per login
    attempt (the single-use state behind the login cookie), not per address, so the junk budget
    of that address being spent does not lock the office out (ADR 0070 section 5)."""
    world = _world(secret_file)  # login_rate_per_minute_per_ip=10 is the junk-callback budget
    world.client.cookies.clear()
    junk = [
        world.client.get(CALLBACK, params={"code": "c", "state": f"nope-{n}"}) for n in range(11)
    ]
    assert [response.status_code for response in junk] == [400] * 10 + [429]
    assert int(junk[-1].headers["retry-after"]) >= 1
    assert junk[-1].json()["title"] == "too many requests"
    assert world.context.metrics.get("carto_api_login_failures_total") == 10, (
        "a limited callback is refused before it is examined or audited"
    )
    assert world.context.metrics.get("carto_api_audit_rows_total") == 10
    for n in range(11):
        response = world.login()
        assert response.status_code == 302, (n, response.text)
        world.client.cookies.delete(SESSION_COOKIE_NAME)
    assert len(world.sessions) == 11
    assert world.context.metrics.get("carto_api_logins_total") == 11
    assert world.context.metrics.get("carto_api_rate_limited_total") == 1
    assert world.context.metrics.get("carto_api_login_callbacks_rate_limited_total") == 1
    assert world.context.metrics.get("carto_api_login_starts_rate_limited_total") == 0


def test_a_callback_with_a_foreign_cookie_spends_the_junk_budget_of_its_address(
    secret_file: Path,
) -> None:
    world = _world(secret_file, login_rate_per_minute_per_ip=1)
    own = world.start_login()
    own_cookie = world.client.cookies.get(LOGIN_COOKIE_NAME)
    assert own_cookie
    world.client.cookies.clear()
    other = world.start_login()
    world.client.cookies.set(LOGIN_COOKIE_NAME, own_cookie)
    foreign = world.finish_login(other)
    assert foreign.status_code == 400, "the one junk token of this address"
    attempts = world.ports.login_attempts
    assert isinstance(attempts, InMemoryLoginAttemptStore)
    assert len(attempts) == 1, "the other attempt was consumed"
    world.provider.nonce = own["nonce"]
    assert world.finish_login(own).status_code == 302, "a valid cookie needs no address budget"
    assert len(attempts) == 0
    world.client.cookies.clear()
    limited = world.client.get(CALLBACK, params={"error": "access_denied"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert len(attempts) == 0, "nothing to pop: the cookie-less callback never reaches the store"


# --- callback ----------------------------------------------------------------------------------


def test_callback_issues_a_session_with_the_cookie_flags(secret_file: Path) -> None:
    world = _world(secret_file)
    query = world.start_login()
    response = world.finish_login(query)
    assert response.status_code == 302, response.text
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    lowered = cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered
    assert "domain=" not in lowered
    value = world.client.cookies.get(SESSION_COOKIE_NAME)
    assert value
    assert len(world.sessions) == 1
    assert world.sessions.get(hash_session_id(value)) is not None, "only the hash is stored"
    assert world.sessions.get(value) is None

    [token_request] = world.provider.token_requests
    form = {k: v[0] for k, v in parse_qs(token_request.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "code-1"
    assert form["redirect_uri"] == world.settings.api.redirect_url
    digest = hashlib.sha256(form["code_verifier"].encode("ascii")).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert token_request.headers["authorization"] == f"Basic {basic}"

    me = world.client.get(ME).json()
    assert me["subject"] == "alice"
    assert me["display_name"] == "Alice Example"
    assert me["roles"] == ["integration_engineer"]
    assert "review_links" in me["permissions"] and "reveal" not in me["permissions"]
    assert len(me["csrf_token"]) >= 16
    assert [entry.action for entry in world.audit.entries()] == ["auth.login"]
    assert world.audit.entries()[0].after == {"roles": ["integration_engineer"]}
    assert world.context.metrics.get("carto_api_logins_total") == 1

    record = world.sessions.get(hash_session_id(value))
    assert record is not None
    assert record.authenticated_at == NOW - timedelta(seconds=5), "auth_time drives step-up"
    assert record.ip == "testclient"


def test_a_state_is_usable_once_and_unknown_states_are_refused(secret_file: Path) -> None:
    world = _world(secret_file)
    query = world.start_login()
    assert world.finish_login(query).status_code == 302
    replay = world.finish_login(query)
    assert replay.status_code == 400
    assert replay.json()["title"] == "login failed"
    assert world.client.get(CALLBACK, params={"code": "c", "state": "unknown"}).status_code == 400
    assert world.client.get(CALLBACK, params={"error": "access_denied"}).status_code == 400
    assert world.client.get(CALLBACK).status_code == 400
    assert world.context.metrics.get("carto_api_login_failures_total") == 4


def test_an_expired_login_attempt_is_refused(secret_file: Path) -> None:
    world = _world(secret_file)
    query = world.start_login()
    world.clock.advance(minutes=11)
    assert world.finish_login(query).status_code == 400
    assert len(world.sessions) == 0


@pytest.mark.parametrize(
    ("override", "step"),
    [
        ({"nonce": "not-the-nonce"}, "token"),
        ({"iss": "https://other.test"}, "token"),
        ({"aud": "another-client"}, "token"),
        ({"exp": int(NOW.timestamp()) - 3600}, "token"),
        ({"sub": None}, "token"),
    ],
)
def test_a_bad_id_token_is_refused(secret_file: Path, override: dict[str, Any], step: str) -> None:
    world = _world(secret_file)
    query = world.start_login()
    world.provider.overrides = override
    response = world.finish_login(query)
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["title"] == "login failed"
    assert step in body["detail"]
    assert "eyJ" not in response.text, "no token fragment in the problem"
    assert len(world.sessions) == 0
    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")
    assert _cleared(response, LOGIN_COOKIE_NAME), "the login cookie is cleared on failure"
    failed = world.audit.entries()[-1]
    assert failed.action == "auth.login_failed" and failed.actor == ANONYMOUS_ACTOR
    assert failed.target == step
    assert "eyJ" not in failed.reason and "not-the-nonce" not in failed.reason


def test_a_token_signed_by_another_key_is_refused(secret_file: Path) -> None:
    world = _world(secret_file)
    query = world.start_login()
    other = Provider(world.clock)
    other.nonce = world.provider.nonce
    world.provider.private_pem = other.private_pem  # signs with a key the JWKS does not list
    assert world.finish_login(query).status_code == 400
    assert world.provider.jwks_calls == 2, "the JWKS is refreshed once before refusing"


def test_groups_without_a_binding_grant_no_role(secret_file: Path) -> None:
    world = _world(secret_file)
    world.provider.groups = ["unmapped-group"]
    assert world.login().status_code == 302
    me = world.client.get(ME)
    assert me.status_code == 200
    assert me.json()["roles"] == [] and me.json()["permissions"] == []
    for path in ("/links", "/entities", "/systems", "/sources", "/fields", "/audit"):
        assert world.client.get(f"{API_PREFIX}{path}").status_code == 403, path


def _issued_value(response: httpx.Response) -> str:
    cookie = response.headers["set-cookie"]
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _cookie_lines(response: httpx.Response, name: str) -> list[str]:
    return [line for line in response.headers.get_list("set-cookie") if line.startswith(f"{name}=")]


def _cleared(response: httpx.Response, name: str) -> bool:
    """One ``Set-Cookie`` for ``name`` with an empty value and ``Max-Age=0``."""
    lines = _cookie_lines(response, name)
    if len(lines) != 1:
        return False
    head, _, rest = lines[0].partition(";")
    return head.split("=", 1)[1].strip('"') == "" and "max-age=0" in rest.lower()


# --- the login cookie binds the callback to the browser that started it (ADR 0070, M7) -------


def test_login_sets_the_binding_cookie_and_the_attempt_keeps_only_its_hash(
    secret_file: Path,
) -> None:
    world = _world(secret_file)
    response = world.client.get(LOGIN)
    assert response.status_code == 302
    [line] = _cookie_lines(response, LOGIN_COOKIE_NAME)
    lowered = line.lower()
    assert "httponly" in lowered and "secure" in lowered and "samesite=lax" in lowered
    assert "path=/" in lowered and "domain=" not in lowered
    assert f"max-age={int(LOGIN_ATTEMPT_TTL.total_seconds())}" in lowered
    value = world.client.cookies.get(LOGIN_COOKIE_NAME)
    assert value and len(value) >= 32
    attempts = world.ports.login_attempts
    assert isinstance(attempts, InMemoryLoginAttemptStore)
    [attempt] = attempts.rows()
    assert attempt.binding_hash == hashlib.sha256(value.encode()).hexdigest()
    assert value not in repr(attempt), "only the hash is stored"


def test_callback_without_the_login_cookie_is_refused(secret_file: Path) -> None:
    """Login CSRF: an attacker starts a login and hands the callback URL to a victim whose
    browser never saw the login cookie."""
    world = _world(secret_file)
    query = world.start_login()
    world.client.cookies.delete(LOGIN_COOKIE_NAME)
    response = world.finish_login(query)
    assert response.status_code == 400, response.text
    assert "state" in response.json()["detail"]
    assert len(world.sessions) == 0
    assert world.provider.token_requests == [], "the code is never exchanged"
    assert world.audit.entries()[-1].action == "auth.login_failed"
    assert world.context.metrics.get("carto_api_login_failures_total") == 1


def test_callback_with_another_attempts_cookie_is_refused(secret_file: Path) -> None:
    world = _world(secret_file)
    victim = world.start_login()
    victim_cookie = world.client.cookies.get(LOGIN_COOKIE_NAME)
    assert victim_cookie
    world.client.cookies.clear()
    attacker = world.start_login()
    assert world.client.cookies.get(LOGIN_COOKIE_NAME) != victim_cookie
    # The victim's browser (its own cookie) opens the attacker's callback URL.
    world.client.cookies.set(LOGIN_COOKIE_NAME, victim_cookie)
    response = world.finish_login(attacker)
    assert response.status_code == 400
    assert len(world.sessions) == 0
    assert _cleared(response, LOGIN_COOKIE_NAME)
    attempts = world.ports.login_attempts
    assert isinstance(attempts, InMemoryLoginAttemptStore)
    assert len(attempts) == 1, "the attacker's state was consumed; the victim's own waits"
    # The victim's own flow still completes with its own cookie.
    world.provider.nonce = victim["nonce"]
    assert world.finish_login(victim).status_code == 302


def test_callback_clears_the_login_cookie_and_sets_the_session(secret_file: Path) -> None:
    world = _world(secret_file)
    response = world.login()
    assert response.status_code == 302, response.text
    assert _cookie_lines(response, SESSION_COOKIE_NAME)
    assert _cleared(response, LOGIN_COOKIE_NAME)
    assert world.client.cookies.get(LOGIN_COOKIE_NAME) is None
    assert world.client.get(ME).status_code == 200


def test_a_failed_login_writes_an_anonymous_audit_row_without_claim_values(
    secret_file: Path,
) -> None:
    world = _world(secret_file)
    response = world.client.get(
        CALLBACK, params={"code": "c-secret", "state": "s-secret"}, headers={"X-Request-Id": "r9"}
    )
    assert response.status_code == 400
    [entry] = world.audit.entries()
    assert entry.action == "auth.login_failed" and entry.actor == ANONYMOUS_ACTOR
    assert entry.target == "state" and entry.reason == "unknown or expired state"
    assert entry.request_id == "r9" and entry.ip == "testclient"
    assert "c-secret" not in repr(entry) and "s-secret" not in repr(entry)
    assert world.context.metrics.get("carto_api_login_failures_total") == 1
    assert world.context.metrics.get("carto_api_audit_rows_total") == 1


def test_login_issues_a_new_session_id_every_time(secret_file: Path) -> None:
    world = _world(secret_file)
    world.client.cookies.set(SESSION_COOKIE_NAME, "attacker-chosen-value")
    first = world.login()
    assert first.status_code == 302
    first_value = _issued_value(first)
    assert first_value != "attacker-chosen-value"
    second = world.login()
    assert second.status_code == 302
    assert _issued_value(second) != first_value
    assert world.sessions.get(hash_session_id("attacker-chosen-value")) is None
    assert len(world.sessions) == 2


# --- sessions ----------------------------------------------------------------------------------


def test_me_needs_a_session(secret_file: Path) -> None:
    world = _world(secret_file)
    response = world.client.get(ME)
    assert response.status_code == 401
    assert response.json()["title"] == "authentication required"
    world.client.cookies.set(SESSION_COOKIE_NAME, "no-such-session")
    assert world.client.get(ME).status_code == 401


def test_idle_timeout_ends_the_session(secret_file: Path) -> None:
    world = _world(secret_file)
    world.login()
    world.clock.advance(minutes=20)
    assert world.client.get(ME).status_code == 200, "activity slides the window"
    world.clock.advance(minutes=20)
    assert world.client.get(ME).status_code == 200
    world.clock.advance(minutes=31)
    assert world.client.get(ME).status_code == 401
    assert len(world.sessions) == 0, "an expired row is deleted"


def test_absolute_timeout_ends_the_session_despite_activity(secret_file: Path) -> None:
    world = _world(secret_file)
    world.login()
    for _ in range(12 * 3 - 1):
        world.clock.advance(minutes=20)
        assert world.client.get(ME).status_code == 200
    world.clock.advance(minutes=20)
    assert world.client.get(ME).status_code == 401, "12 hours after login, activity or not"


def test_me_reports_when_the_session_ends(secret_file: Path) -> None:
    world = _world(secret_file)
    world.login()
    me = world.client.get(ME).json()
    assert me["session_expires_at"] == "2026-10-18T12:30:00.000Z"
    for _ in range(35):
        world.clock.advance(minutes=20)
        assert world.client.get(ME).status_code == 200
    me = world.client.get(ME).json()
    assert me["session_expires_at"] == "2026-10-19T00:00:00.000Z", "the absolute deadline wins"


# --- CSRF and logout ---------------------------------------------------------------------------


def test_logout_needs_the_csrf_token_and_invalidates_the_session(secret_file: Path) -> None:
    world = _world(secret_file)
    world.login()
    missing = world.client.post(LOGOUT)
    assert missing.status_code == 403
    assert missing.json()["title"] == "forbidden"
    wrong = world.client.post(LOGOUT, headers={"X-CSRF-Token": "nope"})
    assert wrong.status_code == 403
    assert world.client.get(ME).status_code == 200, "still signed in"
    ok = world.client.post(LOGOUT, headers={"X-CSRF-Token": world.csrf()})
    assert ok.status_code == 204
    assert "max-age=0" in ok.headers["set-cookie"].lower()
    assert world.client.get(ME).status_code == 401
    assert len(world.sessions) == 0
    assert [entry.action for entry in world.audit.entries()] == ["auth.login", "auth.logout"]
    assert world.context.metrics.get("carto_api_csrf_refused_total") == 2


def test_csrf_token_is_per_session(secret_file: Path) -> None:
    world = _world(secret_file)
    world.login()
    first = world.csrf()
    world.client.cookies.clear()
    world.login()
    assert world.csrf() != first


def test_mutations_without_a_session_are_401_before_csrf(secret_file: Path) -> None:
    world = _world(secret_file)
    response = world.client.post(LOGOUT)
    assert response.status_code == 401


def test_requests_are_rate_limited_per_user(secret_file: Path) -> None:
    world = _world(secret_file, request_rate_per_minute_per_user=10)
    world.login()
    statuses = [world.client.get(ME).status_code for _ in range(12)]
    assert statuses.count(200) == 10 and statuses[-1] == 429


# --- back channel, CA file and the groups claim (ADR 0052, Compose dev stack) ------------------

PUBLIC_ISSUER = "https://localhost:8443/dex"
BACKCHANNEL = "http://dex:5556/dex"


def test_backchannel_rewrites_the_provider_endpoints_but_not_the_browser_url(
    secret_file: Path,
) -> None:
    world = _world(
        secret_file,
        oidc={"issuer": PUBLIC_ISSUER, "backchannel_url": BACKCHANNEL, "allow_http_issuer": True},
    )
    query = world.start_login()
    assert world.provider.urls == [f"{BACKCHANNEL}/.well-known/openid-configuration"]
    discovery = world.context.oidc.discovery()
    assert discovery.authorization_endpoint == f"{PUBLIC_ISSUER}/auth"
    assert discovery.token_endpoint == f"{BACKCHANNEL}/token"
    assert discovery.jwks_uri == f"{BACKCHANNEL}/keys"
    response = world.finish_login(query)
    assert response.status_code == 302, response.text
    assert all(url.startswith(BACKCHANNEL) for url in world.provider.urls), world.provider.urls
    assert world.client.get(ME).json()["roles"] == ["integration_engineer"]


def test_backchannel_login_redirects_the_browser_to_the_issuer(secret_file: Path) -> None:
    world = _world(
        secret_file,
        oidc={"issuer": PUBLIC_ISSUER, "backchannel_url": BACKCHANNEL, "allow_http_issuer": True},
    )
    response = world.client.get(LOGIN)
    assert response.status_code == 302
    assert response.headers["location"].startswith(f"{PUBLIC_ISSUER}/auth?")
    assert BACKCHANNEL not in response.headers["location"]


def test_backchannel_still_requires_iss_to_equal_the_issuer(secret_file: Path) -> None:
    world = _world(
        secret_file,
        oidc={"issuer": PUBLIC_ISSUER, "backchannel_url": BACKCHANNEL, "allow_http_issuer": True},
    )
    query = world.start_login()
    world.provider.overrides = {"iss": BACKCHANNEL}
    response = world.finish_login(query)
    assert response.status_code == 400
    assert "token" in response.json()["detail"]
    assert len(world.sessions) == 0
    world.provider.issuer_in_discovery = BACKCHANNEL
    fresh = _world(
        secret_file,
        oidc={"issuer": PUBLIC_ISSUER, "backchannel_url": BACKCHANNEL, "allow_http_issuer": True},
    )
    fresh.provider.issuer_in_discovery = BACKCHANNEL
    assert fresh.client.get(LOGIN).status_code == 503, "discovery must name the public issuer"


def test_backchannel_leaves_other_hosts_alone(secret_file: Path) -> None:
    world = _world(
        secret_file,
        oidc={"issuer": PUBLIC_ISSUER, "backchannel_url": BACKCHANNEL, "allow_http_issuer": True},
    )
    client = world.context.oidc
    assert client.backchannel(f"{PUBLIC_ISSUER}/keys") == f"{BACKCHANNEL}/keys"
    assert client.backchannel("https://keys.example.test/jwks") == "https://keys.example.test/jwks"
    plain = _world(secret_file).context.oidc
    assert plain.backchannel(f"{ISSUER}/keys") == f"{ISSUER}/keys"


def _ca_pem(tmp_path: Path) -> Path:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "carto test CA")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "ca.pem"
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path


def test_ca_file_reaches_the_backchannel_client(tmp_path: Path, secret_file: Path) -> None:
    ca = _ca_pem(tmp_path)
    settings = OidcSettings(issuer=ISSUER, client_id=CLIENT_ID, ca_file=ca)
    context = backchannel_ssl_context(settings)
    assert context is not None
    commons = [
        attribute[1]
        for cert in context.get_ca_certs()
        for rdn in cert["subject"]
        for attribute in rdn
        if attribute[0] == "commonName"
    ]
    assert commons == ["carto test CA"], "only the install CA is trusted"
    with build_backchannel_client(settings) as client:
        assert isinstance(client, httpx.Client)
    assert backchannel_ssl_context(OidcSettings(issuer=ISSUER, client_id=CLIENT_ID)) is None
    missing = OidcSettings(issuer=ISSUER, client_id=CLIENT_ID, ca_file=tmp_path / "nope.pem")
    with pytest.raises(CoreConfigError):
        backchannel_ssl_context(missing)
    with pytest.raises(OidcError) as info:
        OidcClient(missing, "https://localhost/api/v1/auth/callback").discovery()
    assert info.value.kind == "not_configured"
    del secret_file


def test_a_string_groups_claim_is_a_one_element_group_list(secret_file: Path) -> None:
    world = _world(secret_file)
    world.provider.overrides = {"groups": "engineers"}
    assert world.login().status_code == 302
    assert world.client.get(ME).json()["roles"] == ["integration_engineer"]


def test_roles_can_be_bound_by_the_email_claim(secret_file: Path) -> None:
    world = _world(
        secret_file,
        oidc={"groups_claim": "email"},
        bootstrap_group_roles={"alice@example.test": "admin"},
    )
    world.provider.groups = []
    assert world.login().status_code == 302
    assert world.client.get(ME).json()["roles"] == ["admin"]
