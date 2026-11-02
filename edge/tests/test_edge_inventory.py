"""The edge inventory reporter (ADR 0050; spec 14.3): the payload never carries a planted secret
value, nested configuration is dropped and scalars kept, the sha256 is the sources file's bytes,
the inventory is sent on start and again only when the file changes (a refused send is retried),
and ``edge_id`` is the client certificate's common name or the sanitized host name."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import structlog

from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_edge.config import CoreLinkSettings, parse_sources_text
from carto_edge.metrics import default_metrics
from carto_edge.pipeline.forward import (
    INVENTORY_PATH,
    SECRET_KEY_WORDS,
    InventoryReporter,
    build_core_client,
    edge_id_for,
    strip_config,
)
from carto_schema.inventory import EdgeInventory

NOW = datetime(2026, 10, 18, 10, 0, tzinfo=UTC)
PLANTED = "PLANTED-SECRET-77a1"
CORE_URL = "https://ingest.internal:8443"


def sources_yaml(port: int = 5432) -> str:
    return f"""
systems:
  - id: sys_orders
    name: Orders
    owner_group: shop
  - id: sys_pay
    name: Payments
    criticality: high
sources:
  - id: src_orders_db
    system: sys_orders
    type: sql
    secret_ref: vault://db/orders
    backfill_days: 7
    config:
      host: db.internal
      port: {port}
      poll_seconds: 30
      tables: [orders, order_lines]
      verify_tls: true
      private_key: {PLANTED}-1
      authorization: Bearer {PLANTED}-2
      credentials:
        user: app
        pass: {PLANTED}-3
      tls:
        ca: /etc/ca.pem
        client_key: {PLANTED}-4
      hosts:
        - name: a
          key: {PLANTED}-5
  - id: src_pay_log
    system: sys_pay
    type: otlp
    enabled: false
field_policies:
  - field: sys_orders/*/customer_name
    field_class: person_name
    policy: tokenize
    forms: [phonetic.0]
    reason: names are identifiers here
"""


@pytest.fixture
def sources_file(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(sources_yaml(), encoding="utf-8")
    return path


class Core:
    """A fake ingest-api: records inventory posts and answers what the test queues."""

    def __init__(self) -> None:
        self.bodies: list[EdgeInventory] = []
        self.raw: list[bytes] = []
        self.answers: list[int | Exception] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == INVENTORY_PATH
        assert request.headers["content-type"] == "application/json"
        self.raw.append(request.content)
        self.bodies.append(EdgeInventory.model_validate_json(request.content))
        answer = self.answers.pop(0) if self.answers else 200
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, json={"recorded": answer == 200})

    def client(self) -> httpx.Client:
        return build_core_client(
            CoreLinkSettings(url=CORE_URL), transport=httpx.MockTransport(self.handler)
        )


def reporter(core: Core, path: Path, **overrides: object) -> InventoryReporter:
    options: dict[str, object] = {
        "metrics": default_metrics(),
        "edge_id": "edge-gateway",
        "sources_path": path,
        "default_poll_seconds": 60.0,
        "clock": lambda: NOW,
    }
    options.update(overrides)
    return InventoryReporter(
        core.client(),
        "default",
        parse_sources_text(path.read_text(encoding="utf-8")),
        **options,  # type: ignore[arg-type]
    )


def test_strip_config_drops_secret_keys_and_nested_structures() -> None:
    config = {
        "host": "db",
        "port": 5432,
        "ratio": 0.5,
        "flag": True,
        "nothing": None,
        "tables": ["a", "b"],
        "Password": "x",
        "api_key": "x",
        "session_token": "x",
        "Credentials": {"user": "u"},
        "AuthorizationHeader": "x",
        "ssh_key_path": "/k",
        "tls": {"ca": "/ca"},
        "mixed": ["a", 1],
        "rows": [{"k": "v"}],
        "long": "y" * 5000,
    }
    stripped = strip_config(config)
    assert stripped == {
        "host": "db",
        "port": 5432,
        "ratio": 0.5,
        "flag": True,
        "nothing": None,
        "tables": ["a", "b"],
        "long": "y" * 1024,
    }
    assert set(SECRET_KEY_WORDS) == {
        "password",
        "secret",
        "token",
        "key",
        "credential",
        "authorization",
    }
    assert strip_config({}) == {}


def test_the_payload_never_carries_a_planted_secret_and_flattens_the_config(
    sources_file: Path,
) -> None:
    core = Core()
    sender = reporter(core, sources_file)
    assert sender.send_if_changed() is True
    (raw,) = core.raw
    assert PLANTED not in raw.decode("utf-8")
    (body,) = core.bodies
    assert body.tenant_id == "default" and body.edge_id == "edge-gateway"
    assert body.sent_at == NOW
    assert body.sources_file_sha256 == hashlib.sha256(sources_file.read_bytes()).hexdigest()
    assert [(s.id, s.name, s.owner_group, s.criticality) for s in body.systems] == [
        ("sys_orders", "Orders", "shop", "medium"),
        ("sys_pay", "Payments", "", "high"),
    ]
    orders, pay = body.sources
    assert orders.config == {
        "host": "db.internal",
        "port": 5432,
        "poll_seconds": 30,
        "tables": ["orders", "order_lines"],
        "verify_tls": True,
    }
    assert orders.secret_ref == "vault://db/orders"  # noqa: S105 - a reference name
    assert (orders.type, orders.enabled, orders.backfill_days) == ("sql", True, 7)
    assert orders.schedule == "every 30 s"
    assert orders.last_test is None and orders.last_test_at is None
    assert (pay.type, pay.enabled, pay.schedule, pay.config) == ("otlp", False, "push", {})
    (pin,) = body.field_policies
    assert (pin.field, pin.field_class, pin.policy, pin.forms) == (
        "sys_orders/*/customer_name",
        "person_name",
        "tokenize",
        ["phonetic.0"],
    )
    assert pin.reason == "names are identifiers here"
    assert json.loads(raw)["schema_version"] == "1"


