"""``carto-core discover`` (plan M2 "How the engine runs" step 6; package I) on the in-memory
store with the in-memory repositories: one full profile pass and one full link pass over a
planted install, the three output files and their shapes, the reference-time rules, the
summary lines, the error paths, and nothing in the files that is a token. The deployment
wiring (``run_discover`` over ClickHouse and PostgreSQL) runs in a container when Docker is
reachable (``integration``)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from carto_common.ids import derive_ulid
from carto_core.audit import GENESIS_HASH, AuditEntry, chain_hash
from carto_core.discover import (
    ENGINE_ENTITIES_FILE,
    ENGINE_LINKS_FILE,
    SUMMARY_FILE,
    DiscoverError,
    EngineParts,
    default_as_of,
    discover,
    parse_as_of,
    summary_lines,
)
from carto_core.linker.composite import InMemoryCompositeRuleRepository
from carto_core.linker.repository import InMemoryEntityRepository, InMemoryLinkRepository
from carto_core.metadata import InMemoryMetadataStore, SystemRecord
from carto_core.profiler.repository import InMemorySketchRepository, InMemoryWatermarkStore
from carto_core.settings import CoreSettings
from carto_core.store.memory import InMemoryEventStore
from carto_schema.event import CanonicalEvent

TENANT = "default"
BASE_MS = 1_790_000_000_000
DAY0 = date(2026, 10, 12)
AS_OF = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
N = 80
"""Distinct order ids: above the 50-distinct floor of spec 9.1."""


def token(value: str) -> str:
    body = base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).decode()
    return f"t1.{body[:22]}"


def event(
    seed: str,
    system_id: str,
    template_id: str,
    observed_at: datetime,
    identifiers: Sequence[tuple[str, str, str, str]],
) -> CanonicalEvent:
    data = CanonicalEvent.example().model_dump() | {
        "event_id": derive_ulid(BASE_MS, seed),
        "tenant_id": TENANT,
        "system_id": system_id,
        "source_id": f"src_{system_id.removeprefix('sys_')}",
        "template_id": template_id,
        "observed_at": observed_at,
        "ingested_at": observed_at,
        "attributes": {"status": "ok"},
        "identifiers": [
            {"field": f, "form": form, "token": token(value), "shape": shape, "len": len(value)}
            for f, form, value, shape in identifiers
        ],
    }
    return CanonicalEvent.model_validate(data)


def planted_events() -> list[CanonicalEvent]:
    """Orders create ``order_id`` with a ``cart_id`` (a bridge); payments carry the same order
    id one minute later (an exact link); a third system's field shares nothing."""
    events: list[CanonicalEvent] = []
    for i in range(N):
        day = DAY0 + timedelta(days=i % 3)
        at = datetime(day.year, day.month, day.day, 9, i % 60, tzinfo=UTC)
        order = f"SO-{i:05d}"
        cart = f"C{i:05d}"
        events.append(
            event(
                f"ord{i}",
                "sys_orders",
                "tpl_create",
                at,
                [("order_id", "raw", order, "AA-99999"), ("cart_id", "raw", cart, "A99999")],
            )
        )
        events.append(
            event(
                f"pay{i}",
                "sys_pay",
                "tpl_auth",
                at + timedelta(minutes=1),
                [("merchant_ref", "raw", order, "AA-99999")],
            )
        )
        events.append(
            event(
                f"wms{i}",
                "sys_wms",
                "tpl_po",
                at + timedelta(hours=2),
                [("po_num", "raw", f"PO{i:06d}", "AA999999")],
            )
        )
    return events


class Recorder:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self._head = GENESIS_HASH

    def record(self, entry: AuditEntry) -> str:
        self.entries.append(entry)
        self._head = chain_hash(self._head, entry.content())
        return self._head

    def latest_hash(self, tenant_id: str) -> str:
        return self._head


def parts(events: Sequence[CanonicalEvent], *, stored_at: datetime = AS_OF) -> EngineParts:
    store = InMemoryEventStore()
    store.add_events(events, stored_at=stored_at)
    metadata = InMemoryMetadataStore()
    system = SystemRecord("sys_orders", "Order system")
    metadata._systems[(TENANT, "sys_orders")] = system
    return EngineParts(
        store=store,
        metadata=metadata,
        sketches=InMemorySketchRepository(),
        watermarks=InMemoryWatermarkStore(),
        links=InMemoryLinkRepository(),
        entities=InMemoryEntityRepository(),
        composite_rules=InMemoryCompositeRuleRepository(),
        audit=Recorder(),
        settings=CoreSettings(_env_file=None),
    )


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, object]:
    out = tmp_path_factory.mktemp("discover") / "out"
    result = discover(parts(planted_events()), tenant_id=TENANT, out_dir=out, as_of=AS_OF)
    return out, result


