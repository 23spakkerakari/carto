"""carto_edge.connectors.sql_dialects: the sqlglot statement check (spec 8.1.4 layer 3), the
connection helpers and the privilege interpretation."""

from __future__ import annotations

import pytest

from carto_edge.connectors.base import InvalidConfigError, ResolvedHost
from carto_edge.connectors.sql_dialects import (
    PrivilegeQuery,
    StatementCheckError,
    check_statement,
    mssql_connection_string,
    mysql_connect_kwargs,
    mysql_session_statements,
    postgres_connect_kwargs,
    privilege_queries,
    write_findings,
)
from carto_edge.secrets import Credentials

APPENDIX_A = """
SELECT id, po_num, order_ref, status, warehouse_code, created_by, updated_at
FROM purchase_orders
WHERE updated_at > :watermark
AND updated_at <= :upper
ORDER BY updated_at
LIMIT :batch
"""
BY_ID = "SELECT id, status FROM purchase_orders WHERE id > :watermark ORDER BY id LIMIT :batch"


def test_appendix_a_query_is_accepted() -> None:
    checked = check_statement(APPENDIX_A, "postgresql", upper=True)
    assert checked.tables == ("purchase_orders",)
    assert checked.dialect == "postgresql"
    assert checked.sql.startswith("SELECT id")
    assert check_statement(APPENDIX_A, "mysql", upper=True).tables == ("purchase_orders",)


def test_an_integer_watermark_query_binds_no_upper() -> None:
    assert check_statement(BY_ID, "postgresql").tables == ("purchase_orders",)


def test_a_time_range_query_must_bind_upper() -> None:
    """ADR 0035 item 3: a datetime or date watermark reads (:watermark, :upper]."""
    with pytest.raises(StatementCheckError, match="closed range"):
        check_statement(BY_ID, "postgresql", upper=True)


def test_tsql_top_form_is_accepted() -> None:
    sql = (
        "SELECT TOP (:batch) id, updated_at FROM dbo.purchase_orders "
        "WHERE updated_at > :watermark ORDER BY updated_at"
    )
    assert check_statement(sql, "mssql").tables == ("dbo.purchase_orders",)


def test_with_select_is_accepted_and_ctes_are_not_tables() -> None:
    sql = (
        "WITH recent AS (SELECT * FROM purchase_orders WHERE updated_at > :watermark) "
        "SELECT r.id FROM recent r JOIN warehouses w ON w.code = r.warehouse_code LIMIT :batch"
    )
    assert check_statement(sql, "postgresql").tables == ("purchase_orders", "warehouses")


def test_trailing_semicolon_and_comments_are_fine() -> None:
    sql = "-- latest rows\nSELECT id FROM t WHERE x > :watermark LIMIT :batch;"
    assert check_statement(sql, "postgresql").tables == ("t",)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (
            "UPDATE purchase_orders SET status = 'x' WHERE id > :watermark LIMIT :batch",
            "single SELECT",
        ),
        ("DELETE FROM purchase_orders WHERE id > :watermark LIMIT :batch", "single SELECT"),
        ("INSERT INTO t SELECT * FROM u WHERE a > :watermark LIMIT :batch", "single SELECT"),
        ("SELECT id FROM t WHERE a > :watermark LIMIT :batch; DROP TABLE t", "exactly one"),
        ("SELECT id INTO t2 FROM t WHERE a > :watermark LIMIT :batch", "INTO"),
        ("SELECT nextval('s'), id FROM t WHERE a > :watermark LIMIT :batch", "nextval"),
        ("SELECT pg_sleep(10), id FROM t WHERE a > :watermark LIMIT :batch", "pg_sleep"),
        (
            "SELECT id FROM t WHERE a > :watermark AND b = :other LIMIT :batch",
            "named bind parameters",
        ),
        ("SELECT id FROM t WHERE a > :watermark AND b = ? LIMIT :batch", "named bind parameters"),
        ("SELECT id FROM t WHERE a > :watermark LIMIT 10", "must use :batch and :watermark"),
        (
            "SELECT id FROM t WHERE a > :watermark AND a <= :upper LIMIT :batch",
            "datetime and date watermarks only",
        ),
        ("SELECT id FROM t WHERE a > '%(watermark)s' LIMIT :batch", "formatting"),
        ("SELECT id FROM t WHERE a > %s LIMIT :batch", "formatting"),
        ("SELECT id FROM t WHERE a > '{watermark}' LIMIT :batch", "formatting"),
        ("SELECT id FROM t WHERE a > :watermark LIMIT :batch FOR UPDATE", "locking"),
        ("", "empty"),
        ("SELECT id FROM t WHERE a > :watermark LIMIT :batch GARBAGE GARBAGE (((", "parse"),
        ("GRANT SELECT ON t TO u", "single SELECT"),
        ("SELECT (SELECT setval('s', 1)) FROM t WHERE a > :watermark LIMIT :batch", "setval"),
    ],
)
def test_rejected_statements(sql: str, reason: str) -> None:
    with pytest.raises(StatementCheckError, match=reason):
        check_statement(sql, "postgresql")


