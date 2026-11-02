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

The in-flight bound of ADR 0043 section 2 is now an `InflightSlots` object instead of a bare
`BoundedSemaphore`: it is still only ever tried, never waited on, but it remembers when each
slot was taken. `/readyz` answers 503 (`{"status": "unavailable"}`, no detail, spec 12) when
every slot is held and none has been held for less than the body timeout. The bound is
exclusive: a slot held exactly the timeout is not yet stalled. A slot legitimately held that
long by every request at once means the listener cannot admit anything honest, which is what
the orchestrator's probe should see. uvicorn's configuration is unchanged; a comment in
`server.py` says the app owns the deadline.

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
  claim, `MetadataStore.templates_over_ceiling` tells `ingest-api` in one grouped query (per
  system of the batch: rows held, and how many of the batch's `system/template` pairs are
  among them) whether any system would hold more than the ceiling once the batch's templates
  were upserted. A batch over it is a 422 `too_many_templates` whose detail names the system id
  only; the edge parks it, which is the fail-closed outcome; nothing is claimed or written;
  `carto_ingest_templates_refused_total` counts it. Ids a system already holds add nothing,
  so a full system keeps accepting its known templates. A failed count is a 503, never an open
  door. The check runs before the claim rather than inside the ledger's `done` transaction so
  that a refused batch leaves no events in ClickHouse; the price is a race between concurrent
  batches of one system, bounded by `max_inflight_batches` times the distinct templates of a
  batch, which the ceiling's size makes immaterial.
- `ingest.max_edges_per_tenant` (default 64, 1 to 10,000). `replace_inventory` counts the
  tenant's `edge_inventories` rows first in its transaction and refuses an `edge_id` the tenant
  has not recorded yet once it holds that many, with `MetadataLimitError`, which `ingest-api`
  answers as 422 `too_many_edges` and counts in `carto_ingest_edges_refused_total`. The raise
  rolls the transaction back, so none of that inventory's systems or sources is written. A
  known edge always replaces its copy.
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
`DiskBuffer.purge_parked(older_than)` deletes the parked rows created before the cutoff in one
committed transaction, returns the count and the batch id of the oldest one, and releases
their bytes only once the delete is committed, as `ack` does; pending rows are never touched.
The pass counts `carto_edge_parked_purged_total` and logs one warning with the count, the
oldest batch id (a ULID) and the age, never a payload (spec 2.3 invariant 7). `runtime.py` is
not edited: the forwarder owns the buffer and already has a loop, so the hook lives there.

## Alternatives

- A uvicorn-level body timeout: none exists for the body; `timeout_keep_alive` covers idle
  connections between requests, not a request that trickles.
- Treating 408 as a parking status at the edge: the batch is fine, the connection was not.
- A per-request memory accounting instead of a ratio: the ratio is one comparison per chunk
  against a number known before the first byte comes out.
- Checking the template ceiling inside the ledger's `done` transaction: exact, but after the
  ClickHouse write, so a refused batch would leave orphaned events behind.
- Deleting old templates instead of refusing new ones: templates are referenced by fields,
  nodes and the console's names; a deletion policy is a product decision for M6.
- Expiring parked rows from `EdgeRuntime.housekeeping`: the runtime does not hold the buffer.

## Consequences

- A trickling client gets 408 after the deadline and the slot back in circulation; eight of
  them no longer starve honest batches, and a wedged listener fails its readiness probe.
- An operator whose edge sends batches that compress past 200x sees a ratio 413 and raises
  `max_decompression_ratio`; the measurements above say no real source needs that.
- A system at its template ceiling stops accepting new templates until an operator raises the
  ceiling or the templates are curated; the batches wait parked at the edge and expire after
  `parked_max_age_days`, so the ceiling is also a data-loss boundary after a week by default,
  which the warning per purge pass makes visible.
- An edge clock more than a day ahead of core's refuses every batch until it is fixed.
- The leak test stamps the simulator's files as having arrived on the pinned clock, since the
  upload connector takes a file's mtime as `received_at` and so as `ingested_at`.
- Deferred: `stored_at` as the version column (section 4, a ClickHouse migration); cutting
  config lists to `MAX_CONFIG_LIST_ITEMS` in the edge's `strip_config` (section 3).

## Tests

`core/tests/test_core_ingest_app.py` (a stalled ASGI receive on each internal route is 408 and
frees the slot; `/readyz` 503 once every slot outlives the deadline; ratio cap against the
whole wire length and the realistic batch; `ingested_at` bounds; the template and edge
ceilings), `test_core_settings.py` (defaults and ranges), `test_core_metadata.py` (the
in-memory ceilings, the statement shapes), `packages/carto-schema/tests/test_schema_inventory.py`
(the list cap), `edge/tests/test_edge_buffer.py` (purge and byte accounting),
`edge/tests/test_edge_forward.py` (the pass, its period, the loop, the log line),
`edge/tests/test_edge_leak.py` (end to end through the real app).
