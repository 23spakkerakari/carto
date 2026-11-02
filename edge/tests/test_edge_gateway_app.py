"""carto_edge.gateway.app (spec 8.1.2, 8.1.6, 8.4, 8.5, 12, 14.12, 16; plan M1 wave 2 D2): the
OTLP and webhook receivers hand records to the ingestor and answer 429/503 under backpressure,
the internal tokenize and reveal endpoints map the service's typed errors, health and metrics
answer, every response carries a request id, and no problem body or log line ever carries
request content."""

from __future__ import annotations

import gzip
import hashlib
import hmac
import io
import json
import secrets
import warnings
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import zstandard
from fastapi import FastAPI
from fastapi.routing import APIRoute
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.logs.v1 import logs_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

with warnings.catch_warnings():
    # starlette deprecates httpx (vs httpx2) under its TestClient at import time; the workspace
    # pins httpx, so the one-off is scoped (as in core/tests/test_core_ingest_app.py).
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from carto_common.crypto import InternalAssertion, SigningKey, sign_assertion
from carto_common.ids import ULID_PATTERN
from carto_common.logging import configure_logging
from carto_common.mtls import ROLES, ActAsRole
from carto_edge.config import (
    EdgeSettings,
    PiiSettings,
    RevealSettings,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.gateway.app import (
    MTLS_REFUSED_METRIC,
    PROBLEM_CONTENT_TYPE,
    ROUTE_ROLES,
    create_gateway_app,
)
from carto_edge.gateway.otlp import MAX_WIRE_RECORDS
from carto_edge.keys import ASSERTION_PUBLIC_KEY_FILE
from carto_edge.metrics import EdgeMetrics, default_metrics
from carto_edge.pipeline.forward import SourceHealth
from carto_edge.pipeline.ingestor import BackpressureState, IngestOutcome
from carto_edge.pipeline.model import FieldClass, RawRecord, ReadItem
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.reveal import (
    REVEAL_AUDIENCE,
    REVEAL_PERMISSION,
    SEARCH_PERMISSION,
    TOKENIZE_AUDIENCE,
)
from carto_edge.runtime import (
    EdgeRuntime,
    build_connector_context,
    build_reveal_service,
    build_runtime,
)
from carto_edge.secrets import EdgeSecretResolver, LocalSecretStore, SecretError
from carto_schema.event import EventKind

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
SECRET = "hook-secret-0123456789abcdef"  # noqa: S105
SIGNING = SigningKey.generate()
STRANGER = SigningKey.generate()
MARKER = "quokka-zebra-marker"
PROTOBUF = {"content-type": "application/x-protobuf"}
JSON_TYPE = {"content-type": "application/json"}
ORDER_ID = "SO-0004471"


# ---------------------------------------------------------------------------------------------
# Fakes and fixtures
# ---------------------------------------------------------------------------------------------


@dataclass
class FakeIngestor:
    records: list[RawRecord] = field(default_factory=list)
    state: BackpressureState = BackpressureState.OK
    fail: bool = False
    full: bool = False
    calls: int = 0
    durable_calls: int = 0

    def ingest(self, records: Iterable[ReadItem], *, durable: bool = False) -> IngestOutcome:
        batch = [record for record in records if isinstance(record, RawRecord)]
        self.calls += 1
        self.durable_calls += int(durable)
        if self.fail:
            raise RuntimeError(f"pipeline exploded on {MARKER}")
        if self.full:
            outcome = IngestOutcome(records=len(batch), events=len(batch))
            outcome.dropped["buffer_full"] = len(batch)
            return outcome
        self.records.extend(batch)
        return IngestOutcome(records=len(batch), events=len(batch))

    def flush(self) -> int:
        return 0

    def backpressure(self) -> BackpressureState:
        return self.state


def sources() -> SourcesFile:
    return SourcesFile(
        systems=[
            SystemConfig(id="sys_web", name="Webstore"),
            SystemConfig(id="sys_orders", name="Orders"),
        ],
        sources=[
            SourceConfig(id="src_web_log", system="sys_web", type=SourceType.OTLP),
            SourceConfig(
                id="src_orders_log", system="sys_orders", type=SourceType.OTLP, enabled=False
            ),
            SourceConfig(
                id="src_hooks",
                system="sys_orders",
                type=SourceType.WEBHOOK,
                config={"max_body_bytes": 2048, "event_type_field": "type"},
                secret_ref="local://hook-secret",  # noqa: S106
            ),
            SourceConfig(
                id="src_hooks_off",
                system="sys_orders",
                type=SourceType.WEBHOOK,
                enabled=False,
                secret_ref="local://hook-secret",  # noqa: S106
            ),
            SourceConfig(
                id="src_upload",
                system="sys_web",
                type=SourceType.UPLOAD,
                config={"paths": ["web/*.ndjson"]},
            ),
        ],
    )


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[EdgeRuntime]:
    settings = EdgeSettings(
        state_dir=tmp_path / "state",
        pii=PiiSettings(enabled=False),
        reveal=RevealSettings(values_per_user_per_hour=3, max_tokens_per_request=4),
    )
    built = build_runtime(settings, sources(), init_local_keys=True, detector=RegexDetector())
    try:
        with LocalSecretStore.open(settings, built.keys.kms) as store:
            store.put("hook-secret", SECRET)
        # Opens the resolver's store on this thread and caches the value, so requests served on
        # the TestClient's portal threads never touch the thread-bound SQLite connection.
        build_connector_context(built).secrets.resolve("local://hook-secret")
        yield built
    finally:
        built.close()


@dataclass
class World:
    client: TestClient
    ingestor: FakeIngestor
    metrics: EdgeMetrics
    runtime: EdgeRuntime


def make_world(
    runtime: EdgeRuntime,
    *,
    reveal_enabled: bool = True,
    ready: Callable[[], bool] | None = None,
    health: SourceHealth | None = None,
) -> World:
    if reveal_enabled:
        (runtime.settings.keys_dir / ASSERTION_PUBLIC_KEY_FILE).write_text(
            SIGNING.verify_key.to_text(), encoding="utf-8"
        )
    reveal = build_reveal_service(runtime, clock=lambda: NOW)
    ingestor = FakeIngestor()
    metrics = default_metrics()
    app = create_gateway_app(
        runtime.settings,
        runtime,
        ingestor,
        reveal,
        metrics,
        ready=ready,
        clock=lambda: NOW,
        health=health,
    )
    return World(TestClient(ActAsCaller(app)), ingestor, metrics, runtime)


class ActAsCaller(ActAsRole):
    """Act as the service each route expects (ADR 0031): the tests above are about other
    behaviour; the role checks are tested below with :class:`ActAsRole` directly."""

    def __init__(self, app: Any) -> None:
        super().__init__(app, None)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if path.startswith("/webhooks/"):
            self.role = "proxy"
        elif path.startswith("/internal/"):
            self.role = "api"
        else:
            self.role = "otel-collector"
        await super().__call__(scope, receive, send)


@pytest.fixture
def world(runtime: EdgeRuntime) -> World:
    return make_world(runtime)


@pytest.fixture
def logs() -> io.StringIO:
    stream = io.StringIO()
    configure_logging("edge", stream=stream)
    return stream


def assert_problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["request_id"] == response.headers["x-request-id"]
    assert isinstance(body, dict)
    return body


# ---------------------------------------------------------------------------------------------
# OTLP helpers
# ---------------------------------------------------------------------------------------------


def string(text: str) -> common_pb2.AnyValue:
    return common_pb2.AnyValue(string_value=text)


def log_line(text: str, time_ns: int = 1_791_547_200_000_000_000) -> logs_pb2.LogRecord:
    return logs_pb2.LogRecord(time_unix_nano=time_ns, body=string(text))


def resource(source_id: str | None, *texts: str) -> logs_pb2.ResourceLogs:
    attributes = [common_pb2.KeyValue(key="host.name", value=string("web-1"))]
    if source_id is not None:
        attributes.append(common_pb2.KeyValue(key="carto.source_id", value=string(source_id)))
    return logs_pb2.ResourceLogs(
        resource=resource_pb2.Resource(attributes=attributes),
        scope_logs=[logs_pb2.ScopeLogs(log_records=[log_line(text) for text in texts])],
    )


def otlp_body(*resources: logs_pb2.ResourceLogs) -> bytes:
    data: bytes = logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=list(resources)
    ).SerializeToString()
    return data