def test_mysql_and_mssql_denied_functions() -> None:
    with pytest.raises(StatementCheckError, match="sleep"):
        check_statement("SELECT sleep(5), id FROM t WHERE a > :watermark LIMIT :batch", "mysql")
    with pytest.raises(StatementCheckError, match="load_file"):
        check_statement(
            "SELECT load_file('/x'), id FROM t WHERE a > :watermark LIMIT :batch", "mysql"
        )
    with pytest.raises(StatementCheckError, match="xp_cmdshell"):
        check_statement(
            "SELECT TOP (:batch) xp_cmdshell('dir') FROM t WHERE a > :watermark", "mssql"
        )


def test_statement_check_error_is_an_invalid_config_error() -> None:
    assert issubclass(StatementCheckError, InvalidConfigError)


def test_statement_length_limit() -> None:
    sql = (
        "SELECT id FROM t WHERE a > :watermark AND b IN ("  # noqa: S608
        + ",".join(["1"] * 20_000)
        + ") LIMIT :batch"
    )
    with pytest.raises(StatementCheckError, match="exceeds"):
        check_statement(sql, "postgresql")


# ---------------------------------------------------------------------------------------------
# connection helpers
# ---------------------------------------------------------------------------------------------

CREDS = Credentials("carto_ro", "p}w;d")


def test_postgres_pins_hostaddr_and_keeps_host_for_verify_full() -> None:
    kwargs = postgres_connect_kwargs(
        host="wms-replica.internal.example",
        resolved=ResolvedHost(host="wms-replica.internal.example", address="10.20.1.5", port=5432),
        port=5432,
        database="wms",
        credentials=CREDS,
        sslmode="verify-full",
        ca_file="/etc/carto/ca.pem",
        connect_timeout_seconds=10,
        query_timeout_seconds=30,
    )
    assert kwargs["host"] == "wms-replica.internal.example"
    assert kwargs["hostaddr"] == "10.20.1.5"
    assert kwargs["sslmode"] == "verify-full"
    assert kwargs["sslrootcert"] == "/etc/carto/ca.pem"
    assert "default_transaction_read_only=on" in kwargs["options"]
    assert "statement_timeout=30000" in kwargs["options"]
    # ADR 0035 item 6: naive watermarks compare in UTC, whatever the server's default zone.
    assert "-c TimeZone=UTC" in kwargs["options"]
    assert kwargs["password"] == "p}w;d"  # noqa: S105


def test_mysql_kwargs_and_session() -> None:
    kwargs = mysql_connect_kwargs(
        host="db",
        port=3306,
        database="wms",
        credentials=CREDS,
        ssl=True,
        ca_file=None,
        connect_timeout_seconds=10,
        query_timeout_seconds=30,
    )
    assert kwargs["ssl_verify_cert"] and kwargs["ssl_verify_identity"]
    assert kwargs["read_timeout"] == 30
    plain = mysql_connect_kwargs(
        host="db",
        port=3306,
        database="wms",
        credentials=CREDS,
        ssl=False,
        ca_file=None,
        connect_timeout_seconds=10,
        query_timeout_seconds=30,
    )
    assert plain["ssl_disabled"] is True
    assert mysql_session_statements(30) == (
        "SET SESSION max_execution_time = 30000",
        "SET SESSION TRANSACTION READ ONLY",
    )


