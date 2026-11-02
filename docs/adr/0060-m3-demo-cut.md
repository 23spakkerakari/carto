# ADR 0060: The trace milestone is built demo-first, as a strict subset on its own branch

Status: accepted, 2026-11-02 (plan `docs/plans/M3-demo.md`; `docs/plans/M3.md`; spec 21 M3)

## Context

The next goal is a recorded demo of the pitch (map, trace, manual hop) as early as possible.
The M3 plan is the full trace milestone: assembler with batches and clock offsets, hourly hop
statistics in ClickHouse, reveal with step-up, load tests at the Section 17 volume, scenario C.
Most of it is not on the demo path. The founder answered the thirteen M3 questions on
2026-11-01 with the plan's recommendations (recorded under the questions in `M3.md`), so the
assembler's shape is settled and code may start.

## Decision

- `docs/plans/M3-demo.md` is the plan of a strict subset of M3: the assembler without
  composite pairs, batch rows or clock offsets; hop statistics as a rollup recomputed per
  graph pass (ADR 0061); tokenize-for-search without reveal; the map and trace screens; a
  "Likely manual" preview badge (ADR 0063). Everything else in the M3 plan is listed there
  under "Left for M3 proper" and nothing is dropped.
- The work runs on branch `m3-demo`, cut from `m3-trace-spec` at e12b626, whose PR targets
  `m3-trace-spec`; M3 proper continues on top of it. The `fix-c2` merge, the force push of the
  rebased M2 branch and the PR of `m1-review-fixes` to `main` wait until after the recording.
- ADRs 0060 to 0066 record the decisions the demo cut builds; the M3 plan's other decisions
  get their ADRs when their packages are built.
- `full` assembly in the demo cut is a replay of the read window over the existing index: the
  watermark is cleared, transaction ids stay, and the keys of newly accepted links merge the
  transactions they join. The M3 plan's reassembly from scratch into new ids belongs to the
  nightly hub repair, which is deferred with the nightly rebuild.

## Alternatives

- Build M3 in plan order and record afterwards: the recording waits on batches, offsets,
  reveal and the load run, none of which the video shows.
- Record on the M2 console alone: the pitch's second and third beats (trace, manual hop) have
  nothing on screen.

## Consequences

- Spec 21's M3 acceptance (F1 on scenario C, batch targets, p95 at the Section 17 volume) is
  not claimed by the demo cut; the plan's acceptance table is the recording checklist.
- Every module built here is named in `M3.md`'s packages, so M3 proper extends rather than
  replaces it; where the demo simplifies (ADR 0061), the ADR says what supersedes it.
