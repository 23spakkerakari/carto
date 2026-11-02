# ADR 0073: The ingest path gets a body deadline, a decompression ratio cap, row ceilings, a bound on ingested_at, and parked batches expire at the edge

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, sweep of 2026-11-02;
spec 2.3 invariants 7 and 8, 8.5, 12, threat-model row 10). Amends ADR 0043 section 2 (the
in-flight slot) and ADR 0036 (the plausible range now covers `ingested_at`); records what ADR
0034 item 2 left open about the version column.

## Context

The security sweep of 2026-11-02 read `ingest-api` and the edge forwarder as a client holding
the `edge-gateway` certificate would, and found five ways such a client could wedge or grow
core, or fill its own buffer, without breaking any contract. All are conditional on that
certificate, so Medium at most; all are fixed here because each is a missing limit, and spec
2.3 invariant 8 wants a limit on every input.

1. The body read held an in-flight slot with no deadline. `with admitted(): await receive()`
   streamed the body (`_read_body`) for as long as the client cared to trickle it, and uvicorn
   has no body timeout. Eight trickling connections (`max_inflight_batches` defaults to 8)
   answered every honest batch 429 for ever, while `/healthz` and `/readyz` stayed green.
2. The only bound on decompression was the absolute `max_decompressed_bytes` (16 MiB), so a
   frame of a few hundred bytes expanded to the whole cap, and both the wire body and the
   decompressed body were held twice (a `bytearray` and its `bytes(...)` copy).
3. Rows named by the edge grew without bound: one `templates` row per distinct
   `(system_id, template_id)` the edge sends, never deleted; one `edge_inventories` row per
   `edge_id` the body names; and a config list in an inventory had no length cap.
4. `ingested_at` was not range-checked (ADR 0036 bounds `observed_at` only), and it is the
   version column of the `ReplacingMergeTree` tables (ADR 0034 item 2), so one far-future value
   would win every later merge for that sort key.
5. A batch core refuses for good is parked in the edge's disk buffer (ADR 0014) and never
   expires, so a persistent 422 parked every batch until the 20 GB buffer was full.

## 1. A deadline on the body read, and a readiness probe that notices wedged slots

`ingest.body_timeout_seconds` (default 30, 1 to 600) is an `asyncio.timeout` around
`_read_body` on every internal route. When it passes, the request is answered 408 (an RFC 7807
problem that never echoes the partial body), the slot is released by the same context manager
that took it, and `carto_ingest_body_timeout_total` counts it. The edge treats 408 like any
other non-parking status: it backs off and sends the batch again.

The 408 carries `Connection: close`. Without it the slot was free but the connection was not:
h11 keeps an HTTP/1.1 connection alive after an early response, and uvicorn cancels its
keep-alive timer on every byte received, so a client trickling one byte at a time held one of
the listener's `limit_concurrency=256` connections for ever while the slot went back into
circulation. With the header h11 moves to `MUST_CLOSE` once the response is sent and uvicorn
closes the transport (verified against a real uvicorn and a raw TLS socket in
`test_core_ingest_mtls.py`: the 408 arrives at the deadline with the header, and the next read
on the socket is end of stream).

