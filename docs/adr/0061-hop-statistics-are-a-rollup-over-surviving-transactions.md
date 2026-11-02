# ADR 0061: Hop statistics are a rollup over surviving transactions, recomputed each graph pass

Status: accepted, 2026-11-02 (plan `docs/plans/M3-demo.md` decision D2; `docs/plans/M3.md`
decision 4; spec 7.2, 7.3, 9.8)

## Context

The M3 plan streams one row per consecutive node pair into `hop_transitions`, aggregates the
current day through a materialized view into an `AggregatingMergeTree` of DDSketch states and
rebuilds closed days nightly into a `ReplacingMergeTree`, so that 2,000 events/s never re-read
the window on every pass. The demo runs two simulator days. The map needs support, `p_next`,
latency quantiles and error counts per hop, the timeline needs the hop's p50 and p95, and the
settling sweep needs each entity's quiet span.

## Decision

- The graph pass reads the window's events under their latest assignment
  (`EventStore.ordered_transactions`: `argMax(txn_id, version)` per event, grouped by
  transaction, in spec 9.7 order) and computes, in memory, every consecutive pair's latency,
  the error flag and the receiving event's actor kind. It writes the `hops` rows
  (migration 0005: support, `p_next`, exact p50, p95 and p99, error count, the human actor
  share, the accepted link types between the two systems) as the tenant's whole set, keeping
  `is_manual_confirmed` of a row that already existed. `HopView` keeps the M3 plan's shape, so
  the hourly tables slot in underneath without an API change.
- Terminal nodes (spec 9.8) and the per-entity quiet span are computed over the settled
  transactions of the window; `entity_stats` (p99 inter-event gap, quiet span, settled count)
  is the table the settling sweep reads, and `GraphSettings.min_settled_for_terminals` (20) is
  the floor below which no node is terminal and the quiet span is the max span, because a
  handful of transactions says nothing about an entity's shape.
- `txn_merges.token_end` records the `field_ref/form` whose token caused a merge, never the
  token, for the review screen's "ambiguous merges" detail.
- `txn_events` is partitioned by month: the assembler's micro-batches span days, never a
  hundred partitions, and a month drop is cheap at retention.

## Alternatives

- Build decision 4 now: three ClickHouse tables, a materialized view and the nightly rebuild,
  none of which the video shows.

## Consequences

- Superseded in part when M3 proper builds decision 4; the rollup over the current day stays
  the shape the API reads.
- The pass re-reads the window every five minutes; at the Section 17 volume that is what
  decision 4 replaces.
