"""Per-engine knowledge for the SQL connector (spec 8.1.4): the statement check, how to open a
read-only session, and how to read the login's effective privileges.

Layer 3 of the read-only enforcement is :func:`check_statement`: ``sqlglot`` parses the admin's
query template and anything other than one ``SELECT`` (or ``WITH ... SELECT``) is refused: no
``INTO``, no locking clause, no DML or DDL anywhere in the tree, none of the functions that
write or stall (``nextval``, ``pg_sleep``, ``xp_cmdshell`` ...), and only the named bind
parameters ``:watermark`` and ``:batch``, plus ``:upper`` for a time-typed watermark (ADR 0035,
where it is required; it is refused for integer and decimal watermarks); string formatting
markers are refused outright.

Layer 2 is the session: PostgreSQL connects with ``default_transaction_read_only=on``, a
``statement_timeout`` and ``TimeZone=UTC`` and polls inside ``BEGIN READ ONLY`` (set through
SQLAlchemy's ``postgresql_readonly`` option, which psycopg turns into the read-only transaction);
MySQL sets ``max_execution_time`` and polls inside ``START TRANSACTION READ ONLY``; SQL Server
has no read-only transaction mode, so the connection string carries ``ApplicationIntent=ReadOnly``
and ``Encrypt=yes`` and the grant check carries the weight.

Layer 1, the grants, is what ``test()`` inspects with the queries from
:func:`privilege_queries`, interpreted by :func:`write_findings`: ``has_table_privilege`` and
``pg_roles`` on PostgreSQL, ``SHOW GRANTS FOR CURRENT_USER()`` on MySQL (every privilege that
is not in the read-only set counts as write-capable), ``HAS_PERMS_BY_NAME`` plus the server
and database roles on SQL Server.

SSRF (spec 14.7, ADR 0018): PostgreSQL pins the policy's address with ``hostaddr`` while
``host`` keeps the name so ``sslmode=verify-full`` still verifies it; MySQL and SQL Server
validate the host through the policy at each connect and connect by name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from carto_edge.connectors.base import InvalidConfigError, ResolvedHost
from carto_edge.secrets import Credentials

__all__ = [
    "CLOCK_SQL",
    "DENIED_FUNCTIONS",
    "MYSQL_READ_ONLY_PRIVILEGES",
    "REQUIRED_PARAMETERS",
    "SQLGLOT_READ",
    "CheckedStatement",
    "Dialect",
    "PrivilegeQuery",
    "StatementCheckError",
    "check_statement",
    "mssql_connection_string",
    "mysql_connect_kwargs",
    "mysql_session_statements",
    "postgres_connect_kwargs",
    "privilege_queries",
    "write_findings",
]

Dialect = Literal["postgresql", "mysql", "mssql"]

SQLGLOT_READ: Final[dict[str, str]] = {"postgresql": "postgres", "mysql": "mysql", "mssql": "tsql"}

REQUIRED_PARAMETERS: Final = frozenset({"watermark", "batch"})
"""Spec 8.1.4: only named bind parameters ``:watermark`` and ``:batch``; time-typed watermarks
add ``:upper`` (ADR 0035)."""
UPPER_PARAMETER: Final = "upper"

CLOCK_SQL: Final[dict[str, str]] = {
    "postgresql": "SELECT CURRENT_TIMESTAMP AS now",
    "mysql": "SELECT UTC_TIMESTAMP(6) AS now",
    "mssql": "SELECT SYSUTCDATETIME() AS now",
}
"""The database's own clock in UTC, the base of ``:upper`` (ADR 0035 item 3)."""

DENIED_FUNCTIONS: Final = frozenset(
    {
        "nextval",
        "setval",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_notify",
        "lo_import",
        "lo_export",
        "lo_unlink",
        "dblink",
        "dblink_exec",
        "xp_cmdshell",
        "sp_executesql",
        "xp_regwrite",
        "xp_fileexist",
        "openrowset",
        "opendatasource",
        "load_file",
        "sleep",
        "benchmark",
        "get_lock",
        "release_lock",
        "sys_exec",
        "sys_eval",
    }
)

