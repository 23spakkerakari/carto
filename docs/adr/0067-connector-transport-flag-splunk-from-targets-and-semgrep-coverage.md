# ADR 0067: Connector transport flag, Splunk `from` targets and Semgrep coverage

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, packages H and T;
spec 2.3 invariants 1, 6, 7 and 9, 8.1, 8.1.3, 8.1.4, 14.4, 14.9, 18.3). Records the
decisions of the November 2026 security fixes that touch the connectors and the CI rule that
guards them. ADR 0037 and ADR 0041 stand; this one adds to them.

## 1. Weakened transport needs `insecure_transport: true`, audited once per enable

Spec 8.1: "TLS on by default with certificate verification; custom CA bundles allowed;
verification can't be disabled without an admin flag that is audited and shown as a warning in
the UI." Before this change `SqlConfig` accepted `sslmode: disable|allow|prefer|require`,
`ssl: false`, `encrypt: false` and `trust_server_certificate: true` silently, and
`SplunkConfig` accepted `verify_tls: false` and `allow_plaintext: true` with a log line from
`net/http.py` or the connector's constructor and no audit row. Every one of those settings was a
flag in its own right, which is exactly the problem: a reviewer reading a sources file had to
know six keys and their safe values to see that a source talks to its system unverified, and
nothing in the audit trail said so.

**Decision.**

- One explicit flag, `insecure_transport: bool = False`, on `SqlConfig` and `SplunkConfig`. A
  model validator refuses the config when any transport key is set below certificate
  verification and the flag is off: on PostgreSQL an `sslmode` other than `verify-ca` or
  `verify-full`; on MySQL `ssl: false`; on SQL Server `encrypt: false` or
  `trust_server_certificate: true`; on Splunk `verify_tls: false` or `allow_plaintext: true`.
  The message names the keys and the flag (`sslmode: weakens transport security (spec 8.1);
  set insecure_transport: true to accept it (audited, warned)`) and never echoes a value; the
  models already hide inputs in their errors.
- The keys are judged for the configured dialect. `ssl: false` on a PostgreSQL source changes
  nothing (the PostgreSQL connection plan reads `sslmode`), so it neither needs the flag nor
  appears in the audit row; the row says what is actually weakened. Each config exposes the
  judged list as `weakened_transport: tuple[str, ...]`.
- `allow_plaintext` keeps its own meaning (the constructor still refuses an `http://` base URL
  without it) and now also needs `insecure_transport`. Two flags for plaintext is deliberate:
  `allow_plaintext` says what the operator wants, `insecure_transport` is the one audited
  acknowledgement that spec 8.1 asks for, whatever the connector type.
- The audit row and the warning are written by the scheduler, not by the connector, when it
  enables the source: the `check()` whose result can enable the source (the test passed and the
  credential is not write-capable) writes one edge audit row (action
  `source.insecure_transport`, actor `system:edge`, target the source id, details
  `{"connector": <type>, "keys": [...]}`) and one `scheduler.insecure_transport` warning. A
  refused test writes nothing: the row records that the gateway started reading a system over
  a weakened transport, which is the event an auditor wants, and a source the gate refused is
  not read. The scheduler recognises the config through a small structural protocol (any
  `ConnectorConfig` with `weakened_transport`), so the upload and SFTP configs, which have no
  such keys, are not touched.
- Per enable, not per poll, and not per client build. A row per poll would fill the chain with
  thousands of identical rows per day for one operator decision and hide the rows that matter
  (reveals, key operations); a row at config load would be written by every process that reads
  the sources file (the CLI, the analyzer) for sources that are never polled. The enable is the
  one moment the decision takes effect. A source that is refused and later accepted (a corrected
  grant, ADR 0041) is enabled again and gets a second row.
- `net/http.py` keeps its per-client warning for `verify=False`; it fires once per client the
  connector opens and costs nothing. The Splunk constructor keeps its plaintext warning for the
  same reason.