def otlp_response(content: bytes) -> logs_service_pb2.ExportLogsServiceResponse:
    response = logs_service_pb2.ExportLogsServiceResponse()
    response.ParseFromString(content)
    return response


# ---------------------------------------------------------------------------------------------
# OTLP
# ---------------------------------------------------------------------------------------------


def test_otlp_records_reach_the_ingestor_and_untagged_ones_are_reported(world: World) -> None:
    body = otlp_body(
        resource("src_web_log", "GET /cart 200", "GET /checkout 500"),
        resource(None, "untagged one", "untagged two", "untagged three"),
    )
    response = world.client.post("/v1/logs", content=body, headers=PROTOBUF)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"
    parsed = otlp_response(response.content)
    assert parsed.HasField("partial_success")
    assert parsed.partial_success.rejected_log_records == 3
    assert MARKER not in parsed.partial_success.error_message
    records = world.ingestor.records
    assert [record.text for record in records] == ["GET /cart 200", "GET /checkout 500"]
    assert {(record.source_id, record.system_id) for record in records} == {
        ("src_web_log", "sys_web")
    }
    assert {record.kind for record in records} == {EventKind.LOG}
    assert {record.received_at for record in records} == {NOW}
    assert all(record.locator.startswith("otlp:") for record in records)

    again = world.client.post("/v1/logs", content=body, headers=PROTOBUF)
    assert again.status_code == 200
    first, second = world.ingestor.records[:2], world.ingestor.records[2:]
    assert [r.locator for r in first] == [r.locator for r in second]


