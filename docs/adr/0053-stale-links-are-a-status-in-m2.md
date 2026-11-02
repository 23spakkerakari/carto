# ADR 0053: A stale link is a status in M2 and an alert in M4

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 11; spec 9.6, 10.2)

## Context

Spec 9.6 marks an accepted link `stale` when its match rate drops more than 50% against its
7-day baseline and raises a schema drift signal; the signal belongs to the detector (spec 10.2),
which is M4.

## Decision

- When a link is accepted (or auto-accepted) the linker stores `baseline_match_rate`, its
  `c_ab` at that time. Every pass re-verifies accepted links; when the current `c_ab` is below
  `(1 - LinkerSettings.stale_drop_share) x baseline`, the link becomes `stale`, an audit row is
  written and the review queue shows it with its current and baseline rates. When the rate
  recovers above the threshold the link returns to `accepted` with another audit row.
- A stale link is not used for new families until it recovers or a reviewer re-accepts it.
- The schema drift alert of spec 10.2 is built with the detector in M4 on top of this status.

## Alternatives

- Drop the link to `proposed`: loses the reviewer's decision for what may be a weekend dip.

## Consequences

- The M4 plan reads `key_links.status = 'stale'` transitions from the audit log.