**Alternatives.** Refusing weakened transport outright: the Compose fixtures and more than one
pilot replica have no certificate chain the edge can verify, and spec 8.1 allows the flag. A
flag per key (`allow_insecure_sslmode`, ...): the reviewer's problem again. Writing the row from
the connector's constructor: connectors have no audit handle on purpose (they get secrets and
the network policy through `ConnectorContext` and nothing else), and the constructor runs in
processes that never poll.

**Tests.** `edge/tests/test_edge_conn_sql_check.py` (each weakened key per dialect refused
without the flag, accepted and listed with it, keys of other dialects ignored, several keys all
named), `test_edge_conn_splunk.py` (the two Splunk keys), `test_edge_scheduler.py` (one row and
one warning across three polls, none for a verified source, none for a refused test, a second
row for the enable that follows a refusal). The guide's SQL and Splunk key tables carry the
flag.

## 2. `from` reads only `datamodel:` and `lookup:` datasets; an unquoted backslash before a delimiter is refused

ADR 0037 item 4 keeps `savedsearch` off the Splunk command allowlist because it spawns a search
carto cannot see. `from` was on the list as a generating command, and `| from savedsearch:<name>`
is the same thing under an allowed name: the saved search runs with whatever stages it holds,
`| delete` or `| collect` included, on every window of every poll. The 2026-11-01 review ran
`check_search("| from savedsearch:nightly_cleanup")` and it was accepted (finding H1).

`from` stays on the list because data models and lookups are datasets the check can see
through, but the stage must name one of those two kinds: the text after the command has to
match `datamodel:` or `lookup:` (also the function form `lookup(<name>)`), case-insensitive,
quoted names allowed. `savedsearch:`, a bare name, or nothing at all is refused with a message
that names the rule and never repeats the search.

The scanner also treated an unquoted backslash as escaping the next character, inside quotes
and out. Inside double quotes that is Splunk's rule; outside them the scanner could not be sure
Splunk agrees, so `index=a \| outputlookup evil` was checked as one `search` stage while Splunk
may run two (finding M13, plausible, not confirmed against Splunk). The scanner now refuses an
unquoted backslash immediately before `|`, `[`, `]` or a backtick; a backslash before any other
character keeps its old meaning, and a backslash inside double quotes is untouched. Refusing is
the fail-closed choice: a search that needs a literal `|` outside quotes can quote it.

Alternatives: dropping `from` altogether (loses the data-model reads partners use); resolving
the saved search through the REST API and checking its body (a second request per poll, a race
between check and run, and the saved search can change under carto).

Tests: `edge/tests/test_edge_conn_splunk_spl.py` accepts the four dataset forms and refuses the
six `savedsearch` and bare forms and the four backslash-before-delimiter forms.


## 3. The Semgrep rule matches the call forms the connectors use

Spec 8.1 wants a Semgrep rule that fails CI when a connector calls a mutating API, and spec
18.3 a self-test next to it. The rule matched `$C.post()`, `$C.request("POST", ...)` and
`$C.execute("<literal>")`, and none of those is how the connectors talk to their systems: the
Splunk connector builds every request with `client.build_request(method, ...)` and sends it
with `client.send()` (so it can stream and retry), and the SQL connector runs
`connection.execute(text(sql))` and `session.exec_driver_sql(...)` through SQLAlchemy. The
`# nosemgrep` on the Splunk export POST suppressed a match that never happened. A new write in
either connector would have passed CI.

**Decision.** The rule (`tools/semgrep/connector-read-only.yaml`) gains, with the existing
keyword regex and no new allowances:

- `$C.build_request("POST"|"PUT"|"PATCH"|"DELETE", ...)` and the `method=` keyword forms, plus
  the `method=` forms of `$C.request(...)`.
