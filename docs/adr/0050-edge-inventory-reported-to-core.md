# ADR 0050: The edge reports its inventory; core shows it read-only

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 8; founder decision 2 of
2026-10-12; spec 7.3 `systems`, `sources`, `fields`, 12, 13 Setup screens, 14.3)

## Context

The M1 edge reads its systems, sources and field policy pins from a file the admin writes; core
only ever saw heartbeats. The spec's Setup screens assume core knows the connectors. The
founder asked to "pull in more info for users as soon as we can" without opening a control path
from core to the edge.

## Decision

- `POST /internal/inventory` (mutual TLS, role `edge-gateway`) takes one
  `carto_schema.inventory.EdgeInventory`: systems; sources with type, enabled flag,
  configuration without secrets (`secret_ref` name only; the edge removes secret-bearing keys
  and flattens the rest to scalars and string lists), backfill window, schedule text and the
  last read-only test result; and the field policy pins. The edge sends it on start and on
  every change of its sources file (`edge_id` and the file's sha256 identify the copy).
- Core replaces its copy for that edge as a whole and upserts `systems`, `sources` (new columns
  `enabled`, `backfill_days`, `schedule`, `last_test`, `last_test_at`, `edge_id`,
  `inventory_sha256`) and the pinned `fields.policy`. Heartbeats keep carrying health.
- The console shows Systems, Setup > Sources (type, health, lag, last error, last test) and
  Setup > Field policies (class, policy, shapes, never values) read-only. Writing configuration
  from the console to the edge stays deferred to M6 planning: it is a new path across the trust
  boundary (spec 5.2).
- `config` values are untrusted text everywhere they are rendered (spec 13).

## Alternatives

- Core writes the sources file through an edge endpoint: the control plane; not decided.
- Keep heartbeats only: the first screen would show links between systems a user cannot see.

## Consequences

- The sources file stays the source of truth; a console edit is impossible until M6.
- The leak scan of the Compose job covers the inventory payload and the Sources API response.