def test_push_routes_ingest_durably_and_answer_503_when_the_buffer_refused(
    world: World,
) -> None:
    body = otlp_body(resource("src_web_log", "GET /cart 200"))
    assert world.client.post("/v1/logs", content=body, headers=PROTOBUF).status_code == 200
    hook = json.dumps({"type": "order.created"}).encode()
    response = world.client.post("/webhooks/src_hooks", content=hook, headers=signed(hook))
    assert response.status_code == 202
    assert world.ingestor.durable_calls == world.ingestor.calls == 2
    world.ingestor.full = True
    refused = world.client.post("/v1/logs", content=body, headers=PROTOBUF)
    problem = assert_problem(refused, 503)
    assert refused.headers["retry-after"]
    assert "full" in problem["detail"]
    hook_refused = world.client.post("/webhooks/src_hooks", content=hook, headers=signed(hook))
    assert_problem(hook_refused, 503)


def test_access_lines_carry_the_route_template_never_the_path_or_query(
    world: World, logs: io.StringIO
) -> None:
    world.client.get(f"/webhooks/src_hooks?token={MARKER}")
    world.client.post(f"/webhooks/{MARKER}", content=b"{}")
    world.client.get("/healthz")
    lines = [json.loads(line) for line in logs.getvalue().splitlines() if line.strip()]
    access = [line for line in lines if line.get("event") == "request"]
    assert {line["route"] for line in access} >= {"/webhooks/{source_id}", "/healthz"}
    assert all(isinstance(line["duration_ms"], float | int) for line in access)
    assert MARKER not in logs.getvalue()


def test_otlp_without_rejections_has_no_partial_success(world: World) -> None:
    response = world.client.post(
        "/v1/logs", content=otlp_body(resource("src_web_log", "ok")), headers=PROTOBUF
    )
    assert response.status_code == 200
    assert not otlp_response(response.content).HasField("partial_success")


def test_otlp_disabled_sources_are_rejected(world: World) -> None:
    response = world.client.post(
        "/v1/logs", content=otlp_body(resource("src_orders_log", "a", "b")), headers=PROTOBUF
    )
    assert response.status_code == 200
    assert otlp_response(response.content).partial_success.rejected_log_records == 2
    assert world.ingestor.records == []
    assert world.ingestor.calls == 0


