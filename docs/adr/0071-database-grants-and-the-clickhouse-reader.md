# ADR 0071: Database grants follow the code, and a read-only ClickHouse user for the console

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, package G; spec 2.3
invariant 9, 9.9, 14.4, 14.9). Amends ADR 0043 section 10 (the grant the ledger's release and
prune need) and records what ADR 0034 item 4 left unsaid.

The security review of 2026-11-01 read the grants of migration 0004 against the code that runs
under them and found three gaps, plus one in the ClickHouse side of the Compose stack. Each is
small and they are decided together because they are one subject: the database user a process
connects as holds exactly what that process executes (spec 2.3 invariant 9).

## 1. The ledger stays DELETE-based and the grant follows the code

`PostgresBatchLedger.release` and `prune` are `DELETE FROM ingest_batches` (ADR 0043 section
10), but 0004 granted `carto_ingest` only `SELECT, INSERT, UPDATE` on that table. Both callers
swallow the refusal (`ingest/app.py` logs "batch claim release failed", `LedgerPruner.run_once`
logs "ledger pruning failed"), so in the pilot stack, where `ingest-api` connects as
`carto_ingest`, every failed ClickHouse write left a claim nobody could release until the claim
timeout and the hourly pruner deleted nothing, ever.

Decision: migration 0006 grants `DELETE ON ingest_batches TO carto_ingest`, guarded by role
existence like 0004's blocks. The ledger is not rewritten as soft deletes: the claim protocol of
ADR 0034 is correct and tested, the gap was the grant. The role still has no `DELETE` on
`templates`, `fields`, `systems`, `sources`, the inventories or source health, and nothing on
`audit_log`, `llm_calls` or the identity tables. `test_core_ledger.py` pins the statement
shapes so a change to the ledger's SQL is a change to this grant, and the Docker-marked
integration test connects as `carto_ingest`, releases and prunes, and is refused `DELETE FROM
templates`.

## 2. The worker loses the identity tables

0004 gave `carto_worker` every metadata table, `users`, `groups`, `role_bindings` and
`sessions` among them. The worker (profiler, linker, namer, the audit of its own transitions)
never reads them; only the console api does (`api/stores_postgres.py`). A compromised worker
could have read every session id hash and rewritten role bindings.

Decision: 0006 revokes the four identity tables from `carto_worker`, and with them
`role_bindings_id_seq`, the one sequence an identity table owns, which 0004's `APP_SEQUENCES`
had granted `USAGE` on (`users`, `groups` and `sessions` carry text ids and own none; review
2026-11-02, finding 12). It keeps `settings` whole, not `SELECT` alone: it reads the engine thresholds there (ADR 0055) and the profiler
upserts its watermark there (`profiler/repository.py`). The plan also names `login_attempts`;
no such table exists (the api keeps login attempts in its session store), so a migration that
creates one grants it to `carto_app` alone.

## 3. `llm_calls` is append-only

Spec 9.9 says every LLM call is audited: task, payload hash, model, token counts, latency,
status. 0004 created `llm_calls` as an ordinary metadata table with `UPDATE` and `DELETE` for
both console roles, so the audit of what left for the provider could be edited by the process
that wrote it. Nothing updates or deletes a row: `llm_gateway/audit.py` only inserts.

Decision: 0006 makes `llm_calls` what `audit_log` is under spec 14.9: `SELECT, INSERT` for
`carto_app` and `carto_worker` and a `BEFORE UPDATE OR DELETE` plus `BEFORE TRUNCATE` trigger
pair that refuses the owner too. 0004's trigger function names `audit_log` in its message, so
0006 carries `llm_calls_append_only()` of its own. `llm_calls` keeps its `updated_at` column
(0004 created it; a column drop is not worth a rewrite of the sink), which the trigger makes a
constant.

## 4. A read-only ClickHouse user for the console, now, in Compose

The Compose stack had one ClickHouse user, the owner `carto`, serving `migrate`, `ingest-api`,
`api` and `worker`, with a YAML comment deferring per-service users to M6. On this branch the
worker reads ClickHouse through `store/clickhouse.py`, which only queries and streams, but the
M3 demo cut gives it `txn_events` and the hop tables to write (`ClickHouseStore.append_txn_events`),
so the worker is not a reader. The api opens no ClickHouse client of its own today and will
only ever read (spec 14.4: "API reads"); the writers are `db/clickhouse.py` (ingest and the
bundle loader), `migrations.py` (migrate) and, from M3, the worker.

