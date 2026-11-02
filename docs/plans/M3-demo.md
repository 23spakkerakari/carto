# M3 demo cut: trace, on camera

Status: drafted 2026-11-02 on branch `m3-demo`, cut from `m3-trace-spec` at e12b626 (which
stacks on `spec-data-layer` and the rebased `m2-map-discovery`; `fix-c2` is not merged and the
M2 branch is not force-pushed yet, both deferred until after the recording).

This plan is a strict subset of `docs/plans/M3.md`. Every decision of that plan stands; this
file names which of its packages are built now, which are cut for the video, and the handful
of decisions the cut itself adds. The full M3 later is a superset: nothing built here is thrown
away, and the section "Left for M3 proper" is the list to pick up. Read the M3 plan's
"What the code read changed" table before touching anything; it still applies.

## Goal

A recorded demo of the one-line pitch on the simulator's shop scenario, running on the Compose
stack with the real edge, core, worker and console:

1. **Map.** The engine finds how five systems pass an order along and a reviewer confirms the
   links in business language. Then the system graph.
2. **Trace.** Type `4471` (or the cart id, the merchant reference, the PO number, the shipment
   number) and see the order's path across all five systems as a timeline.
3. **Where a person is still doing the talking.** The Payments to Warehouse hop carries a
   "Likely manual" badge: no shared identifier, matched on date, amount and name, 30% of the
   rows typed by clerks in business hours.

Alerts (M4) and the cost table (M5) are narrated, not shown.

## Storyboard

| Beat | Screen | What is on screen | Needs |
| --- | --- | --- | --- |
| 0 | terminal | `make dev`; the simulator replays two days of the shop at 60x; the edge tokenizes and forwards | exists (M1, M2) |
| 1 | Map › Review | the proposal queue: "92% of Warehouse POs match an Order within 2 hours; the match uses the digits inside the order reference"; accept the exact and bridge links, then the composite Payments to Warehouse proposal, which the engine never accepts on its own | exists (M2); accept enqueues a full assembly (decision D3 below) |
| 2 | Map › Entities | the family named "Order" | exists (M2) |
| 3 | Map | the graph: Webstore, Order system, Payments, Warehouse, Shipping as columns; template nodes renamed "Order created", "PO created"; hops with volume and median lag; the Payments to Warehouse hop badged "Likely manual" | packages C, E, F |
| 4 | Trace | search `4471`: one transaction, status completed, five systems; the timeline with each hop's time and lag; "matched on the digits form" | packages B, D, E, F, H |
| 5 | Trace | search `c-88213`, then `SH-5521`: the same transaction found from either end | same |
| 6 | Trace, Details | identifiers shown as tokens; narration: values never leave the customer's network, a permitted user reveals with step-up (M3 proper) | package E (identifiers in the view) |
| 7 | narration | the nightly file that does not arrive and the alert it raises (M4); what the clerk hop costs (M5) | nothing |

## What exists today

M2's demo note in `docs/plans/M2.md` lists it: the edge inventory and ingest, the profiler and
linker running in the worker every five minutes, the review queue and decisions with the audit
log, entities, systems, sources and field policies screens, OIDC through Dex in the dev
profile, the Compose stack with the simulator in live mode (`shop`, 2 days, 60x: about 48
minutes of wall clock for the full replay; links appear after the first profile and link pass).

## What is built (the kept packages)

Package letters are the M3 plan's. "Lite" means the subset named.

### A-lite. Schema, migrations, store, index, settings (wave 0)

- PostgreSQL migration 0005 with the M3 plan's grant block, holding the tables the demo
  needs: `transactions` (with `merged_into`), `txn_token_index`, `txn_token_fanout`,
  `txn_merges`, `batch_keys` (batch-role ends, so they never merge), `nodes` (with `version`),
  `terminal_nodes`, `hops`; `entities.max_span_days` and `entities.version`. Not now:
  `batches`, `batch_txns`, `composite_matches`, `flow_variants`, `system_clock_offsets`,
  `search_counters` (each listed under "Left for M3 proper"; a later migration adds them).
