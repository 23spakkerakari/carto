# ADR 0033: The edge audit chain is anchored hourly outside the file; a torn final line is recovered

Status: accepted by the founder, 2026-10-12 (spec 14.9, 2.3 invariant 6). Amends ADR 0014.

## Context

`audit.ndjson` is hash-chained from a fixed genesis hash (ADR 0014). The chain detects an edit
in the middle of the file, but not two other changes:

- Deleting the last rows leaves a shorter chain that still verifies.
- Rewriting a row and recomputing every later hash leaves a chain that still verifies.

Spec 14.9 closes this for core's table with a daily anchor exported to the customer's syslog
or SIEM. The edge file had no anchor. Separately, a power cut during an append leaves a
partial last line, and opening the file then refuses to start the gateway and every audited
CLI command.

## Decision

1. **Anchor.** Every hour, and at gateway shutdown, the gateway writes an anchor: the last
   row's `seq`, `row_hash` and `ts`. It goes out as a product log line
   (`event=audit.anchor`), which customers already ship off the host with their container
   logs. It needs no new setting.
2. **Optional syslog target.** `audit.anchor_syslog` (host, port, TLS on by default, RFC 5424
   over TCP with RFC 5425 framing) sends the same anchor straight to the customer's syslog or
   SIEM. A failed send is logged and retried at the next anchor; it never blocks auditing.
3. **Verification.** `carto-edge audit verify --anchor <seq>:<row_hash>` (repeatable) checks
   that the chain still holds each anchored row with that hash. Anchors come from the
   customer's own log store, outside the edge's control.
4. **Torn write.** On open, a final line without a newline that fails to parse is moved to
   `audit.ndjson.torn-<UTC timestamp>`, an `audit.recovered` row is appended that records the
   byte count, and the gateway starts. Any other mismatch, anywhere in the file, still refuses
   to open.

## Alternatives

- A per-row HMAC under a KMS-wrapped audit key: a rewrite then needs the key, but an attacker
  with the edge's key access can still rewrite, and truncation is still invisible without an
  anchor. Left for M6 if a partner asks.
- Anchoring only to core: core is the system the edge record is meant to be independent of
  (ADR 0014).
- Daily anchors, as for core: rows written since the last anchor can be removed unnoticed, so
  an hour bounds that window to an hour of reveals.

## Consequences

- Up to one hour of audit rows can still be truncated without detection.
- The install guides tell the customer to keep container logs, or to set the syslog target,
  and how to run `audit verify` with anchors.
- A torn line is preserved for inspection, never deleted.
