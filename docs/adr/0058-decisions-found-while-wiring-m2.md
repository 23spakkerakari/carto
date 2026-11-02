# ADR 0058: Decisions found while wiring the M2 packages together

Status: accepted, 2026-10-18 (plan `docs/plans/M2.md`, review pass; spec 9.3, 9.4, 9.5, 12,
14.2, 14.9)

The work packages of M2 were built in parallel against the wave 0 seams. Connecting them
settled a handful of questions the plan and the earlier ADRs left open. Each is small; they are
recorded together so the reviewer finds them in one place.

## 1. A human decision produces one audit row, written by the route

The linker's `ReviewService` and the console route both know how to audit a decision. The route
holds the request id, the client address and the user agent, which spec 14.9 wants in the row
and the service never receives. The API's `LinkPortAdapter` therefore gives its `ReviewService`
a no-op recorder and the route writes the row; transitions the linker makes on its own
(auto-accept, stale, recover, re-propose) are audited by the worker's `PostgresAuditLog`.
Proposals are engine output, not decisions, and are not audited. A test asserts exactly one
row per decision.

## 2. ETags of systems and entities derive from `updated_at`

`systems` and `entities` carry no version column; their `ETag` is `updated_at` in epoch
milliseconds and `PATCH` compares it inside the `UPDATE`. Two writes within one millisecond
would share a version; a version column in a later migration makes it exact. `key_links` has a
real `version` that every decision bumps.

## 3. Bridges are verified from co-occurrence, not from the token join

A bridge pairs two different identifiers of one event, so the token join of spec 9.3 is empty by
construction and the `n_match < 20` filter would discard every bridge. Bridge features come from
the `CooccurrenceRow`: `n_match` is the number of co-occurring events, `c_ab`/`c_ba` the share of
each end's distinct values seen together, `pairing_consistency` the functional-dependency share
of spec 9.4; the role is `transaction` when both directions are near one-to-one, the score is
`max(logistic, consistency)` for a transaction bridge at or above `bridge_consistency_min` and
`min(logistic, consistency)` otherwise.

## 4. Composite matching re-estimates `u` within the blocked pairs and constrains `m >= u`

With `u` held at the random-pair estimate, date-blocked near misses received a match-sized
boost on the date component and EM collapsed to "everything matches". The posterior that picks
the threshold uses `u` re-estimated within the blocks; the stored rule and the pair weights keep
the random-pair `u` as spec 9.5 words it, so M3 re-applies the same threshold. A component whose
`m` would fall below `u` is clamped, so disagreement never counts in favour of a match. The spec
9.3 `min_match` filter applies to composite rules too.

## 5. Composite matching also runs beside a partial exact link

Spec 9.5 tries composite matching "when no exact link scores above 0.5". Scenario C's HRIS
carries the candidate id in 60% of rows and the rest is keyed by hand, so an exact link exists
and covers too little. `LinkerSettings.composite_coverage_floor` (0.8) adds the second trigger:
the best exact or bridge link covers less than that share of the later end. A strong exact link
with high coverage still suppresses composite matching for that pair (scenario C's HRIS to
payroll link C06 at 98% hides the composite C08), which bounds composite recall on that
scenario; whether composite matching should also run beside strong links is a founder question
for M3.

## 6. Dates from day-first exports

The edge's slashed-date parser reads `a/b/yyyy` month-first unless `a > 12` (scenario C's
legacy onboarding system writes `%d/%m/%Y`), so date tokens of days 1 to 12 disagree across
systems. Left as a known hazard for the gate and recorded here; a per-source date-order hint is
an edge change for M3.

## 7. Windows are day-aligned

The profiler reads `[start of (today - window_days + 1), start of (today + 2))` rather than
`[now - window_days, now + 1 day)`, so day sketches cover whole days and a full pass equals the
incremental passes (a property test holds it). The linker uses the same window.

## 8. The concentration feature is the excess over the flat baseline

Spec 9.3's `concentration` is the share of matches the ten most matched tokens hold. With `n`
matched tokens of fan-out one that share is `10 / n` even when every token matches exactly once,
so a sparse but clean link reads as concentrated. The hard filter keeps the literal definition
(`concentration > 0.5` discards); the scoring feature is the excess over `10 / n`, rescaled to
`[0, 1]` (`score.concentration_excess`).

## 9. Composite matching tries both orderings of a system pair

A clerk keys payments into purchase orders within hours, so the rolling-day correlation peaks of
the two orderings are nearly equal and noise picked the wrong one (shop L06 blocked 110 pairs
instead of 4,135). Both orderings are now matched and one rule stands per system pair, the one
with more matched pairs, then the higher score; the other ordering is reported with its stage.

## Consequences

- The reviewer reads one audit row per decision and finds linker-made transitions under the
  actor `linker`.
- `make eval` reports composite recall against the trigger of item 5; the M3 plan picks up the
  founder question and the date-order hint.
