"""The LLM gateway's egress allowlist as an ``httpx`` client (spec 9.9, 14.5; ADR 0054).

Spec 9.9: "Network egress allows only the configured endpoint." The network policy enforces it
outside the process; this module enforces it inside, so an adapter bug, a redirect or a proxy
variable cannot route a payload anywhere else:

- every request passes :func:`check_request` twice, in a request event hook and again in
  :class:`AllowlistTransport` directly in front of the connection, and is refused with
  :class:`~carto_core.llm_gateway.schemas.ProviderError` unless its scheme is ``https`` and its
  host and port are the configured endpoint's;
- ``trust_env=False``: no proxy, CA or ``NO_PROXY`` variables from the environment;
- ``follow_redirects=False`` and the hook refuses a redirected request even when a caller asks
  for following, because the redirect target is a request like any other;
- the timeout comes from ``llm.timeout_seconds`` and applies to connect, read, write and pool;
- a response is read to at most :data:`MAX_RESPONSE_BYTES` (1 MiB, spec 2.3 invariant 8): a
  larger ``Content-Length`` is refused before the body is read and a longer stream is cut off
  while it is read.

Error messages name the refusal, never a URL beyond the host, and never a payload byte.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import httpx

from carto_core.llm_gateway.schemas import ProviderError

if TYPE_CHECKING:
    from carto_common.settings import LlmSettings

__all__ = [
    "MAX_RESPONSE_BYTES",
    "AllowlistTransport",
    "EndpointPolicy",
    "build_client",
    "check_request",
    "endpoint_policy",
]

MAX_RESPONSE_BYTES: Final = 1 << 20
"""Longest response body the client reads (1 MiB); a suggestion list is a few kilobytes."""

_HTTPS_PORT: Final = 443


@dataclass(frozen=True, slots=True)
class EndpointPolicy:
    """The one origin the gateway may talk to: ``https`` and this host and port."""

    host: str
    port: int

    def allows(self, url: httpx.URL) -> str | None:
        """Return why ``url`` is refused, or ``None`` when it is the configured origin."""
        if url.scheme != "https":
            return "scheme is not https"
        if (url.host or "").lower() != self.host:
            return "host is not the configured endpoint"
        if (url.port or _HTTPS_PORT) != self.port:
            return "port is not the configured endpoint's"
        return None


def endpoint_policy(settings: LlmSettings) -> EndpointPolicy:
    """Derive the policy from ``llm.endpoint``; refuse an endpoint that is not a clean origin."""
    if settings.endpoint is None:
        msg = "llm.endpoint is required"
        raise ProviderError(msg)
    try:
        url = httpx.URL(settings.endpoint)
    except (httpx.InvalidURL, TypeError) as exc:
        msg = "llm.endpoint is not a valid URL"
        raise ProviderError(msg) from exc
    if url.scheme != "https" or not url.host:
        msg = "llm.endpoint must be an https URL with a host"
        raise ProviderError(msg)
    if url.userinfo or url.query or url.fragment:
        msg = "llm.endpoint must not carry userinfo, a query or a fragment"
        raise ProviderError(msg)
    return EndpointPolicy(host=url.host.lower(), port=url.port or _HTTPS_PORT)


def check_request(policy: EndpointPolicy, request: httpx.Request) -> None:
    """Raise :class:`ProviderError` unless ``request`` goes to the configured origin."""
    reason = policy.allows(request.url)
    if reason is not None:
        msg = f"egress refused: {reason} (allowed: {policy.host}:{policy.port})"
        raise ProviderError(msg)


class _CappedStream(httpx.SyncByteStream):
    """Yield at most ``limit`` bytes from ``inner`` and refuse the rest."""

    def __init__(self, inner: httpx.SyncByteStream, limit: int) -> None:
        self._inner = inner
        self._limit = limit

    def __iter__(self) -> Iterator[bytes]:
        total = 0
        for chunk in self._inner:
            total += len(chunk)
            if total > self._limit:
                msg = "response refused: body larger than the limit"
                raise ProviderError(msg)
            yield chunk

    def close(self) -> None:
        self._inner.close()


class AllowlistTransport(httpx.BaseTransport):
    """Check the origin directly in front of the connection and cap the response body."""

    def __init__(
        self,
        policy: EndpointPolicy,
        inner: httpx.BaseTransport,
        *,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._policy = policy
        self._inner = inner
        self._max_response_bytes = max_response_bytes

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        check_request(self._policy, request)
        response = self._inner.handle_request(request)
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_response_bytes:
            response.close()
            msg = "response refused: declared length larger than the limit"
            raise ProviderError(msg)
        stream = response.stream
        if not isinstance(stream, httpx.SyncByteStream):  # pragma: no cover - sync client only
            response.close()
            msg = "response refused: not a synchronous stream"
            raise ProviderError(msg)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_CappedStream(stream, self._max_response_bytes),
            extensions=response.extensions,
            request=request,
        )

    def close(self) -> None:
        self._inner.close()


def build_client(
    settings: LlmSettings, *, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """An ``httpx.Client`` that can only reach ``llm.endpoint`` (module docstring).

    ``transport`` replaces the network transport (tests pass ``httpx.MockTransport``); the
    allowlist wraps whatever is given, so a test transport is checked like the real one.
    """
    policy = endpoint_policy(settings)
    inner = transport if transport is not None else httpx.HTTPTransport(retries=0)
    timeout = httpx.Timeout(settings.timeout_seconds)

    def refuse_unless_allowed(request: httpx.Request) -> None:
        check_request(policy, request)

    return httpx.Client(
        base_url=settings.endpoint or "",
        transport=AllowlistTransport(policy, inner),
        trust_env=False,
        follow_redirects=False,
        max_redirects=0,
        timeout=timeout,
        event_hooks={"request": [refuse_unless_allowed]},
    )
