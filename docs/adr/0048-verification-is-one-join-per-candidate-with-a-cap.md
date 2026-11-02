# ADR 0048: Verification is one store join per candidate pair, capped per run

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 3; spec 9.2, 9.3, 17)

## Context

Spec 9.3 computes `n_match`, the lag distribution, fan-out both ways and the top-10
concentration from "an exact join on `event_identifiers` over the window". Spec 17 budgets a
full discovery pass over 7 days, 50 systems and 1,000 candidate fields at 30 minutes. LSH at
containment 0.2 can propose far more pairs than that budget verifies.

## Decision

- Each candidate is verified by one query (`EventStore.pair_join`) that returns every feature
  of the spec 9.3 table that comes from the join; `n_a` and `n_b` are exact distinct counts in
  the same query, not HyperLogLog estimates, because the join already touches both ends.
- `domain_size` for the chance ratio comes from the dominant shape of each end as spec 9.3
  describes (`9999` is `10^4`, `AA-9999999` is `26^2 x 10^7`); the smaller of the two ends'
  domains is used, because a match can only come from values both domains hold.
- Candidates are ranked by estimated containment (bottom-k, ADR 0047) and the run verifies at
  most `LinkerSettings.max_candidates_per_run` (default 5,000). Bridge pairs are always included
  before LSH pairs. Pairs beyond the cap wait for the next pass; a nightly full pass runs with
  the same cap, so the cap bounds time per pass, not coverage over days.
- A pair already decided (`accepted`, `rejected`) is re-verified for staleness and re-proposal
  but never counted against a new slot before undecided pairs.

## Alternatives

- Compute features from the sketches alone: no lag, no fan-out, no concentration; the spec's
  table cannot be filled.
- One query for all pairs at once: ClickHouse would join the whole table with itself; memory and
  time both unbounded.

## Consequences

- `make bench-linker` (measured, not gating) reports the pass time at the spec 17 scale with a
  synthetic sketch set.
- Raising the cap is a setting; the eval report records the cap in effect.
