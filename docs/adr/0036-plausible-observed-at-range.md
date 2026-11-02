# ADR 0036: An event's observed_at must fall in a plausible range

Status: accepted by the founder, 2026-10-12 (spec 7.1, 7.2, 14.10, 2.3 invariant 8). Built on
2026-10-15: core in ADR 0043 sections 11 and 12, the edge in section 13 (the analyzer's
`--as-of`, chosen by the founder that day).

## Context

`events` and `event_identifiers` are partitioned by the day of `observed_at`. ClickHouse
refuses one insert that touches more than 100 partitions. A batch of up to 5,000 events can do
that when it carries a sparse backfill of old logs, or a timestamp parser misreads dates into
1970 or 2099. ingest-api answered 503, the edge retried forever, and every later batch waited
behind it. Events older than retention are also deleted by the table TTL as soon as they land,
so loading them does nothing.

## Decision

The plausible range is from `now - events retention - 1 day` to `now + 1 day`.

1. **Edge.** After timestamp parsing:
   - an event older than the range is dropped and counted in
     `carto_edge_events_out_of_range_total{reason="too_old"}`;
   - an event later than the range gets `observed_at = ingested_at` and
     `observed_at_quality = ingest`, and is counted with `reason="future"`. A future time is
     almost always a parsing error, and the event is still worth keeping.
   The offline analyzer applies the same rule against its run time, and the manifest reports
   both counts.
2. **Core.** ingest-api refuses an event outside the range with 422, so the edge parks the
   batch rather than retrying. The ClickHouse writer splits each insert into groups of at most
   90 partition days, and answers any other permanent ClickHouse error with 422.

## Alternatives

- Raise `max_partitions_per_insert_block`: hides the parser bugs this range catches.
- Clamp old events to the range start: invents a time and corrupts timing analysis.
- Drop future events: loses events a parser misread but that are otherwise good.

## Consequences

- A backfill deeper than retention loads nothing past retention; the install guide says so.
- `load-bundle` applies the core range at load time, so a bundle older than retention loads
  only its recent part and reports the rest.
