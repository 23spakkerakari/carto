# M1 review fixes

Status: in progress on branch `m1-review-fixes` (rebased on `m1-edge-pipeline` at eb57b02),
started 2026-10-12.
Source: a staff-level review of M1 on 2026-10-11 (bugs, leaks, weak architecture), with the
founder's decisions of 2026-10-11 and 2026-10-12. M1 acceptance waits on this work.

## Goal

Every confirmed review finding is fixed with a regression test, or listed below as deferred
with its reason. The security invariants of spec 2.3 gain the tests that would have caught
each leak.

## Founder decisions recorded with this plan

| Decision | ADR |
| --- | --- |
| Edge audit anchor written hourly as a product log line, optional syslog target; torn final line recovered, mid-file mismatch still fails closed | 0033 |
| Ingest ledger claims a batch before writing; `events` and `event_identifiers` become `ReplacingMergeTree` keyed to deduplicate by event | 0034 |
| Connector cursors advance on the query boundary: Splunk windows on index time; SQL reads closed time ranges up to the database clock minus a settle delay; cursor-only checkpoints | 0035 |
| A plausible `observed_at` range: no older than event retention, no more than one day ahead | 0036 |
| Source queries are checked against allowlists, not denylists (SQL Server statements, Splunk commands and capabilities, MySQL grants through roles) | 0037 |
| Least-privilege database users stay in M6 | none (spec 21 M6) |

Not in this plan: certificate authorities. On 2026-10-12 the founder chose both a CA per
listener and ADR 0031's route roles. Another session builds it on `m1-edge-pipeline` as an
amendment to ADR 0031; this branch does not touch PKI or mutual TLS code.

## Work packages

Each package lands as conventional commits with tests first. ADR numbers 0038 to 0045 are
reserved for decisions made inside the packages: one per package, A1 0038, A2 0039, B 0040,
C 0041, D 0042, E 0043; 0044 and 0045 stay free. ADRs 0038, 0039, 0041 and 0043 list, under
Consequences, the plan items below that their package had not built by 2026-10-15. On
2026-10-15 branch `fix-e2` built ADR 0034 and ADR 0036 (ADR 0043 sections 10 to 13), with the
founder's choice of an `--as-of` reference time for the analyzer. On 2026-10-17 branch `fix-c2`
built ADR 0037 items 1 to 3, SFTP ignoring the SSH configuration, upload confinement and the
CSV field limit (ADR 0041 Consequences); MySQL connecting to the validated address stays open
there with its reason.

### A1. Field names and structure (`pipeline/parse/names.py`, `parser.py`, `parse/common.py`, `parse/csv.py`)

- The name gate runs the secret and PII name checks on the raw name before masking; a
  secret-named field is dropped there, and the classifier receives the name class as a hint so
  spec 8.3 rules 1 and 2 hold for masked names. Distinct masked names never share one path.
- Map detection forgets: names counted over a recent window, scoped per source, never at the
  root, and configured names win over the map check.
- The name register deletes descendants of a parent that becomes a map, evicts parents by
  least recent use, writes no more than it can load, and stores value-like names as digests.
- Paths are tuples of segments until the end, so dotted map keys and numeric keys are not
  mistaken for structure.
- A learned CSV header is provisional until its cells never reappear as values in the first
  rows; otherwise columns are `col_<i>` and the first row is data.
- Lone surrogates are replaced at the decode boundary.

### A2. Templates and repetition (`templates.py`, `parse/access_log.py`, `redact.py`, `classify.py`, `stats.py`, `pii.py`)

- Before Drain3 mining, the value side of `k=v` and `k: v` tokens and every secret-shaped
  token is masked; template redaction also applies the secret check.
- Access-log routes keep a segment only when it repeats at its position for the system;
  methods are limited to the RFC verbs.
- XML root names, connector template hints and generalized file names pass the same gate.
- Every repetition rule (names, template clusters, kept values) counts a record once by
  `event_id`, not once per delivery.
- Field statistics, classifier caches and templates have one memory budget with least
  recently used eviction; templates are capped per system.
- Template state persists with ASCII escapes; a failed flush never aborts shutdown.
- The IBAN pattern no longer backtracks; upload locators no longer log file names.

### B. Product logs, leak test, CI and eval

- The log processor masks integers and floats of four or more digits unless the key names a
  metric; the secret assignment pattern accepts quoted keys and more secret words; Python
  warnings and uncaught exceptions go through the processor.
- The leak test scans JSON numbers, stderr, heartbeats, the ledger, and normalized forms
  (case, Unicode, escapes, URL encoding); markers include name parts and email local parts;
  a stale bundle fails the test.
- CI runs every step with `pipefail`; the full-scenario leak test and the read-only
  integration tests fail instead of skipping in CI.
- Coverage gate 85% on `edge/carto_edge/pipeline` (measured 95% on 2026-10-12).
- Eval: false alerts as the maximum per flow and day, noise records as singletons in
  transaction F1, time to detect from the moment a fault becomes detectable, days in the
  scenario's time zone.
- GitHub Actions and base images pinned by digest.

### C. Connectors (`connectors/`, `net/retry.py`, `pipeline/ingestor.py`)

- ADR 0035 cursors for SQL and Splunk, typed SQL cursors, PostgreSQL session time zone UTC.
- ADR 0037 allowlists.
- MySQL connects to the validated address; SFTP ignores the user's SSH configuration.
- Upload walks without following links or junctions and confines paths to its base directory;
  CSV field size limit applied and per-row errors skipped.
- Splunk responses are read with byte limits and backfill is streamed.
- The circuit breaker clears its probe on cancellation.

### D. Edge state, keys and audit

- Reveal vault upserts keep the latest expiry; housekeeping purges expired entries.
- Admin commands write the audit row before the change and the outcome after it.
- ADR 0033 audit anchor and torn-write recovery.
- Key rotation commits through one atomic pointer; key files are fsynced and created 0600.
- zstd decompression windows capped at the edge; parked batches have their own budget, a gauge
  and an audited requeue or purge command; a batch failing repeatedly with 5xx stops blocking
  the queue head.
- The Vault URL must be https or a loopback address; the Vault clients ignore proxy settings.
- Secret lookups are single-flight per reference and serve cached values without waiting.
- Reveal quotas and nonces are rebuilt from the audit file on start.
- One OTLP record limit.

### E. Core

- ADR 0034 ledger claim and `ReplacingMergeTree`, with ledger pruning.
- ClickHouse client without session ids; inserts split by partition day; permanent ClickHouse
  errors answered 422.
- ADR 0036 range checks.
- The shared schema rejects value-shaped field names, attribute keys and dropped fields, and
  digit runs of six or more in `template_text`.
- `load-bundle` keys chunks by event range, hashes the bytes it loads, and checks the tenant.
- zstd windows capped, decompression and validation off the event loop, in-flight batches
  bounded.
- ClickHouse migrations under a PostgreSQL advisory lock; late heartbeats never overwrite
  newer ones; PostgreSQL `sslmode` defaults to `verify-full`.
- Compose services get memory, CPU and process limits.

## Acceptance

`make check` passes locally on Windows; the integration and compose jobs pass in CI (Docker
is not running on the laptop). Each finding above has a test that fails before its fix.
