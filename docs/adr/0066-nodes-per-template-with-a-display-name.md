# ADR 0066: A flow-graph node is one template with a reviewer's display name; grouping waits

Status: accepted, 2026-11-02 (plan `docs/plans/M3.md` decision 5; `docs/plans/M3-demo.md`
decision D7; spec 3 glossary, 7.3 `nodes`, 9.8, 13)

## Context

Spec 9.8 makes a node `(system, template)` by default and lets reviewers group templates into
one named event type. A template's `template_text` is customer influenced (ADR 0056) and must
never be a display string; `templates.display_name` exists but nothing writes it.

## Decision

- `nodes` (migration 0005) holds one row per `(tenant_id, system_id, template_id)` with
  `id = system_id/template_id`, `display_name` (nullable; the console shows the template id
  and kind when null, never `template_text`), `event_type_label` (nullable, unused until
  grouping is built) and a real `version` for the ETag. The graph pass upserts rows it meets
  and leaves `display_name`, `event_type_label` and `version` alone.
- `PATCH /nodes/{node_id:path}` sets `display_name`, bumps `version`, and is audited with the
  names and version only (invariant 6).
- Grouping templates into an event type (the view-time mapping of M3 plan decision 5), flow
  variants and the stale and skew warnings are M3 proper.
- `transactions.current_node` and `txn_events.node_id` carry the node id, never the name, so a
  rename rewrites no membership row.

## Consequences

- The demo's map shows template nodes laid out by system; the dry run renames the nodes on
  the order's path before the take.
- `templates.display_name` stays unused; a later migration may drop it.