The deadline covers the body only. Nothing in the app sees a request before its headers are
complete, so a client that trickles the request line or the headers is bounded by the server
alone, and uvicorn has no header deadline either: `timeout_keep_alive` runs between requests
and is cancelled on the first byte of the next one; h11's `max_incomplete_event_size` (16 KiB,
`server.py`) bounds a header block that never ends, not one that arrives slowly. A slow
header trickle is therefore bounded only by `limit_concurrency` (256 connections, after which
uvicorn answers 503) and, where a reverse proxy fronts the listener, by that proxy's
`client_header_timeout`. In the Compose stack nothing fronts `ingest-api`: `edge-gateway`
speaks to it directly over mTLS (the nginx `proxy-otlp` and `proxy-webhooks` services sit in
front of the gateway's push listeners, not of `ingest-api`), so the header phase there has no
deadline at all, and only a holder of the `edge-gateway` certificate can reach the listener to
exploit it (threat-model row 10, the same condition as the five items above). This ADR leaves
that as it is and records it under Consequences.

The in-flight bound of ADR 0043 section 2 is now an `InflightSlots` object instead of a bare
`BoundedSemaphore`: it is still only ever tried, never waited on, but it remembers when each
slot was taken. `/readyz` answers 503 (`{"status": "unavailable"}`, no detail, spec 12) when
every slot is held and none has been held for less than the body timeout. The bound is
exclusive: a slot held exactly the timeout is not yet stalled. A slot legitimately held that
long by every request at once means the listener cannot admit anything honest, which is what
the orchestrator's probe should see. The Compose healthcheck of `ingest-api` therefore probes
`/readyz` (it probed `/healthz`, which never reports this), with `carto-core healthcheck`
presenting the service certificate; a wedged or store-less listener shows as unhealthy and
`edge-gateway`'s `service_healthy` dependency waits on it. Compose does not restart on an
unhealthy probe by itself; `deploy/compose/README.md` says so. uvicorn's configuration is
unchanged; a comment in `server.py` says the app owns the deadline.

## 2. A ratio cap on decompression, and one buffer per body

`ingest.max_decompression_ratio` (default 200, 2 to 10,000) is checked on every chunk that
comes out of the decompressor, against the whole wire length (known before decompression
starts): output past `wire_length * max_decompression_ratio` is a 413 with its own detail. The
absolute cap stays and still wins for a full-size body.

Measured with zstd level 3 on batches the edge would send: 5,000 events with their own ULIDs,
times and HMAC tokens compress about 10x; 5,000 events that differ only in ULID and time (the
same identifiers in every event, the most repetitive batch a real source produces) compress
53x; one event, 1.8x. The default of 200 leaves a 4x margin over the worst realistic
batch, and the setting goes to 10,000 for an operator who ever sees the ratio 413 in
`carto_ingest_errors_total`. A batch that is one event repeated byte for byte compresses 400x
and is refused; the edge never produces one, since every event carries its own id.

The copies are gone: `_read_body` returns the `bytearray` it read into; `decompress_capped`
reads it through the buffer protocol (no `BytesIO` copy) and returns the `bytearray` it decoded
into; pydantic validates either directly. Per slot the buffers are now at most
`max_body_bytes + max_decompressed_bytes` = 5 + 16 = 21 MiB (they were 2 x 5 + 2 x 16 =
42 MiB), and for a small frame the decompressed buffer is at most 200x the wire length. With
eight slots
that is 168 MiB of buffers at the caps, against the `ingest-api` service's 1g `mem_limit` (ADR
0043 section 8). The wire buffer lives until validation returns (the thread-pool call holds
it), and the validated batch itself, the larger object, is bounded by the 5,000-event batch of
spec 8.5, not by this ADR.

## 3. Row ceilings on what the edge names

- `ingest.max_templates_per_system` (default 50,000, 1,000 to 1,000,000). Before the ledger
  claim, `MetadataStore.templates_over_ceiling` tells `ingest-api` whether any system would
  hold more than the ceiling once the batch's templates were upserted, in at most two grouped
  queries on one connection. The first binds the batch's `(system, template)` pairs as two
  parallel `text[]` arrays, unnests them and left-joins `templates` on the full primary key
  `(tenant_id, system_id, id)`: one key probe per pair, so its cost is the batch size (at most
  5,000 pairs), whatever the system holds; it yields, per system, how many of the batch's ids
  are known. The second, `COUNT(*)` of the rows per system, is an index-only range scan of the
  system's primary-key entries, O(rows in system), up to 50,000 rows at the default ceiling;
  it is issued only for the systems whose batch carries an id they do not hold yet. A batch of
  known ids, the steady state of a system at or near its ceiling and of every system once its
  templates have been seen, is answered by the key probes alone. The first cut concatenated
  `system_id || '/' || id` on every row of the system and tested it against a list, which
  scanned the whole system per batch; the second cut did the join and the total in one
  statement, which still range-scanned the system's rows on every batch. The counts come back
  as two result sets and the check is unchanged: rows held, less known ids, plus the batch's
  distinct ids, against the ceiling. A batch over the ceiling is a 422
  `too_many_templates` whose detail names the system id only; the edge parks it, which is the
  fail-closed outcome; nothing is claimed or written; `carto_ingest_templates_refused_total`
  counts it. Ids a system already holds add nothing, so a full system keeps accepting its known
  templates. A failed count is a 503, never an open door. The check runs before the claim rather
  than inside the ledger's `done` transaction so that a refused batch leaves no events in
  ClickHouse, and it takes no lock: batches of one system in flight at once can each pass it,
  so the system can exceed the ceiling by at most `max_inflight_batches` times the distinct
  templates of one batch (8 x 5,000 at the defaults, against a ceiling of 50,000), a bound the
  setting's description and the handler's docstring state. Serialising the check is not worth a
  lock on the hot path for an overshoot of that size.