MAX_STATEMENT_LEN: Final = 16_384

_FORMAT_MARKERS: Final = re.compile(r"%\(|%[sdfr]|\{[^{}]*\}")

_FORBIDDEN_NODES: Final[tuple[type[exp.Expression], ...]] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Merge,
    exp.Grant,
    exp.Revoke,
    exp.Command,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Copy,
    exp.Use,
    exp.Pragma,
    exp.Execute,
    exp.Kill,
    exp.Lock,
    exp.Into,
    exp.Describe,
)

MYSQL_READ_ONLY_PRIVILEGES: Final = frozenset(
    {"SELECT", "USAGE", "SHOW VIEW", "SHOW DATABASES", "PROCESS", "REPLICATION CLIENT"}
)
"""Every other MySQL privilege (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, ALL, EXECUTE,
FILE, SUPER, the dynamic admin privileges ...) makes the login write-capable."""

_PG_TABLE_PRIVILEGES: Final = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")
_MSSQL_TABLE_PRIVILEGES: Final = ("INSERT", "UPDATE", "DELETE", "ALTER")
_MYSQL_GRANT_RE: Final = re.compile(
    r"^GRANT\s+(?P<privileges>.+?)\s+ON\s+(?P<object>.+?)\s+TO\s", re.IGNORECASE
)


class StatementCheckError(InvalidConfigError):
    """The query template is not a single read-only SELECT with the allowed parameters."""


