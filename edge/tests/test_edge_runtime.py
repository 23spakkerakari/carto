"""carto_edge.runtime: the gateway and the analyzer build the same stack from settings and a
sources file; keys persist across builds, modes differ as ADR 0017 says, reveal is disabled
until core's public key is installed, and nothing in a repr carries material."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from carto_common.crypto import SigningKey
from carto_edge.config import (
    EdgeSettings,
    KmsSettings,
    PiiSettings,
    SourceConfig,
    SourcesFile,
    SourceType,
    SystemConfig,
)
from carto_edge.keys import ASSERTION_PUBLIC_KEY_FILE, KeyManagementError
from carto_edge.metrics import default_metrics
from carto_edge.net.ssrf import DefaultNetworkPolicy
from carto_edge.pipeline.model import RawRecord, VaultEntry
from carto_edge.pipeline.pii import RegexDetector
from carto_edge.runtime import (
    EdgeRuntime,
    build_connector_context,
    build_reveal_service,
    build_runtime,
    ensure_local_keys,
)
from carto_edge.secrets import EdgeSecretResolver
from carto_schema.event import EventKind

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def sources() -> SourcesFile:
    return SourcesFile(
        systems=[SystemConfig(id="sys_web", name="Webstore")],
        sources=[
            SourceConfig(
                id="src_web",
                system="sys_web",
                type=SourceType.UPLOAD,
                config={"paths": ["web/*.ndjson"]},
            )
        ],
    )


def settings(tmp_path: Path, kms: KmsSettings | None = None) -> EdgeSettings:
    return EdgeSettings(
        state_dir=tmp_path / "state",
        pii=PiiSettings(enabled=False),
        kms=kms if kms is not None else KmsSettings(),
    )


def record(i: int) -> RawRecord:
    payload = {"ts": "2026-09-23T04:00:00Z", "msg": "cart created", "cart_id": f"c-{88000 + i}"}
    return RawRecord(
        source_id="src_web",
        system_id="sys_web",
        kind=EventKind.LOG,
        locator=f"app.ndjson:line:{i}",
        received_at=NOW,
        text=json.dumps(payload),
    )


def test_analyze_runtime_creates_local_keys_and_a_second_build_reuses_them(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    first = build_runtime(
        cfg, sources(), mode="analyze", init_local_keys=True, detector=RegexDetector()
    )
    assert isinstance(first, EdgeRuntime)
    assert first.key_versions == [1]
    assert first.classifier.settings.quarantine_samples == 1
    assert first.stats.path is None
    for i in range(1, 6):  # ADR 0017 pass 1: statistics first, decisions after
        assert first.pipeline.observe_only(record(i))
    result = first.pipeline.process(record(1))
    assert result.event is not None
    assert first.write_vault_entries([result]) >= 1
    raw_tokens = [i.token for i in result.event.identifiers if i.form == "raw"]
    assert first.vault.reveal(raw_tokens, now=NOW) == dict.fromkeys(raw_tokens, "c-88001")
    token = first.tokenizer.actor_token("jsmith")
    first.close()
    assert (cfg.keys_dir / "rotation.json").is_file()
    assert cfg.local_kms_key_file.is_file()
    assert cfg.audit_file.is_file()

    second = build_runtime(cfg, sources(), mode="gateway", detector=RegexDetector())
    assert second.tokenizer.actor_token("jsmith") == token  # the persisted key, not a new one
    assert second.classifier.settings.quarantine_samples == cfg.classify.quarantine_samples
    assert second.stats.path == cfg.stats_file
    assert second.pipeline.process(record(1)).event is not None
    second.flush()
    assert cfg.stats_file.is_file()
    assert any(cfg.templates_dir.glob("*.json"))
    second.close()


def test_gateway_runtime_refuses_to_start_without_keys(tmp_path: Path) -> None:
    with pytest.raises(KeyManagementError, match="key init"):
        build_runtime(settings(tmp_path), sources(), detector=RegexDetector())


def test_ensure_local_keys_only_creates_for_the_local_provider(tmp_path: Path) -> None:
    token_file = tmp_path / "vault-token"
    token_file.write_text("s.token", encoding="utf-8")
    cfg = settings(
        tmp_path,
        kms=KmsSettings(
            provider="vault", vault_url="https://vault.internal:8200", vault_token_file=token_file
        ),
    )
    with pytest.raises(KeyManagementError, match="vault KMS"):
        build_runtime(cfg, sources(), init_local_keys=True, detector=RegexDetector())
    local = settings(tmp_path / "other")
    runtime = build_runtime(local, sources(), init_local_keys=True, detector=RegexDetector())
    assert ensure_local_keys(runtime.keys, local) is False  # already there: nothing created
    runtime.close()


def test_reveal_service_is_disabled_until_the_public_key_is_installed(tmp_path: Path) -> None:
    cfg = settings(tmp_path)
    runtime = build_runtime(cfg, sources(), init_local_keys=True, detector=RegexDetector())
    assert build_reveal_service(runtime).enabled is False
    (cfg.keys_dir / ASSERTION_PUBLIC_KEY_FILE).write_text(
        SigningKey.generate().verify_key.to_text(), encoding="utf-8"
    )
    assert build_reveal_service(runtime, clock=lambda: NOW).enabled is True
    runtime.close()


def test_connector_context_shares_one_resolver_and_applies_the_allow_list(
    tmp_path: Path,
) -> None:
    cfg = settings(tmp_path)
    runtime = build_runtime(cfg, sources(), init_local_keys=True, detector=RegexDetector())
    context = build_connector_context(runtime)
    again = build_connector_context(runtime)
    assert isinstance(context.secrets, EdgeSecretResolver)
    assert context.secrets is again.secrets is runtime.secrets
    assert isinstance(context.network, DefaultNetworkPolicy)
    assert context.tenant_id == cfg.tenant_id
    runtime.close()


def test_repr_names_mode_and_versions_only(tmp_path: Path) -> None:
    runtime = build_runtime(
        settings(tmp_path),
        sources(),
        mode="analyze",
        init_local_keys=True,
        detector=RegexDetector(),
    )
    text = repr(runtime)
    assert text == "EdgeRuntime(mode='analyze', key_versions=[1])"
    runtime.close()


def test_gateway_housekeeping_purges_expired_vault_entries_hourly(tmp_path: Path) -> None:
    config = settings(tmp_path)
    build_runtime(config, sources(), mode="analyze", init_local_keys=True).close()
    runtime = build_runtime(config, sources(), mode="gateway")
    tick = [0.0]
    runtime.monotonic = lambda: tick[0]
    metrics = default_metrics()
    runtime.metrics = metrics
    token_text = runtime.keyring.active.token("id", "CUST-88210")
    runtime.vault.put_many([VaultEntry(token_text, "CUST-88210", NOW - timedelta(days=1))])
    try:
        runtime.flush()  # first tick runs the chores
        assert runtime.vault.count() == 0
        assert metrics.get("carto_edge_vault_purged_total") == 1
        runtime.vault.put_many([VaultEntry(token_text, "CUST-88210", NOW - timedelta(days=1))])
        tick[0] = 10.0
        runtime.flush()  # ten seconds later: not yet
        assert runtime.vault.count() == 1
        tick[0] = 3601.0
        runtime.flush()  # an hour later
        assert runtime.vault.count() == 0
    finally:
        runtime.close()


def test_analyze_flush_does_not_run_gateway_housekeeping(tmp_path: Path) -> None:
    runtime = build_runtime(settings(tmp_path), sources(), mode="analyze", init_local_keys=True)
    token_text = runtime.keyring.active.token("id", "CUST-88210")
    runtime.vault.put_many([VaultEntry(token_text, "CUST-88210", NOW - timedelta(days=1))])
    try:
        runtime.flush()
        assert runtime.vault.count() == 1
    finally:
        runtime.close()


def _anchor_lines(text: str) -> list[dict[str, object]]:
    lines = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("event") == "audit.anchor"]


def test_gateway_publishes_the_audit_anchor_hourly_and_at_close(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = settings(tmp_path)
    build_runtime(config, sources(), mode="analyze", init_local_keys=True).close()
    runtime = build_runtime(config, sources(), mode="gateway")
    assert runtime.anchors is not None
    tick = [0.0]
    runtime.monotonic = lambda: tick[0]
    capsys.readouterr()
    runtime.flush()
    first = _anchor_lines(capsys.readouterr().out)
    assert [line["seq"] for line in first] == [0]
    runtime.audit.record("reveal", "user:1", "vault", {"revealed": 1})
    tick[0] = 60.0
    runtime.flush()
    assert _anchor_lines(capsys.readouterr().out) == []
    runtime.close()
    last = _anchor_lines(capsys.readouterr().out)
    assert [line["seq"] for line in last] == [1]
    assert last[0]["row_hash"] == runtime.audit.last_hash


def test_analyze_runtime_publishes_no_anchor(tmp_path: Path) -> None:
    runtime = build_runtime(settings(tmp_path), sources(), mode="analyze", init_local_keys=True)
    assert runtime.anchors is None
    runtime.close()
