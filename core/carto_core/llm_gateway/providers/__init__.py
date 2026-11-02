"""What every provider adapter shares (ADR 0054; spec 9.9, 14.3).

An adapter owns the HTTP shape of one provider and nothing else. This module gives it the
pieces that must behave the same everywhere so no adapter can weaken them:

- :func:`read_api_key` reads ``llm.api_key_file`` at use (spec 14.3: fetched at use, never an
  environment value, never logged), bounded in size, and returns the trimmed text; the value
  goes into one request header and nowhere else.
- :func:`post_json` sends one JSON request through the allowlisted client of
  :mod:`carto_core.llm_gateway.egress` and maps every failure to
  :class:`~carto_core.llm_gateway.schemas.ProviderError` with a status class only (``http
  4xx``, ``http 5xx``, ``timeout``, ``transport error``, ``malformed response``). A refusal the
  gateway may retry (408, 429, 5xx, timeout, transport) is a :class:`TransientProviderError`.
  No message ever carries a response body, a URL or a payload byte.
- :class:`Completion` carries the text and the token counts; :class:`UsageProvider` is the
  optional widening of :class:`~carto_core.llm_gateway.schemas.Provider` that returns it, so
  the audit row gets token counts without changing the fixed seam.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol, runtime_checkable

import httpx

from carto_core.llm_gateway.schemas import ProviderError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "MAX_API_KEY_BYTES",
    "Completion",
    "TransientProviderError",
    "UsageProvider",
    "parse_json_object",
    "post_json",
    "read_api_key",
    "send_json",
    "status_class",
]

MAX_API_KEY_BYTES: Final = 4096
"""Longest credential file accepted; provider keys are a few hundred bytes."""

_TRANSIENT_STATUSES: Final = frozenset({408, 429})


class TransientProviderError(ProviderError):
    """A refusal that may clear on retry: 408, 429, any 5xx, a timeout or a transport error."""


@dataclass(frozen=True, slots=True)
class Completion:
    """One model answer: the raw text and the token counts the provider reported, if any."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class UsageProvider(Protocol):
    """A :class:`~carto_core.llm_gateway.schemas.Provider` that also reports token usage."""

    name: str

    def complete_with_usage(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> Completion: ...


def read_api_key(path: Path | None, provider: str) -> str:
    """The credential from ``path``, trimmed of line ends; errors name the provider only."""
    if path is None:
        msg = f"{provider}: llm.api_key_file is required"
        raise ProviderError(msg)
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_API_KEY_BYTES + 1)
    except OSError as exc:
        msg = f"{provider}: cannot read the credential file"
        raise ProviderError(msg) from exc
    if len(data) > MAX_API_KEY_BYTES:
        msg = f"{provider}: credential file larger than {MAX_API_KEY_BYTES} bytes"
        raise ProviderError(msg)
    try:
        key = data.decode("utf-8").strip("\r\n\t ")
    except UnicodeDecodeError as exc:
        msg = f"{provider}: credential file is not UTF-8 text"
        raise ProviderError(msg) from exc
    if not key or not key.isprintable():
        msg = f"{provider}: credential file is empty or not printable"
        raise ProviderError(msg)
    return key


def status_class(status_code: int) -> str:
    """``http 4xx`` for 404, ``http 5xx`` for 503: what an error message may say."""
    return f"http {status_code // 100}xx"


def send_json(
    client: httpx.Client,
    path: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, object],
    provider: str,
) -> httpx.Response:
    """POST ``body`` as JSON to ``path`` on the client's endpoint and return the response.

    The path is relative, so the client's ``base_url`` (the configured endpoint) supplies the
    origin and the allowlist checks it anyway. A transport failure is a :class:`ProviderError`
    whose message names the provider and the failure class; the status is not judged here.
    """
    try:
        return client.post(path, headers=dict(headers), json=body)
    except ProviderError:
        raise
    except httpx.TimeoutException as exc:
        msg = f"{provider}: timeout"
        raise TransientProviderError(msg) from exc
    except httpx.TransportError as exc:
        msg = f"{provider}: transport error ({type(exc).__name__})"
        raise TransientProviderError(msg) from exc
    except httpx.HTTPError as exc:
        msg = f"{provider}: request failed ({type(exc).__name__})"
        raise ProviderError(msg) from exc


def parse_json_object(response: httpx.Response, provider: str) -> dict[str, object]:
    """The JSON object of a 2xx response; any other status or body is a :class:`ProviderError`
    naming a status class only (408, 429 and 5xx as :class:`TransientProviderError`)."""
    status = response.status_code
    if status >= 500 or status in _TRANSIENT_STATUSES:
        msg = f"{provider}: {status_class(status)}"
        raise TransientProviderError(msg)
    if status >= 300:
        msg = f"{provider}: {status_class(status)}"
        raise ProviderError(msg)
    try:
        parsed: object = json.loads(response.content)
    except (ValueError, UnicodeDecodeError) as exc:
        msg = f"{provider}: malformed response (not JSON)"
        raise ProviderError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"{provider}: malformed response (not an object)"
        raise ProviderError(msg)
    return parsed


def post_json(
    client: httpx.Client,
    path: str,
    *,
    headers: Mapping[str, str],
    body: Mapping[str, object],
    provider: str,
) -> dict[str, object]:
    """:func:`send_json` then :func:`parse_json_object`."""
    response = send_json(client, path, headers=headers, body=body, provider=provider)
    return parse_json_object(response, provider)
