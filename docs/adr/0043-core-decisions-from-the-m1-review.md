# ADR 0043: Core decisions from the M1 review (work package E)

Status: proposed, 2026-10-15 (plan `docs/plans/M1-review-fixes.md`, package E; spec 2.3
invariants 2 and 8, 5.5, 8.5, 14.4, 14.6). Records the choices made while fixing the core
findings, and, in sections 10, 11 and 13 (added 2026-10-15), how core builds ADR 0034 and both
halves of ADR 0036, without repeating what those ADRs decide. Section 12 lists where the build
departs from them. The founder reviews it with the package.

## 1. Ingest decodes at most one bounded zstd frame

**Context.** An 18-byte frame that declares a 128 MiB window made the decompressor allocate
129 MiB per request before writing any output. Core read only the first frame, where the edge
reads across frames.

**Decision.** In `core/carto_core/ingest/app.py`:

- The decompressor refuses windows above `ZSTD_MAX_WINDOW_BYTES` (8 MiB). The edge compresses at
  level 3, with 2 MiB windows.
- A body must be exactly one frame. `_zstd_frame_size` walks the block headers (RFC 8878) to
  find the frame's end; a skippable frame, a second frame or trailing bytes are a 400, a
  malformed frame is a 400, and output above `ingest.max_decompressed_bytes` is a 413.
- `max_decompressed_bytes` defaults to 16 MiB (was 64 MiB), at most 128 MiB (was 512 MiB), and
  still at least `max_body_bytes` (5 MiB). A full batch of 5,000 events is a few MiB of JSON.

The edge parks a batch answered 400, 413 or 422.

## 2. Decoding runs off the event loop, and batches in flight are bounded

**Context.** Decompressing and validating a 5 MB batch on the event loop stalled every other
request for about a second.

**Decision.**

- The handler reads the capped body on the loop, then decompresses and validates it in
  Starlette's thread pool (`run_in_threadpool`).
- `ingest.max_inflight_batches` (default 8, 1 to 64) bounds the requests being read, decoded or
  written. It is a `threading.BoundedSemaphore`, only ever tried without waiting, so it is
  correct whatever loop or thread holds it. A request over the bound is answered 429 with
  `Retry-After: 1` and counted in `carto_ingest_busy_total`; the edge waits as told, capped at
  its own retry maximum.
- `/internal/ingest` holds its slot across read, decode, tenant check and write.
  `/internal/heartbeat` holds it for read and decode only; its single-row upsert runs outside.
- uvicorn's `limit_concurrency=256` is unchanged.

## 3. One ClickHouse client without a session

**Context.** ingest-api shares one clickhouse-connect client across its thread pool. With the
default generated session id, concurrent inserts ran in one session and failed with
`SESSION_IS_LOCKED`.

**Decision.** `create_client` passes `autogenerate_session_id=False`; nothing core does needs a
session, so one shared client stays enough and no client per thread or pool is needed.

## 4. Migrations run under a PostgreSQL advisory lock

**Context.** ClickHouse has no migration lock, so two migrate jobs (a Compose restart, two pods)
could apply the same file twice.

**Decision.** `migration_lock(engine)` (`core/carto_core/migrations.py`) takes the session-level
advisory lock `MIGRATION_LOCK_KEY` (`0x636172746F6D6967`, ASCII "cartomig") on its own
autocommit connection, so no transaction stays open. It tries first, logs that another run holds
it, then waits. It releases explicitly; when the release fails the connection is invalidated so
a pooled connection cannot keep the lock. `carto-core migrate` runs the ClickHouse migrations,
the retention TTLs and Alembic inside the lock, also with `--clickhouse-only`, which therefore
needs PostgreSQL too.

## 5. An older heartbeat never overwrites a newer one

**Decision.** `source_health` gains `sent_at` (Alembic `0002`), set to the earlier of the
heartbeat's `sent_at` and core's arrival time, so an edge whose clock runs ahead cannot pin the
row. The upsert updates only when the stored `sent_at` is NULL or not after the new one; on a tie
the later commit wins. A row written before `0002` is read by its `received_at`.

**Alternatives.** Ordering on `received_at` alone: arrival order is what goes wrong.

## 6. The canonical schema refuses names that carry values

**Context.** ADR 0027 and ADR 0030 mask value-shaped names on the edge. Core had no check of its
own, so an edge bug or an older edge could still deliver a value as a name.

**Decision.** In `packages/carto-schema/carto_schema/event.py`:

- Every dot-separated segment of a field name (`Identifier.field`, `dropped_fields`) is at most
  64 characters (`MAX_NAME_SEGMENT_LEN`), has no `@` after its first character (`@attr` stays
  valid) and no run of three digits (`NAME_DIGIT_RUN`). `*` and indexes below 20 pass. These are
  the edge's own masking rules.
- Attribute keys get the same checks, and their 1 to 128 length, in a validator that runs before
  field validation, because pydantic puts a dict key in the error location and would echo it.
  No error message names the field.
