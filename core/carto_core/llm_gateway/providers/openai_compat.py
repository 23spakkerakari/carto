"""The OpenAI-compatible chat completions adapter (ADR 0054; spec 9.9).

``POST {endpoint}/chat/completions`` with ``Authorization: Bearer`` from ``llm.api_key_file``,
the configured model, ``max_tokens``, a ``system`` message (the task prompt) and a ``user``
message (the fenced payload), ``temperature`` 0 and, while the endpoint accepts it,
``response_format: {"type": "json_object"}``. Many self-hosted and gateway deployments speak
this shape but not every one knows ``response_format``: when such an endpoint answers 400 and
the first bytes of its error text name ``response_format``, the adapter repeats the request
once without the field and stops sending it. The error body is inspected for that one word and
never repeated. The answer is ``choices[0].message.content``; token counts come from
``usage.prompt_tokens`` and ``usage.completion_tokens``. Errors are the shared behaviour of
:mod:`carto_core.llm_gateway.providers`.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Final

import httpx

from carto_core.llm_gateway.providers import (
    Completion,
    parse_json_object,
    read_api_key,
    send_json,
)
from carto_core.llm_gateway.schemas import ProviderError

if TYPE_CHECKING:
    from carto_common.settings import LlmSettings

__all__ = ["CHAT_COMPLETIONS_PATH", "OpenAICompatibleProvider"]

CHAT_COMPLETIONS_PATH: Final = "/chat/completions"
_JSON_OBJECT: Final[dict[str, object]] = {"type": "json_object"}
_PROBE_BYTES: Final = 4096


def _usage_count(usage: object, key: str) -> int | None:
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def _refused_response_format(response: httpx.Response) -> bool:
    """A 400 whose first bytes name ``response_format``: the endpoint does not know the field."""
    if response.status_code != httpx.codes.BAD_REQUEST:
        return False
    return b"response_format" in response.content[:_PROBE_BYTES]


class OpenAICompatibleProvider:
    """One adapter: the HTTP shape of chat completions and nothing else."""

    name = "openai"

    def __init__(
        self, settings: LlmSettings, client: httpx.Client, *, json_mode: bool = True
    ) -> None:
        if not settings.model:
            msg = "openai: llm.model is required"
            raise ProviderError(msg)
        self._settings = settings
        self._client = client
        self._model = settings.model
        self._json_mode = json_mode
        self._lock = threading.Lock()

    @property
    def model(self) -> str:
        return self._model

    @property
    def json_mode(self) -> bool:
        """Whether ``response_format`` is still sent (false once the endpoint refused it)."""
        with self._lock:
            return self._json_mode

    def complete(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> str:
        return self.complete_with_usage(task_prompt, payload_json, output_schema).text

    def complete_with_usage(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> Completion:
        del output_schema  # the schema travels inside the task prompt
        headers = {
            "authorization": f"Bearer {read_api_key(self._settings.api_key_file, self.name)}",
            "content-type": "application/json",
            "accept": "application/json",
        }
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._settings.max_output_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": task_prompt},
                {"role": "user", "content": payload_json},
            ],
        }
        if self.json_mode:
            response = self._send(body | {"response_format": _JSON_OBJECT}, headers)
            if _refused_response_format(response):
                with self._lock:
                    self._json_mode = False
                response = self._send(body, headers)
        else:
            response = self._send(body, headers)
        return _parse(parse_json_object(response, self.name))

    def _send(self, body: dict[str, object], headers: dict[str, str]) -> httpx.Response:
        return send_json(
            self._client, CHAT_COMPLETIONS_PATH, headers=headers, body=body, provider=self.name
        )


def _parse(answer: dict[str, object]) -> Completion:
    choices = answer.get("choices")
    if not isinstance(choices, list) or not choices:
        msg = "openai: malformed response (no choices)"
        raise ProviderError(msg)
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        msg = "openai: malformed response (no message content)"
        raise ProviderError(msg)
    usage = answer.get("usage")
    return Completion(
        text=content,
        input_tokens=_usage_count(usage, "prompt_tokens"),
        output_tokens=_usage_count(usage, "completion_tokens"),
    )
