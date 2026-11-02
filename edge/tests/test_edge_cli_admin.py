"""carto-edge key, secret and source commands (spec 8.4, 14.3, 8.1.4; ADR 0013): keys rotate and
rewrap without changing tokens, secrets never come from an argument and never reach the output,
every change is audited, and ``source test`` fails for a source that cannot be enabled."""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from carto_edge.audit import verify as verify_audit
from carto_edge.cli.admin import RETIRED_SUFFIX
from carto_edge.cli.main import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, main
from carto_edge.config import EdgeSettings
from carto_edge.keys import KeyManager
from carto_edge.secrets import EdgeSecretResolver

SECRET_VALUE = "wombat-secret-value-0123456789"  # noqa: S105 - synthetic test value


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith("CARTO_"):
            monkeypatch.delenv(name)
    yield
    # main() configures logging for the process; leave the defaults for the tests after us.
    structlog.reset_defaults()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    KeyManager(EdgeSettings(state_dir=state_dir)).init(create_local_kms=True)
    return state_dir


def run(*argv: str) -> int:
    return main(list(argv))


def test_key_status_prints_versions_and_fingerprints_only(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("key", "status", "--state-dir", str(state)) == EXIT_OK
    status = json.loads(capsys.readouterr().out)
    assert status["active_version"] == 1
    assert status["provider"] == "local"
    assert len(status["active_fingerprint"]) == 16


def test_key_rotate_and_rewrap_keep_tokens_and_are_audited(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = EdgeSettings(state_dir=state)
    before = KeyManager(settings).load_keyring().active.token("id", "SO-0004471")
    assert run("key", "rotate", "--overlap-days", "30", "--state-dir", str(state)) == EXIT_OK
    assert "Restart edge-gateway" in capsys.readouterr().out
    keyring = KeyManager(settings).load_keyring()
    assert keyring.active.version == 2
    assert keyring.previous[0].token("id", "SO-0004471") == before

    master = settings.local_kms_key_file
    old_master = master.read_bytes()
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_OK
    out = capsys.readouterr().out
    assert "tokens are unchanged" in out
    assert master.read_bytes() != old_master
    retired = master.with_name(master.name + RETIRED_SUFFIX)
    assert retired.read_bytes() == old_master
    again = KeyManager(settings).load_keyring()
    assert again.previous[0].token("id", "SO-0004471") == before
    # A second rewrap refuses while the retired master is still there.
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE

    audit = verify_audit(settings.audit_file)
    assert audit.ok and audit.rows == 6
    assert _actions(settings) == [
        "key.rotate.intent",
        "key.rotate.done",
        "key.rewrap.intent",
        "key.rewrap.done",
        "key.rewrap.intent",
        "key.rewrap.failed",
    ]


def test_key_commands_fail_cleanly_without_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("key", "status", "--state-dir", str(tmp_path / "none")) == EXIT_FAILURE
    assert "key init" in capsys.readouterr().err
    assert run("key", "rotate", "--overlap-days", "0", "--state-dir", str(tmp_path)) == EXIT_USAGE


def test_secret_set_reads_stdin_or_a_file_never_an_argument(
    state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_VALUE + "\n"))
    assert run("secret", "set", "splunk-token", "--state-dir", str(state)) == EXIT_OK
    value_file = tmp_path / "value.txt"
    value_file.write_text("from-a-file-value\n", encoding="utf-8")
    argv = ["secret", "set", "sftp-key", "--from-file", str(value_file), "--state-dir", str(state)]
    assert run(*argv) == EXIT_OK
    assert run("secret", "list", "--state-dir", str(state)) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-2:] == ["local://sftp-key", "local://splunk-token"]
    assert SECRET_VALUE not in captured.out + captured.err
    resolver = EdgeSecretResolver(
        EdgeSettings(state_dir=state), KeyManager(EdgeSettings(state_dir=state)).kms
    )
    try:
        assert resolver.resolve("local://splunk-token") == SECRET_VALUE
        assert resolver.resolve("local://sftp-key") == "from-a-file-value"
    finally:
        resolver.close()
    audit_text = EdgeSettings(state_dir=state).audit_file.read_text(encoding="utf-8")
    assert SECRET_VALUE not in audit_text
    assert "secret.set" in audit_text

    assert run("secret", "delete", "sftp-key", "--state-dir", str(state)) == EXIT_OK
    assert run("secret", "delete", "sftp-key", "--state-dir", str(state)) == EXIT_FAILURE
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert run("secret", "set", "empty", "--state-dir", str(state)) == EXIT_USAGE
    # There is no positional for the value: an extra argument is a usage error.
    assert main(["secret", "set", "x", SECRET_VALUE, "--state-dir", str(state)]) == EXIT_USAGE