- `ingest.max_edges_per_tenant` (default 64, 1 to 10,000). `replace_inventory` takes the
  transaction-level advisory lock `pg_advisory_xact_lock(EDGE_CEILING_LOCK_KEY,
  hashtext(tenant_id))` first in its transaction (`EDGE_CEILING_LOCK_KEY` is the ASCII bytes of
  `edge` as an `int4`, distinct from the migration lock's `bigint` key), then counts the tenant's
  `edge_inventories` rows and refuses an `edge_id` the tenant has not recorded yet once it holds
  that many, with `MetadataLimitError`, which `ingest-api` answers as 422 `too_many_edges` and
  counts in `carto_ingest_edges_refused_total`. The raise rolls the transaction back, so none of
  that inventory's systems or sources is written, and the lock goes with it. The advisory lock
  was chosen over `SELECT ... FOR UPDATE` on the tenant's rows: a row lock does not serialise two
  inserts of distinct new edge ids, and a tenant with no rows yet has nothing to lock, so two
  first edges under a ceiling of one would both get in. A known edge always replaces its copy;
  it takes the lock too, briefly, so the count a new edge sees is never mid-write.
- `carto_schema.inventory.MAX_CONFIG_LIST_ITEMS` (64): a config list value is at most 64
  strings (`ConfigList`), and the committed JSON Schema gains the `maxItems`. The edge's
  `strip_config` does not cut lists to that length; a longer list is refused by core with 422,
  which the inventory reporter logs and retries next round. Cutting at the edge is a one-line
  change in `forward.py` for the next pass over that file.

## 4. `ingested_at` is bounded like `observed_at`

`check_range` applies the ADR 0036 range (`now - events retention - 1 day` to `now + 1 day`,
on core's clock) to `ingested_at` as well: an event outside it refuses the batch with the same
422 shape, entries typed `ingested_at_too_old` or `ingested_at_future`, at most
`MAX_RANGE_ERRORS` entries across both columns, no time echoed, counted in
`carto_ingest_out_of_range_total`. An edge whose clock runs more than a day ahead of core's is
a misconfiguration this surfaces at once rather than a version column it quietly poisons.

The right long-term fix is to make core's own `stored_at` (the writer's wall clock, migration
0007) the version column of `events`, so that no value from the body decides which copy of a
row survives a merge. That is a ClickHouse migration that recreates the table under a new
engine definition (ClickHouse migrations are never edited in place, ADR 0034) and belongs to
M6, or to the milestone in which the trace tables land, whichever comes first; this ADR records
it so the bound above is understood as the backstop it is.

## 5. Parked batches expire at the edge

`core.parked_max_age_days` (default 7, 1 to 365) on the edge's core link. The forwarder's loop
already runs periodic work (`refresh_gauges` every pass); it now also runs
`Forwarder.purge_parked`, which throttles itself to once per `PARKED_PURGE_SECONDS` (hourly,
like the runtime's housekeeping of ADR 0033) and runs at once on the first pass.
`DiskBuffer.purge_parked(older_than, now=...)` deletes the rows parked before the cutoff in one
committed transaction, returns the count and the batch id of the one parked longest ago, and
releases their bytes only once the delete is committed, as `ack` does; pending rows are never
touched. The pass counts `carto_edge_parked_purged_total` and logs one warning with the count,
the oldest batch id (a ULID) and the age, never a payload (spec 2.3 invariant 7). `runtime.py`
is not edited: the forwarder owns the buffer and already has a loop, so the hook lives there.

Parked rows are aged from when they were parked, not from when they were appended: a batch
that waited in the queue through a long core outage and was then refused gets its full
`parked_max_age_days` from the refusal, where aging it from its append time could purge it in
the same pass that parked it. `DiskBuffer.park(row_id, at=, reason=)` stamps `parked_at_ms`
with the forwarder's clock and `parked_reason` with the status (`http_422`; a label of at most
`MAX_PARKED_REASON_LEN` characters, never content). The buffer's schema is now versioned
(`PRAGMA user_version`, `SCHEMA_VERSION` 2; it was `CREATE TABLE IF NOT EXISTS` alone): a file
at version 0 that already holds `batches` is a version-1 file and gains the two columns and the
ledger table on open, in one transaction per version; its rows parked before the migration have
no `parked_at_ms` and are aged from `created_at_ms`, which is all that is known about them; a
file a newer edge wrote is refused with `BufferSchemaError` rather than read with the wrong
columns.

A purge is a data-loss event, and a log line and a counter are not a record of one. Each
purged batch leaves a row in the buffer's `purged` table, written in the purge's transaction:
`batch_id`, `events`, `parked_reason` and `purged_at_ms`, no payload. The table is cut to the
last `PURGED_LEDGER_ROWS` (10,000) rows by the same transaction, so it cannot grow with the
refusals it records; `DiskBuffer.purged_ledger(limit)` reads it newest first for an operator or
a later console view. At the defaults that is 10,000 batches of up to 5,000 events, the loss of
weeks of persistent refusal, on file across restarts.

## Alternatives

- A uvicorn-level body timeout: none exists for the body; `timeout_keep_alive` covers idle
  connections between requests, not a request that trickles.
- Treating 408 as a parking status at the edge: the batch is fine, the connection was not.
- A per-request memory accounting instead of a ratio: the ratio is one comparison per chunk
  against a number known before the first byte comes out.
- Checking the template ceiling inside the ledger's `done` transaction: exact, but after the
  ClickHouse write, so a refused batch would leave orphaned events behind.
- A lock around the template ceiling check: serialises every batch of a tenant on the hot
  path to remove an overshoot of at most `max_inflight_batches` batches' templates.
- `SELECT ... FOR UPDATE` on the tenant's `edge_inventories` rows for the edge ceiling: does
  not serialise two inserts of distinct new edges, and locks nothing for a tenant without rows.
- Keeping purged batch ids in the log only: the log rotates, the counter resets with the
  process, and neither says which batches went.
- Deleting old templates instead of refusing new ones: templates are referenced by fields,
  nodes and the console's names; a deletion policy is a product decision for M6.
- Expiring parked rows from `EdgeRuntime.housekeeping`: the runtime does not hold the buffer.

## Consequences

- A trickling client gets 408 after the deadline, the slot goes back into circulation and its
  connection is closed; eight of them no longer starve honest batches, 256 of them no longer
  hold every connection, and a wedged listener fails the readiness probe the stack now runs.
- An edge upgraded to this version migrates its buffer file on first open; an edge downgraded
  past it refuses the file (`BufferSchemaError`) instead of ignoring the columns it does not
  know. The operator restores the matching edge version or starts a new buffer.
- An operator whose edge sends batches that compress past 200x sees a ratio 413 and raises
  `max_decompression_ratio`; the measurements above say no real source needs that.
- A system at its template ceiling stops accepting new templates until an operator raises the
  ceiling or the templates are curated; the batches wait parked at the edge and expire after
  `parked_max_age_days`, so the ceiling is also a data-loss boundary after a week by default,
  which the warning per purge pass makes visible.
- An edge clock more than a day ahead of core's refuses every batch until it is fixed.
- The header phase of a request to `ingest-api` has no deadline (section 1): a client with
  the `edge-gateway` certificate that trickles headers holds one of the 256 connections for as
  long as it likes, and in the Compose stack no proxy's `client_header_timeout` stands in
  front. The next pass over `server.py` should set uvicorn's `timeout_keep_alive` low (it bounds
  the idle gap between requests on a kept-alive connection, which the edge's client reuses) and
  add a header deadline, either an h11-level timer in the protocol class or a proxy with
  `client_header_timeout` in front of the listener, as `proxy-webhooks` already has (10 s).
- The leak test stamps the simulator's files as having arrived on the pinned clock, since the
  upload connector takes a file's mtime as `received_at` and so as `ingested_at`.
- Deferred: `stored_at` as the version column (section 4, a ClickHouse migration); cutting
  config lists to `MAX_CONFIG_LIST_ITEMS` in the edge's `strip_config` (section 3).

## Tests

`core/tests/test_core_ingest_app.py` (a stalled ASGI receive on each internal route is 408
with `Connection: close` and frees the slot; a chunked body inside the deadline is accepted;
`/readyz` 503 once every slot outlives the deadline; ratio cap against the whole wire length
and the realistic batch; `ingested_at` bounds; the template and edge ceilings),
`test_core_ingest_mtls.py` (a raw TLS socket trickling a body to a real uvicorn gets the 408
and then end of stream), `test_core_settings.py` (defaults and ranges), `test_core_metadata.py`
(the in-memory ceilings, ids known per system, the statement shapes: `unnest` join driven by
the batch, no concatenation, the totals statement, the advisory lock; and the PostgreSQL
store against a recording engine, showing that a batch of known ids issues the key probes
only and a batch that would grow a system issues the total count as well), `test_core_m2_integration.py` (Docker: both ceilings on
PostgreSQL, the rollback of a refused inventory, the lock released with it),
`test_core_compose_limits.py` (the healthcheck probes `/readyz`),
`packages/carto-schema/tests/test_schema_inventory.py` (the list cap),
`edge/tests/test_edge_buffer.py` (purge and byte accounting, aging from the park time, the
version-1 file migrated on open, a newer file refused, the purged ledger and its bound),
`edge/tests/test_edge_forward.py` (the pass, its period, the loop, the log line, 408 retried
not parked, the status as the parked reason and the ledger row), `edge/tests/test_edge_leak.py`
(end to end through the real app).