def test_discover_writes_links_entities_and_summary(run: tuple[Path, object]) -> None:
    out, _result = run
    links = json.loads((out / ENGINE_LINKS_FILE).read_text(encoding="utf-8"))
    entities = json.loads((out / ENGINE_ENTITIES_FILE).read_text(encoding="utf-8"))
    summary = json.loads((out / SUMMARY_FILE).read_text(encoding="utf-8"))
    assert isinstance(links, list) and isinstance(entities, list)
    pairs = {
        (link["a"]["system_id"], link["a"]["path"], link["b"]["system_id"], link["b"]["path"])
        for link in links
    }
    assert ("sys_orders", "order_id", "sys_pay", "merchant_ref") in pairs
    assert ("sys_orders", "cart_id", "sys_orders", "order_id") in pairs
    assert not any("sys_wms" in (link["a"]["system_id"], link["b"]["system_id"]) for link in links)
    exact = next(link for link in links if link["b"]["path"] == "merchant_ref")
    assert exact["link_type"] == "exact" and exact["status"] == "proposed"
    assert exact["a"]["field_ref"] == "sys_orders/tpl_create/order_id"
    assert exact["a"]["template_id"] == "tpl_create" and exact["a"]["form"] == "raw"
    assert exact["affected_volume"] == N and exact["evidence"]["n_match"] == N
    assert 0.5 <= exact["score"] <= 1.0 and exact["role"] == "transaction"
    assert "sentence" in exact["extra"] and "Order system" in exact["extra"]["sentence"]
    bridge = next(link for link in links if link["a"]["path"] == "cart_id")
    assert bridge["link_type"] == "bridge" and bridge["evidence"]["pairing_consistency"] == 1.0
    # auto-accept is off, so the engine's own families are empty (the eval policy builds them)
    assert entities == []
    assert summary["as_of"] == AS_OF.isoformat()
    # the profiler's day-aligned window (ADR 0058 item 7): the last window_days UTC days
    # including today, plus the day ahead; the link pass joins over the same days
    assert summary["window"] == {
        "start": (AS_OF - timedelta(days=6)).isoformat(),
        "end": (AS_OF + timedelta(days=2)).isoformat(),
    }
    assert summary["weights_version"] == "v1-hand-set"
    assert summary["profile"]["full"] is True and summary["profile"]["rows_read"] == 4 * N
    assert summary["link"]["candidates_considered"] >= 2
    assert summary["links"]["total"] == len(links)
    assert summary["links"]["by_status"] == {"proposed": len(links)}
    assert summary["settings"]["linker"]["proposal_threshold"] == 0.5
    assert summary["settings"]["profiler"]["window_days"] == 7
    assert isinstance(summary["composite"]["pairs_considered"], int)
    assert "bundle" not in summary


def test_nothing_in_the_files_is_a_token(run: tuple[Path, object]) -> None:
    out, _result = run
    text = "".join(
        (out / name).read_text(encoding="utf-8")
        for name in (ENGINE_LINKS_FILE, ENGINE_ENTITIES_FILE, SUMMARY_FILE)
    )
    assert "t1." not in text
    for i in range(N):
        assert token(f"SO-{i:05d}") not in text and f"SO-{i:05d}" not in text


def test_summary_lines_name_counts_and_paths(run: tuple[Path, object]) -> None:
    out, result = run
    lines = summary_lines(result)  # type: ignore[arg-type]
    assert lines[0].startswith("discover: tenant default, as of 2026-10-15T00:00:00+00:00")
    assert any(line.startswith("  profile    320 identifier rows") for line in lines)
    assert any("weights v1-hand-set" in line for line in lines)
    assert any(str(out / ENGINE_LINKS_FILE) in line for line in lines)
    assert "t1." not in "\n".join(lines)


def test_parse_as_of_and_default_as_of() -> None:
    assert parse_as_of("2026-10-08") == datetime(2026, 10, 8, tzinfo=UTC)
    assert parse_as_of("2026-10-08T01:02:03+02:00") == datetime(2026, 10, 7, 23, 2, 3, tzinfo=UTC)
    with pytest.raises(DiscoverError, match="ISO 8601"):
        parse_as_of("yesterday")
    with pytest.raises(DiscoverError, match="offset"):
        parse_as_of("2026-10-08T01:02:03")
    stored = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    engine = parts(planted_events()[:3], stored_at=stored)
    assert default_as_of(engine.store, TENANT) == stored + timedelta(seconds=1)
    empty = InMemoryEventStore()
    clock = datetime(2026, 10, 20, tzinfo=UTC)
    assert default_as_of(empty, TENANT, clock=lambda: clock) == clock