- ClickHouse 0009 `txn_events` (spec 7.2 plus `stored_at`) only. No `hop_transitions`, no
  hourly tables (decision D2).
- Store Protocol: `assembly_rows`, `transaction_events`, `events_with_token`,
  `append_txn_events`, `ordered_transactions(window)` (for the rollup), in `ClickHouseStore`
  and `InMemoryEventStore` with the contract suite covering each, including a moved event
  (two `txn_events` versions, `argMax(txn_id, version)` on every event-first reader).
- `AssemblyIndex` Protocol with `InMemoryAssemblyIndex` and `PostgresAssemblyIndex`, the
  per-tenant `pg_try_advisory_xact_lock`, and the shared contract suite.
- The M2 fixes the assembler depends on (M3 decision 12, question 9): stable entity ids (the
  family that inherits the name inherits the id), `entities.version`, `replace_all` leaving
  `version`, `updated_at` and `max_span_days` alone on unchanged rows, the namer's re-read
  before its upsert, the try-lock around the profile and link passes, the worker docstring.
- `JobKind.ASSEMBLE`, `JobKind.GRAPH`, `WorkerSettings.assemble_interval_seconds`,
  `AssemblerSettings`, `GraphSettings` (every number a setting, M3 decision 13),
  `ApiSettings.edge`, `ApiSettings.search_per_user_per_minute`.
- `carto_schema.api`: `TraceSummary`, `TraceView`, `TraceEventView`, `IdentifierView`,
  `SearchResponse` (with `matched_forms`, never tokens), `GraphView`, `NodeView`, `HopView`
  (with `manual_signals`, decision D4), `NodePatch`, `Problem.type`, `Permission.SEARCH`.
  `BatchView`, `FlowVariantView`, `RevealCall`, `RevealResult` and `LinkDecision.direction`
  are added in M3 proper. Committed JSON Schemas regenerated.

### B-lite. Assembler

- `keys.py`: key ends from accepted and auto-accepted exact and bridge links of transaction
  role; `union_find.py` with merge-into-oldest and the refusal counters on
  `key_links.ambiguous_merges`; `batches.py` reduced to batch-role ends never merging and the
  daily fanout rule removing a hub token from the index (no `batches` row yet); `pass_.py`:
  watermark, micro-batch, write order PostgreSQL then ClickHouse then the watermark, expiry,
  status (`in_progress`, `completed`, `errored`; M3 decision 12), ordering by `observed_at`
  with ties by `ingested_at` then `event_id` (no clock offsets), late events, and `full`
  reassembly over the read window (needed by decision D3).
- Property tests: no event lost or duplicated; order independence when no guardrail fires;
  deterministic refusals otherwise; a batch key never merges; expiry exact; a late event joins
  and re-evaluates status; `full` equals incremental; a repeated micro-batch changes nothing.
- Coverage at least 85% on `core/carto_core/assembler`.

### C-lite. Graph

- `nodes.py`: a `nodes` row per `(system_id, template_id)` with `display_name` defaulting to
  the template id and kind, never `template_text` (M3 decision 5; no event-type grouping yet).
- `rollup.py`: on every `GRAPH` pass, `hops` from the ordered surviving transactions of the
  read window (support, `p_next`, p50, p95, p99 latency, error count, the human actor share
  of receiving events, the link types between the two systems), terminal nodes per entity
  over settled transactions (spec 9.8); `GraphPass` for the worker. Decision D2 says why
  there is no hourly table.
- Property tests: `p_next` of a node's successors sums to at most one; always-last nodes are
  terminal and never-last nodes are not; hop support equals the transition count.

### D-lite. Signer, edge client, search

- `carto-ctl key init` writes the Ed25519 assertion key (wrapped by the local KMS of ADR 0025)
  and the public key file for the edge; `carto_core.signer.AssertionSigner` with a fresh
  nonce and a 60 s lifetime; `InternalAssertion` gains `key_id` and `request_hash` (M3
  decision 18, the part the tokenize path needs; the previous-key file and the audit-file
  rebuild are M3 proper).
