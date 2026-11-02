# carto

Source of truth: docs/SPEC.md. Read it fully before starting work.

## Rules

- Section 2.3 of the spec lists non-negotiable security invariants. Never weaken one.
  If a task seems to need it, stop and ask.
- Build milestone by milestone (spec Section 21). Write docs/plans/M<n>.md first,
  then tests, then code. Don't start the next milestone until acceptance passes.
- Record deviations and new decisions as ADRs in docs/adr/.
- Treat logs, files, connector responses, configs and LLM output as untrusted input.
- Never use real customer data in tests. Use the simulator.
- Engine changes (linker, assembler, detector, manual hops) must run `make eval`
  and report metric deltas in the PR.
- New dependencies: maintained, permissive license; justify in the PR.
- Architectural questions go to the founder before code is written; do not assume.

## Layout (spec Section 20, ADR 0008)

uv workspace. Each member holds one importable package directly inside it:
`packages/carto-schema/carto_schema`, `packages/carto-common/carto_common`, `edge/carto_edge`,
`core/carto_core`, `simulator/carto_simulator`, `eval/carto_eval`, `tools/carto-ctl/carto_ctl`.
Tests live in `<member>/tests/` with file names that are unique across the workspace and no
`conftest.py` duplicates (mypy checks everything in one run).

`legacy/self-writing-integrations/` is the previous product, frozen (ADR 0005). Never modify,
import, lint or test it.

## Commands

- make setup      # uv sync --locked --all-packages
- make check      # lint + type + test + schema-check (what CI's python job runs)
- make sim SCENARIO=shop DAYS=14   # simulator batch mode with ground truth
- make eval SCENARIO=shop          # eval harness report
- make sec        # scanners available locally (CI runs the full set)
- make dev        # Compose stack + simulator live mode (M1)
- On Windows without uv on PATH: make UV="py -3.11 -m uv" check

## Milestone status

- M0 Foundations: built on branch `m0-foundations` on 2026-10-06 (docs/plans/M0.md); on `main`.
- M1 Edge pipeline and offline analyzer: built on branch `m1-edge-pipeline` on 2026-10-10
  (docs/plans/M1.md, ADRs 0011 to 0032); merged to `main` on 2026-10-12 (PR #1). Throughput met
  on the laptop's full gateway path (ADR 0028). Acceptance waits on the founder's reference-node
  run and on the review fixes: branch `m1-review-fixes` (docs/plans/M1-review-fixes.md, ADRs
  0033 to 0043), all six packages and `fix-e2` merged into it and CI green on 2026-10-15; its PR
  to `main` is still to be opened. Items each ADR lists under Consequences as not built remain.
- M2 Map discovery: built on branch `m2-map-discovery` on 2026-10-17 and 2026-10-18
  (docs/plans/M2.md, ADRs 0046 to 0058): profiler, linker, composite links, console API and web
  console, worker, LLM gateway, scenario C, eval driver, discover and gate report. Eval on
  2026-10-19: scenario A meets every spec 18.4 target; scenario C meets four of five (exact and
  bridge recall 0.80, two legacy links under the distinct floor at that volume; founder call in
  the demo note). CI green on all eleven jobs on 2026-10-20 (pushed as `m2-map-discovery-built`
  at 2cb3bd0 because the rebased milestone branch needs the user's force push). Acceptance
  waits on that founder call and the partner gate; `fix-c2` is not merged into it yet. Next:
  M3 trace (write docs/plans/M3.md first).
- M3 Trace: plan drafted 2026-10-20 on branch `m3-trace` (docs/plans/M3.md, cut from
  `m2-map-discovery` at 1e1b2d4); revised 2026-10-28 on branch `m3-trace-spec` (cut from
  `spec-data-layer` at c2e9a75) after five read-only reviews of the code against the plan:
  corrections in the plan's "What the code read changed", editorial spec fixes in 3, 5.2, 7.2,
  7.3, 9.7, 9.8, 12, 13, 15, 18, 21, threat-model rows 3 and 8, an amendment on ADR 0052.
  The thirteen founder questions were answered on 2026-11-01 with the plan's recommendations
  (recorded under the questions; spec 8.4, 9.5, 9.7, 9.8, 12, 14.2, 14.9 edited). Code waits
  on the M2 acceptance and the `fix-c2` rebase. ADRs from 0060 when code starts.
- Spec direction 2026-10-21 on branch `spec-data-layer` (ADR 0059, proposed): hosted data layer,
  business rules and hosted deployment as v2 (spec 22.2 to 22.4); partial failures, stale data
  and missed events into M4 (spec 10.1, 10.2); partner questions 9 to 25 in spec 23 open.