@dataclass(frozen=True, slots=True)
class CheckedStatement:
    sql: str
    dialect: Dialect
    tables: tuple[str, ...]
    """Referenced base tables (``schema.table`` when qualified), CTE names excluded."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def check_statement(sql: str, dialect: Dialect, *, upper: bool = False) -> CheckedStatement:
    """Spec 8.1.4 layer 3: refuse everything but one parameterized read-only SELECT. With
    ``upper`` the template must also bind ``:upper`` (a time-typed watermark, ADR 0035);
    without it ``:upper`` is refused."""
    required = REQUIRED_PARAMETERS | {UPPER_PARAMETER} if upper else REQUIRED_PARAMETERS
    text = sql.strip().rstrip(";").strip()
    if not text:
        msg = "the query is empty"
        raise StatementCheckError(msg)
    if len(text) > MAX_STATEMENT_LEN:
        msg = f"the query exceeds {MAX_STATEMENT_LEN} characters"
        raise StatementCheckError(msg)
    if _FORMAT_MARKERS.search(text):
        msg = "the query contains string formatting markers; use :watermark and :batch only"
        raise StatementCheckError(msg)
    try:
        parsed = sqlglot.parse(text, read=SQLGLOT_READ[dialect])
    except ParseError as exc:
        msg = f"the query does not parse as {dialect}"
        raise StatementCheckError(msg) from exc
    expressions = [expression for expression in parsed if expression is not None]
    if len(expressions) != 1:
        msg = f"the query must be exactly one statement, found {len(expressions)}"
        raise StatementCheckError(msg)
    tree = expressions[0]
    if not isinstance(tree, exp.Select):
        msg = f"the query must be a single SELECT (or WITH ... SELECT), not {type(tree).__name__}"
        raise StatementCheckError(msg)
    if tree.args.get("into") is not None:
        msg = "SELECT ... INTO is not allowed"
        raise StatementCheckError(msg)
    if tree.args.get("locks"):
        msg = "locking clauses (FOR UPDATE / FOR SHARE) are not allowed"
        raise StatementCheckError(msg)
    seen_parameters: set[str] = set()
    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            msg = f"{type(node).__name__} is not allowed inside the query"
            raise StatementCheckError(msg)
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in DENIED_FUNCTIONS:
                msg = f"function {name}() is not allowed"
                raise StatementCheckError(msg)
        if isinstance(node, exp.Placeholder | exp.Parameter | exp.SessionParameter):
            name = str(node.name)
            if name not in required:
                allowed = ", ".join(f":{item}" for item in sorted(required))
                msg = f"only the named bind parameters {allowed} are allowed"
                if name == UPPER_PARAMETER:
                    msg += " (:upper belongs to datetime and date watermarks only)"
                raise StatementCheckError(msg)
            seen_parameters.add(name)
    if seen_parameters != required:
        needed = " and ".join(f":{item}" for item in sorted(required))
        msg = f"the query must use {needed}"
        if upper and UPPER_PARAMETER not in seen_parameters:
            msg += (
                " (a time-typed watermark reads a closed range: "
                "WHERE <column> > :watermark AND <column> <= :upper, ADR 0035)"
            )
        raise StatementCheckError(msg)
    ctes = {str(cte.alias_or_name).lower() for cte in tree.find_all(exp.CTE)}
    tables: list[str] = []
    for table in tree.find_all(exp.Table):
        name = str(table.name)
        if not name:
            continue
        schema = str(table.db)
        if not schema and name.lower() in ctes:
            continue
        full = f"{schema}.{name}" if schema else name
        if full not in tables:
            tables.append(full)
    return CheckedStatement(text, dialect, tuple(sorted(tables)))


# ---------------------------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------------------------


def postgres_connect_kwargs(
    *,
    host: str,
    resolved: ResolvedHost,
    port: int,
    database: str,
    credentials: Credentials,
    sslmode: str,
    ca_file: str | None,
    connect_timeout_seconds: int,
    query_timeout_seconds: int,
) -> dict[str, Any]:
    """psycopg keyword arguments: ``hostaddr`` pins the policy's address, ``host`` keeps the
    name for ``verify-full``, ``options`` makes every transaction read-only with a timeout and
    pins the session time zone to UTC, so a naive watermark compares with ``timestamptz``
    columns in UTC whatever the server's default zone is (ADR 0035 item 6; MySQL sessions set
    ``time_zone = '+00:00'``)."""
    kwargs: dict[str, Any] = {
        "host": host,
        "hostaddr": resolved.address,
        "port": port,
        "dbname": database,
        "user": credentials.username,
        "password": credentials.password,
        "sslmode": sslmode,
        "connect_timeout": connect_timeout_seconds,
        "application_name": "carto-edge",
        "options": (
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={query_timeout_seconds * 1000} "
            "-c TimeZone=UTC"
        ),
    }
    if ca_file:
        kwargs["sslrootcert"] = ca_file
    return kwargs


def mysql_connect_kwargs(
    *,
    host: str,
    port: int,
    database: str,
    credentials: Credentials,
    ssl: bool,
    ca_file: str | None,
    connect_timeout_seconds: int,
    query_timeout_seconds: int,
) -> dict[str, Any]:
    """PyMySQL keyword arguments; TLS with certificate and identity verification when ``ssl``."""
    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "database": database,
        "user": credentials.username,
        "password": credentials.password,
        "connect_timeout": connect_timeout_seconds,
        "read_timeout": query_timeout_seconds,
        "write_timeout": connect_timeout_seconds,
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if ssl:
        kwargs["ssl_verify_cert"] = True
        kwargs["ssl_verify_identity"] = True
        if ca_file:
            kwargs["ssl_ca"] = ca_file
        else:
            kwargs["ssl"] = {}
    else:
        kwargs["ssl_disabled"] = True
    return kwargs


def mysql_session_statements(query_timeout_seconds: int) -> tuple[str, ...]:
    """Run after connecting: the per-statement timeout and read-only transactions."""
    return (
        f"SET SESSION max_execution_time = {query_timeout_seconds * 1000}",
        "SET SESSION TRANSACTION READ ONLY",
    )


def _odbc_value(value: str) -> str:
    """Brace-quote an ODBC connection string value (``}`` is doubled inside braces)."""
    return "{" + value.replace("}", "}}") + "}"


def mssql_connection_string(
    *,
    host: str,
    port: int,
    database: str,
    credentials: Credentials,
    driver: str,
    encrypt: bool,
    trust_server_certificate: bool,
    connect_timeout_seconds: int,
) -> str:
    """pyodbc connection string with ``ApplicationIntent=ReadOnly`` and ``Encrypt=yes``."""
    parts = [
        f"DRIVER={_odbc_value(driver)}",
        f"SERVER={_odbc_value(f'{host},{port}')}",
        f"DATABASE={_odbc_value(database)}",
        f"UID={_odbc_value(credentials.username)}",
        f"PWD={_odbc_value(credentials.password)}",
        f"Encrypt={'yes' if encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if trust_server_certificate else 'no'}",
        "ApplicationIntent=ReadOnly",
        f"Connect Timeout={connect_timeout_seconds}",
        "APP=carto-edge",
    ]
    return ";".join(parts) + ";"


# ---------------------------------------------------------------------------------------------
# Privileges (spec 8.1.4 test())
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PrivilegeQuery:
    name: str
    sql: str
    params: Mapping[str, Any]
    table: str | None = None


def privilege_queries(dialect: Dialect, tables: Sequence[str]) -> list[PrivilegeQuery]:
    """The read-only queries ``test()`` runs to learn what the login can do."""
    queries: list[PrivilegeQuery] = []
    if dialect == "postgresql":
        for table in tables:
            columns = ", ".join(
                f"has_table_privilege(current_user, :t, '{privilege}') AS can_{privilege.lower()}"
                for privilege in _PG_TABLE_PRIVILEGES
            )
            queries.append(PrivilegeQuery("table", f"SELECT {columns}", {"t": table}, table))
        queries.append(
            PrivilegeQuery(
                "role",
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user",
                {},
            )
        )
    elif dialect == "mysql":
        queries.append(PrivilegeQuery("grants", "SHOW GRANTS FOR CURRENT_USER()", {}))
    else:
        for table in tables:
            columns = ", ".join(
                f"HAS_PERMS_BY_NAME(:t, 'OBJECT', '{privilege}') AS can_{privilege.lower()}"
                for privilege in _MSSQL_TABLE_PRIVILEGES
            )
            queries.append(PrivilegeQuery("table", f"SELECT {columns}", {"t": table}, table))
        queries.append(
            PrivilegeQuery(
                "role",
                "SELECT IS_SRVROLEMEMBER('sysadmin') AS sysadmin, "
                "IS_MEMBER('db_owner') AS db_owner, IS_MEMBER('db_datawriter') AS db_datawriter, "
                "IS_MEMBER('db_ddladmin') AS db_ddladmin",
                {},
            )
        )
    return queries


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


def write_findings(
    dialect: Dialect, query: PrivilegeQuery, rows: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Explanations of every write capability the rows reveal; empty means read-only so far."""
    findings: list[str] = []
    if dialect == "mysql":
        for row in rows:
            for line in row.values():
                findings.extend(_mysql_grant_findings(str(line)))
        return findings
    for row in rows:
        if query.name == "table":
            for key, value in row.items():
                if str(key).startswith("can_") and _truthy(value):
                    privilege = str(key)[4:].upper()
                    findings.append(f"login can {privilege} on {query.table}")
        else:
            for key, value in row.items():
                if _truthy(value):
                    findings.append(f"login has the {key} attribute")
    return findings


def _mysql_grant_findings(line: str) -> list[str]:
    match = _MYSQL_GRANT_RE.match(line.strip())
    if match is None:
        if line.strip().upper().startswith("GRANT PROXY"):
            return ["login can PROXY as another user"]
        return []
    target = match.group("object").strip()
    findings: list[str] = []
    # Column-level grants read ``SELECT (id, status)``: drop the lists before splitting.
    privileges = re.sub(r"\([^)]*\)", "", match.group("privileges"))
    for raw in privileges.split(","):
        privilege = raw.strip().upper()
        if privilege and privilege not in MYSQL_READ_ONLY_PRIVILEGES:
            findings.append(f"login has {privilege} on {target}")
    if "WITH GRANT OPTION" in line.upper():
        findings.append(f"login has GRANT OPTION on {target}")
    return findings