- `EdgeClient` implementing `EdgeTransport` with `edge_id` on every call (M3 decision 7):
  `httpx` over mutual TLS with the pinned CA, 5 s timeout, no redirects; 429 passed through
  with `Retry-After`, other edge errors 502 without the body, 503 `search_unavailable` when
  no edge is configured. Tokenize only; reveal is M3 proper.
- `GET /traces/search?q=`: NFKC and trim, 1 to 256 characters, `tokenize` then `find` (M3
  decision 17), `events_with_token` over the retention window, transactions through
  `txn_events` with `argMax(txn_id, version)`, the forms that matched, core's per-user burst
  cap of 10 per minute (M3 decision 8; the `search_counters` table is M4's and is not written).
- `Problem.type` and `problem_response` (M3 decision 19).

### E-lite. API

- `GET /traces/{txn_id}` (303 for a merged-away id), `GET /graph`, `PATCH /nodes/{node_id:path}`
  with `ETag` and `If-Match` on `nodes.version`, audited with names and version only;
  `view` for reads, `review_links` for node naming. No `GET /batches/{id}`, no reveal route,
  no `LinkDecision.direction`.
- The authorization matrix regenerated from OpenAPI: search and the trace reads for every
  role that holds `view`, the node patch for `review_links`, 503 for search when no edge is
  configured; the business-viewer scoping row recorded as deferred (question 13).

### F-lite. Web

- Map screen: `@xyflow/react` (MIT, the M3 plan's dependency) laid out by system column,
  template nodes inside, hops with volume and median lag, hop detail with the quantiles and
  recent transactions, the "Likely manual" badge with its two signals in plain language, the
  node rename dialog (display name only). No grouping, no variants, no stale or skew warnings.
- Trace screen: the search box (also on the shell header as "Where is my ___?"; the query
  never goes into an `href`), results, the timeline with node, system, time, latency from the
  previous node, status, current location, "matched on `digits`" explanation, identifiers as
  tokens behind Details. No band, no batches, no expired marking, no Reveal button.
- The contract chain: regenerated `core/openapi/openapi.v1.json`, `npm run gen:api`,
  `types.ts` and `contract.ts` entries, msw handlers for search, traces, graph and nodes, XSS
  fixtures for every new string field (node names, map labels, attribute values, the search
  echo), `Untrusted` for every customer string, axe on both screens, Vitest for the timeline
  ordering, the Playwright review-to-trace journey against the mocked API, a 390 x 844
  project for search and the timeline.

### G-lite. Eval

- `discover` runs assemble and graph after link and writes `txn_membership.ndjson` and
  `graph.json`; the driver inverts the locator map to locator keys per event and fills the
  membership prediction so `transaction_pairwise_f1` scores; the report shows the F1 row on
  scenario A (`make eval-engine SCENARIO=shop`). Batch scoring, scenario C, the CI history
  restore and the delta comment are M3 proper. CLAUDE.md's rule holds: the assembler is an
  engine change, so the F1 and the M2 link metrics before and after are reported in the PR.

### H-lite. Edge

- Tokenize returns tokens labelled by form (`tokens: list[{form, token, key_version}]`), the
  `key_id` and `request_hash` checks, `RevealSettings.queries_per_user_per_hour` (600) applied
  to tokenize queries instead of the reveal quota (M3 decision 8); the gateway test gains a
  round trip with a core-signed assertion. No `date_order`, no simulator changes.

### I-lite. Compose

- `api` gets the edge URL, CA and client certificate (already issued by `carto-ctl pki init`
  as the `api` role); `carto-ctl key init` output mounted into `edge-gateway` as
  `assertion-public.key`; the worker runs `assemble` every 30 s and `graph` every five
  minutes; `README` of `deploy/compose` gains the two steps. No CI browser step against the
  stack; the recording is the manual run of journey 4.4.

## Decisions the cut adds (ADRs)

ADRs start at 0060 as the M3 plan says. The first records this cut; the rest are the M3
plan's decisions as built, each marked with what the demo leaves out.

- **D1. The demo cut is a milestone branch of its own, inside M3.** `docs/plans/M3-demo.md`
  is the plan, `m3-demo` the branch; its PR targets `m3-trace-spec`, and M3 proper continues
  on top of it. ADR 0060.
- **D2. Hop statistics are a rollup over surviving transactions, recomputed each graph pass.**
  The M3 plan streams `hop_transitions` into an `AggregatingMergeTree` and rebuilds closed
  days nightly (decision 4) so 2,000 events/s do not re-read the window each pass. The demo
  runs two simulator days; the rollup reads `txn_events` for the window on every pass and
  writes the `hops` rows the API reads. `HopView` keeps the shape, so the hourly tables slot
  in underneath later. ADR 0061, superseded in part when decision 4 is built.
- **D3. Accepting a transaction link enqueues a full assembly over the read window.** The
  assembler keys events as they arrive; events stored before a link was accepted carry no key
  from it. Spec 4.3 then 4.4 (accept, then trace) needs those events assembled without waiting
  for the nightly `full` pass, so the accept, reject and association routes enqueue
  `assemble` with `payload['full']` (deduped by the queue), and the console's review screen
  says "transactions are being rebuilt" from the job's state. This is not a demo shortcut; it
  belongs in M3 proper. ADR 0062.
- **D4. The "Likely manual" badge is a preview from two of spec 11.1's six signals.** Link
  type (the two systems are joined by an accepted composite link and no accepted exact link)
  and actor (the share of receiving events with `actor_kind = human`). `HopView.manual_signals`
  lists them in plain language and `is_manual_score` stays reserved for M5, which replaces the
  preview with the weighted score, the confirmation UI and the cost inputs. The badge shows
  when the link signal holds or the human share is at least 0.7; on scenario A the Payments to
  Warehouse hop shows it on the link signal with "30% typed by people". ADR 0063.
- **D5. Entity ids are stable** (M3 decision 12 as answered by question 9). ADR 0064.
- **D6. One edge through `EdgeTransport`, search audited at the edge and capped in core**
  (M3 decisions 7, 8, 17 and the tokenize half of 18). ADR 0065.
- **D7. Nodes per template with a display name; grouping deferred** (M3 decision 5). ADR 0066.
- The composite merge-key ADR (M3 decision 1) is written when `composite_matches` is built in
  M3 proper, not now: on scenario A every Warehouse row carries `order_ref`, so the warehouse
  event joins its transaction through the exact link and only the 2% clerk typos (0.6% of
  transactions) would need the composite pair. Scenario C needs it; the demo does not.

## Left for M3 proper (nothing here is dropped)

- Decision 1: `composite_matches` and composite pairs as merge keys; scenario C's F1.
- Decision 3's `batches`, `batch_txns`, `BatchView`, `GET /batches/{id}`, batches on the
  timeline; batch scoring in the eval.
- Decision 4: `hop_transitions`, the hourly aggregating table, the materialized view, the
  nightly rebuilt table, `GraphPass` rebuild; supersedes D2 in part.
- Decision 5's event-type grouping (`event_type_label`, `id = system_id/~label`); flow
  variants; stale and skew warnings on the map.
- Decision 6 and question 4: clock offsets, `system_clock_offsets`, `LinkDecision.direction`,
  the synthetic skew scenario and scenario A's reverse hop (package H).
- Decisions 9, 10, 11: step-up, the reveal grant setters, `POST /reveal`, the Reveal button,
  the 60 s auto-hide, the reveal runbook and install docs.
- Decision 14: the eval's batch resolution, CI history restore, regression enforcement, the
  delta comment; scenario C in the engine eval.
- Decision 15 and question 12: `make load`, k6, the reference-node run, the CI load smoke.
- Decision 18's previous public key and the nonce and quota rebuild from the audit file;
  question 11's `authenticated_at` claim.
- Decision 20: journey 4.4 in the compose CI job.
- `search_counters` (M4's self-alert input), `date_order` per source, scenario C's batch, the
  threat-model rows 3 and 8 test names, the leak scan over the reveal path.
- The `fix-c2` merge, the M2 force push, the PR of `m1-review-fixes` to `main`, and the
  2026-11-01 security findings (none on the demo path; the pip-audit red is expected in CI).

## Order of work

1. This plan; ADR 0060; `@xyflow/react` added with its licence line.
2. Wave 0: A-lite with ADRs 0064 and 0066 (stable entity ids and nodes land before the
   assembler reads the tables).
3. Wave 1 in parallel: B-lite, C-lite (against the in-memory store and index), D-lite and
   H-lite (against the edge's in-process app with a test keypair), F-lite's scaffolding
   (dependency, routes, msw handlers from the committed schema).
4. Wave 2: E-lite (needs B, C, D), G-lite (needs B, C), F-lite screens (needs E's OpenAPI
   document), I-lite; ADRs 0061, 0062, 0063, 0065.
5. `make check`, `make web-check`, `make eval-engine SCENARIO=shop` with the deltas, push,
   read CI (the pip-audit job is red for the known advisory; every other job must be green).
6. Dry run of the storyboard on the Compose stack with a clean `make dev-down`; fix what the
   stack shows; record.

## Tests that must exist

- Coverage at least 85% on `core/carto_core/assembler` and `core/carto_core/graph`.
- Store and index contract suites on both implementations; the integration job covers the
  PostgreSQL index under a duplicated micro-batch and a crash between the ClickHouse write
  and the watermark, `txn_events` versions, and `carto_app` holding no INSERT on the new
  tables; a reviewer patch on an entity succeeds after a link pass that did not change it.
- The property tests under B-lite and C-lite; the eval self-check stays perfect (truth as
  prediction scores F1 1.0).
- The authorization matrix with the new routes; search rate limit (core 429, edge 429 passed
  through); one audit row per node patch, never carrying `template_text`; the assertion round
  trip with a replayed nonce refused; `key_id` and `request_hash` mismatch refused; D3's
  enqueue on every decision route, deduped.
- Leak test over scenario A extended to `GET /traces/*` and `GET /graph` and node names
  (invariant: no raw value in core; the trace view carries tokens, never values).
- XSS corpus on the timeline, the map and the rename dialog; axe on both screens; Playwright
  review-to-trace journey against the mocked API, at 1280 and at 390 x 844.

## Acceptance (for the recording, not spec 21)

| Shown | How |
| --- | --- |
| Every link of scenario A's truth that the M2 eval finds is accepted on camera and the family is "Order" | beat 1 and 2 on the stack |
| The graph shows five systems and the Payments to Warehouse badge | beat 3 |
| `4471`, `c-88213`, `X9-0442`, `88-210` and `SH-5521` each return the same transaction with five systems | beats 4 and 5; the Playwright journey proves it on the mocked API, the dry run on the stack |
| Transaction pairwise F1 on scenario A reported with the M2 link metrics unchanged | `make eval-engine SCENARIO=shop`, in the PR |
| `make check`, `make web-check` green locally; CI green except the known pip-audit advisory | before recording |

Spec 21's M3 acceptance (F1 on C, batch targets, p95 at the Section 17 volume) is M3 proper's.

## Risks

- **Timing on camera.** The simulator at 60x needs several minutes before the first link
  pass has enough rows; the dry run fixes the moment to start recording. The assembler needs
  accepted links; D3 makes the rebuild after acceptance a matter of seconds on two days of
  data, and the review screen says when it is done.
- **Node names.** Default display names are template ids. The rename dialog exists so beat 3
  shows business language; the dry run renames the ten or so nodes on the path before the
  take, which is honest (a reviewer did it) and fast.
- **The clerk hop.** The badge depends on the composite proposal appearing and being accepted
  in beat 1. M2's eval finds L06 on scenario A; if a two-day replay lacks the rows for the
  composite trigger, the replay runs longer (`--days 3`), not the thresholds.
- **Search quota.** The edge's tokenize quota today is the reveal quota of 100 per hour;
  H-lite raises it before the dry run, otherwise a rehearsal exhausts it.
