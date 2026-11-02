"""Container-backed checks of the PostgreSQL identity stores and the audit log (spec 14.1, 14.9;
ADR 0051; migration 0004 of package A): sessions round-trip with only the cookie hash stored,
pending logins pop once, users keep their reveal grant across logins, role bindings seed once,
audit rows chain per tenant and verify, an attempted UPDATE is either rejected by the trigger
or detected by the verifier. Skipped when Docker is unreachable."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import docker  # type: ignore[import-untyped]
import pytest
import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.config import testcontainers_config

from carto_core.api.ports import LoginAttempt, RoleBinding, SessionRecord, UserRecord
from carto_core.api.stores_postgres import (
    PostgresRoleBindingStore,
    PostgresSessionStore,
    PostgresUserStore,
)
from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash
from carto_core.audit_postgres import PostgresAuditLog, normalize, verify_audit_log
from carto_core.db.postgres import create_engine_from_settings
from carto_core.migrations import apply_postgres_migrations
from carto_core.settings import PostgresSettings
from carto_schema.api import Role

POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _docker_available() -> bool:
    try:
        client = docker.from_env()
    except Exception:
        return False
    try:
        client.ping()
    except Exception:
        return False
    finally:
        client.close()
    return True


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


@pytest.fixture(scope="module", autouse=True)
def _no_ryuk_on_windows() -> None:
    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlalchemy.Engine]:
    password_file = tmp_path_factory.mktemp("secrets") / "postgres"
    password_file.write_text(DB_PASSWORD + "\n", encoding="utf-8")
    with PostgresContainer(
        POSTGRES_IMAGE, username=DB_USER, password=DB_PASSWORD, dbname=DB_NAME, driver=None
    ) as container:
        settings = PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=DB_NAME,
            user=DB_USER,
            password_file=password_file,
            sslmode="disable",
        )
        engine = create_engine_from_settings(settings)
        apply_postgres_migrations(engine)
        yield engine
        engine.dispose()


def _session(session_id: str, subject: str = "alice") -> SessionRecord:
    return SessionRecord(
        id=session_id,
        tenant_id="default",
        subject=subject,
        csrf_token="c" * 43,
        authenticated_at=NOW - timedelta(seconds=5),
        last_seen_at=NOW,
        expires_at=NOW + timedelta(hours=12),
        roles=(Role.ADMIN, Role.AUDITOR),
        display_name="Alice",
        ip="10.0.0.1",
        user_agent="pytest",
        reveal_granted=True,
    )


def test_sessions_round_trip_touch_and_delete(engine: sqlalchemy.Engine) -> None:
    store = PostgresSessionStore(engine)
    assert store.ping()
    store.create(_session("a" * 64))
    loaded = store.get("a" * 64)
    assert loaded == _session("a" * 64)
    store.touch("a" * 64, NOW + timedelta(minutes=5))
    touched = store.get("a" * 64)
    assert touched is not None and touched.last_seen_at == NOW + timedelta(minutes=5)
    store.delete("a" * 64)
    assert store.get("a" * 64) is None
    assert store.get("missing") is None


def test_expired_sessions_are_swept_on_create(engine: sqlalchemy.Engine) -> None:
    store = PostgresSessionStore(engine)
    old = replace(_session("b" * 64), expires_at=NOW - timedelta(seconds=1))
    store.create(old)
    store.create(_session("c" * 64))
    assert store.get("b" * 64) is None
    assert store.get("c" * 64) is not None
    store.delete("c" * 64)


def test_pending_logins_pop_once_and_never_resolve_as_sessions(engine: sqlalchemy.Engine) -> None:
    store = PostgresSessionStore(engine)
    attempt = LoginAttempt(
        state_hash="d" * 64,
        tenant_id="default",
        code_verifier="v" * 64,
        nonce="n" * 43,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
    )
    store.put(attempt)
    assert store.get("d" * 64) is None, "a pending login is not a session"
    assert store.pop("d" * 64) == attempt
    assert store.pop("d" * 64) is None
    store.create(_session("e" * 64))
    assert store.pop("e" * 64) is None, "a session is not a login attempt"


def test_users_keep_their_reveal_grant_across_logins(engine: sqlalchemy.Engine) -> None:
    store = PostgresUserStore(engine)
    first = store.upsert(UserRecord("default", "alice", "Alice", "a@example.test", NOW, True))
    assert first.reveal_granted is False, "the grant is never set by a login"
    with engine.begin() as connection:
        connection.execute(
            sqlalchemy.text(
                "UPDATE users SET reveal_granted = true WHERE tenant_id = 'default' "
                "AND subject = 'alice'"
            )
        )
    second = store.upsert(
        UserRecord("default", "alice", "Alice E.", "ae@example.test", NOW + timedelta(days=1))
    )
    assert second.reveal_granted is True
    assert second.display_name == "Alice E." and second.email == "ae@example.test"
    assert second.last_login_at == NOW + timedelta(days=1)
    assert store.get("default", "alice") == second
    assert store.get("default", "nobody") is None


def test_role_bindings_seed_once(engine: sqlalchemy.Engine) -> None:
    store = PostgresRoleBindingStore(engine)
    bindings = [
        RoleBinding("seed:admins", "default", "admins", Role.ADMIN),
        RoleBinding(
            "seed:eng", "default", "engineers", Role.INTEGRATION_ENGINEER, {"system": "shop"}
        ),
    ]
    assert store.list("default") == []
    assert store.seed(bindings) is True
    assert store.seed([RoleBinding("seed:x", "default", "x", Role.AUDITOR)]) is False
    listed = store.list("default")
    assert [(b.group_name, b.role) for b in listed] == [
        ("admins", Role.ADMIN),
        ("engineers", Role.INTEGRATION_ENGINEER),
    ]
    assert listed[1].scope == {"system": "shop"}
    assert store.list("other") == []


def test_audit_rows_chain_per_tenant_and_verify(engine: sqlalchemy.Engine) -> None:
    log = PostgresAuditLog(engine)
    assert log.latest_hash("default") == GENESIS_HASH
    entries = [
        AuditEntry(
            at=NOW + timedelta(seconds=index),
            actor="alice",
            action="link.accept",
            target=f"link:{index}",
            before={"status": "proposed", "score": 0.9, "version": 1},
            after={"status": "accepted", "score": 0.9, "version": 2, "token": "scrubbed"},
            reason="",
            request_id=f"req-{index}",
            ip="10.0.0.1",
            user_agent="pytest",
        )
        for index in range(3)
    ]
    hashes = [log.record(entry) for entry in entries]
    assert log.latest_hash("default") == hashes[-1]
    other = log.record(replace(entries[0], tenant_id="other"))
    assert log.latest_hash("other") == other
    assert verify_audit_log(engine, "default") is None
    assert verify_audit_log(engine, "other") is None
    assert verify_audit_log(engine, "empty") is None
    page = log.list("default", None, 2)
    assert [row.target for row in page.items] == ["link:2", "link:1"]
    assert page.next_cursor == str(page.items[-1].id)
    rest = log.list("default", page.next_cursor, 2)
    assert [row.target for row in rest.items] == ["link:0"] and rest.next_cursor is None
    assert rest.items[0].after == {"status": "accepted", "score": 0.9, "version": 2}
    assert rest.items[0].row_hash == hashes[0]
    assert log.list("default", "garbage", 2).items == ()
    with engine.connect() as connection:
        row = connection.execute(
            sqlalchemy.text(
                "SELECT prev_hash, row_hash FROM audit_log WHERE tenant_id = 'default' "
                "ORDER BY id LIMIT 1"
            )
        ).first()
    assert row is not None and row.prev_hash == GENESIS_HASH
    assert row.row_hash == chain_hash(GENESIS_HASH, normalize(entries[0]).content())


def test_tampering_is_rejected_or_detected(engine: sqlalchemy.Engine) -> None:
    log = PostgresAuditLog(engine)
    log.record(
        AuditEntry(
            NOW,
            "alice",
            "entity.patch",
            "entity:1",
            None,
            {"name": "x"},
            "",
            None,
            None,
            None,
            "tamper",
        )
    )
    rejected = False
    try:
        with engine.begin() as connection:
            connection.execute(
                sqlalchemy.text("UPDATE audit_log SET actor = 'mallory' WHERE tenant_id = 'tamper'")
            )
    except SQLAlchemyError:
        rejected = True
    if rejected:
        assert verify_audit_log(engine, "tamper") is None
    else:
        broken = verify_audit_log(engine, "tamper")
        assert broken is not None
