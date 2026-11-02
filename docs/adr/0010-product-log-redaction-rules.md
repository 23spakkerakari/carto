# ADR 0010: Product log redaction rules

Status: accepted, 2026-10-06 (spec 14.12, 2.3 invariant 7; spec 0.1 item 10 for the choices
the spec leaves open)

## Context

Spec 14.12 says the product's own logs must drop secret-named keys, mask high-entropy strings and
"anything shaped like a token or identifier", and never log request or response bodies. It does
not define "shaped like an identifier", nor how third-party libraries that log through the
standard library are covered. The M0 implementation in `carto_common.logging` had to decide.

## Decision

The `redact` processor in `carto_common.logging` applies, in order, the rules documented in
`packages/carto-common/README.md`. The decisions that go beyond the spec text:

1. **Identifier trigger.** A whitespace-delimited token is masked to its shape (spec 8.3 rule:
   digits to `9`, letters to `A`, punctuation kept) when it contains a run of four or more digits,
   or four or more digits in total when it also contains `-` or `_`. The second case exists
   because the spec's own identifier examples (`88-210`, `X9-0442`, Section 1.1) have no run of
   four digits and would otherwise be logged in clear and fail the Section 18.3 leak test. ISO
   dates, times and timestamps, versions, durations, IP addresses and grouped counts stay readable.
2. **ULIDs stay in clear.** The product's own `event_id`, `txn_id` and alert ids are ULIDs and are
   the only way to follow a record across services. A customer identifier that happens to be a
   canonical ULID would therefore be logged as is; the edge never logs identifier values at all,
   so this is accepted.
3. **Secret-named keys** are matched on any name segment (after splitting on `_`, `-`, `.`,
   spaces, camelCase and letter-digit boundaries), singular or plural, unless the last segment is
   a count-like qualifier (`count`, `total`, `len`, `size`, `ref`, `version`, `name`, `type`,
   `expires`, `ttl` and similar). So `secret_value`, `password_hash`, `session_id` and
   `x-api-key` are redacted while `token_count` and `secret_ref` survive.
4. **Hex runs** of eight or more characters that contain both letters and digits (UUIDs,
   content hashes, the simulator's leak markers) are masked to their shape.
5. **E-mail addresses and URL userinfo passwords** are masked even though the spec does not
   name them: both are PII or secrets whenever they appear.
6. **Standard-library logging is bridged.** `configure_logging` installs a root handler with
   structlog's `ProcessorFormatter`, so records from uvicorn, httpx, database drivers and stray
   `logging.getLogger()` calls pass through the same redaction. Libraries that install their own
   handlers must be told not to (uvicorn: `log_config=None`).
7. **Non-string scalars pass through** (amended by ADR 0040: numbers of four or more digits are
   masked unless their key names a metric). Identifiers must be logged as strings or not at all;
   counts stay numbers. The Section 18.3 leak test catches violations.

## Alternatives

- Mask only strings under known identifier keys: misses free-text messages, which is where
  identifiers leak most often.
- Mask every number: destroys counts, durations and versions that operators need.

## Consequences

Logs remain debuggable (shapes and ULIDs survive) while identifier values, secrets and PII do
not. The leak test in M1 is the arbiter; rules are tightened there if it finds anything.