@pytest.mark.parametrize("encoding", ["gzip", "zstd"])
def test_otlp_accepts_gzip_and_zstd(world: World, encoding: str) -> None:
    body = otlp_body(resource("src_web_log", "one", "two"))
    data = gzip.compress(body) if encoding == "gzip" else zstandard.ZstdCompressor().compress(body)
    response = world.client.post(
        "/v1/logs", content=data, headers={**PROTOBUF, "content-encoding": encoding}
    )
    assert response.status_code == 200
    assert [record.text for record in world.ingestor.records] == ["one", "two"]


def test_otlp_refuses_oversized_bodies(world: World) -> None:
    cap = world.runtime.settings.gateway.otlp_max_body_bytes
    wire = assert_problem(
        world.client.post("/v1/logs", content=b"\x00" * (cap + 1), headers=PROTOBUF), 413
    )
    assert wire["instance"] == "/v1/logs"
    bomb = zstandard.ZstdCompressor().compress(b"\x00" * (cap * 4))
    assert len(bomb) < cap
    assert_problem(
        world.client.post(
            "/v1/logs", content=bomb, headers={**PROTOBUF, "content-encoding": "zstd"}
        ),
        413,
    )
    assert world.ingestor.calls == 0


def test_otlp_refuses_other_media_types_and_encodings(world: World) -> None:
    body = otlp_body(resource("src_web_log", "x"))
    assert_problem(world.client.post("/v1/logs", content=body, headers=JSON_TYPE), 415)
    assert_problem(world.client.post("/v1/logs", content=body), 415)
    assert_problem(
        world.client.post("/v1/logs", content=body, headers={**PROTOBUF, "content-encoding": "br"}),
        415,
    )
    assert world.ingestor.calls == 0


@pytest.mark.parametrize(
    ("state", "status", "retry_after"),
    [(BackpressureState.SLOW, 429, "5"), (BackpressureState.FULL, 503, "30")],
)
def test_otlp_backpressure_answers_with_retry_after(
    world: World, state: BackpressureState, status: int, retry_after: str
) -> None:
    world.ingestor.state = state
    response = world.client.post(
        "/v1/logs", content=otlp_body(resource("src_web_log", "x")), headers=PROTOBUF
    )
    assert_problem(response, status)
    assert response.headers["retry-after"] == retry_after
    assert world.ingestor.calls == 0


def test_otlp_bad_protobuf_is_a_400_without_the_body(world: World, logs: io.StringIO) -> None:
    body = b"\xff\xff\xff" + MARKER.encode() * 10
    problem = assert_problem(world.client.post("/v1/logs", content=body, headers=PROTOBUF), 400)
    assert MARKER not in json.dumps(problem)
    assert MARKER not in logs.getvalue()
    assert "bad_protobuf" in logs.getvalue()


# ---------------------------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------------------------


def signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"x-carto-signature": f"v1={digest}", **JSON_TYPE}


def test_webhook_with_a_valid_signature_is_ingested(world: World) -> None:
    body = json.dumps(
        [{"type": "order.created", "order_id": ORDER_ID}, {"type": "order.paid", "n": 2}]
    ).encode()
    response = world.client.post("/webhooks/src_hooks", content=body, headers=signed(body))
    assert response.status_code == 202, response.text
    assert response.json() == {"accepted": 2}
    records = world.ingestor.records
    assert [(r.source_id, r.system_id, r.kind) for r in records] == [
        ("src_hooks", "sys_orders", EventKind.WEBHOOK)
    ] * 2
    assert records[0].fields == {"type": "order.created", "order_id": ORDER_ID}
    assert records[0].received_at == NOW


def test_webhook_signature_is_required(world: World, logs: io.StringIO) -> None:
    body = json.dumps({"type": "order.created", "note": MARKER}).encode()
    for headers in (JSON_TYPE, signed(body, secret="another-secret-0123456789")):  # noqa: S106
        problem = assert_problem(
            world.client.post("/webhooks/src_hooks", content=body, headers=headers), 401
        )
        assert MARKER not in json.dumps(problem)
    assert world.ingestor.calls == 0
    output = logs.getvalue()
    assert "signature_rejected" in output
    assert "src_hooks" in output
    assert MARKER not in output