- `template_text` refuses a run of six digits (`MAX_TEMPLATE_DIGIT_RUN`); shorter numbers are
  protocol constants such as `HTTP/1.1 200`.
- `SchemaVersion` stays `"1"`: the rules are Python validators, and the committed JSON Schemas
  change only in their descriptions.

## 7. PostgreSQL connections verify the server by default

**Decision.** `PostgresSettings.sslmode` defaults to `verify-full` (`ca_file` becomes
`sslrootcert`); `prefer` and `allow` are not accepted. Turning verification off is explicit: the
Compose stack sets `CARTO_POSTGRES__SSLMODE: disable` on its internal network, and the
container tests do the same.

**Alternatives.** `require`: it encrypts but accepts any certificate, so it does not stop a
man in the middle.

## 8. Every Compose service has memory, CPU and process limits

**Decision.** `deploy/compose/compose.yaml` sets `mem_limit`, `cpus` and `pids_limit` on every
service, sized for the pilot VM of spec 5.5. The limits are not reservations, so the CPU caps may
add up to more than the VM has.

| Service | Memory | CPUs | Processes | Why |
| --- | --- | --- | --- | --- |
| clickhouse | 4g | 2.0 | 2048 | largest budget; reads its cgroup limit; many threads |
| edge-gateway | 2g | 2.0 | 256 | |
| postgres | 1g | 1.0 | 256 | |
| ingest-api | 1g | 2.0 | 256 | |
| simulator | 1g | 1.0 | 128 | dev profile only |
| migrate | 512m | 1.0 | 128 | one-shot |
| otel-collector | 512m | 1.0 | 256 | |
| pki-init, key-init | 256m | 0.5 | 64 | one-shot |
| proxy-otlp, proxy-webhooks | 128m | 0.5 | 64 | nginx, one worker (ADR 0032) |
| volumes-init | 64m | 0.25 | 32 | one-shot |

The two proxies came from `main` after the package was written; they were added when the package
was rebased on 2026-10-14. `core/tests/test_core_compose_limits.py` requires the three limits on
every service and the largest memory for ClickHouse, so a new service without limits fails CI.

## 9. Smaller decisions

- `load-bundle` closes the ClickHouse client and disposes the engine, also when the load fails
  (`cli._open_stores` is a context manager).

## 10. How the ledger claims a batch (ADR 0034)

**Decision.** In `core/carto_core/ingest/ledger.py`:

- Alembic `0003` adds `state` (`pending` or `done`, a check constraint) and `claimed_at` to
  `ingest_batches`; rows from before it are `done`, claimed at their `received_at`.
- `claim` is one `INSERT ... ON CONFLICT DO UPDATE ... WHERE state = 'pending' AND claimed_at <
  :stale_before RETURNING (xmax = 0)`. A returned row is the caller's claim (new, or a stale
  claim taken over); concurrent takeovers serialize on the row lock and the `WHERE` is checked
  again, so one wins. Otherwise a second statement reads the state: `done` is a duplicate, and
  anything else (a fresh claim, or a row released or pruned in between) is busy.
- `ingest.claim_timeout_seconds` sets the timeout: default 120, 30 to 3600. A busy batch is
  answered 503 with `Retry-After: 5`; the edge backs off on its own after a 503.
- After the write, `complete` upserts the row as `done` whoever holds the claim by then: the
  rows are in ClickHouse either way.
- A failed write calls `release`, which deletes the row only while it is still the caller's
  `pending` claim (matched on `claimed_at`), so the edge's retry is taken at once instead of
  waiting out the timeout.
- A `LedgerPruner` thread in `ingest-api` runs `prune` at start and then hourly: it deletes rows
  received before `now - events_days - 1 day`, 10,000 rows per statement. A failed run is
  logged and tried at the next interval.
- `load-bundle` uses the same claims. A chunk another run holds stops the load with "re-run
  later".
- ClickHouse migrations `0003` to `0006` drop the two event tables and create them again as
  `ReplacingMergeTree`, with the sort keys of ADR 0034. The tables are dropped `SYNC`, and any
  rows in an existing installation are lost; the install guide says so.

## 11. How core enforces the observed_at range (ADR 0036)

**Decision.**

- `RetentionSettings.observed_at_range(now)` (`carto_common.settings`) gives the range, with both
  ends included. The edge will use the same function.
- `ingest-api` checks the range after validation and before the claim, on its own clock. A
  batch with any event outside the range is a 422 listing at most 20 events as
  `{"loc": ["events", i, "observed_at"], "type": "observed_at_too_old" | "observed_at_future"}`.
  The detail gives the count, and no entry carries the time.
- `ClickHouseWriter` groups events by UTC day, oldest first, at most 90 days per group, and
  inserts each group's events and then its identifiers. A group that fails leaves the earlier
  groups written; the batch is not marked done, and the replacing tables merge the copies that
  the retry writes.
