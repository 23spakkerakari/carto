# ADR 0064: Entity ids are stable across link passes, and entities carry a version

Status: accepted, 2026-11-02 (plan `docs/plans/M3.md` decision 12 and question 9, answered
2026-11-01; `docs/plans/M3-demo.md` decision D5; spec 7.3 `entities`, 9.6, 9.7)

## Context

Since ADR 0053 a family's id is a content hash of its member ends, rebuilt by every link pass
(`carto_core.linker.families`). Only the name carries over when a family grows. Transactions
(spec 9.7) need a stable entity reference: `transactions.entity_id` would dangle the first
time a reviewer accepts one more link. The entity's `max_span_days` (spec 7.3) must survive
the same growth. Two hazards existed in `PostgresEntityRepository.replace_all`: it stamped
`updated_at = now` on every entity on every pass, so the console's `updated_at`-derived ETag
made a reviewer's `PATCH /entities/{id}` answer 412 whenever a pass ran between the GET and
the PATCH (every five minutes), and its explicit column list would reset `max_span_days` on
growth.

## Decision

- `inherit_names` matches a previous entity to the family whose members contain all of its
  members (largest overlap first, each previous entity once, as before) and the matched family
  **inherits the previous entity's id** along with its name, description and confirmation. A
  family with no previous entity gets the content-hash id, which is therefore the id of its
  first appearance and never changes afterwards. A family that shrinks (a member leaves after
  a rejection) is a new family with a new id, as before; it keeps no name.
- `entities` gains `version` (bumped by reviewer patches only; migration 0005) and
  `max_span_days` (nullable; the assembler's default applies when null). The entity ETag is
  `W/"<version>"`, as `key_links` has, and `set_name` takes `expected_version`.
- `replace_all` leaves alone every row whose name fields and members did not change (no
  `updated_at`, `version` or `max_span_days` write); a row whose fields changed keeps its
  `version` and `max_span_days`.
- `FamilyBuilder.rebuild` re-reads the stored entities after the namer returns and keeps a
  confirmed name that landed meanwhile, so a slow LLM suggestion never reverts a reviewer.

## Consequences

- `transactions.entity_id` points at a row that outlives link passes. When a family shrinks,
  the transactions of the lost member's events re-point on their next assembly; the demo cut
  accepts the gap.
- `updated_at_version` stays for `systems` only.
