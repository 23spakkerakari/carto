# ADR 0057: Review items in M2 show counts, shapes, lag and the sentence, not example transactions

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 7; spec 4.3, 9.6)

## Decision

Spec 4.3 item 1 shows the reviewer "example transactions" and spec 9.6 a plain-language
sentence with the features. Transactions and reveal exist from M3. The M2 queue item carries the
sentence, the spec 9.3 features behind "Details", the top shapes of both ends and the matched
volume; M3 adds the example transactions and the reveal button to the same item. Nothing in an
item is a token or a value (spec 2.3 invariant 2).

## Consequences

- `LinkView` (`carto_schema.api`) is extended, not replaced, in M3.
