"""Conformance of every registered adapter, plus the fake (ADR 0054): a recorded exchange
through ``httpx.MockTransport`` proves the credential header, the model, the two messages, the
parsed text and token counts, the error mapping (429, 500, timeout, 400) without any payload in
the message, and that nothing goes to a host other than the endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from carto_common.settings import LlmSettings
from carto_core.llm_gateway import providers as provider_tools
from carto_core.llm_gateway.egress import build_client
from carto_core.llm_gateway.providers import (
    Completion,
    TransientProviderError,
    UsageProvider,
    read_api_key,
)
from carto_core.llm_gateway.providers import anthropic as anthropic_module
from carto_core.llm_gateway.providers import openai_compat as openai_module
from carto_core.llm_gateway.providers.anthropic import AnthropicProvider
from carto_core.llm_gateway.providers.fake import FakeBehaviour, FakeProvider
from carto_core.llm_gateway.providers.openai_compat import OpenAICompatibleProvider
from carto_core.llm_gateway.registry import BUILT_PROVIDERS, DEFERRED_PROVIDERS, provider_for
from carto_core.llm_gateway.schemas import Provider, ProviderError

ENDPOINT = "https://llm.example/v1"
API_KEY = "sk-live-RECORDED-KEY-0001"
TASK_PROMPT = "You label fields. Output JSON only."
PAYLOAD = '{"task":"label_field","fields":[{"field_id":"f1","path":"orders.order_id"}]}'
ANSWER = (
    '{"suggestions":[{"target_id":"f1","label_type":"order","label":"Order number",'
    '"confidence":0.9}]}'
)

ANTHROPIC_RESPONSE: dict[str, Any] = {
    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-5",
    "content": [{"type": "text", "text": ANSWER}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 812, "output_tokens": 57},
}

OPENAI_RESPONSE: dict[str, Any] = {
    "id": "chatcmpl-9x1",
    "object": "chat.completion",
    "created": 1760745600,
    "model": "gpt-4o-mini-2024-07-18",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": ANSWER, "refusal": None},
            "logprobs": None,
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 812, "completion_tokens": 57, "total_tokens": 869},
    "system_fingerprint": "fp_1",
}


@dataclass(frozen=True)
class Exchange:
    """One recorded exchange: how the adapter must ask and what the provider answered."""

    provider: str
    path: str
    credential: Callable[[httpx.Headers], str | None]
    messages: Callable[[dict[str, Any]], tuple[str, str]]
    response: dict[str, Any]
    module: Any
    path_constant: str


EXCHANGES: dict[str, Exchange] = {
    "anthropic": Exchange(
        provider="anthropic",
        path="/v1/v1/messages",
        credential=lambda headers: headers.get("x-api-key"),
        messages=lambda body: (body["system"], body["messages"][0]["content"]),
        response=ANTHROPIC_RESPONSE,
        module=anthropic_module,
        path_constant="MESSAGES_PATH",
    ),
    "openai": Exchange(
        provider="openai",
        path="/v1/chat/completions",
        credential=lambda headers: (headers.get("authorization") or "").removeprefix("Bearer "),
        messages=lambda body: (body["messages"][0]["content"], body["messages"][1]["content"]),
        response=OPENAI_RESPONSE,
        module=openai_module,
        path_constant="CHAT_COMPLETIONS_PATH",
    ),
}


def test_every_built_provider_has_a_recorded_exchange() -> None:
    assert set(EXCHANGES) == set(BUILT_PROVIDERS)


def _settings(tmp_path: Path, provider: str, **overrides: Any) -> LlmSettings:
    key_file = tmp_path / "key"
    key_file.write_text(API_KEY + "\n")
    values: dict[str, Any] = {
        "enabled": True,
        "provider": provider,
        "endpoint": ENDPOINT,
        "model": "model-x",
        "api_key_file": key_file,
        "timeout_seconds": 5,
        "max_output_tokens": 256,
    }
    values.update(overrides)
    return LlmSettings(**values)


class Recorder:
    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)


def _adapter(
    tmp_path: Path, name: str, respond: Callable[[httpx.Request], httpx.Response]
) -> tuple[Provider, Recorder]:
    recorder = Recorder(respond)
    settings = _settings(tmp_path, name)
    client = build_client(settings, transport=httpx.MockTransport(recorder))
    return provider_for(settings, client), recorder


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
def test_the_request_shape_and_the_parsed_answer(tmp_path: Path, name: str) -> None:
    exchange = EXCHANGES[name]
    adapter, recorder = _adapter(
        tmp_path, name, lambda _r: httpx.Response(200, json=exchange.response)
    )
    assert isinstance(adapter, Provider)
    assert isinstance(adapter, UsageProvider)
    assert adapter.name == name

    completion = adapter.complete_with_usage(TASK_PROMPT, PAYLOAD, {"type": "object"})
    assert completion == Completion(ANSWER, 812, 57)
    assert adapter.complete(TASK_PROMPT, PAYLOAD, {}) == ANSWER

    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.url == httpx.URL("https://llm.example" + exchange.path)
    assert exchange.credential(request.headers) == API_KEY
    body = json.loads(request.content)
    assert body["model"] == "model-x"
    assert body["max_tokens"] == 256
    assert exchange.messages(body) == (TASK_PROMPT, PAYLOAD)
    assert "tools" not in body
    if name == "anthropic":
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert body["messages"][0]["role"] == "user"
    else:
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        assert body["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
@pytest.mark.parametrize(
    ("status", "transient"),
    [
        (429, True),
        (500, True),
        (503, True),
        (408, True),
        (400, False),
        (401, False),
        (404, False),
        (302, False),
    ],
)
def test_statuses_map_to_provider_errors_with_a_class_only(
    tmp_path: Path, name: str, status: int, transient: bool
) -> None:
    body = {"error": {"type": "x", "message": f"SECRET-ECHO {PAYLOAD}"}}
    adapter, _ = _adapter(tmp_path, name, lambda _r: httpx.Response(status, json=body))
    with pytest.raises(ProviderError) as info:
        adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert isinstance(info.value, TransientProviderError) is transient
    text = str(info.value)
    assert text == f"{name}: http {status // 100}xx"
    assert "SECRET-ECHO" not in text
    assert API_KEY not in text


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
@pytest.mark.parametrize(
    "exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.ConnectError("down")]
)
def test_timeouts_and_transport_errors_are_transient(
    tmp_path: Path, name: str, exc: Exception
) -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        raise exc

    adapter, _ = _adapter(tmp_path, name, respond)
    with pytest.raises(TransientProviderError) as info:
        adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert PAYLOAD not in str(info.value)
    assert "slow" not in str(info.value)


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"content": "text", "choices": "x"}).encode(),
        json.dumps({"content": [{"type": "tool_use"}], "choices": [{"message": {}}]}).encode(),
        json.dumps({"content": [], "choices": []}).encode(),
    ],
)
def test_malformed_answers_are_provider_errors(tmp_path: Path, name: str, body: bytes) -> None:
    adapter, _ = _adapter(tmp_path, name, lambda _r: httpx.Response(200, content=body))
    with pytest.raises(ProviderError, match="malformed response"):
        adapter.complete(TASK_PROMPT, PAYLOAD, {})


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
def test_missing_usage_gives_no_token_counts(tmp_path: Path, name: str) -> None:
    response = {k: v for k, v in EXCHANGES[name].response.items() if k != "usage"}
    adapter, _ = _adapter(tmp_path, name, lambda _r: httpx.Response(200, json=response))
    assert isinstance(adapter, UsageProvider)
    assert adapter.complete_with_usage(TASK_PROMPT, PAYLOAD, {}) == Completion(ANSWER, None, None)


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
def test_an_absolute_url_to_another_host_never_leaves(
    tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    exchange = EXCHANGES[name]
    monkeypatch.setattr(
        exchange.module, exchange.path_constant, "https://evil.example" + exchange.path
    )
    adapter, recorder = _adapter(
        tmp_path, name, lambda _r: httpx.Response(200, json=exchange.response)
    )
    with pytest.raises(ProviderError, match="egress refused"):
        adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert recorder.requests == []


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
def test_the_credential_is_read_at_use_and_errors_name_no_path(tmp_path: Path, name: str) -> None:
    adapter, recorder = _adapter(
        tmp_path, name, lambda _r: httpx.Response(200, json=EXCHANGES[name].response)
    )
    (tmp_path / "key").unlink()
    with pytest.raises(ProviderError) as info:
        adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert str(info.value) == f"{name}: cannot read the credential file"
    assert recorder.requests == []
    (tmp_path / "key").write_text("rotated-key")
    adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert EXCHANGES[name].credential(recorder.requests[0].headers) == "rotated-key"


@pytest.mark.parametrize("name", BUILT_PROVIDERS)
def test_a_missing_model_is_refused_at_construction(tmp_path: Path, name: str) -> None:
    settings = _settings(tmp_path, name, model=None)
    with pytest.raises(ProviderError, match=r"llm\.model is required"):
        provider_for(
            settings,
            build_client(settings, transport=httpx.MockTransport(lambda _r: httpx.Response(200))),
        )


def test_read_api_key_bounds(tmp_path: Path) -> None:
    path = tmp_path / "k"
    path.write_bytes(b"x" * (provider_tools.MAX_API_KEY_BYTES + 1))
    with pytest.raises(ProviderError, match="larger than"):
        read_api_key(path, "p")
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ProviderError, match="not UTF-8"):
        read_api_key(path, "p")
    path.write_text("\n")
    with pytest.raises(ProviderError, match="empty"):
        read_api_key(path, "p")
    with pytest.raises(ProviderError, match="api_key_file is required"):
        read_api_key(None, "p")
    path.write_text("  key-1 \r\n")
    assert read_api_key(path, "p") == "key-1"


def test_openai_falls_back_when_response_format_is_refused(tmp_path: Path) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if b"response_format" in request.content:
            return httpx.Response(
                400,
                json={
                    "error": {"message": "Unrecognized request argument supplied: response_format"}
                },
            )
        return httpx.Response(200, json=OPENAI_RESPONSE)

    adapter, recorder = _adapter(tmp_path, "openai", respond)
    assert isinstance(adapter, OpenAICompatibleProvider)
    assert adapter.json_mode is True
    assert adapter.complete(TASK_PROMPT, PAYLOAD, {}) == ANSWER
    assert len(recorder.requests) == 2
    assert b"response_format" not in recorder.requests[1].content
    assert (adapter.json_mode, len(recorder.requests)) == (False, 2)
    adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert len(recorder.requests) == 3
    assert b"response_format" not in recorder.requests[2].content


def test_openai_other_400s_are_not_retried(tmp_path: Path) -> None:
    adapter, recorder = _adapter(
        tmp_path, "openai", lambda _r: httpx.Response(400, json={"error": {"message": "bad model"}})
    )
    with pytest.raises(ProviderError, match="http 4xx"):
        adapter.complete(TASK_PROMPT, PAYLOAD, {})
    assert len(recorder.requests) == 1


def test_the_registry_builds_the_right_adapter_and_its_own_client(tmp_path: Path) -> None:
    assert isinstance(provider_for(_settings(tmp_path, "anthropic")), AnthropicProvider)
    assert isinstance(provider_for(_settings(tmp_path, "openai")), OpenAICompatibleProvider)
    for name in DEFERRED_PROVIDERS:
        with pytest.raises(ProviderError, match="not built yet; see ADR 0054"):
            provider_for(_settings(tmp_path, name))
    with pytest.raises(ProviderError, match=r"llm\.provider is not set"):
        provider_for(LlmSettings(endpoint=ENDPOINT))
    with pytest.raises(ProviderError, match=r"llm\.endpoint is required"):
        provider_for(_settings(tmp_path, "anthropic", endpoint=None))


def test_the_fake_is_a_provider_but_not_registrable() -> None:
    fake = FakeProvider()
    assert isinstance(fake, Provider)
    assert isinstance(fake, UsageProvider)
    assert "fake" not in BUILT_PROVIDERS
    with pytest.raises(ValueError, match="provider"):
        LlmSettings(enabled=True, provider="fake")  # type: ignore[arg-type]


def test_the_fake_conforms_to_the_exchange() -> None:
    fake = FakeProvider()
    text = fake.complete(TASK_PROMPT, PAYLOAD, {"type": "object"})
    parsed = json.loads(text)
    assert parsed["suggestions"][0]["target_id"] == "f1"
    (call,) = fake.calls
    assert (call.task_prompt, call.payload_json) == (TASK_PROMPT, PAYLOAD)
    assert fake.complete_with_usage(TASK_PROMPT, PAYLOAD, {}).input_tokens == 120
    erroring = FakeProvider(FakeBehaviour.ERROR)
    with pytest.raises(ProviderError):
        erroring.complete(TASK_PROMPT, PAYLOAD, {})
    scripted = FakeProvider(script=["one", TransientProviderError("fake: http 5xx")])
    assert scripted.complete(TASK_PROMPT, PAYLOAD, {}) == "one"
    with pytest.raises(TransientProviderError):
        scripted.complete(TASK_PROMPT, PAYLOAD, {})
    assert json.loads(scripted.complete(TASK_PROMPT, PAYLOAD, {}))["suggestions"]