def test_webhook_unknown_sources_are_404_without_echoing_the_path(world: World) -> None:
    body = b"{}"
    for source_id in (f"src_{MARKER}", "src_hooks_off", "src_upload", "src_web_log"):
        response = world.client.post(f"/webhooks/{source_id}", content=body, headers=signed(body))
        problem = assert_problem(response, 404)
        assert problem["detail"] == "unknown source"
        assert problem["instance"] == "/webhooks/{source_id}"
        assert MARKER not in response.text
    assert world.ingestor.calls == 0


def test_webhook_oversized_body_is_413(world: World) -> None:
    body = json.dumps({"pad": "x" * 3000}).encode()
    assert_problem(
        world.client.post("/webhooks/src_hooks", content=body, headers=signed(body)), 413
    )
    assert world.ingestor.calls == 0


def test_webhook_body_that_is_not_json_is_400(world: World) -> None:
    for body in (b"not json " + MARKER.encode(), b"[1, 2]", b'"text"'):
        response = world.client.post("/webhooks/src_hooks", content=body, headers=signed(body))
        assert_problem(response, 400)
        assert MARKER not in response.text
    assert world.ingestor.calls == 0


@pytest.mark.parametrize(
    ("state", "status", "retry_after"),
    [(BackpressureState.SLOW, 429, "5"), (BackpressureState.FULL, 503, "30")],
)
def test_webhook_backpressure_answers_with_retry_after(
    world: World, state: BackpressureState, status: int, retry_after: str
) -> None:
    world.ingestor.state = state
    body = b'{"type": "order.created"}'
    response = world.client.post("/webhooks/src_hooks", content=body, headers=signed(body))
    assert_problem(response, status)
    assert response.headers["retry-after"] == retry_after


def test_webhook_secret_failure_is_503(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(self: EdgeSecretResolver, secret_ref: str) -> str:
        raise SecretError(f"unknown local secret {secret_ref!r}")

    monkeypatch.setattr(EdgeSecretResolver, "resolve", broken)
    body = b'{"type": "order.created"}'
    problem = assert_problem(
        world.client.post("/webhooks/src_hooks", content=body, headers=signed(body)), 503
    )
    assert problem["detail"] == "secret unavailable"
    assert world.ingestor.calls == 0


# ---------------------------------------------------------------------------------------------
# Internal tokenize and reveal
# ---------------------------------------------------------------------------------------------


def assertion(
    *,
    audience: str = REVEAL_AUDIENCE,
    permission: str = REVEAL_PERMISSION,
    key: SigningKey = SIGNING,
    subject: str = "user:42",
) -> str:
    return sign_assertion(
        key,
        InternalAssertion(
            subject=subject,
            permission=permission,
            purpose="ticket INC-1001",
            audience=audience,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=60),
            nonce=secrets.token_urlsafe(16),
            request_id="req-1",
        ),
    )


def tokenize_assertion(key: SigningKey = SIGNING) -> str:
    return assertion(audience=TOKENIZE_AUDIENCE, permission=SEARCH_PERMISSION, key=key)


def store_value(runtime: EdgeRuntime, value: str) -> str:
    result = runtime.tokenizer.build_event_identifiers(
        [("order_id", FieldClass.IDENTIFIER, ("raw", "norm"), value)],
        expires_at=NOW + timedelta(days=30),
    )
    runtime.vault.put_many(result.vault_entries)
    return result.vault_entries[0].token


def test_tokenize_and_reveal_with_signed_assertions(world: World) -> None:
    raw_token = store_value(world.runtime, ORDER_ID)
    tokenized = world.client.post(
        "/internal/tokenize", json={"assertion": tokenize_assertion(), "query": ORDER_ID}
    )
    assert tokenized.status_code == 200, tokenized.text
    assert raw_token in tokenized.json()["tokens"]
    assert tokenized.json()["key_versions"] == world.runtime.key_versions
    assert tokenized.headers["cache-control"] == "no-store"

    missing = "t1." + "A" * 22
    revealed = world.client.post(
        "/internal/reveal", json={"assertion": assertion(), "tokens": [raw_token, missing]}
    )
    assert revealed.status_code == 200, revealed.text
    assert revealed.json() == {
        "values": {raw_token: ORDER_ID},
        "missing": [missing],
        "remaining_quota": 2,
    }
    assert revealed.headers["cache-control"] == "no-store"
    assert ULID_PATTERN.fullmatch(revealed.headers["x-request-id"])


