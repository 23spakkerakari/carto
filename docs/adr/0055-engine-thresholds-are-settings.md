# ADR 0055: Every spec 9.x threshold is a setting with the spec default

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 13; spec 9 "Every threshold
below is a config value with the stated default; the eval harness is how defaults get tuned")

## Decision

`ProfilerSettings` and `LinkerSettings` in `core/carto_core/settings.py` carry every number of
spec 9.1 to 9.6 (window days, cadence, permutations, bottom-k, `q` floors, containment
threshold, candidate cap, hard filters, direction share, fan-out limits, bridge consistency,
proposal and auto-accept thresholds, re-proposal delta, stale drop share, composite window,
target precision) with the spec default and a bounded range. Eval tuning changes a setting or
a weights file (ADR 0049), never code. The eval report prints the settings in effect.

## Consequences

- A threshold that appears as a literal in engine code is a review finding.
- Settings are frozen at process start; a change needs a restart, as for every service.
