"""The PostgreSQL metadata reader of the LLM gateway (spec 9.9; plan M2 package G): a joined
row becomes a cleaned, bounded field summary (system id when the system has no name, no
template text when the template is unknown, shapes capped, control characters and token-shaped
substrings never survive); against a real database (integration, skipped without Docker) the
summaries come from ``fields``, ``systems``, ``templates`` and ``entity_fields`` only and the
payload built from them passes the forbidden-content oracle."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy

from carto_core.linker.repository import Entity, PostgresEntityRepository
from carto_core.llm_gateway.metadata_reader import (
    MAX_FIELD_IDS,
    MAX_TOP_SHAPES,
    PostgresMetadataReader,
    summary_from_row,
)
from carto_core.llm_gateway.payload import (
    build_label_field_payload,
    build_name_entity_payload,
    forbidden_content_check,
)
from carto_core.llm_gateway.schemas import MAX_TEMPLATE_TEXT_LEN
from carto_core.metadata import FieldRecord, PostgresMetadataStore, TemplateRecord
from carto_core.store.model import FieldForm
from carto_schema.inventory import EdgeInventory, InventorySystem

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
TENANT = "default"
TOKEN = "t1.AAAAAAAAAAAAAAAAAAAAAA"  # noqa: S105 - a token-shaped test string, not a secret


def test_a_row_becomes_a_cleaned_summary() -> None:
    row = {
        "field_id": "sys_a/tpl_a/order_id",
        "path": "order\x00_id\n",
        "system_id": "sys_a",
        "shapes_json": [{"shape": f"S{i}", "share": 0.1} for i in range(MAX_TOP_SHAPES + 3)],
        "system_name": "Order  system",
        "template_text": "order <*> created for " + TOKEN + " x" * MAX_TEMPLATE_TEXT_LEN,
    }
    summary = summary_from_row(TENANT, row)
    assert summary.field_id == "sys_a/tpl_a/order_id"
    assert summary.path == "order_id"
    assert summary.system_name == "Order system"
    assert len(summary.top_shapes) == MAX_TOP_SHAPES and summary.top_shapes[0] == "S0"
    assert TOKEN not in summary.template_text and "<*>" in summary.template_text
    assert len(summary.template_text) <= MAX_TEMPLATE_TEXT_LEN
    assert summary.attributes == []


def test_missing_joins_fall_back_to_the_id_and_an_empty_text() -> None:
    summary = summary_from_row(
        TENANT,
        {
            "field_id": "sys_a/tpl_a/x",
            "path": "x",
            "system_id": "sys_a",
            "shapes_json": '[{"shape": "9999", "share": 1.0}, {"bogus": 1}]',
            "system_name": None,
            "template_text": None,
        },
    )
    assert summary.system_name == "sys_a" and summary.template_text == ""
    assert summary.top_shapes == ["9999"], "text JSON is read; a malformed entry is skipped"
    assert summary_from_row(TENANT, {"field_id": "f", "shapes_json": "nope"}).top_shapes == []


def test_the_reader_bounds_the_ids_it_asks_for() -> None:
    assert MAX_FIELD_IDS >= 64, "a task takes up to 64 fields"
    engine = sqlalchemy.create_engine("postgresql+psycopg://carto:x@127.0.0.1:1/carto")
    assert PostgresMetadataReader(engine).field_summaries(TENANT, []) == [], "no round trip"


# --- PostgreSQL ----------------------------------------------------------------------------------

POSTGRES_IMAGE = os.environ.get("CARTO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
DB_USER = "carto_test"
DB_PASSWORD = "carto_test"  # noqa: S105 - throwaway container credential
DB_NAME = "carto_test"


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


integration = [
    pytest.mark.integration,
    pytest.mark.skipif(not _docker_available(), reason="Docker is unreachable"),
    pytest.mark.filterwarnings("ignore::ResourceWarning"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
]


@pytest.fixture(scope="module")
def engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sqlalchemy.Engine]:
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415
    from testcontainers.core.config import testcontainers_config  # noqa: PLC0415

    from carto_core.db.postgres import create_engine_from_settings  # noqa: PLC0415
    from carto_core.migrations import apply_postgres_migrations  # noqa: PLC0415
    from carto_core.settings import PostgresSettings  # noqa: PLC0415

    if os.name == "nt":
        testcontainers_config.ryuk_disabled = True
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


def _field(ref: str, shapes: tuple[tuple[str, float], ...] = (("9999", 1.0),)) -> FieldRecord:
    system_id, template_id, path = ref.split("/", 2)
    return FieldRecord(
        field_ref=ref,
        system_id=system_id,
        template_id=template_id,
        path=path,
        field_class="identifier",
        policy="tokenize",
        pinned=False,
        forms=("raw",),
        count=100,
        shapes=shapes,
        first_seen=NOW,
        last_seen=NOW,
    )


@pytest.mark.parametrize("_marker", [pytest.param(None, marks=integration)])
def test_summaries_come_from_the_metadata_tables_only(
    engine: sqlalchemy.Engine, _marker: None
) -> None:
    metadata = PostgresMetadataStore(engine)
    metadata.replace_inventory(
        EdgeInventory(
            schema_version="1",
            tenant_id=TENANT,
            edge_id="edge",
            sent_at=NOW,
            sources_file_sha256="d" * 64,
            systems=[InventorySystem(id="sys_a", name="Order system")],
            sources=[],
        ),
        NOW,
    )
    metadata.upsert_templates(
        TENANT,
        [
            TemplateRecord(
                system_id="sys_a",
                template_id="tpl_a",
                template_text="order <*> created by " + TOKEN,
                kind="log",
                first_seen=NOW,
                last_seen=NOW,
                count=1,
            )
        ],
    )
    metadata.upsert_fields(
        TENANT,
        [
            _field("sys_a/tpl_a/order_id", (("9999", 0.9), ("99999", 0.1))),
            _field("sys_b/tpl_b/order_ref"),
            _field("sys_a/tpl_a/other"),
        ],
    )
    PostgresEntityRepository(engine).replace_all(
        TENANT,
        [
            Entity(
                id="ent_0123456789abcdef",
                members=(
                    FieldForm("sys_a/tpl_a/order_id", "raw"),
                    FieldForm("sys_a/tpl_a/order_id", "digits.0"),
                    FieldForm("sys_b/tpl_b/order_ref", "raw"),
                ),
            )
        ],
        NOW,
    )
    reader = PostgresMetadataReader(engine)

    summaries = {
        s.field_id: s
        for s in reader.field_summaries(
            TENANT, ["sys_a/tpl_a/order_id", "sys_b/tpl_b/order_ref", "sys_x/none/none"]
        )
    }
    assert set(summaries) == {"sys_a/tpl_a/order_id", "sys_b/tpl_b/order_ref"}
    order_id = summaries["sys_a/tpl_a/order_id"]
    assert order_id.system_name == "Order system" and order_id.top_shapes == ["9999", "99999"]
    assert order_id.template_text == "order <*> created by <*>", "the token never leaves"
    order_ref = summaries["sys_b/tpl_b/order_ref"]
    assert order_ref.system_name == "sys_b" and order_ref.template_text == ""
    assert reader.field_summaries("other", ["sys_a/tpl_a/order_id"]) == []

    members = reader.entity_members(TENANT, "ent_0123456789abcdef")
    assert sorted(m.field_id for m in members) == ["sys_a/tpl_a/order_id", "sys_b/tpl_b/order_ref"]
    assert reader.entity_members(TENANT, "ent_missing") == []

    payload = build_name_entity_payload(reader, TENANT, "ent_0123456789abcdef").model_dump_json()
    assert forbidden_content_check(payload, forbidden=[TOKEN]) == ()
    labels = build_label_field_payload(reader, TENANT, ["sys_a/tpl_a/other"])
    assert [f.path for f in labels.fields] == ["other"]
