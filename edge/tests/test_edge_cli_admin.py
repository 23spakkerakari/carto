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

from carto_common.crypto import LocalKms, WrappedKey
from carto_edge.audit import verify as verify_audit
from carto_edge.cli.admin import NEW_SUFFIX, RETIRED_SUFFIX
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
    # A second rewrap refuses while the retired master is still there, and never tells the
    # operator to remove a file (ADR 0070 section 7).
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert RETIRED_SUFFIX in err and "remove" not in err

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


def _fail_second_staging_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    real_replace = Path.replace
    seen = 0

    def replace(self: Path, target: str | os.PathLike[str]) -> Path:
        nonlocal seen
        if self.name.endswith(".rewrap"):
            seen += 1
            if seen == 2:
                msg = "disk gone"
                raise OSError(msg)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)


def test_an_interrupted_local_rewrap_is_resumed_by_the_next_run(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0070 section 7: a crash after the first file is rewrapped leaves ``local-kms.key.new``
    beside files under both masters; the next ``key rewrap`` resumes and finishes, and no
    message ever says to remove the staged master (the only key for those files)."""
    settings = EdgeSettings(state_dir=state)
    KeyManager(settings).vault_data_key()
    master = settings.local_kms_key_file
    staged = master.with_name(master.name + NEW_SUFFIX)
    retired = master.with_name(master.name + RETIRED_SUFFIX)
    old_master = master.read_bytes()
    before = KeyManager(settings).load_keyring().active.token("id", "SO-0004471")

    _fail_second_staging_replace(monkeypatch)
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "re-run `carto-edge key rewrap`" in err
    assert "remove" not in err and "delete" not in err
    monkeypatch.undo()
    assert master.read_bytes() == old_master and staged.is_file() and not retired.exists()
    new_kms = LocalKms.open(staged)
    key_ids = {p.name: WrappedKey.read(p).key_id for p in KeyManager(settings).wrapped_key_files()}
    assert new_kms.key_id in key_ids.values() and len(set(key_ids.values())) == 2
    assert not list(settings.keys_dir.glob("*.rewrap"))

    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_OK
    out = capsys.readouterr().out
    assert "tokens are unchanged" in out and "resumed" in out
    assert master.read_bytes() != old_master and not staged.exists()
    assert retired.read_bytes() == old_master
    assert LocalKms.open(master).key_id == new_kms.key_id
    for path in KeyManager(settings).wrapped_key_files():
        assert WrappedKey.read(path).key_id == new_kms.key_id
    assert KeyManager(settings).load_keyring().active.token("id", "SO-0004471") == before
    assert _actions(settings) == [
        "key.rewrap.intent",
        "key.rewrap.failed",
        "key.rewrap.intent",
        "key.rewrap.done",
    ]
    assert verify_audit(settings.audit_file).ok


def _snapshot(keys_dir: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in keys_dir.iterdir() if path.is_file()}


def test_the_staged_master_is_written_whole_or_not_at_all(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-11-02 (Low): ``.new`` goes through ``write_private_file`` (a temporary
    file and a rename), so an interrupt while it is written leaves no partial ``.new``."""
    import carto_edge.cli.admin as admin  # noqa: PLC0415

    settings = EdgeSettings(state_dir=state)
    master = settings.local_kms_key_file
    staged = master.with_name(master.name + NEW_SUFFIX)
    real = admin.write_private_file
    written: list[str] = []

    def recording(path: Path, data: bytes) -> None:
        written.append(path.name)
        if path.name == staged.name:
            assert len(data) == 32
            msg = "disk gone"
            raise OSError(msg)
        real(path, data)

    monkeypatch.setattr(admin, "write_private_file", recording)
    before = _snapshot(settings.keys_dir)
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    assert written == [staged.name]
    assert not staged.exists() and not staged.with_name(staged.name + ".tmp").exists()
    assert _snapshot(settings.keys_dir) == before
    monkeypatch.undo()
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_OK


def test_a_truncated_staged_master_nothing_depends_on_may_be_removed(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of 2026-11-02 (Low): a ``.new`` that cannot be opened is a dead end only if the
    command never says what to do. When every wrapped file still unwraps under the current
    master, nothing depends on it and the message says to remove only ``.new`` and re-run."""
    settings = EdgeSettings(state_dir=state)
    KeyManager(settings).vault_data_key()
    master = settings.local_kms_key_file
    staged = master.with_name(master.name + NEW_SUFFIX)
    staged.write_bytes(b"\x00" * 16)  # truncated: not a 32-byte key
    before = _snapshot(settings.keys_dir)
    token = KeyManager(settings).load_keyring().active.token("id", "SO-0004471")
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "cannot be opened" in err and "nothing depends on it" in err
    assert f"remove only {staged.name}" in err and "re-run `carto-edge key rewrap`" in err
    assert _snapshot(settings.keys_dir) == before
    assert _actions(settings) == ["key.rewrap.intent", "key.rewrap.failed"]
    staged.unlink()
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_OK
    assert KeyManager(settings).load_keyring().active.token("id", "SO-0004471") == token


def test_a_truncated_staged_master_a_file_depends_on_is_kept(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other branch: a file is under a key that is not the current master, so the
    unreadable ``.new`` may be the only key for it; the command says to keep everything."""
    settings = EdgeSettings(state_dir=state)
    KeyManager(settings).vault_data_key()
    master = settings.local_kms_key_file
    staged = master.with_name(master.name + NEW_SUFFIX)
    _fail_second_staging_replace(monkeypatch)
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    monkeypatch.undo()
    capsys.readouterr()
    staged.write_bytes(b"\x00" * 16)  # the staged master is damaged afterwards
    before = _snapshot(settings.keys_dir)
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "cannot be opened" in err and "keep every file" in err
    assert "do not unwrap under the current master" in err
    assert "remove" not in err and "delete" not in err
    assert _snapshot(settings.keys_dir) == before


def test_an_interrupt_before_the_retired_copy_is_resumed(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review of 2026-11-02 (Low): every file is under the new master, ``.new`` is staged,
    and the copy of the old master to ``.retired`` fails; the next run installs it."""
    import carto_edge.cli.admin as admin  # noqa: PLC0415

    settings = EdgeSettings(state_dir=state)
    KeyManager(settings).vault_data_key()
    master = settings.local_kms_key_file
    staged = master.with_name(master.name + NEW_SUFFIX)
    retired = master.with_name(master.name + RETIRED_SUFFIX)
    old_master = master.read_bytes()
    token = KeyManager(settings).load_keyring().active.token("id", "SO-0004471")
    real = admin.write_private_file

    def failing(path: Path, data: bytes) -> None:
        if path.name.endswith(RETIRED_SUFFIX):
            msg = "disk gone"
            raise OSError(msg)
        real(path, data)

    monkeypatch.setattr(admin, "write_private_file", failing)
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "every key file is under the new master" in err and "re-run" in err
    assert "remove" not in err and "delete" not in err
    monkeypatch.undo()
    assert master.read_bytes() == old_master and staged.is_file() and not retired.exists()
    new_kms = LocalKms.open(staged)
    for path in KeyManager(settings).wrapped_key_files():
        assert WrappedKey.read(path).key_id == new_kms.key_id

    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_OK
    assert "resumed" in capsys.readouterr().out
    assert LocalKms.open(master).key_id == new_kms.key_id and not staged.exists()
    assert retired.read_bytes() == old_master
    assert KeyManager(settings).load_keyring().active.token("id", "SO-0004471") == token
    assert _actions(settings) == [
        "key.rewrap.intent",
        "key.rewrap.failed",
        "key.rewrap.intent",
        "key.rewrap.done",
    ]


def test_an_interrupt_between_the_retired_copy_and_the_rename_is_resumed(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``.retired`` is written and the rename of ``.new`` over the master fails: both
    ``.retired`` and ``.new`` exist, and the next run finishes the install."""
    settings = EdgeSettings(state_dir=state)
    KeyManager(settings).vault_data_key()
    master = settings.local_kms_key_file
    staged = master.with_name(master.name + NEW_SUFFIX)
    retired = master.with_name(master.name + RETIRED_SUFFIX)
    old_master = master.read_bytes()
    token = KeyManager(settings).load_keyring().active.token("id", "SO-0004471")
    real_replace = Path.replace

    def replace(self: Path, target: str | os.PathLike[str]) -> Path:
        if self.name.endswith(NEW_SUFFIX):
            msg = "disk gone"
            raise OSError(msg)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "every key file is under the new master" in err and "re-run" in err
    monkeypatch.undo()
    assert master.read_bytes() == old_master and staged.is_file()
    assert retired.read_bytes() == old_master
    new_kms = LocalKms.open(staged)

    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_OK
    assert "resumed" in capsys.readouterr().out
    assert LocalKms.open(master).key_id == new_kms.key_id and not staged.exists()
    assert retired.read_bytes() == old_master
    for path in KeyManager(settings).wrapped_key_files():
        assert WrappedKey.read(path).key_id == new_kms.key_id
    assert KeyManager(settings).load_keyring().active.token("id", "SO-0004471") == token
    assert verify_audit(settings.audit_file).ok


def test_a_planted_retired_master_refuses_the_rewrap_without_saying_remove(
    state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = EdgeSettings(state_dir=state)
    master = settings.local_kms_key_file
    retired = master.with_name(master.name + RETIRED_SUFFIX)
    retired.write_bytes(bytes(32))
    before = {path.name: path.read_bytes() for path in settings.keys_dir.iterdir()}
    assert run("key", "rewrap", "--state-dir", str(state)) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert RETIRED_SUFFIX in err and "remove" not in err and "delete" not in err
    assert {path.name: path.read_bytes() for path in settings.keys_dir.iterdir()} == before
    assert _actions(settings) == ["key.rewrap.intent", "key.rewrap.failed"]


def test_key_commands_fail_cleanly_without_keys(
    tmp_path: Path, state: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("key", "status", "--state-dir", str(tmp_path / "none")) == EXIT_FAILURE
    assert "key init" in capsys.readouterr().err
    assert run("key", "rotate", "--overlap-days", "0", "--state-dir", str(tmp_path)) == EXIT_USAGE
    # The ceiling keeps ``timedelta`` and ``retire_at`` inside a sane range (ADR 0070 section 7).
    assert run("key", "rotate", "--overlap-days", "401", "--state-dir", str(state)) == EXIT_USAGE
    assert "400" in capsys.readouterr().err


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
    assert rows[0]["details"] == {}, "not even the secret's length is audited"
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