def write_sources(tmp_path: Path, paths: list[str]) -> Path:
    config = tmp_path / "sources.yaml"
    config.write_text(
        "systems:\n"
        "  - {id: sys_web, name: Webstore}\n"
        "sources:\n"
        "  - id: src_files\n"
        "    system: sys_web\n"
        "    type: upload\n"
        f"    config: {{paths: {json.dumps(paths)}}}\n"
        "  - id: src_otlp\n"
        "    system: sys_web\n"
        "    type: otlp\n",
        encoding="utf-8",
    )
    return config


def test_source_test_reports_each_source_and_fails_when_one_cannot_be_enabled(
    state: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "app.log"
    log.write_text("hello\n", encoding="utf-8")
    good = write_sources(tmp_path, [str(log)])
    assert run("source", "test", "--config", str(good), "--state-dir", str(state)) == EXIT_OK
    out = capsys.readouterr().out
    assert "src_files (upload): can be enabled; read-only: verified" in out
    assert "src_otlp (otlp): pushed by the collector; skipped" in out

    bad = write_sources(tmp_path, [str(tmp_path / "missing.log")])
    assert run("source", "test", "--config", str(bad), "--state-dir", str(state)) == EXIT_FAILURE
    assert "can NOT be enabled" in capsys.readouterr().out

    argv = ["source", "test", "--config", str(good), "--source", "src_nope"]
    assert run(*argv, "--state-dir", str(state)) == EXIT_USAGE
    assert run("source", "test", "--state-dir", str(state)) == EXIT_USAGE  # no sources file


def _actions(settings: EdgeSettings) -> list[str]:
    lines = settings.audit_file.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["action"] for line in lines]


@pytest.mark.parametrize(
    "argv",
    [
        ("key", "rotate"),
        ("key", "rewrap"),
        ("secret", "set", "splunk-token"),
        ("secret", "delete", "splunk-token"),
    ],
)
def test_no_change_without_an_audit_row_first(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    settings = EdgeSettings(state_dir=state)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_VALUE + "\n"))
    assert run("secret", "set", "splunk-token", "--state-dir", str(state)) == EXIT_OK
    keys_before = {path.name: path.read_bytes() for path in settings.keys_dir.iterdir()}
    master_before = settings.local_kms_key_file.read_bytes()
    # A row edited in place: the chain no longer verifies, so nothing can be audited.
    text = settings.audit_file.read_text(encoding="utf-8")
    settings.audit_file.write_text(text.replace("splunk-token", "other-token", 1), encoding="utf-8")
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("a-new-value\n"))
    assert run(*argv, "--state-dir", str(state)) == EXIT_FAILURE
    assert "audit" in capsys.readouterr().err
    assert {path.name: path.read_bytes() for path in settings.keys_dir.iterdir()} == keys_before
    assert settings.local_kms_key_file.read_bytes() == master_before
    resolver = EdgeSecretResolver(settings, KeyManager(settings).kms)
    try:
        assert resolver.resolve("local://splunk-token") == SECRET_VALUE
    finally:
        resolver.close()


def test_changes_write_intent_then_outcome(state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = EdgeSettings(state_dir=state)
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET_VALUE + "\n"))
    assert run("secret", "set", "splunk-token", "--state-dir", str(state)) == EXIT_OK
    assert run("key", "rotate", "--state-dir", str(state)) == EXIT_OK
    assert run("secret", "delete", "missing", "--state-dir", str(state)) == EXIT_FAILURE
    assert _actions(settings) == [
        "secret.set.intent",
        "secret.set.done",
        "key.rotate.intent",
        "key.rotate.done",
        "secret.delete.intent",
        "secret.delete.done",
    ]
    rows = [json.loads(line) for line in settings.audit_file.read_text().splitlines()]
    assert rows[3]["details"]["active_version"] == 2
    assert rows[5]["details"] == {"existed": False}
    assert verify_audit(settings.audit_file).ok


def test_a_failed_change_records_the_failure(state: Path) -> None:
    settings = EdgeSettings(state_dir=state)
    (settings.keys_dir / "rotation.json").write_text("{}", encoding="utf-8")
    assert run("key", "rotate", "--state-dir", str(state)) == EXIT_FAILURE
    assert _actions(settings) == ["key.rotate.intent", "key.rotate.failed"]
    rows = [json.loads(line) for line in settings.audit_file.read_text().splitlines()]
    assert rows[1]["details"] == {"error": "KeyManagementError"}