def test_internal_assertion_problems_are_401(world: World) -> None:
    raw_token = store_value(world.runtime, ORDER_ID)
    cases = [
        ("/internal/reveal", {"assertion": assertion(key=STRANGER), "tokens": [raw_token]}),
        ("/internal/reveal", {"assertion": tokenize_assertion(), "tokens": [raw_token]}),
        ("/internal/tokenize", {"assertion": assertion(), "query": ORDER_ID}),
    ]
    for path, payload in cases:
        response = world.client.post(path, json=payload)
        assert_problem(response, 401)
        assert ORDER_ID not in response.text


def test_internal_rate_limit_is_429_with_retry_after(world: World) -> None:
    for _ in range(3):
        ok = world.client.post(
            "/internal/tokenize", json={"assertion": tokenize_assertion(), "query": ORDER_ID}
        )
        assert ok.status_code == 200
    limited = world.client.post(
        "/internal/tokenize", json={"assertion": tokenize_assertion(), "query": ORDER_ID}
    )
    assert_problem(limited, 429)
    assert limited.headers["retry-after"] == "3600"


def test_internal_too_many_tokens_is_413(world: World) -> None:
    tokens = [f"t1.{secrets.token_urlsafe(16)}" for _ in range(5)]
    response = world.client.post(
        "/internal/reveal", json={"assertion": assertion(), "tokens": tokens}
    )
    assert_problem(response, 413)


def test_internal_endpoints_are_503_without_the_assertion_key(runtime: EdgeRuntime) -> None:
    world = make_world(runtime, reveal_enabled=False)
    assert_problem(
        world.client.post(
            "/internal/tokenize", json={"assertion": tokenize_assertion(), "query": ORDER_ID}
        ),
        503,
    )
    assert_problem(
        world.client.post(
            "/internal/reveal", json={"assertion": assertion(), "tokens": ["t1." + "B" * 22]}
        ),
        503,
    )


def test_internal_validation_problems_list_locations_only(world: World) -> None:
    response = world.client.post(
        "/internal/reveal",
        json={"tokens": [f"not-a-token-{MARKER}"], f"extra_{MARKER}": MARKER},
    )
    problem = assert_problem(response, 422)
    assert MARKER not in response.text
    errors = problem["errors"]
    assert isinstance(errors, list)
    assert {tuple(error["loc"]) for error in errors} == {("tokens", 0), ("assertion",), ("*",)}
    assert all(set(error) == {"loc"} for error in errors)


def test_internal_bodies_must_be_json(world: World) -> None:
    assert_problem(
        world.client.post("/internal/reveal", content=b"{not json", headers=JSON_TYPE), 400
    )
    assert_problem(
        world.client.post(
            "/internal/reveal", content=b"{}", headers={"content-type": "text/plain"}
        ),
        415,
    )
    assert_problem(
        world.client.post("/internal/tokenize", content=b"x" * (70 * 1024), headers=JSON_TYPE),
        413,
    )


# ---------------------------------------------------------------------------------------------
# Health, metrics, request ids, errors
# ---------------------------------------------------------------------------------------------


def test_health_and_readiness(runtime: EdgeRuntime) -> None:
    world = make_world(runtime)
    assert world.client.get("/healthz").json() == {"status": "ok"}
    assert world.client.get("/readyz").json() == {"status": "ready"}
    flag = {"ready": False}
    gated = make_world(runtime, ready=lambda: flag["ready"])
    assert gated.client.get("/readyz").status_code == 503
    flag["ready"] = True
    assert gated.client.get("/readyz").status_code == 200