- Permanent errors are a `clickhouse_connect` `DataError` (a value it cannot serialize), or one
  of the server codes in `PERMANENT_ERROR_CODES`: the parse, type, range and size codes, and 565
  `TOO_MANY_PARTITIONS`. The code comes from ClickHouse's `X-ClickHouse-Exception-Code` header,
  whatever exception class carries it. These are answered 422, release the claim, and are
  counted in `carto_ingest_refused_total`. Code 252 `TOO_MANY_PARTS` stays transient:
  ClickHouse 24.8 raises it both for "too many partitions for a single insert block", which the
  day groups prevent, and for "merges are slower than inserts", which passes. Everything else
  (unknown table, memory, timeouts, the network) is a 503.
- `load-bundle` cuts its chunks first and then skips the events outside the range at load time,
  so a chunk's batch id always names the same events. It reports the skipped events as too old
  or future.
- Every 503 from `ingest-api` now carries `Retry-After`.

## 12. Departures from ADR 0034 and ADR 0036, for the founder's review

- ADR 0034 item 2 says `ReplacingMergeTree(ingested_at)` for both tables. `event_identifiers`
  has no `ingested_at` column, so it uses `ReplacingMergeTree` without a version: duplicates are
  identical rows, and either may stay.
- New in the build: releasing the claim after a failed write, and `complete` as an upsert.
- `load-bundle` skips both too-old and future events. ADR 0036 names only "the rest" of a bundle
  older than retention; a future event would be refused by `ingest-api` too.
- A chunk loaded with some events skipped as future stays `done`, so a later re-run does not
  load those events even once they are in range.

## 13. The edge range and the analyzer's reference time (ADR 0036 item 1)

**Context.** Simulator batch runs have fixed dates (`DEFAULT_START_DATE` 2026-09-23), and so do
the records of most edge tests. Checked against the wall clock, they fall out of the range a
month later, and the leak test, `make analyze`, `make bench` and the analyzer tests would drop
every record.

**Decision** (the founder chose the reference-time option on 2026-10-15):

- `EdgePipeline.process` checks the parsed time with `carto_common.settings.observed_at_range`
  against the pipeline's clock, after parsing and before classification. Older: dropped with
  reason `too_old`. Later: `observed_at` becomes the record's `received_at` with
  `observed_at_quality=ingest`, so the event id derives from the ingest time. Both are counted
  in `PipelineCounters.out_of_range` and marked on `PipelineResult.out_of_range`.
- The gateway's clock is `carto_edge.pipeline.pipeline.wall_clock`. Its new counter
  `carto_edge_events_out_of_range_total{source_id, reason}` counts both cases; `too_old` is also
  a `carto_edge_dropped_total` reason.
- The analyzer checks against its run time unless `--as-of` names another time: an ISO 8601
  date (midnight UTC) or a time with an offset. A naive time is refused. The manifest notes the
  time used. `carto-edge bench` takes the same option. `BundleCounts` gains `events_too_old` and
  `events_future`, both defaulting to 0 so manifests written before them still load;
  `MANIFEST.md` and the summary show them.
- `carto-sim as-of --out DIR` prints the start of the day after a run, read from its
  manifest. `make analyze`, `make bench` and `make bench-gateway` pass it as `--as-of`.
- The edge tests pin `wall_clock` to 2026-10-15 12:00 UTC (autouse fixture in
  `edge/tests/conftest.py`). The range tests pass explicit clocks. Core's integration test
  analyzes and loads scenario A as of `carto_simulator.api.run_as_of`.

**Alternatives.** Dating simulator runs relative to today: the eval ground truth would change
by the day and fault days could fall on weekends. Skipping the check for simulator input: a
special case in product code.

## Consequences

- A simulator run longer than event retention loses its first days to the range, as a
  backfill would.
- Queries that need exact counts must read with `FINAL` (ADR 0034 item 3). No core query needs
  one in M1.
- **Not built, from the plan:** `load-bundle` keys chunks by index, not by event range, so a
  rerun with another `--chunk-size` duplicates events; it reads the files again after hashing
  them; it checks each event's tenant against the manifest but not the manifest against core's
  tenant; and its decompressor has no window cap.
- Spec 14.4 asks for TLS to PostgreSQL and ClickHouse. The Compose stack turns it off on its
  internal network (`sslmode: disable`, ClickHouse over HTTP), with only a YAML comment deferring
  TLS to M6. This ADR records that deferral; the founder confirms it.

## Tests

`core/tests/test_core_ingest_app.py`, `test_core_ledger.py`, `test_core_bundle.py`,
`test_core_settings.py`, `test_core_clickhouse_rows.py`,
`test_core_cli.py`, `test_core_migrations_sql.py`, `test_core_compose_limits.py`,
`test_core_integration.py` (Docker only); `packages/carto-schema/tests/test_schema_event.py`,
`test_schema_property.py`.
