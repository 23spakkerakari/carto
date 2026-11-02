"""``carto-core`` argument handling and exit codes: usage errors are 2 and never raise, failures
to reach a store are 1 with a message that names the cause and never a credential, and the
ingest-api command refuses to start without its TLS files before touching any database."""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from carto_common.pki import create_ca, issue_certificate, write_pem
from carto_core import cli
from carto_core.cli import EXIT_FAILURE, EXIT_OK, EXIT_USAGE, build_parser, main
from carto_core.db.clickhouse import ClickHouseWriter
from carto_core.ingest.ledger import PostgresBatchLedger
from carto_core.settings import CoreSettings


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("CARTO_"):
            monkeypatch.delenv(name)
    # Unreachable local ports so a store connection fails at once, never a real server.
    monkeypatch.setenv("CARTO_CLICKHOUSE__URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CARTO_CLICKHOUSE__SECURE", "false")
    monkeypatch.setenv("CARTO_CLICKHOUSE__TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("CARTO_POSTGRES__PORT", "1")
    monkeypatch.setenv("CARTO_POSTGRES__SSLMODE", "disable")
    monkeypatch.setenv("CARTO_POSTGRES__CONNECT_TIMEOUT_SECONDS", "1")


@pytest.mark.parametrize(
    "argv",
    [[], ["frobnicate"], ["verify-bundle"], ["load-bundle"], ["migrate", "--bogus"]],
)
def test_usage_errors_return_2_without_raising(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    try:
        code = main(argv)
    except SystemExit as exc:
        pytest.fail(f"main({argv!r}) raised SystemExit({exc.code!r})")
    assert code == EXIT_USAGE
    assert capsys.readouterr().out == ""


def test_help_returns_0(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == EXIT_OK
    out = capsys.readouterr().out
    for command in ("migrate", "ingest-api", "verify-bundle", "load-bundle"):
        assert command in out


def test_migrate_scopes_are_exclusive() -> None:
    parser = build_parser()
    args = parser.parse_args(["migrate", "--clickhouse-only"])
    assert args.clickhouse_only and not args.postgres_only
    with pytest.raises(SystemExit):
        parser.parse_args(["migrate", "--clickhouse-only", "--postgres-only"])


def test_chunk_size_is_validated_before_anything_runs(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["load-bundle", "--bundle", "nowhere.carto", "--chunk-size", "0"]) == EXIT_USAGE
    assert "--chunk-size" in capsys.readouterr().err
    assert main(["load-bundle", "--bundle", "nowhere.carto", "--chunk-size", "6000"]) == EXIT_USAGE


def test_invalid_settings_are_a_usage_error_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CARTO_POSTGRES__PASSWORD", "hunter2-in-the-environment")
    assert main(["migrate"]) == EXIT_USAGE
    err = capsys.readouterr().err
    assert "invalid settings" in err
    assert "hunter2" not in err


def test_unreachable_clickhouse_is_a_failure_naming_the_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["migrate", "--clickhouse-only"]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "migration failed" in err
    assert "cannot connect to ClickHouse" in err


def test_unreachable_postgres_is_a_failure_naming_the_cause(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["migrate", "--postgres-only"]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "migration failed" in err
    assert "OperationalError" in err


def test_ingest_api_refuses_to_start_without_tls_files(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ingest-api"]) == EXIT_FAILURE
    err = capsys.readouterr().err
    assert "ingest-api cannot start" in err
    assert "tls_cert_file" in err


def _pki(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ca = create_ca()
    write_pem(ca, tmp_path / "ca.crt", tmp_path / "ca.key")
    leaf = issue_certificate(ca, "ingest-api", dns_names=("ingest-api", "localhost"))
    write_pem(leaf, tmp_path / "ingest-api.crt", tmp_path / "ingest-api.key")
    monkeypatch.setenv("CARTO_INGEST__TLS_CERT_FILE", str(tmp_path / "ingest-api.crt"))
    monkeypatch.setenv("CARTO_INGEST__TLS_KEY_FILE", str(tmp_path / "ingest-api.key"))
    monkeypatch.setenv("CARTO_INGEST__CLIENT_CA_FILE", str(tmp_path / "ca.crt"))


def _mock_client(monkeypatch: pytest.MonkeyPatch, status: int, seen: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, json={"status": "ok"})

    def factory(settings: CoreSettings, timeout: float) -> httpx.Client:
        cli.health_ssl_context(settings)  # the real context must build from the files
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)

    monkeypatch.setattr(cli, "_health_client", factory)


@pytest.mark.parametrize(("status", "code"), [(200, EXIT_OK), (503, EXIT_FAILURE)])
def test_healthcheck_maps_the_status(
    status: int,
    code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pki(tmp_path, monkeypatch)
    seen: list[str] = []
    _mock_client(monkeypatch, status, seen)
    assert main(["healthcheck", "--url", "https://localhost:8443/healthz"]) == code
    assert seen == ["https://localhost:8443/healthz"]
    if code == EXIT_FAILURE:
        assert "HTTP 503" in capsys.readouterr().err


def test_healthcheck_refuses_plain_http_and_missing_tls_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["healthcheck", "--url", "http://localhost:8443/healthz"]) == EXIT_USAGE
    assert main(["healthcheck", "--url", "https://localhost/healthz", "--timeout", "0"]) == 2
    capsys.readouterr()
    assert main(["healthcheck", "--url", "https://localhost:8443/healthz"]) == EXIT_FAILURE
    assert "tls_cert_file" in capsys.readouterr().err


def test_healthcheck_against_nothing_listening_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _pki(tmp_path, monkeypatch)
    argv = ["healthcheck", "--url", "https://127.0.0.1:1/healthz", "--timeout", "2"]
    assert main(argv) == EXIT_FAILURE
    assert "healthcheck failed" in capsys.readouterr().err


def test_module_entry_point_matches_main() -> None:
    assert sys.modules["carto_core.cli"].main is main


class _Closable:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1

    def dispose(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("fail", [False, True])
def test_open_stores_closes_the_client_and_disposes_the_engine(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    client, engine = _Closable(), _Closable()
    monkeypatch.setattr(cli, "create_client", lambda _settings: client)
    monkeypatch.setattr(cli, "create_engine_from_settings", lambda _settings: engine)
    expected = pytest.raises(RuntimeError) if fail else contextlib.nullcontext()
    with expected, cli._open_stores(CoreSettings()) as (writer, ledger):
        assert isinstance(writer, ClickHouseWriter)
        assert isinstance(ledger, PostgresBatchLedger)
        if fail:
            raise RuntimeError("load failed")
    assert client.closed == 1
    assert engine.closed == 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["migrate"], ["lock", "clickhouse", "retention", "postgres", "unlock"]),
        (["migrate", "--clickhouse-only"], ["lock", "clickhouse", "retention", "unlock"]),
        (["migrate", "--postgres-only"], ["lock", "postgres", "unlock"]),
    ],
)
def test_migrate_runs_both_stores_under_the_advisory_lock(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: list[str]
) -> None:
    """Two migrate jobs at once (a Compose restart, two pods) must not interleave: every
    ClickHouse and PostgreSQL step runs while the PostgreSQL advisory lock is held."""
    steps: list[str] = []
    client, engine = _Closable(), _Closable()
    monkeypatch.setattr(cli, "create_client", lambda _settings: client)
    monkeypatch.setattr(cli, "create_engine_from_settings", lambda _settings: engine)

    @contextlib.contextmanager
    def fake_lock(locked_engine: object) -> Iterator[None]:
        assert locked_engine is engine
        steps.append("lock")
        try:
            yield
        finally:
            steps.append("unlock")

    def step(name: str, result: list[str]) -> Any:
        def run(*_args: object, **_kwargs: object) -> list[str]:
            steps.append(name)
            return result

        return run

    monkeypatch.setattr(cli, "migration_lock", fake_lock)
    monkeypatch.setattr(cli, "apply_clickhouse_migrations", step("clickhouse", []))
    monkeypatch.setattr(cli, "apply_retention", step("retention", []))
    monkeypatch.setattr(cli, "apply_postgres_migrations", step("postgres", []))
    assert main(argv) == EXIT_OK
    assert steps == expected
    assert engine.closed == 1
    assert client.closed == (0 if "--postgres-only" in argv else 1)