def test_the_default_poll_interval_names_the_schedule_when_the_config_has_none(
    sources_file: Path,
) -> None:
    text = sources_file.read_text(encoding="utf-8").replace("      poll_seconds: 30\n", "")
    sources_file.write_text(text, encoding="utf-8")
    body = reporter(Core(), sources_file).build()
    assert body.sources[0].schedule == "every 60 s"
    body = reporter(Core(), sources_file, default_poll_seconds=None).build()
    assert body.sources[0].schedule is None


def test_resend_only_when_the_sources_file_changes_and_after_a_refusal(
    sources_file: Path,
) -> None:
    core = Core()
    sender = reporter(core, sources_file)
    metrics = default_metrics()
    sender = reporter(core, sources_file, metrics=metrics)
    assert sender.send_if_changed() is True
    assert sender.send_if_changed() is False
    assert sender.send_if_changed() is False
    assert len(core.bodies) == 1
    assert metrics.get("carto_edge_inventories_total") == 1
    sources_file.write_text(sources_yaml(port=6543), encoding="utf-8")
    assert sender.send_if_changed() is True
    assert len(core.bodies) == 2
    assert core.bodies[1].sources[0].config["port"] == 6543
    assert (
        core.bodies[1].sources_file_sha256 == hashlib.sha256(sources_file.read_bytes()).hexdigest()
    )
    assert core.bodies[1].sources_file_sha256 != core.bodies[0].sources_file_sha256
    # A refused send is retried the next round, a transport error too; neither raises.
    sources_file.write_text(sources_yaml(port=7654), encoding="utf-8")
    core.answers = [503, httpx.ConnectError(f"refused {PLANTED}")]
    with structlog.testing.capture_logs() as logs:
        assert sender.send_if_changed() is False
        assert sender.send_if_changed() is False
        assert sender.send_if_changed() is True
    assert len(core.bodies) == 5
    assert any(entry.get("status") == 503 for entry in logs)
    assert any(entry.get("error") == "ConnectError" for entry in logs)
    assert PLANTED not in repr(logs)
    assert sender.send_if_changed() is False
    assert metrics.get("carto_edge_inventories_total") == 3