- `$C.execute(text("=~/write/"), ...)`, the `sa.text` and `sqlalchemy.text` spellings, and
  `$C.exec_driver_sql("=~/write/", ...)`. As before only a literal statement is judged: the
  connector's templates arrive as variables and are checked by `sqlglot` at load (ADR 0037,
  0041); the session statements it does write as literals (`SET SESSION ...`, `START
  TRANSACTION READ ONLY`, `COMMIT`, `ROLLBACK`) do not start with a write keyword.
- Opening a file for writing: `$C.open(path, "w"|"wb"|"a"|...)`, pathlib's `path.open("w")`,
  the builtin `open(path, "w")`, each with the `mode=` form. A mode is a short string of
  `r w a x b t +`; one that contains `w`, `a`, `x` or `+` writes. The SFTP connector reads
  through `start_sftp_client()` and `readdir`; the upload connector opens `"rb"`.
- Command execution on an SSH connection: `$C.run(...)`, `$C.create_process(...)`,
  `$C.create_subprocess(...)`.
- Local file mutation the list did not name: `os.replace`, `shutil.move`, `shutil.copyfile`,
  `shutil.copy2`, `shutil.copytree` (`os.remove`, `os.unlink`, `os.rename` were already caught
  through `$C.remove` and friends).

The fixture `tools/semgrep/connector-read-only.py` holds a `ruleid:` line for every new pattern
and an `ok:` line for the read it sits next to (`build_request("GET", ...)`,
`execute(text("SELECT ..."))`, `exec_driver_sql("SET SESSION ...")`, `sftp.open(path, "rb")`,
`path.open("rb")`, `start_sftp_client()`), so `semgrep --test` proves both halves: the rule
sees the write and leaves the read alone. The Splunk export POST keeps its `nosemgrep` with the
reason in the comment above it (spec 8.1.3: the documented export is Splunk's only interface
and creates nothing on the search head); it is now the one real suppression in the package.

Semgrep is not installed on the development laptop; the YAML is checked locally by loading it
and `semgrep --test` plus the scan over `edge/carto_edge/connectors` run in CI's `semgrep` job.

## 4. The private key file is owner-only from its first instant

`carto_common.pki.write_pem` opened the key with `"xb"` under the process umask and `chmod`ed
it to 0600 afterwards: with the default umask of 022 the file was world-readable between the
two calls. `carto_common.crypto._create_private` already does this right for the edge's key
material (`os.open` with `O_CREAT | O_EXCL` and mode 0600 on the call itself, then write and
fsync).

**Decision.** `write_pem` creates the key through `_create_private`. The existing refusal to
overwrite a key stays, and the `FileExistsError` a race could raise between the check and the
exclusive open is reported with the same message. `_create_private` keeps its name;
`carto-common` is one package and the two modules are its own, so no public alias is added for
one internal caller. A POSIX test creates a key under umask 0 and asserts mode 0600; a test
asserts an existing key is untouched by a refused write. Windows has no mode bits; the test
skips there, as the `carto-ctl` one does.

## Consequences

- A sources file with a weakened transport setting and no `insecure_transport: true` fails to
  load after this change; the operator adds the flag or restores verification. The Docker
  integration fixtures (`test_edge_conn_sql_pg.py`, `test_edge_conn_sql_mysql.py`) carry it.
- The audit chain gains at most one `source.insecure_transport` row per enable of such a
  source; `carto-edge audit verify` lists them like any other row. The console's source page
  (spec 8.1 "shown as a warning in the UI") does not read the edge audit file; this package
  leaves the UI part unbuilt and the warning reaches the operator through the gateway log.
- The flag is per source, not per deployment: an operator who accepts one unverified replica
  does not accept all of them.
- The Semgrep rule now forbids opening any local file for writing inside a connector, which is
  spec 8.1's intent; a connector that needs a scratch file (none does today) would be built on
  the disk buffer or the state directory through the pipeline, not in the connector.
- `$C.run(...)` also matches `asyncio.run(...)` inside a connector module, which is not
  something a connector should call either.