def test_second_run_refreshes_rather_than_duplicates(tmp_path: Path) -> None:
    engine = parts(planted_events())
    first = discover(engine, tenant_id=TENANT, out_dir=tmp_path, as_of=AS_OF)
    second = discover(engine, tenant_id=TENANT, out_dir=tmp_path, as_of=AS_OF)
    assert len(second.links) == len(first.links)
    assert second.link.proposed == 0 and second.link.updated == len(first.links)
    assert all(link.version == 2 for link in second.links)


def test_output_directory_that_cannot_be_created_is_a_discover_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(DiscoverError, match="cannot create"):
        discover(
            parts(planted_events()[:3]), tenant_id=TENANT, out_dir=blocker / "out", as_of=AS_OF
        )


# --- the deployment wiring, in containers ------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        return False
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


@pytest.mark.integration
@pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable")
@pytest.mark.filterwarnings("ignore::ResourceWarning")
@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_run_discover_over_clickhouse_and_postgres(tmp_path: Path) -> None:
    """``carto-core discover`` without a bundle: events written through the ingest writer,
    the stores migrated, one discovery, the same files as the in-memory run."""
    from testcontainers.community.clickhouse import ClickHouseContainer  # noqa: PLC0415
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    from testcontainers.core.config import testcontainers_config  # noqa: PLC0415

    from carto_core.db.clickhouse import ClickHouseWriter, create_client  # noqa: PLC0415
    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.discover import run_discover  # noqa: PLC0415
    from carto_core.migrations import (  # noqa: PLC0415
        apply_clickhouse_migrations,
        apply_postgres_migrations,
    )
    from carto_core.settings import ClickHouseSettings, PostgresSettings  # noqa: PLC0415

    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
    password = "carto_test"  # noqa: S105 - throwaway container credential
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "ch").write_text(password + "\n", encoding="utf-8")
    (secrets / "pg").write_text(password + "\n", encoding="utf-8")
    ch_image = os.environ.get("CARTO_TEST_CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:24.8")
    pg_image = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
    with (
        ClickHouseContainer(
            ch_image, username="carto_test", password=password, dbname="carto"
        ) as ch,
        PostgresContainer(
            pg_image, username="carto_test", password=password, dbname="carto", driver=None
        ) as pg,
    ):
        clickhouse = ClickHouseSettings(
            url=f"http://{ch.get_container_host_ip()}:{ch.get_exposed_port(8123)}",
            database="carto",
            user="carto_test",
            password_file=secrets / "ch",
            secure=False,
            timeout_seconds=60,
        )
        postgres = PostgresSettings(
            host=pg.get_container_host_ip(),
            port=int(pg.get_exposed_port(5432)),
            database="carto",
            user="carto_test",
            password_file=secrets / "pg",
            sslmode="disable",
        )
        settings = CoreSettings(_env_file=None, clickhouse=clickhouse, postgres=postgres)
        client = create_client(clickhouse)
        try:
            apply_clickhouse_migrations(client, settings.retention)
            engine = create_engine_from_settings(postgres)
            try:
                apply_postgres_migrations(engine)
            finally:
                engine.dispose()
            ClickHouseWriter(client).write_events(_recent(planted_events()))
        finally:
            client.close()
        out = tmp_path / "out"
        lines = run_discover(settings, out_dir=out, bundle=None, as_of=None)
        assert lines[0].startswith("discover: tenant default")
        links = json.loads((out / ENGINE_LINKS_FILE).read_text(encoding="utf-8"))
        pairs = {(link["a"]["path"], link["b"]["path"]) for link in links}
        assert ("order_id", "merchant_ref") in pairs
        assert (out / SUMMARY_FILE).is_file() and (out / ENGINE_ENTITIES_FILE).is_file()


def _recent(events: Sequence[CanonicalEvent]) -> list[CanonicalEvent]:
    """The planted events shifted to the last three days, inside the plausible range."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    shift = today - timedelta(days=3) - datetime(DAY0.year, DAY0.month, DAY0.day, tzinfo=UTC)
    return [
        item.model_copy(
            update={
                "observed_at": item.observed_at + shift,
                "ingested_at": item.ingested_at + shift,
            }
        )
        for item in events
    ]
