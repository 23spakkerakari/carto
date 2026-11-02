# ADR 0051: The core audit log arrives with the first mutating endpoint

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 9; spec 2.3 invariant 6, 14.9,
21)

## Context

Spec 2.3 invariant 6 covers review decisions and entity naming, which are M2's first mutations.
Spec 21 lists external anchoring under M6, but the table and the chain cannot be retrofitted
under rows that already exist.

## Decision

- Migration 0004 creates `audit_log` (`id`, `tenant_id`, `at`, `actor`, `action`, `target`,
  `before`, `after`, `reason`, `request_id`, `ip`, `user_agent`, `prev_hash`, `row_hash`) with a
  trigger that rejects `UPDATE` and `DELETE`, and grants `INSERT` and `SELECT` only to the
  application role `carto_app` when that role exists.
- `row_hash = SHA-256(prev_hash || canonical_json(content))` with the canonical form defined
  once in `carto_core.audit` (sorted keys, no whitespace, UTF-8); the first row chains from 64
  zeros. One chain per tenant; appends are serialized per tenant with a transaction-scoped
  advisory lock so two API workers cannot fork the chain.
- `before` and `after` carry metadata views only; `carto_core.audit.scrub` drops any key that
  names a secret or a token as a backstop.
- `carto-core audit-verify` walks the chain and reports the first break. The spec names
  `carto-ctl audit verify`; `carto-ctl` has no database driver and no database credentials, so
  the command lives in `carto-core`, which already connects as the service user. The daily
  anchor export to syslog or SIEM is M6 (spec 21).
- Every mutating console endpoint and every linker decision (auto-accept, stale) writes a row.

## Alternatives

- Audit rows in the product log only: not tamper-evident, not queryable by the auditor role.

## Consequences

- `GET /audit` serves the table to `admin` and `auditor`.
- The authorization matrix test asserts that every mutating route produced an audit row.
