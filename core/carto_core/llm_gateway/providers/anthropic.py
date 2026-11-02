"""The Anthropic Messages API adapter (ADR 0054; spec 9.9).

``POST {endpoint}/v1/messages`` with ``x-api-key`` from ``llm.api_key_file``,
``anthropic-version: 2023-06-01``, the configured model and ``max_tokens``, the task prompt as
``system`` and the fenced payload as the one user message. No tools are declared (spec 9.9: the
model has no tools). The answer is the first ``text`` content block; token counts come from
``usage``. Everything else, errors included, is the shared behaviour of
:mod:`carto_core.llm_gateway.providers`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from carto_core.llm_gateway.providers import Completion, post_json, read_api_key
from carto_core.llm_gateway.schemas import ProviderError

if TYPE_CHECKING:
    import httpx

    from carto_common.settings import LlmSettings

__all__ = ["ANTHROPIC_VERSION", "MESSAGES_PATH", "AnthropicProvider"]

ANTHROPIC_VERSION: Final = "2023-06-01"
MESSAGES_PATH: Final = "/v1/messages"


def _usage_count(usage: object, key: str) -> int | None:
    if isinstance(usage, dict):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


class AnthropicProvider:
    """One adapter: the HTTP shape of the Messages API and nothing else."""

    name = "anthropic"

    def __init__(self, settings: LlmSettings, client: httpx.Client) -> None:
        if not settings.model:
            msg = "anthropic: llm.model is required"
            raise ProviderError(msg)
        self._settings = settings
        self._client = client
        self._model = settings.model

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> str:
        return self.complete_with_usage(task_prompt, payload_json, output_schema).text

    def complete_with_usage(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> Completion:
        del output_schema  # the schema travels inside the task prompt; no tool is declared
        headers = {
            "x-api-key": read_api_key(self._settings.api_key_file, self.name),
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }
        body: dict[str, object] = {
            "model": self._model,
            "max_tokens": self._settings.max_output_tokens,
            "system": task_prompt,
            "messages": [{"role": "user", "content": payload_json}],
        }
        answer = post_json(
            self._client, MESSAGES_PATH, headers=headers, body=body, provider=self.name
        )
        content = answer.get("content")
        if not isinstance(content, list):
            msg = "anthropic: malformed response (no content list)"
            raise ProviderError(msg)
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    usage = answer.get("usage")
                    return Completion(
                        text=text,
                        input_tokens=_usage_count(usage, "input_tokens"),
                        output_tokens=_usage_count(usage, "output_tokens"),
                    )
        msg = "anthropic: malformed response (no text block)"
        raise ProviderError(msg)