def test_an_invalid_change_keeps_the_last_good_model_but_reports_the_new_digest(
    sources_file: Path,
) -> None:
    core = Core()
    sender = reporter(core, sources_file)
    assert sender.send_if_changed() is True
    sources_file.write_text("systems: [\n", encoding="utf-8")
    with structlog.testing.capture_logs() as logs:
        assert sender.send_if_changed() is True
    assert any(entry["event"] == "inventory.sources_file_invalid" for entry in logs)
    last = core.bodies[-1]
    assert [s.id for s in last.sources] == ["src_orders_db", "src_pay_log"], "the last good model"
    assert last.sources_file_sha256 == hashlib.sha256(sources_file.read_bytes()).hexdigest()
    assert sender.send_if_changed() is False
    sources_file.unlink()
    assert sender.send_if_changed() is True, "an unreadable file hashes the model instead"


def test_without_a_file_the_digest_covers_the_model(sources_file: Path) -> None:
    sender = reporter(Core(), sources_file, sources_path=None)
    first = sender.current_sha256()
    assert first == sender.current_sha256()
    assert len(first) == 64


def test_edge_id_is_the_certificate_common_name_or_the_sanitized_host_name(tmp_path: Path) -> None:
    ca = create_ca()
    write_pem(ca, tmp_path / "ca.crt", tmp_path / "ca.key")
    leaf = issue_certificate(ca, "edge-gateway", dns_names=("edge-gateway",))
    write_pem(leaf, tmp_path / "edge.crt", tmp_path / "edge.key")
    link = CoreLinkSettings(
        url=CORE_URL,
        ca_file=tmp_path / "ca.crt",
        cert_file=tmp_path / "edge.crt",
        key_file=tmp_path / "edge.key",
    )
    assert edge_id_for(link) == "edge-gateway"
    assert (
        edge_id_for(CoreLinkSettings(url=CORE_URL), hostname=lambda: "WH Node 3/eu")
        == "WH-Node-3-eu"
    )
    assert edge_id_for(CoreLinkSettings(url=CORE_URL), hostname=lambda: "") == "edge"
    assert len(edge_id_for(CoreLinkSettings(url=CORE_URL), hostname=lambda: "h" * 300)) == 128
    broken = CoreLinkSettings(
        url=CORE_URL, cert_file=tmp_path / "missing.crt", key_file=tmp_path / "edge.key"
    )
    assert edge_id_for(broken, hostname=lambda: "fallback") == "fallback"
    assert EdgeInventory.model_fields["edge_id"].metadata, "the contract bounds it"
    sender = InventoryReporter(
        Core().client(),
        "default",
        parse_sources_text(sources_yaml()),
        metrics=default_metrics(),
        edge_id="bad id!",
    )
    assert sender.edge_id == "bad-id-"


def test_run_sends_at_start_and_stops_promptly(sources_file: Path) -> None:
    core = Core()
    sender = reporter(core, sources_file)
    stop = threading.Event()
    thread = threading.Thread(target=sender.run, args=(stop, 30.0), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while not core.bodies and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(5.0)
    assert not thread.is_alive()
    assert len(core.bodies) == 1


def test_run_survives_a_faulty_round(sources_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sender = reporter(Core(), sources_file)
    calls: list[int] = []

    def explode(_self: InventoryReporter) -> bool:
        calls.append(1)
        raise RuntimeError(PLANTED)

    monkeypatch.setattr(InventoryReporter, "send_if_changed", explode)
    stop = threading.Event()
    with structlog.testing.capture_logs() as logs:
        thread = threading.Thread(target=sender.run, args=(stop, 0.01), daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()
        thread.join(5.0)
    assert len(calls) >= 2
    assert any(entry.get("error") == "RuntimeError" for entry in logs)
    assert PLANTED not in repr(logs)