def test_metrics_count_requests_by_route_template_and_status(world: World) -> None:
    world.client.get("/healthz")
    body = b"{}"
    world.client.post(f"/webhooks/src_{MARKER}", content=body, headers=signed(body))
    world.client.get(f"/nowhere/{MARKER}")
    response = world.client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = response.text
    assert 'carto_edge_requests_total{route="/healthz",status="200"} 1' in text
    assert 'carto_edge_requests_total{route="/webhooks/{source_id}",status="404"} 1' in text
    assert 'carto_edge_requests_total{route="unmatched",status="404"} 1' in text
    assert MARKER not in text
    assert world.metrics.get("carto_edge_requests_total", route="/healthz", status="200") == 1


def test_request_ids_are_echoed_when_well_formed(world: World) -> None:
    echoed = world.client.get("/healthz", headers={"x-request-id": "req-abc.1"})
    assert echoed.headers["x-request-id"] == "req-abc.1"
    replaced = world.client.get("/healthz", headers={"x-request-id": "bad id <script>"})
    assert ULID_PATTERN.fullmatch(replaced.headers["x-request-id"])
    problem = assert_problem(
        world.client.post("/v1/logs", content=b"x", headers={"x-request-id": "req-xyz"}), 415
    )
    assert problem["request_id"] == "req-xyz"


def test_unknown_routes_are_problems_without_the_path(world: World) -> None:
    response = world.client.get(f"/nowhere/{MARKER}?token={MARKER}")
    problem = assert_problem(response, 404)
    assert problem["instance"] == ""
    assert MARKER not in response.text
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert world.client.get(path).status_code == 404
    assert_problem(world.client.get("/v1/logs"), 405)


def test_store_failures_are_retryable_503s_logged_by_type_only(
    world: World, logs: io.StringIO
) -> None:
    """The OTLP exporter drops data on a 500 and retries a 503: a failure while storing pushed
    records must never be a 500 (spec 8.1 at-least-once, 8.5)."""
    world.ingestor.fail = True
    response = world.client.post(
        "/v1/logs", content=otlp_body(resource("src_web_log", MARKER)), headers=PROTOBUF
    )
    problem = assert_problem(response, 503)
    assert response.headers["retry-after"]
    assert problem["instance"] == "/v1/logs"
    assert MARKER not in response.text
    output = logs.getvalue()
    assert "RuntimeError" in output
    assert MARKER not in output
    assert world.metrics.get("carto_edge_requests_total", route="/v1/logs", status="503") == 1


def test_unhandled_errors_are_500_problems_logged_by_type_only(
    world: World, logs: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"parser exploded on {MARKER}")

    monkeypatch.setattr("carto_edge.gateway.app.parse_logs_request", explode)
    response = world.client.post(
        "/v1/logs", content=otlp_body(resource("src_web_log", MARKER)), headers=PROTOBUF
    )
    problem = assert_problem(response, 500)
    assert problem["instance"] == "/v1/logs"
    assert MARKER not in response.text
    output = logs.getvalue()
    assert "RuntimeError" in output
    assert MARKER not in output
    assert world.metrics.get("carto_edge_requests_total", route="/v1/logs", status="500") == 1


def test_push_reads_feed_source_health(runtime: EdgeRuntime) -> None:
    health = SourceHealth()
    world = make_world(runtime, health=health)
    body = otlp_body(resource("src_web_log", "GET /cart 200", "GET /cart 201"))
    assert world.client.post("/v1/logs", content=body, headers=PROTOBUF).status_code == 200
    state = health.snapshot()["src_web_log"]
    assert state.records == 2
    assert state.last_success_at == NOW


def test_no_response_or_log_line_carries_request_content(world: World, logs: io.StringIO) -> None:
    marked = json.dumps({"order_id": MARKER, "note": MARKER}).encode()
    responses = [
        world.client.post("/v1/logs", content=marked, headers=PROTOBUF),
        world.client.post("/v1/logs", content=marked, headers=JSON_TYPE),
        world.client.post("/webhooks/src_hooks", content=marked, headers=JSON_TYPE),
        world.client.post(
            "/webhooks/src_hooks",
            content=marked,
            headers={**JSON_TYPE, "x-carto-signature": MARKER},
        ),
        world.client.post("/internal/reveal", content=marked, headers=JSON_TYPE),
        world.client.post("/internal/tokenize", content=marked, headers=JSON_TYPE),
        world.client.post("/internal/tokenize", json={"assertion": MARKER, "query": MARKER}),
    ]
    for response in responses:
        assert response.status_code >= 400
        assert MARKER not in response.text
    assert MARKER not in logs.getvalue()


