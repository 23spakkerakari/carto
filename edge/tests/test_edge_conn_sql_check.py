"""carto_edge.connectors.sql_dialects: the sqlglot statement check (spec 8.1.4 layer 3), the
connection helpers and the privilege interpretation."""

from __future__ import annotations

import pytest

from carto_edge.connectors.base import InvalidConfigError, ResolvedHost
from carto_edge.connectors.sql_dialects import (
    ALLOWED_FUNCTION_TYPES,
    ALLOWED_FUNCTIONS,
    PrivilegeQuery,
    StatementCheckError,
    check_statement,
    mssql_connection_string,
    mysql_connect_kwargs,
    mysql_grants_using_sql,
    mysql_roles,
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


# ---------------------------------------------------------------------------------------------
# ADR 0037 item 1: allowlists, base tables and CTEs only
# ---------------------------------------------------------------------------------------------


def test_everyday_scalar_and_aggregate_functions_are_allowed() -> None:
    """A template may shape its columns with the usual functions without anyone listing them
    per install (ADR 0037 Consequences: a missing function is a one-line code change)."""
    postgres = (
        "SELECT id, COALESCE(status, 'unknown') AS status, LOWER(TRIM(created_by)) AS actor, "
        "DATE_TRUNC('day', updated_at) AS day, EXTRACT(EPOCH FROM updated_at) AS epoch, "
        "TO_CHAR(updated_at, 'YYYY-MM-DD') AS printed, payload ->> 'order_ref' AS order_ref, "
        "jsonb_extract_path_text(payload, 'po', 'num') AS po_num, "
        "CAST(amount AS TEXT) AS amount, ROUND(amount, 2) AS rounded, "
        "CASE WHEN qty > 0 THEN 'some' ELSE 'none' END AS qty_class, "
        "COUNT(*) OVER () AS total, ROW_NUMBER() OVER (ORDER BY updated_at) AS rn, "
        "updated_at AT TIME ZONE 'UTC' AS utc, age(updated_at) AS age, "
        "updated_at + INTERVAL '1 day' AS next_day, NOW() AS seen "
        "FROM purchase_orders WHERE updated_at > :watermark AND updated_at <= :upper "
        "AND EXISTS (SELECT 1 FROM warehouses w WHERE w.code = warehouse_code) "
        "ORDER BY updated_at LIMIT :batch"
    )
    assert check_statement(postgres, "postgresql", upper=True).tables == (
        "purchase_orders",
        "warehouses",
    )
    mysql = (
        "SELECT id, IFNULL(status, 'unknown') AS status, DATE_FORMAT(updated_at, '%Y') AS y, "
        "UNIX_TIMESTAMP(updated_at) AS epoch, CONVERT_TZ(updated_at, '+00:00', 'UTC') AS utc, "
        "JSON_UNQUOTE(JSON_EXTRACT(payload, '$.ref')) AS ref, CONCAT_WS('-', a, b) AS ab, "
        "TIMESTAMPDIFF(SECOND, created_at, updated_at) AS secs, GROUP_CONCAT(tag) AS tags "
        "FROM purchase_orders WHERE updated_at > :watermark AND updated_at <= :upper "
        "GROUP BY id ORDER BY updated_at LIMIT :batch"
    )
    assert check_statement(mysql, "mysql", upper=True).tables == ("purchase_orders",)
    mssql = (
        "SELECT TOP (:batch) id, ISNULL(status, 'unknown') AS status, "
        "CONVERT(VARCHAR(10), updated_at, 120) AS printed, DATEADD(day, 1, updated_at) AS nxt, "
        "DATEDIFF(second, created_at, updated_at) AS secs, FORMAT(updated_at, 'yyyy') AS y, "
        "IIF(qty > 0, 'some', 'none') AS qty_class, JSON_VALUE(payload, '$.ref') AS ref, "
        "SYSUTCDATETIME() AS seen, LEN(po_num) AS n, CHARINDEX('-', po_num) AS dash, "
        "TRY_CAST(amount AS INT) AS amount, STRING_AGG(tag, ',') AS tags "
        "FROM dbo.purchase_orders WHERE updated_at > :watermark AND updated_at <= :upper "
        "GROUP BY id ORDER BY updated_at"
    )
    assert check_statement(mssql, "mssql", upper=True).tables == ("dbo.purchase_orders",)


@pytest.mark.parametrize(
    ("sql", "dialect", "reason"),
    [
        # Functions off the allowlist, named in the refusal
        (
            "SELECT nextval('s'), id FROM t WHERE a > :watermark LIMIT :batch",
            "postgresql",
            "nextval",
        ),
        (
            (
                "SELECT id FROM t WHERE a > :watermark AND b = set_config('x', 'y', false) "
                "LIMIT :batch"
            ),
            "postgresql",
            "set_config",
        ),
        (
            "SELECT id FROM t WHERE a > :watermark AND b = pg_read_file('x') LIMIT :batch",
            "postgresql",
            "pg_read_file",
        ),
        (
            "SELECT id, (SELECT lo_import('/etc/passwd')) FROM t WHERE a > :watermark LIMIT :batch",
            "postgresql",
            "lo_import",
        ),
        (
            "SELECT id FROM t WHERE a > :watermark AND b = get_lock('x', 1) LIMIT :batch",
            "mysql",
            "get_lock",
        ),
        (
            "SELECT id FROM t WHERE a > :watermark AND b = sys_exec('x') LIMIT :batch",
            "mysql",
            "sys_exec",
        ),
        (
            "SELECT TOP (:batch) id FROM t WHERE a > :watermark AND b = sp_executesql('x')",
            "mssql",
            "sp_executesql",
        ),
        (
            "SELECT TOP (:batch) id FROM t WHERE a > :watermark AND b = dbo.fn_custom(a)",
            "mssql",
            "fn_custom",
        ),
        (
            "SELECT id FROM t WHERE a > :watermark AND b = my_udf(a) LIMIT :batch",
            "postgresql",
            "my_udf",
        ),
        (
            (
                "SELECT id FROM t WHERE a > :watermark AND b = query_to_xml('x', true, true, '') "
                "LIMIT :batch"
            ),
            "postgresql",
            "query_to_xml",
        ),
        # Table-valued functions and remote sources in FROM
        (
            "SELECT * FROM generate_series(1, 10) WHERE a > :watermark LIMIT :batch",
            "postgresql",
            "table-valued function",
        ),
        (
            "SELECT * FROM t, LATERAL unnest(t.arr) u WHERE a > :watermark LIMIT :batch",
            "postgresql",
            "unnest",
        ),
        (
            (
                "SELECT * FROM dblink('x', 'DELETE FROM t') AS r(a int) WHERE a > :watermark "
                "LIMIT :batch"
            ),
            "postgresql",
            "DBLINK",
        ),
        (
            (
                "SELECT a FROM t, JSON_TABLE(t.j, '$[*]' COLUMNS (x INT PATH '$.x')) jt "
                "WHERE a > :watermark LIMIT :batch"
            ),
            "mysql",
            "jsontable",
        ),
        (
            "SELECT TOP (:batch) * FROM OPENQUERY(lnk, 'DELETE FROM x') WHERE a > :watermark",
            "mssql",
            "OPENQUERY",
        ),
        (
            (
                "SELECT TOP (:batch) * FROM OPENROWSET('SQLNCLI', 'x', 'DELETE FROM x') "
                "WHERE a > :watermark"
            ),
            "mssql",
            "OPENROWSET",
        ),
        (
            (
                "SELECT TOP (:batch) * FROM OPENDATASOURCE('SQLNCLI', 'x').db.dbo.t "
                "WHERE a > :watermark"
            ),
            "mssql",
            "OPENDATASOURCE",
        ),
        ("SELECT TOP (:batch) * FROM lnk.db.dbo.t WHERE a > :watermark", "mssql", "linked server"),
        (
            "SELECT TOP (:batch) * FROM dbo.fn_split('a,b') WHERE a > :watermark",
            "mssql",
            "table-valued function fn_split",
        ),
        (
            "SELECT TOP (:batch) * FROM t CROSS APPLY STRING_SPLIT(t.x, ',') WHERE a > :watermark",
            "mssql",
            "string_split",
        ),
        (
            "SELECT TOP (:batch) * FROM t CROSS APPLY OPENJSON(t.j) WHERE a > :watermark",
            "mssql",
            "openjson",
        ),
        # Hints, sequences and tableless statements
        ("SELECT TOP (:batch) * FROM t WITH (NOLOCK) WHERE a > :watermark", "mssql", "table hints"),
        (
            "SELECT TOP (:batch) * FROM t WITH (TABLOCKX, HOLDLOCK) WHERE a > :watermark",
            "mssql",
            "not allowed on t",
        ),
        ("SELECT * FROM t USE INDEX (i) WHERE a > :watermark LIMIT :batch", "mysql", "table hints"),
        (
            "SELECT /*+ MAX_EXECUTION_TIME(0) */ a FROM t WHERE a > :watermark LIMIT :batch",
            "mysql",
            "optimizer hints",
        ),
        (
            "SELECT TOP (:batch) NEXT VALUE FOR dbo.seq, a FROM t WHERE a > :watermark",
            "mssql",
            "NEXT VALUE FOR",
        ),
        (
            "SELECT TOP (:batch) a FROM t WHERE a > :watermark AND b = NEXT VALUE FOR dbo.seq",
            "mssql",
            "NEXT VALUE FOR",
        ),
        ("SELECT 1 WHERE 1 > :watermark LIMIT :batch", "postgresql", "at least one table"),
        (
            "WITH x AS (SELECT 1 AS a) SELECT a FROM x WHERE a > :watermark LIMIT :batch",
            "postgresql",
            "at least one table",
        ),
    ],
)
def test_adr_0037_refusals(sql: str, dialect: str, reason: str) -> None:
    with pytest.raises(StatementCheckError, match=reason):
        check_statement(sql, dialect)  # type: ignore[arg-type]


def test_reads_that_are_not_writes_stay_allowed() -> None:
    """Derived tables, joins, set operations, temporal and sampled reads only read."""
    for sql, dialect in [
        ("SELECT * FROM (SELECT id FROM t WHERE a > :watermark) s LIMIT :batch", "postgresql"),
        (
            "SELECT t.a FROM t LEFT JOIN u ON u.id = t.id WHERE a > :watermark LIMIT :batch",
            "postgresql",
        ),
        ("SELECT a FROM ONLY t WHERE a > :watermark LIMIT :batch", "postgresql"),
        ("SELECT a FROM t TABLESAMPLE SYSTEM (1) WHERE a > :watermark LIMIT :batch", "postgresql"),
        ("SELECT a FROM t PARTITION (p0) WHERE a > :watermark LIMIT :batch", "mysql"),
        ("SELECT TOP (:batch) a FROM t FOR SYSTEM_TIME ALL WHERE a > :watermark", "mssql"),
        ("SELECT TOP (:batch) a FROM db.dbo.t WHERE a > :watermark", "mssql"),
    ]:
        assert check_statement(sql, dialect).tables  # type: ignore[arg-type]


def test_the_allowlists_hold_no_table_valued_or_writing_function() -> None:
    """The lists are code, so a reviewer sees every addition; this pins what must never be
    on them."""
    names = {cls.__name__.lower() for cls in ALLOWED_FUNCTION_TYPES}
    assert names.isdisjoint({"unnest", "openjson", "jsontable", "explode", "generateseries"})
    for dialect, allowed in ALLOWED_FUNCTIONS.items():
        assert allowed == {name.lower() for name in allowed}, dialect
        assert allowed.isdisjoint(
            {
                "nextval",
                "setval",
                "pg_sleep",
                "sleep",
                "load_file",
                "xp_cmdshell",
                "sp_executesql",
                "openquery",
                "openrowset",
                "opendatasource",
                "dblink",
                "get_lock",
                "sys_exec",
                "set_config",
                "pg_read_file",
                "lo_import",
            }
        ), dialect


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
    roles_query, query = privilege_queries("mysql", ["purchase_orders"])
    assert roles_query.sql == "SELECT CURRENT_ROLE() AS roles"
    assert write_findings("mysql", roles_query, [{"roles": "`r`@`%`"}]) == []
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


def test_mysql_roles_are_collected_and_expanded() -> None:
    """ADR 0037 item 3: CURRENT_ROLE() and the role-grant lines name the roles; the connector
    then asks SHOW GRANTS ... USING them. Role lines themselves are not write findings."""
    lines = [
        "GRANT USAGE ON *.* TO `carto_ro`@`%`",
        "GRANT SELECT ON `wms`.`purchase_orders` TO `carto_ro`@`%`",
        "GRANT `wms_writer`@`%`,`auditor`@`localhost` TO `carto_ro`@`%`",
    ]
    assert mysql_roles("NONE", lines) == ["`wms_writer`@`%`", "`auditor`@`localhost`"]
    assert mysql_roles("`active`@`%`", lines) == [
        "`active`@`%`",
        "`wms_writer`@`%`",
        "`auditor`@`localhost`",
    ]
    assert mysql_roles(None, []) == []
    assert mysql_roles("`wms_writer`@`%`", lines)[0] == "`wms_writer`@`%`"  # deduplicated
    assert (
        mysql_grants_using_sql(["`wms_writer`@`%`", "`auditor`@`localhost`"])
        == "SHOW GRANTS FOR CURRENT_USER() USING `wms_writer`@`%`, `auditor`@`localhost`"
    )
    _roles, grants = privilege_queries("mysql", ["purchase_orders"])
    rows = [{"Grants for carto_ro@%": line} for line in lines]
    assert write_findings("mysql", grants, rows) == []
    # A name with a backtick cannot be rebuilt into an account, so it never reaches the query.
    assert mysql_roles("`bad``name`@`%`", []) == ["`name`@`%`"]


def test_an_unreadable_mysql_grant_line_is_a_write_finding() -> None:
    """ADR 0037 item 3: the parser does not know the line is harmless, so it is not."""
    _roles, grants = privilege_queries("mysql", ["purchase_orders"])
    rows = [
        {"Grants for u@%": "GRANT SELECT ON `wms`.`purchase_orders` TO `u`@`%`"},
        {"Grants for u@%": "SOMETHING NEW THE SERVER SAYS"},
        {"Grants for u@%": "REVOKE INSERT ON `wms`.* FROM `u`@`%`"},
    ]
    findings = write_findings("mysql", grants, rows)
    assert findings == ["grant line could not be read: 'SOMETHING NEW THE SERVER SAYS'"]
    long_line = "X" * 500
    assert write_findings("mysql", grants, [{"g": long_line}]) == [
        f"grant line could not be read: {'X' * 80!r}"
    ]


def test_mssql_privilege_queries_and_findings() -> None:
    queries = privilege_queries("mssql", ["dbo.purchase_orders"])
    assert [query.name for query in queries] == ["table", "database", "role", "linked servers"]
    assert "HAS_PERMS_BY_NAME(:t, 'OBJECT', 'INSERT')" in queries[0].sql
    assert "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT')" in queries[1].sql
    assert queries[1].params == {}
    assert "IS_SRVROLEMEMBER('sysadmin')" in queries[2].sql
    assert queries[3].sql == "SELECT name FROM sys.servers WHERE server_id <> 0"
    # ADR 0037 item 2: database-scope writes and visible linked servers are findings.
    assert write_findings(
        "mssql", queries[1], [{"can_insert": 0, "can_update": 1, "can_delete": 0, "can_alter": 0}]
    ) == ["login can UPDATE at database scope"]
    assert write_findings("mssql", queries[3], []) == []
    assert write_findings("mssql", queries[3], [{"name": "WMS-ARCHIVE"}, {"name": "ERP"}]) == [
        "login sees linked server WMS-ARCHIVE",
        "login sees linked server ERP",
    ]
    queries = privilege_queries("mssql", ["dbo.purchase_orders"])
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
        queries[2],
        [{"sysadmin": 1, "db_owner": 0, "db_datawriter": None, "db_ddladmin": 0}],
    ) == ["login has the sysadmin attribute"]
    assert isinstance(queries[0], PrivilegeQuery)
