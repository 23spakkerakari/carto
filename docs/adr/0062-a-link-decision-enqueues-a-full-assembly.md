# ADR 0062: A link decision enqueues a full assembly over the read window

Status: accepted, 2026-11-02 (plan `docs/plans/M3-demo.md` decisions D3 and D8; spec 4.3,
4.4, 9.7)

## Context

The assembler keys events as they arrive, from the ends of the links accepted at that moment.
Events stored before a reviewer accepted a link carry no key from it, so after spec 4.3
(accept the links) the trace of spec 4.4 would show nothing until the nightly `full` pass.

## Decision

- `POST /links/{id}/accept`, `/reject` and `/association` enqueue `assemble` with
  `payload["full"]` (deduped by the queue) when `WorkerSettings.assemble_after_decision` is
  set (default on). The review screen reads the pending job to say "transactions are being
  rebuilt".
- In the demo cut `full` is a replay (ADR 0060): the watermark is cleared and the read window
  is re-read over the existing index, so ids stay and the new link's keys merge the
  transactions they join. A reject's enqueue therefore stops new merges through the rejected
  link but cannot undo the ones it caused; the nightly reassembly of M3 proper does that.

## Consequences

- One decision re-reads the window (two simulator days in the demo; the max span plus a day
  in service). Several decisions in a row cost one replay, because the queue dedupes.
- M3 proper keeps the enqueue and replaces the replay with the plan's reassembly.
