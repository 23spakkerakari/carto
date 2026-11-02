"""OIDC authorization code flow with PKCE (spec 14.1; ADR 0052; founder decision 1).

Explicit and small rather than a framework integration: the discovery document is fetched
once per process from ``<issuer>/.well-known/openid-configuration`` (and must name the same
issuer), the authorization URL is built here with ``state``, ``nonce`` and an S256 code
challenge, the code is exchanged at the token endpoint with the client secret read from its
file at use (never held on the settings object), and the ID token is verified against the
provider's JWKS with Authlib: signature, ``iss``, ``aud``/``azp``, ``exp``/``iat`` with 60 s
leeway, and the ``nonce`` of this login. The JWKS is cached and refreshed once when a token
names an unknown key.

Back channel (Compose, ADR 0052): browsers reach the provider at the issuer URL under the
console origin while the api process reaches it on the internal network. With
``api.oidc.backchannel_url`` set, the discovery, token and JWKS requests go to that base and
every discovered endpoint that starts with the issuer prefix is rewritten to it; the
authorization endpoint the browser is sent to is never rewritten and the ID token's ``iss``
must still equal the issuer. ``api.oidc.ca_file`` is the CA bundle the back channel trusts
(the install's private CA). The ``httpx.Client`` is injectable so tests use a mock transport;
the default client follows no redirects, times out in 10 s and only talks to ``https://``
endpoints unless ``allow_http_issuer`` is set for the Compose dev profile. Errors carry a kind
and never a token or a claim value.

``authlib.jose`` emits a deprecation warning at import (Authlib 1.9 moves to ``joserfc``, which
it depends on and whose errors it raises for claim validation); the warning is filtered here,
where the import is, so the workspace's ``filterwarnings = error`` holds everywhere else.
"""

from __future__ import annotations

import ssl
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode, urlsplit

import httpx
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from authlib.jose import JsonWebKey, JsonWebToken
    from authlib.jose.errors import JoseError
    from authlib.oidc.core import CodeIDToken
from joserfc.errors import JoseError as JoserfcError

from carto_common.logging import get_logger
from carto_core.settings import CoreConfigError

if TYPE_CHECKING:
    from carto_core.settings import OidcSettings

__all__ = [
    "Discovery",
    "Identity",
    "LoginStart",
    "OidcClient",
    "OidcError",
    "backchannel_ssl_context",
    "build_backchannel_client",
]

log = get_logger(component="api")

MAX_TOKEN_RESPONSE_BYTES: Final = 64 * 1024
MAX_DISCOVERY_BYTES: Final = 256 * 1024
MAX_ID_TOKEN_BYTES: Final = 16 * 1024
MAX_GROUPS: Final = 256
MAX_GROUP_LEN: Final = 256
MAX_SUBJECT_LEN: Final = 256
MAX_NAME_LEN: Final = 128
MAX_EMAIL_LEN: Final = 320
_HTTP_TIMEOUT_SECONDS: Final = 10.0
_LEEWAY_SECONDS: Final = 60
_ALGORITHMS: Final = [
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
]
_STATE_BYTES: Final = 32
_TOKEN_ERRORS: Final = (JoseError, JoserfcError, ValueError, TypeError)
"""Authlib 1.9 raises its own errors for decoding and joserfc's for claim validation."""


