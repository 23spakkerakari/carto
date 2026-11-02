# ADR 0056: Templates and field metadata reach PostgreSQL through ingest-api and the bundle loader

Status: accepted, 2026-10-17 (spec 7.2 "templates live in PostgreSQL", 7.3 `templates`,
`fields`, 9.9 heuristic labelling)

## Context

`template_text` is not a column of `events` (spec 7.2); the canonical event carries it, and
the bundle carries `templates.json` and `fields.json`. The reviewer's sentence, the heuristic
labeller and the LLM payloads need template text and field paths; the profiler needs the
field's class and policy from the edge.

## Decision

- `ingest-api` upserts the distinct `(system_id, template_id)` of every accepted batch into
  `templates` with the template text, kind, first and last seen and a count increment, in the
  same transaction as the ledger's `done` mark, so a retried batch never double-counts.
- `carto-core load-bundle` upserts `templates` from `templates.json` and `fields` (class,
  policy, pinned, forms, shapes, counts) from `fields.json` after the events are written.
- The profiler owns `fields.distinct_estimate`, `null_rate`, `shapes_json`, `quality` and the
  seen times; it never overwrites `class` and `policy`, which come from the edge (events'
  identifiers and the inventory's pins, ADR 0050).
- Template text is customer-influenced text: stored as sent, bounded by the contract, rendered
  as text only (spec 13), fenced as data in prompts (spec 9.9).

## Alternatives

- A `templates` table in ClickHouse: two stores for one row kind, and the console joins it with
  PostgreSQL rows on every page.

## Consequences

- Migration 0004 creates `templates` and `fields`; `ingest-api` gains a PostgreSQL write per
  batch (a few rows), which the compose job's throughput check covers.
