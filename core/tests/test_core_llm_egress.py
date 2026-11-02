"""The gateway's egress allowlist (spec 9.9, 14.5; ADR 0054): only https to the configured
host and port leaves the client; redirects are not followed and a followed one is refused;
environment proxies are ignored; the response body is capped at 1 MiB."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from carto_common.settings import LlmSettings
from carto_core.llm_gateway.egress import (
    MAX_RESPONSE_BYTES,
    EndpointPolicy,
    build_client,
    endpoint_policy,
)
from carto_core.llm_gateway.schemas import ProviderError

ENDPOINT = "https://llm.example:8443/v1"


def _settings(tmp_path: Path, endpoint: str | None = ENDPOINT) -> LlmSettings:
    key_file = tmp_path / "key"
    key_file.write_text("sk-test\n")
    return LlmSettings(
        enabled=True,
        provider="anthropic",
        endpoint=endpoint,
        model="m",
        api_key_file=key_file,
        timeout_seconds=7,
    )


class Recorder:
    def __init__(self, response: httpx.Response | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response = response or httpx.Response(200, json={"ok": True})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response


def test_endpoint_policy_is_host_and_port(tmp_path: Path) -> None:
    assert endpoint_policy(_settings(tmp_path)) == EndpointPolicy("llm.example", 8443)
    assert endpoint_policy(_settings(tmp_path, "https://API.Example.com")) == EndpointPolicy(
        "api.example.com", 443
    )


@pytest.mark.parametrize(
    "endpoint",
    [None, "https://user:pw@llm.example", "https://llm.example/?x=1", "https://llm.example/#f"],
)
def test_endpoint_policy_refuses_odd_endpoints(tmp_path: Path, endpoint: str | None) -> None:
    with pytest.raises(ProviderError):
        endpoint_policy(_settings(tmp_path, endpoint))


def test_a_relative_request_reaches_the_endpoint(tmp_path: Path) -> None:
    recorder = Recorder()
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(recorder))
    response = client.post("/v1/messages", json={"a": 1})
    assert response.status_code == 200
    (request,) = recorder.requests
    assert request.url == httpx.URL("https://llm.example:8443/v1/v1/messages")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/messages",  # an adapter bug building an absolute URL
        "https://llm.example/v1/messages",  # right host, wrong port (443)
        "http://llm.example:8443/v1/messages",  # right origin, clear text
        "https://llm.example.evil/v1/messages",
        "https://llm.example:8443@evil.example/x",
    ],
)
def test_other_origins_are_refused_before_the_transport(tmp_path: Path, url: str) -> None:
    recorder = Recorder()
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(recorder))
    with pytest.raises(ProviderError, match="egress refused"):
        client.post(url, json={"a": 1})
    assert recorder.requests == []


def test_the_transport_checks_even_without_the_hook(tmp_path: Path) -> None:
    recorder = Recorder()
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(recorder))
    client.event_hooks = {"request": [], "response": []}
    with pytest.raises(ProviderError, match="egress refused"):
        client.get("https://evil.example/")
    assert recorder.requests == []


def test_redirects_are_not_followed_and_a_followed_one_is_refused(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(302, headers={"location": "https://evil.example/x"}))
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(recorder))
    response = client.post("/v1/messages", json={})
    assert response.status_code == 302
    assert len(recorder.requests) == 1
    client.max_redirects = 5  # so the hook, not the redirect cap, is what refuses
    with pytest.raises(ProviderError, match="egress refused"):
        client.send(client.build_request("POST", "/v1/messages", json={}), follow_redirects=True)
    assert [r.url.host for r in recorder.requests] == ["llm.example", "llm.example"]


def test_environment_proxies_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing.pem"))
    recorder = Recorder()
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(recorder))
    assert client.trust_env is False
    assert client._mounts == {}, "no proxy mount came from the environment"
    assert client.post("/v1/messages", json={}).status_code == 200
    assert len(recorder.requests) == 1


def test_timeouts_and_redirect_settings_come_from_the_settings(tmp_path: Path) -> None:
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(Recorder()))
    assert client.timeout == httpx.Timeout(7.0)
    assert client.follow_redirects is False
    assert client.max_redirects == 0
    assert client.base_url == httpx.URL(ENDPOINT + "/")


def test_a_declared_oversize_response_is_refused_before_reading(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": str(MAX_RESPONSE_BYTES + 1)},
            stream=httpx.ByteStream(b"x"),
        )

    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="declared length"):
        client.post("/v1/messages", json={})


def test_a_streamed_oversize_response_is_cut_off(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * (MAX_RESPONSE_BYTES + 1)))

    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError, match="larger than the limit"):
        client.post("/v1/messages", json={})


def test_a_response_at_the_limit_is_read(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * MAX_RESPONSE_BYTES))

    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(handler))
    assert len(client.post("/v1/messages", json={}).content) == MAX_RESPONSE_BYTES


def test_refusal_messages_name_the_allowed_origin_only(tmp_path: Path) -> None:
    client = build_client(_settings(tmp_path), transport=httpx.MockTransport(Recorder()))
    with pytest.raises(ProviderError) as info:
        client.post("https://evil.example/secret-path?token=abc", json={"payload": "P"})
    text = str(info.value)
    assert "llm.example:8443" in text
    assert "evil" not in text
    assert "secret-path" not in text
    assert "abc" not in text