class OidcError(Exception):
    """The login cannot complete. ``kind`` is one of ``not_configured``, ``discovery``,
    ``exchange``, ``token`` (the ID token failed verification) or ``claims``."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    """Browser-facing: never rewritten to the back channel."""
    token_endpoint: str
    jwks_uri: str


@dataclass(frozen=True, slots=True)
class Identity:
    """What a verified ID token says about the user; nothing else is kept."""

    subject: str
    display_name: str
    email: str
    groups: tuple[str, ...]
    auth_time: datetime | None


@dataclass(frozen=True, slots=True)
class LoginStart:
    authorization_url: str
    state: str
    nonce: str
    code_verifier: str


def _bounded(value: object, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def backchannel_ssl_context(settings: OidcSettings) -> ssl.SSLContext | None:
    """The TLS context of the back channel: the system roots, or ``ca_file`` alone when set."""
    if settings.ca_file is None:
        return None
    try:
        return ssl.create_default_context(cafile=str(settings.ca_file))
    except (OSError, ssl.SSLError) as exc:
        msg = f"api.oidc.ca_file cannot be loaded: {type(exc).__name__}"
        raise CoreConfigError(msg) from exc


def build_backchannel_client(settings: OidcSettings) -> httpx.Client:
    context = backchannel_ssl_context(settings)
    return httpx.Client(
        timeout=_HTTP_TIMEOUT_SECONDS,
        follow_redirects=False,
        verify=context if context is not None else True,
    )


class OidcClient:
    def __init__(
        self,
        settings: OidcSettings,
        redirect_url: str,
        *,
        scopes: str | None = None,
        http: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._redirect_url = redirect_url
        self._scopes = scopes if scopes is not None else settings.scopes
        self._http = http
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._lock = Lock()
        self._discovery: Discovery | None = None
        self._jwks: Any = None
        self._jwt = JsonWebToken(_ALGORITHMS)

    @property
    def configured(self) -> bool:
        return self._settings.issuer is not None and self._settings.client_id is not None

    @property
    def settings(self) -> OidcSettings:
        return self._settings

    def _client(self) -> httpx.Client:
        if self._http is None:
            try:
                self._http = build_backchannel_client(self._settings)
            except CoreConfigError as exc:
                raise OidcError("not_configured", str(exc)) from exc
        return self._http

    def backchannel(self, url: str) -> str:
        """``url`` with the issuer prefix replaced by ``backchannel_url`` when one is set."""
        issuer, base = self._settings.issuer, self._settings.backchannel_url
        if base is None or issuer is None or not url.startswith(issuer):
            return url
        return base + url[len(issuer) :]

    def _check_url(self, url: str, what: str) -> str:
        parts = urlsplit(url)
        allowed = ("https",) + (("http",) if self._settings.allow_http_issuer else ())
        if parts.scheme not in allowed or not parts.hostname or parts.username:
            raise OidcError("discovery", f"{what} is not an allowed URL")
        return url

    def _get_json(self, url: str, limit: int, kind: str) -> dict[str, Any]:
        try:
            response = self._client().get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(kind, f"request failed: {type(exc).__name__}") from exc
        if response.status_code != 200 or len(response.content) > limit:
            raise OidcError(kind, f"provider answered {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise OidcError(kind, "provider answered non-JSON") from exc
        if not isinstance(data, dict):
            raise OidcError(kind, "provider answered a non-object")
        return data

    def discovery(self) -> Discovery:
        if not self.configured or self._settings.issuer is None:
            raise OidcError("not_configured", "api.oidc.issuer and client_id are required")
        with self._lock:
            if self._discovery is not None:
                return self._discovery
            issuer = self._settings.issuer
            data = self._get_json(
                self.backchannel(f"{issuer}/.well-known/openid-configuration"),
                MAX_DISCOVERY_BYTES,
                "discovery",
            )
            if data.get("issuer") != issuer:
                raise OidcError("discovery", "discovery document names another issuer")
            try:
                discovery = Discovery(
                    issuer=issuer,
                    authorization_endpoint=self._check_url(
                        str(data["authorization_endpoint"]), "authorization_endpoint"
                    ),
                    token_endpoint=self._check_url(
                        self.backchannel(str(data["token_endpoint"])), "token_endpoint"
                    ),
                    jwks_uri=self._check_url(self.backchannel(str(data["jwks_uri"])), "jwks_uri"),
                )
            except KeyError as exc:
                raise OidcError("discovery", "discovery document is incomplete") from exc
            self._discovery = discovery
            return discovery

    def begin(self) -> LoginStart:
        """A fresh state, nonce and PKCE verifier and the URL to send the browser to."""
        discovery = self.discovery()
        state = generate_token(_STATE_BYTES)
        nonce = generate_token(_STATE_BYTES)
        code_verifier = generate_token(64)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.client_id or "",
                "redirect_uri": self._redirect_url,
                "scope": self._scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": create_s256_code_challenge(code_verifier),
                "code_challenge_method": "S256",
            }
        )
        separator = "&" if "?" in discovery.authorization_endpoint else "?"
        return LoginStart(
            f"{discovery.authorization_endpoint}{separator}{query}", state, nonce, code_verifier
        )

    def _load_jwks(self, force: bool = False) -> Any:
        with self._lock:
            if self._jwks is not None and not force:
                return self._jwks
            discovery = self._discovery
            if discovery is None:
                raise OidcError("discovery", "discovery not loaded")
            data = self._get_json(discovery.jwks_uri, MAX_DISCOVERY_BYTES, "token")
            try:
                self._jwks = JsonWebKey.import_key_set(data)
            except _TOKEN_ERRORS as exc:
                raise OidcError("token", "provider JWKS is unusable") from exc
            return self._jwks

    def _decode(self, id_token: str, nonce: str, key_set: Any) -> Any:
        claims = self._jwt.decode(
            id_token,
            key=key_set,
            claims_cls=CodeIDToken,
            claims_options={
                "iss": {"essential": True, "value": self._settings.issuer},
                "aud": {"essential": True, "value": self._settings.client_id},
                "sub": {"essential": True},
                "exp": {"essential": True},
            },
            claims_params={"nonce": nonce, "client_id": self._settings.client_id},
        )
        claims.validate(now=int(self._clock().timestamp()), leeway=_LEEWAY_SECONDS)
        return claims

    def finish(self, code: str, code_verifier: str, nonce: str) -> Identity:
        """Exchange ``code`` and verify the ID token against the JWKS and this login's nonce."""
        discovery = self.discovery()
        try:
            secret = self._settings.read_client_secret()
        except CoreConfigError as exc:
            raise OidcError("not_configured", "client secret file unreadable") from exc
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_url,
            "code_verifier": code_verifier,
            "client_id": self._settings.client_id or "",
        }
        try:
            response = self._client().post(
                discovery.token_endpoint,
                data=form,
                auth=(self._settings.client_id or "", secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise OidcError("exchange", f"token request failed: {type(exc).__name__}") from exc
        if response.status_code != 200 or len(response.content) > MAX_TOKEN_RESPONSE_BYTES:
            raise OidcError("exchange", f"token endpoint answered {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise OidcError("exchange", "token endpoint answered non-JSON") from exc
        id_token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(id_token, str) or not id_token or len(id_token) > MAX_ID_TOKEN_BYTES:
            raise OidcError("exchange", "token response has no usable id_token")
        try:
            claims = self._decode(id_token, nonce, self._load_jwks())
        except _TOKEN_ERRORS:
            # One JWKS refresh covers key rotation; a second failure is the refusal.
            try:
                claims = self._decode(id_token, nonce, self._load_jwks(force=True))
            except _TOKEN_ERRORS as exc:
                raise OidcError("token", f"id_token refused: {type(exc).__name__}") from exc
        return self._identity(dict(claims))

    def _identity(self, claims: dict[str, Any]) -> Identity:
        subject = _bounded(claims.get("sub"), MAX_SUBJECT_LEN)
        if not subject:
            raise OidcError("claims", "id_token has no subject")
        raw_groups = claims.get(self._settings.groups_claim)
        groups: list[str] = []
        if isinstance(raw_groups, list):
            groups = [
                _bounded(group, MAX_GROUP_LEN)
                for group in raw_groups[:MAX_GROUPS]
                if isinstance(group, str) and group
            ]
        elif isinstance(raw_groups, str) and raw_groups:
            # A plain string claim (``email`` on a Dex static user) is a one-element group list.
            groups = [_bounded(raw_groups, MAX_GROUP_LEN)]
        display_name = (
            _bounded(claims.get("name"), MAX_NAME_LEN)
            or _bounded(claims.get("preferred_username"), MAX_NAME_LEN)
            or _bounded(claims.get("email"), MAX_NAME_LEN)
        )
        auth_time_raw = claims.get("auth_time")
        auth_time = (
            datetime.fromtimestamp(float(auth_time_raw), tz=UTC)
            if isinstance(auth_time_raw, int | float) and not isinstance(auth_time_raw, bool)
            else None
        )
        return Identity(
            subject=subject,
            display_name=display_name,
            email=_bounded(claims.get("email"), MAX_EMAIL_LEN),
            groups=tuple(groups),
            auth_time=auth_time,
        )