Decision: `clickhouse/users.d/carto.xml` defines a `readonly` profile (`readonly = 1`, the
owner's memory and time limits) and a `carto_reader` user on it; `api` connects as it with the
`ch_reader_password` secret. The worker stays on the owner until M6 gives it a user of its own
with INSERT and SELECT on the tables it owns (spec 14.4 wants per-service users, not a
read-only worker). The password reaches the user file through
`<password from_env="CARTO_CH_READER_PASSWORD"/>`: a users.d file cannot read a file, the
image's `docker-entrypoint-initdb.d` scripts run only on an empty volume, and the owner is
created with `access_management` off (`CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT: "0"`), so no init
SQL could `CREATE USER`. The `clickhouse` service's entrypoint wrapper in `compose.yaml` reads
the secret file into that variable and execs the image's entrypoint, the pattern Dex already
uses for its client secret. The user accepts any client address like the owner the entrypoint
writes: the `core` Docker network, internal to the host, is the only route to the server (spec
14.5), and it is the network boundary, not the user's `<networks>`, that confines both.
`readonly = 1` also refuses changing a query setting; the stores pass none and
`clickhouse-connect` drops settings the server reports as read-only.

## 5. What remains M6

The per-service ClickHouse users that `carto-core migrate` will create, with an INSERT-only
`carto_ingest` and the reader among them, remain M6 work with TLS to both databases (ADR 0043
Consequences). Until then the Compose stack ships two users, the owner for `migrate` and
`ingest-api` and the reader for the console, and this ADR records that, not only a YAML
comment. A ClickHouse user that `ingest-api` could use today would need `INSERT` on the two
event tables and `SELECT` on `system.settings`; it is an XML user like the reader and a secret
away, and is left for M6 with the rest so the stack's user set changes once.

## Alternatives

- Rewrite release and prune as `UPDATE ... SET state = 'released'` so the ingest role needs no
  `DELETE`: a third ledger state, a pruner that still has to delete, and a claim protocol with
  more cases than ADR 0034 tested, to avoid one grant on one table.
- Keep `llm_calls` editable and rely on the api never exposing a write: the audit of spec 9.9
  would then depend on application code rather than on the database, which is what the
  `audit_log` trigger exists to avoid.
- Create `carto_reader` with init SQL: impossible without turning access management on for the
  owner, which is a larger grant than the one being removed.
- Wait for M6 for any ClickHouse user split: the console would keep write access to the event
  store for another milestone for no reason.

## Consequences

- A failed ClickHouse write in the pilot stack now releases its claim at once and the pruner
  deletes ledger rows past retention, as ADR 0034 item 4 intended. An existing installation
  gets the grant on its next `migrate`; the three roles must exist for the guarded blocks to
  apply, as for 0004.
- The worker cannot read or write sessions, users, groups or role bindings, nor advance
  `role_bindings_id_seq`. Nothing in the worker changes.
- An `UPDATE` or `DELETE` on `llm_calls` fails for every role, the owner included. A retention
  policy for `llm_calls` (none exists) will need the same partition-drop approach as
  `audit_log`, not a `DELETE`.
- The `clickhouse` service starts only when `CARTO_CH_READER_PASSWORD` is set: `from_env`
  refuses a missing variable, which is the loud failure wanted. `make dev` and the CI compose job
  generate `ch_reader_password` with the other secrets; an existing checkout runs `make dev`
  once to get the file.
- `test_core_integration.py` creates the three service roles in its PostgreSQL container before
  migrating, so the guarded grants are exercised there; the role-connected tests run only with
  Docker.
- Deferred (above, section 5): the migrate-created ClickHouse users and TLS to both databases
  remain M6.

## Tests

`core/tests/test_core_migrations_sql.py` (the 0006 statements are fixed strings with the fixed
role names; the linear history now derives from the files), `test_core_ledger.py` (statement
shapes), `test_core_integration.py` (Docker only: release and prune as `carto_ingest`, the
identity tables refused to `carto_worker`, `llm_calls` append-only for both roles and the
owner).
