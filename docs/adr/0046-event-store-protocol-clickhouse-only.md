# ADR 0046: The engine reads the event store through one Protocol, implemented by ClickHouse only

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decisions 1 and 3; founder decision 3 of
2026-10-12; spec 7.2, 9.1 to 9.5, 18.1, 18.2)

## Context

The profiler, the linker and the composite matcher need a handful of queries over `events` and
`event_identifiers`: identifier rows after a watermark, the exact token join of two
`(field, form)` ends with first-seen times and fan-out, co-occurring ends per event, template
event counts, hourly volumes, and events with their keys for blocking. The engine's logic
(sketches, scoring, filters, families, Fellegi-Sunter weights) must be unit-tested without a
database; the queries must be proven against the real tables, which are `ReplacingMergeTree`
and hold duplicates until a merge runs (ADR 0034). A SQLite implementation for laptops without
Docker was proposed and declined: the founder fixes Docker instead, so there is no second SQL
dialect to keep equal.

## Decision

- `carto_core.store.EventStore` is a `Protocol` with the queries listed in
  `core/carto_core/store/model.py` and typed rows (`IdentifierRow`, `PairJoin`,
  `CooccurrenceRow`, `TemplateCount`, `HourlyVolume`, `EventKeys`).
- `carto_core.store.memory.InMemoryEventStore` is the reference semantics and the engine's unit
  test double. It is not a deployment option; nothing in a service constructs it.
- `carto_core.store.clickhouse.ClickHouseStore` is the one implementation services use.
- Semantics every implementation shares: an event counts once by `event_id` and an identifier
  row once by `(event_id, field_ref, form)`; a row's `stored_at` is the earliest copy's; windows
  are half-open on `observed_at`; quantiles are exact-low (the element at index
  `floor(level * (n - 1))` of the sorted values, ClickHouse `quantileExactLow`) so both
  implementations agree exactly; nothing returned is a raw value.
- `core/tests/test_core_store_contract.py` runs one case list against both implementations,
  the ClickHouse one in a container, including a batch loaded twice: every query must answer the
  same as for a single copy.
- ClickHouse migration 0007 adds `stored_at DateTime64(3, 'UTC')` to `events` and
  `event_identifiers` with `DEFAULT ingested_at`, and the writer sets it to core's wall clock at
  insert. The profiler's incremental pass is a watermark on `stored_at`: the edge's
  `ingested_at` can trail core's arrival by hours when the disk buffer backs up, so a watermark
  on it would miss late deliveries. Rows written before the migration read their `ingested_at`
  as `stored_at`, which only makes the first incremental pass after the upgrade scan more.

## Alternatives

- Query ClickHouse directly from the profiler and linker: faster to write, untestable without a
  container, and the join SQL would spread over three modules.
- A SQLite store proven equal by the contract suite: declined by the founder (two dialects for
  six queries).
- Watermark on `observed_at` with a lateness allowance: simpler, loses events later than the
  allowance until the nightly rebuild.

## Consequences

- Local eval runs need Docker; CI's integration job is where the ClickHouse queries are
  verified.
- A new engine query is added to the Protocol, the fake and the contract suite together, never
  to one of them.
- `IDENTIFIER_COLUMNS` and `EVENT_COLUMNS` of `carto_core.db.clickhouse` gain `stored_at`; the
  M1 integration tests that assert column lists change with them.
