# ADR 0034: ingest-api claims a batch before writing it; event tables replace duplicates

Status: accepted by the founder, 2026-10-12 (spec 7.2, 8.5). Amends ADR 0015. Built on
2026-10-15; ADR 0043 sections 10 and 12 record how, and where the build departs from it.

## Context

ADR 0015 checks the ledger, writes to ClickHouse, then records the batch. The review found:

- A retry that arrives while the first delivery is still writing finds no ledger row and is
  written again. The edge times out at 30 seconds, so a slow write invites exactly that.
- A record re-read after an edge crash arrives in a new batch. Spec 8.5 promises idempotency
  by `event_id`, but core never deduplicates by it.
- ADR 0015's remedy, `FINAL`, does nothing on a plain `MergeTree`.
- The ledger is never pruned.

## Decision

1. **Claim first.** `ingest_batches` gains `state` (`pending` or `done`) and `claimed_at`.
   ingest-api inserts a `pending` row with `ON CONFLICT DO NOTHING RETURNING`:
   - a new claim writes the batch, then marks it `done`;
   - a `done` row is a duplicate and is acknowledged without writing;
   - a fresh `pending` row (younger than the claim timeout, default 120 s) is answered 503 with
     `Retry-After`, so the edge retries later;
   - a stale `pending` row is taken over and the batch is written.
2. **Replacing tables.** `events` and `event_identifiers` use
   `ReplacingMergeTree(ingested_at)`. A row is a duplicate only when its whole sort key matches,
   so the sort keys carry the event:
   - `events`: `(tenant_id, system_id, template_id, observed_at, event_id)`, unchanged;
   - `event_identifiers`: `(tenant_id, token, observed_at, event_id, field_ref, form)`. Without
     `event_id`, `field_ref` and `form`, two events sharing a token in the same millisecond
     would merge into one row.
3. **Reads.** Duplicates disappear at background merges. Queries that need exact counts (the
   detector, M4) read with `FINAL`; the linker counts distinct tokens and does not need it.
4. **Pruning.** A housekeeping task deletes ledger rows older than event retention plus one day.

## Alternatives

- Deduplicate in every query (`LIMIT 1 BY event_id`): every M2 to M5 query pays for it, and
  one forgotten query double counts.
- Keep `MergeTree` and rely on the ledger alone: cursor replays still duplicate.

## Consequences

- Inserts cost the same as before. Exact reads pay `FINAL` at query time.
- A replayed record that lands on a different template (Drain3 generalized in between) has a
  different sort key and is not merged. The linker's distinct counts make this harmless; it is
  recorded as a known residual.
- Migrations add new files: the existing tables are recreated under the new engine, since M1
  has no installation to preserve (ClickHouse migrations are never edited in place).