def test_webhook_secrets_shorter_than_16_characters_are_refused(
    world: World, monkeypatch: pytest.MonkeyPatch, logs: io.StringIO
) -> None:
    """An empty or tiny secret would make the HMAC check pass for anyone (review)."""

    def short(self: EdgeSecretResolver, secret_ref: str) -> str:
        return ""

    monkeypatch.setattr(EdgeSecretResolver, "resolve", short)
    body = json.dumps({"type": "order.created"}).encode()
    problem = assert_problem(
        world.client.post("/webhooks/src_hooks", content=body, headers=signed(body, secret="")),
        503,
    )
    assert "secret" in problem["detail"]
    assert world.ingestor.calls == 0
    assert "webhook.secret_too_short" in logs.getvalue()


def test_a_non_ascii_signature_is_a_401_not_a_500(world: World) -> None:
    body = json.dumps({"type": "order.created"}).encode()
    headers = {**JSON_TYPE, "X-Carto-Signature": "v1=\u00e9"}
    response = world.client.post(
        "/webhooks/src_hooks", content=body, headers={k: v.encode() for k, v in headers.items()}
    )
    assert_problem(response, 401)


def test_otlp_floods_are_refused_before_parsing(world: World) -> None:
    flood = logs_service_pb2.ExportLogsServiceRequest(
        resource_logs=[
            logs_pb2.ResourceLogs(
                scope_logs=[
                    logs_pb2.ScopeLogs(
                        log_records=[logs_pb2.LogRecord() for _ in range(MAX_WIRE_RECORDS + 1)]
                    )
                ]
            )
        ]
    ).SerializeToString()
    response = world.client.post("/v1/logs", content=flood, headers=PROTOBUF)
    assert_problem(response, 413)
    assert world.ingestor.calls == 0


# ---------------------------------------------------------------------------------------------
# Service roles (ADR 0031)
# ---------------------------------------------------------------------------------------------


def _inner_app(client: TestClient) -> FastAPI:
    """The FastAPI app behind a test client's role wrapper."""
    app = client.app
    inner = app.app if isinstance(app, ActAsRole) else app
    assert isinstance(inner, FastAPI)
    return inner


def _as(world: World, role: str | None) -> TestClient:
    inner = _inner_app(world.client)
    return TestClient(ActAsRole(inner, role))


def test_every_route_lists_its_roles(world: World) -> None:
    inner = _inner_app(world.client)
    routes = {
        (method, route.path)
        for route in inner.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
    }
    assert routes == set(ROUTE_ROLES)


@pytest.mark.parametrize(
    ("method", "path", "allowed"),
    [
        ("POST", "/v1/logs", {"otel-collector", "collector"}),
        ("POST", "/webhooks/src_hooks", {"proxy"}),
        ("POST", "/internal/tokenize", {"api"}),
        ("POST", "/internal/reveal", {"api"}),
    ],
)
def test_data_routes_refuse_every_other_role(
    world: World, method: str, path: str, allowed: set[str]
) -> None:
    for role in [*sorted(ROLES - allowed), None]:
        response = _as(world, role).request(method, path, content=b"{}")
        assert response.status_code == 403, (role, response.status_code)
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert response.json()["title"] == "forbidden"
    for role in sorted(allowed):
        response = _as(world, role).request(method, path, content=b"{}")
        assert response.status_code != 403, role
    assert world.metrics.get(MTLS_REFUSED_METRIC, route=path.replace("src_hooks", "{source_id}"))


def test_health_and_metrics_accept_any_install_certificate(world: World) -> None:
    for role in [None, "otel-collector", "edge-gateway"]:
        client = _as(world, role)
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_unknown_paths_stay_404(world: World) -> None:
    assert _as(world, None).get("/nowhere").status_code == 404
