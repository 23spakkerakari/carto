"""Per-engine knowledge for the SQL connector (spec 8.1.4): the statement check, how to open a
read-only session, and how to read the login's effective privileges.

Layer 3 of the read-only enforcement is :func:`check_statement`: ``sqlglot`` parses the admin's
query template and anything other than one ``SELECT`` (or ``WITH ... SELECT``) is refused: no
``INTO``, no locking clause, no DML or DDL anywhere in the tree, and only the named bind
parameters ``:watermark`` and ``:batch``, plus ``:upper`` for a time-typed watermark (ADR 0035,
where it is required; it is refused for integer and decimal watermarks); string formatting
markers are refused outright. ADR 0037 item 1 makes the rest an allowlist: the statement reads
base tables and CTEs only (at least one table; no table-valued functions, ``OPENQUERY``,
``OPENROWSET``, ``OPENDATASOURCE`` or linked-server names), carries no table or optimizer
hints and no ``NEXT VALUE FOR``, and calls only aggregates plus the scalar functions in
:data:`ALLOWED_FUNCTION_TYPES` and the per-dialect names in :data:`ALLOWED_FUNCTIONS`. A
function that is missing is added in code, never through a setting.

Layer 2 is the session: PostgreSQL connects with ``default_transaction_read_only=on``, a
``statement_timeout`` and ``TimeZone=UTC`` and polls inside ``BEGIN READ ONLY`` (set through
SQLAlchemy's ``postgresql_readonly`` option, which psycopg turns into the read-only transaction);
MySQL sets ``max_execution_time`` and polls inside ``START TRANSACTION READ ONLY``; SQL Server
has no read-only transaction mode, so the connection string carries ``ApplicationIntent=ReadOnly``
and ``Encrypt=yes`` and the grant check carries the weight.

Layer 1, the grants, is what ``test()`` inspects with the queries from
:func:`privilege_queries`, interpreted by :func:`write_findings`: ``has_table_privilege`` and
``pg_roles`` on PostgreSQL; ``SHOW GRANTS FOR CURRENT_USER()`` on MySQL, repeated ``USING``
every role the login holds (:func:`mysql_roles`, ADR 0037 item 3; a grant line the parser
cannot read counts as a write finding); ``HAS_PERMS_BY_NAME`` per table and at database scope,
the server and database roles, and ``sys.servers`` on SQL Server (a login that sees a linked
server is write-capable, ADR 0037 item 2).

SSRF (spec 14.7, ADR 0018): PostgreSQL pins the policy's address with ``hostaddr`` while
``host`` keeps the name so ``sslmode=verify-full`` still verifies it; MySQL and SQL Server
validate the host through the policy at each connect and connect by name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from carto_edge.connectors.base import InvalidConfigError, ResolvedHost
from carto_edge.secrets import Credentials

__all__ = [
    "ALLOWED_FUNCTIONS",
    "ALLOWED_FUNCTION_TYPES",
    "CLOCK_SQL",
    "MYSQL_READ_ONLY_PRIVILEGES",
    "REMOTE_SOURCES",
    "REQUIRED_PARAMETERS",
    "SQLGLOT_READ",
    "CheckedStatement",
    "Dialect",
    "PrivilegeQuery",
    "StatementCheckError",
    "check_statement",
    "mssql_connection_string",
    "mysql_connect_kwargs",
    "mysql_grants_using_sql",
    "mysql_roles",
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

ALLOWED_FUNCTION_TYPES: Final[frozenset[type[exp.Func]]] = frozenset(
    {
        # Logic, conditionals and conversions (sqlglot models these as functions too)
        exp.And,
        exp.Or,
        exp.Xor,
        exp.Case,
        exp.If,
        exp.Cast,
        exp.TryCast,
        exp.Convert,
        exp.Exists,
        exp.Array,
        exp.Coalesce,
        exp.Nullif,
        exp.Greatest,
        exp.Least,
        exp.Contains,
        # Strings
        exp.Lower,
        exp.Upper,
        exp.Trim,
        exp.Substring,
        exp.Length,
        exp.Concat,
        exp.ConcatWs,
        exp.Replace,
        exp.Left,
        exp.Right,
        exp.StrPosition,
        exp.SplitPart,
        exp.Split,
        exp.RegexpReplace,
        exp.RegexpLike,
        exp.RegexpExtract,
        exp.Initcap,
        exp.Pad,
        exp.Reverse,
        exp.Repeat,
        exp.Stuff,
        exp.Chr,
        exp.Ascii,
        exp.Unicode,
        exp.Hex,
        exp.Unhex,
        exp.MD5,
        exp.SHA,
        exp.SHA2,
        exp.StartsWith,
        exp.EndsWith,
        exp.Translate,
        exp.NumberToStr,
        exp.ToNumber,
        exp.ToChar,
        exp.ToBase64,
        exp.FromBase64,
        # Numbers
        exp.Round,
        exp.Abs,
        exp.Floor,
        exp.Ceil,
        exp.Trunc,
        exp.Pow,
        exp.Sqrt,
        exp.Sign,
        exp.Exp,
        exp.Ln,
        exp.Log,
        exp.SafeDivide,
        # Dates and times
        exp.CurrentTimestamp,
        exp.CurrentDate,
        exp.CurrentTime,
        exp.CurrentDatetime,
        exp.UtcTimestamp,
        exp.TimestampTrunc,
        exp.DateTrunc,
        exp.Extract,
        exp.TimeToStr,
        exp.StrToTime,
        exp.StrToDate,
        exp.TsOrDsToDate,
        exp.TsOrDsToTimestamp,
        exp.TsOrDsToDatetime,
        exp.DateAdd,
        exp.DateSub,
        exp.DateDiff,
        exp.TimestampDiff,
        exp.TimestampAdd,
        exp.TimestampSub,
        exp.TimeAdd,
        exp.TimeSub,
        exp.UnixToTime,
        exp.UnixToStr,
        exp.TimeToUnix,
        exp.StrToUnix,
        exp.ConvertTimezone,
        exp.Year,
        exp.Month,
        exp.Day,
        exp.Hour,
        exp.Minute,
        exp.Second,
        exp.Week,
        exp.Quarter,
        exp.DayOfWeek,
        exp.DayOfMonth,
        exp.DayOfYear,
        exp.WeekOfYear,
        exp.LastDay,
        exp.DateFromParts,
        exp.TimeFromParts,
        exp.TimestampFromParts,
        exp.Timestamp,
        exp.Date,
        exp.Datetime,
        exp.Time,
        exp.TimeStrToTime,
        exp.TimeStrToDate,
        exp.DateStrToDate,
        # JSON reads
        exp.JSONExtract,
        exp.JSONExtractScalar,
        exp.JSONBExtract,
        exp.JSONBExtractScalar,
        # Session facts and identifiers
        exp.CurrentUser,
        exp.CurrentSchema,
        exp.CurrentVersion,
        exp.Uuid,
        # Window numbering (the ranking functions are aggregates in sqlglot)
        exp.RowNumber,
    }
)
"""ADR 0037 item 1: the scalar functions a template may call, as the sqlglot node types the
three dialects parse them to. Every aggregate (``exp.AggFunc``) is allowed as well. Table-valued
functions (``UNNEST``, ``OPENJSON``, ``JSON_TABLE``, ``generate_series`` ...) are not here, so a
template cannot read from one."""

ALLOWED_FUNCTIONS: Final[dict[Dialect, frozenset[str]]] = {
    "postgresql": frozenset(
        {
            "age",
            "array_length",
            "array_to_string",
            "cardinality",
            "char_length",
            "character_length",
            "clock_timestamp",
            "date_bin",
            "every",
            "format",
            "gen_random_uuid",
            "isfinite",
            "json_agg",
            "json_array_length",
            "json_extract_path",
            "json_extract_path_text",
            "json_typeof",
            "jsonb_agg",
            "jsonb_array_length",
            "jsonb_extract_path",
            "jsonb_extract_path_text",
            "jsonb_typeof",
            "justify_days",
            "justify_hours",
            "justify_interval",
            "ltrim",
            "make_date",
            "make_timestamp",
            "make_timestamptz",
            "num_nonnulls",
            "num_nulls",
            "octet_length",
            "overlay",
            "quote_ident",
            "quote_literal",
            "quote_nullable",
            "rtrim",
            "statement_timestamp",
            "string_to_array",
            "substr",
            "timezone",
            "to_json",
            "to_jsonb",
            "transaction_timestamp",
            "width_bucket",
        }
    ),
    "mysql": frozenset(
        {
            "adddate",
            "addtime",
            "bin",
            "bin_to_uuid",
            "bit_length",
            "char_length",
            "character_length",
            "conv",
            "crc32",
            "curtime",
            "dayname",
            "dayofmonth",
            "dayofweek",
            "dayofyear",
            "elt",
            "export_set",
            "field",
            "find_in_set",
            "from_days",
            "inet6_aton",
            "inet6_ntoa",
            "inet_aton",
            "inet_ntoa",
            "is_ipv4",
            "is_ipv6",
            "is_uuid",
            "json_contains",
            "json_contains_path",
            "json_depth",
            "json_keys",
            "json_length",
            "json_pretty",
            "json_quote",
            "json_search",
            "json_type",
            "json_unquote",
            "json_valid",
            "localtime",
            "localtimestamp",
            "ltrim",
            "makedate",
            "maketime",
            "microsecond",
            "mid",
            "monthname",
            "now",
            "oct",
            "octet_length",
            "ord",
            "period_add",
            "period_diff",
            "quote",
            "regexp_instr",
            "regexp_substr",
            "rtrim",
            "sec_to_time",
            "sha",
            "sha1",
            "soundex",
            "space",
            "strcmp",
            "subdate",
            "substring_index",
            "subtime",
            "sysdate",
            "time_format",
            "time_to_sec",
            "timediff",
            "to_days",
            "to_seconds",
            "unix_timestamp",
            "utc_date",
            "utc_time",
            "uuid",
            "uuid_to_bin",
            "weekday",
            "weekofyear",
            "yearweek",
        }
    ),
    "mssql": frozenset(
        {
            "acos",
            "asin",
            "atan",
            "atn2",
            "binary_checksum",
            "charindex",
            "checksum",
            "choose",
            "cos",
            "cot",
            "datalength",
            "datediff_big",
            "datename",
            "datetime2fromparts",
            "datetimefromparts",
            "datetimeoffsetfromparts",
            "datetrunc",
            "db_id",
            "db_name",
            "degrees",
            "difference",
            "eomonth",
            "getutcdate",
            "hashbytes",
            "isdate",
            "isjson",
            "isnumeric",
            "log10",
            "nchar",
            "parse",
            "parsename",
            "patindex",
            "pi",
            "quotename",
            "radians",
            "schema_name",
            "sin",
            "smalldatetimefromparts",
            "soundex",
            "space",
            "square",
            "str",
            "string_escape",
            "suser_sname",
            "switchoffset",
            "sysdatetime",
            "sysdatetimeoffset",
            "sysutcdatetime",
            "tan",
            "timefromparts",
            "todatetimeoffset",
            "try_convert",
            "try_parse",
            "user_name",
        }
    ),
}
"""ADR 0037 item 1: per dialect, the functions sqlglot leaves untyped (``exp.Anonymous``) that a
template may still call, lower-cased. Everything else untyped is refused by name, so ``nextval``,
``pg_sleep``, ``sleep``, ``load_file``, ``xp_cmdshell``, user-defined functions and whatever a
vendor adds next are refused without anyone listing them."""

REMOTE_SOURCES: Final = frozenset({"openquery", "openrowset", "opendatasource", "dblink"})
"""Named in the refusal: these reach another server, where the embedded text may write."""

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
_MSSQL_DATABASE_PRIVILEGES: Final = ("INSERT", "UPDATE", "DELETE", "ALTER")
"""ADR 0037 item 2: held at database scope they apply to every table, present or future."""
_MSSQL_LINKED_SERVERS_SQL: Final = "SELECT name FROM sys.servers WHERE server_id <> 0"
_MYSQL_GRANT_RE: Final = re.compile(
    r"^GRANT\s+(?P<privileges>.+?)\s+ON\s+(?P<object>.+?)\s+TO\s", re.IGNORECASE
)
_MYSQL_ACCOUNT_RE: Final = re.compile(r"`(?P<name>[^`]+)`@`(?P<host>[^`]+)`")
_MYSQL_ROLE_GRANT_RE: Final = re.compile(
    r"^GRANT\s+(?P<roles>`[^`]+`@`[^`]+`(?:\s*,\s*`[^`]+`@`[^`]+`)*)\s+TO\s", re.IGNORECASE
)
"""``GRANT `role`@`host`[, ...] TO `user`@`host``: a role grant has no ``ON`` clause."""
MAX_GRANT_LINE_CHARS: Final = 80


class StatementCheckError(InvalidConfigError):
    """The query template is not a single read-only SELECT with the allowed parameters."""


@dataclass(frozen=True, slots=True)
class CheckedStatement:
    sql: str
    dialect: Dialect
    tables: tuple[str, ...]
    """Referenced base tables (``schema.table`` when qualified), CTE names excluded."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous | exp.AnonymousAggFunc):
        return str(node.name).lower()
    return str(node.key)


def _function_allowed(node: exp.Func, dialect: Dialect) -> bool:
    """ADR 0037 item 1: aggregates, the typed scalar functions and the per-dialect names."""
    if isinstance(node, exp.Anonymous | exp.AnonymousAggFunc):
        return str(node.name).lower() in ALLOWED_FUNCTIONS[dialect]
    if isinstance(node, exp.AggFunc):
        return True
    return type(node) in ALLOWED_FUNCTION_TYPES


def _check_function(node: exp.Func, dialect: Dialect) -> None:
    if _function_allowed(node, dialect):
        return
    name = _function_name(node)
    if name in REMOTE_SOURCES:
        msg = f"{name.upper()} is not allowed; read base tables and CTEs only"
        raise StatementCheckError(msg)
    msg = f"function {name}() is not on the {dialect} read-only allowlist (ADR 0037)"
    raise StatementCheckError(msg)


def _check_table_source(table: exp.Table) -> None:
    """ADR 0037 item 1: a FROM or JOIN item is a base table or CTE, without hints."""
    if table.args.get("hints"):
        label = f" on {table.name}" if table.name else ""
        msg = f"table hints are not allowed{label}"
        raise StatementCheckError(msg)
    tail = "; read base tables and CTEs only"
    for function in table.find_all(exp.Anonymous):
        name = str(function.name).lower()
        if name in REMOTE_SOURCES:
            msg = f"{name.upper()} is not allowed{tail}"
            raise StatementCheckError(msg)
    for part in ("catalog", "db"):
        qualifier = table.args.get(part)
        if qualifier is not None and not isinstance(qualifier, exp.Identifier):
            msg = f"FROM must name a table; its {part} is {type(qualifier).__name__}{tail}"
            raise StatementCheckError(msg)
    source = table.this
    if isinstance(source, exp.Identifier):
        return
    if isinstance(source, exp.Func):
        msg = f"table-valued function {_function_name(source)}() is not allowed in FROM{tail}"
        raise StatementCheckError(msg)
    if isinstance(source, exp.Dot):
        msg = f"four-part (linked server) names are not allowed{tail}"
        raise StatementCheckError(msg)
    msg = f"FROM must name a table, not {type(source).__name__}{tail}"
    raise StatementCheckError(msg)


def check_statement(sql: str, dialect: Dialect, *, upper: bool = False) -> CheckedStatement:
    """Spec 8.1.4 layer 3: refuse everything but one parameterized read-only SELECT over base
    tables and CTEs, calling allowlisted functions only (ADR 0037 item 1). With ``upper`` the
    template must also bind ``:upper`` (a time-typed watermark, ADR 0035); without it ``:upper``
    is refused."""
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
        if isinstance(node, exp.Hint):
            msg = "optimizer hints are not allowed"
            raise StatementCheckError(msg)
        if isinstance(node, exp.NextValueFor):
            msg = "NEXT VALUE FOR is not allowed (it advances a sequence)"
            raise StatementCheckError(msg)
        if isinstance(node, exp.Table):
            _check_table_source(node)
        if isinstance(node, exp.Func):
            _check_function(node, dialect)
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
    if not tables:
        msg = "the query must read at least one table"
        raise StatementCheckError(msg)
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
        # The connector runs ``mysql_grants_using_sql`` afterwards for the roles these reveal.
        queries.append(PrivilegeQuery("roles", "SELECT CURRENT_ROLE() AS roles", {}))
        queries.append(PrivilegeQuery("grants", "SHOW GRANTS FOR CURRENT_USER()", {}))
    else:
        for table in tables:
            columns = ", ".join(
                f"HAS_PERMS_BY_NAME(:t, 'OBJECT', '{privilege}') AS can_{privilege.lower()}"
                for privilege in _MSSQL_TABLE_PRIVILEGES
            )
            queries.append(PrivilegeQuery("table", f"SELECT {columns}", {"t": table}, table))
        database_columns = ", ".join(
            f"HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', '{privilege}') AS can_{privilege.lower()}"
            for privilege in _MSSQL_DATABASE_PRIVILEGES
        )
        queries.append(PrivilegeQuery("database", f"SELECT {database_columns}", {}))
        queries.append(
            PrivilegeQuery(
                "role",
                "SELECT IS_SRVROLEMEMBER('sysadmin') AS sysadmin, "
                "IS_MEMBER('db_owner') AS db_owner, IS_MEMBER('db_datawriter') AS db_datawriter, "
                "IS_MEMBER('db_ddladmin') AS db_ddladmin",
                {},
            )
        )
        queries.append(PrivilegeQuery("linked servers", _MSSQL_LINKED_SERVERS_SQL, {}))
    return queries


def mysql_roles(current_role: object, grant_lines: Iterable[str]) -> list[str]:
    """ADR 0037 item 3: the roles the login holds, as quoted ``\\`name\\`@\\`host\\``
    accounts: the active ones from ``CURRENT_ROLE()`` plus every ``GRANT \\`role\\`@\\`host\\`
    TO ...`` line of ``SHOW GRANTS`` (granted roles the session has not activated; ``SET ROLE``
    would). Names are rebuilt from the matched parts, so no untrusted text reaches the query."""
    roles: list[str] = []

    def add(text: str) -> None:
        for match in _MYSQL_ACCOUNT_RE.finditer(text):
            account = f"`{match.group('name')}`@`{match.group('host')}`"
            if account not in roles:
                roles.append(account)

    if current_role is not None and str(current_role).strip().upper() != "NONE":
        add(str(current_role))
    for line in grant_lines:
        match = _MYSQL_ROLE_GRANT_RE.match(line.strip())
        if match is not None:
            add(match.group("roles"))
    return roles


def mysql_grants_using_sql(roles: Sequence[str]) -> str:
    """``SHOW GRANTS FOR CURRENT_USER() USING ...`` so role privileges appear as grant lines."""
    return "SHOW GRANTS FOR CURRENT_USER() USING " + ", ".join(roles)


def _truthy(value: object) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1)


def write_findings(
    dialect: Dialect, query: PrivilegeQuery, rows: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Explanations of every write capability the rows reveal; empty means read-only so far."""
    findings: list[str] = []
    if dialect == "mysql":
        if query.name == "roles":
            return findings  # the roles row is expanded by mysql_roles, not a grant line
        for row in rows:
            for line in row.values():
                findings.extend(_mysql_grant_findings(str(line)))
        return findings
    for row in rows:
        if query.name in {"table", "database"}:
            where = f"on {query.table}" if query.name == "table" else "at database scope"
            for key, value in row.items():
                if str(key).startswith("can_") and _truthy(value):
                    privilege = str(key)[4:].upper()
                    findings.append(f"login can {privilege} {where}")
        elif query.name == "linked servers":
            # ADR 0037 item 2: a visible linked server is a path to writes elsewhere.
            name = str(next(iter(row.values()), "?"))[:MAX_GRANT_LINE_CHARS]
            findings.append(f"login sees linked server {name}")
        else:
            for key, value in row.items():
                if _truthy(value):
                    findings.append(f"login has the {key} attribute")
    return findings


def _mysql_grant_findings(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.upper().startswith("GRANT PROXY"):
        return ["login can PROXY as another user"]
    if _MYSQL_ROLE_GRANT_RE.match(stripped):
        return []  # the role's own privileges arrive through SHOW GRANTS ... USING
    if stripped.upper().startswith("REVOKE "):
        return []  # a partial revoke only narrows a grant counted elsewhere
    match = _MYSQL_GRANT_RE.match(stripped)
    if match is None:
        # ADR 0037 item 3: what the parser cannot read is not known to be read-only.
        return [f"grant line could not be read: {stripped[:MAX_GRANT_LINE_CHARS]!r}"]
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