def test_mssql_connection_string_read_only_intent_and_escaping() -> None:
    text = mssql_connection_string(
        host="sql.internal",
        port=1433,
        database="wms",
        credentials=CREDS,
        driver="ODBC Driver 18 for SQL Server",
        encrypt=True,
        trust_server_certificate=False,
        connect_timeout_seconds=10,
    )
    assert "ApplicationIntent=ReadOnly" in text
    assert "Encrypt=yes" in text
    assert "TrustServerCertificate=no" in text
    assert "SERVER={sql.internal,1433}" in text
    assert "PWD={p}}w;d}" in text  # braces doubled, the ';' inside stays quoted
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in text


# ---------------------------------------------------------------------------------------------
# privileges
# ---------------------------------------------------------------------------------------------


def test_postgres_privilege_queries_and_findings() -> None:
    queries = privilege_queries("postgresql", ["purchase_orders", "wms.shipments"])
    assert [query.name for query in queries] == ["table", "table", "role"]
    assert queries[0].params == {"t": "purchase_orders"}
    assert "has_table_privilege(current_user, :t, 'INSERT')" in queries[0].sql
    assert (
        write_findings(
            "postgresql",
            queries[0],
            [
                {
                    "can_insert": False,
                    "can_update": False,
                    "can_delete": False,
                    "can_truncate": False,
                }
            ],
        )
        == []
    )
    assert write_findings(
        "postgresql",
        queries[1],
        [{"can_insert": True, "can_update": False, "can_delete": True, "can_truncate": False}],
    ) == [
        "login can INSERT on wms.shipments",
        "login can DELETE on wms.shipments",
    ]
    assert write_findings(
        "postgresql",
        queries[2],
        [{"rolsuper": True, "rolcreatedb": False, "rolcreaterole": False, "rolbypassrls": False}],
    ) == ["login has the rolsuper attribute"]


def test_mysql_grant_parsing() -> None:
    query = privilege_queries("mysql", ["purchase_orders"])[0]
    assert query.sql == "SHOW GRANTS FOR CURRENT_USER()"
    read_only = [
        {"Grants for carto_ro@%": "GRANT USAGE ON *.* TO `carto_ro`@`%`"},
        {
            "Grants for carto_ro@%": (
                "GRANT SELECT, SHOW VIEW ON `wms`.`purchase_orders` TO `carto_ro`@`%`"
            )
        },
        {"Grants for carto_ro@%": "GRANT SELECT (id, status) ON `wms`.`other` TO `carto_ro`@`%`"},
    ]
    assert write_findings("mysql", query, read_only) == []
    writer = [
        {"Grants for u@%": "GRANT SELECT, INSERT ON `wms`.`purchase_orders` TO `u`@`%`"},
        {"Grants for u@%": "GRANT ALL PRIVILEGES ON `other`.* TO `u`@`%` WITH GRANT OPTION"},
        {"Grants for u@%": "GRANT BACKUP_ADMIN ON *.* TO `u`@`%`"},
    ]
    findings = write_findings("mysql", query, writer)
    assert "login has INSERT on `wms`.`purchase_orders`" in findings
    assert "login has ALL PRIVILEGES on `other`.*" in findings
    assert "login has GRANT OPTION on `other`.*" in findings
    assert "login has BACKUP_ADMIN on *.*" in findings


def test_mssql_privilege_queries_and_findings() -> None:
    queries = privilege_queries("mssql", ["dbo.purchase_orders"])
    assert "HAS_PERMS_BY_NAME(:t, 'OBJECT', 'INSERT')" in queries[0].sql
    assert "IS_SRVROLEMEMBER('sysadmin')" in queries[1].sql
    assert (
        write_findings(
            "mssql",
            queries[0],
            [{"can_insert": 0, "can_update": 0, "can_delete": 0, "can_alter": 0}],
        )
        == []
    )
    assert write_findings(
        "mssql", queries[0], [{"can_insert": 1, "can_update": 0, "can_delete": 0, "can_alter": 1}]
    ) == [
        "login can INSERT on dbo.purchase_orders",
        "login can ALTER on dbo.purchase_orders",
    ]
    assert write_findings(
        "mssql",
        queries[1],
        [{"sysadmin": 1, "db_owner": 0, "db_datawriter": None, "db_ddladmin": 0}],
    ) == ["login has the sysadmin attribute"]
    assert isinstance(queries[0], PrivilegeQuery)
