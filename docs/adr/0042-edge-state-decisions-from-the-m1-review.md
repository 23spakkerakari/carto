# ADR 0042: Edge state decisions from the M1 review (work package D)

Status: proposed, 2026-10-12 (plan `docs/plans/M1-review-fixes.md`, package D; spec 2.3
invariants 6 and 7, 8.4, 8.5, 14.4, 14.9). Records the choices made while fixing the edge state,
key and audit findings that ADR 0033 does not already settle. The founder reviews it with the
package.

## 1. The audit anchor line is written beside the log processor, not through it

**Context.** ADR 0033 publishes the hourly audit anchor (`seq`, `row_hash`, `ts`) as a product
log line. Every product log line passes `carto_common.logging.redact` (spec 14.12), which shapes
any lower-case hex run that mixes letters and digits: a 64-hex `row_hash` would be logged as
`AA9999...`, and the anchor could no longer be compared with the chain. Package B also makes the
processor mask integers of four or more digits, which would hide `seq`.

**Decision.** `carto_edge.audit_anchor` renders the anchor line itself (the same JSON keys as
the structlog renderer: `event`, `service`, `level`, `timestamp`) and writes it to the gateway's
log stream. Before writing it checks that `seq` is a non-negative integer, `row_hash` matches
`[0-9a-f]{64}` and `ts` is an ISO 8601 timestamp; anything else raises and nothing is written.
No other line bypasses the processor.

**Why this does not weaken invariant 7.** The line can carry only a row number, a chain hash
and a time. The chain hash covers the whole row including its timestamp and the previous hash,
so it cannot be inverted or dictionary-matched to an identifier the way a hash of a bare value
could. An exemption inside the processor keyed on a field name was rejected: any caller could
then log a 64-hex digest of a raw identifier under that name.

## 2. Key rotation commits through `rotation.json`; every version has its own file

**Context.** A rotation wrote `tenant-key.v<n>.json`, then the new `tenant-key.json`, then
`rotation.json`, with no fsync of a file or its directory. A crash between the second and third
write left `tenant-key.json` at version `n+1` and `rotation.json` at `n`: every start then failed
with a version mismatch, and so did the next rotation.

**Decision.**

- Every tenant key version, the active one included, is `tenant-key.v<n>.json`. A rotation writes
  the new version's file, then atomically replaces `rotation.json`, which names the active
  version. Only after that are superseded files removed. Loads resolve each key file by the
  version `rotation.json` names and check the version in its wrapping context; a file that no
  state names is ignored and overwritten by the next rotation.
- `tenant-key.json`, which `key init` (edge and `carto-ctl`) still writes for version 1, is read
  as the file of the version it holds until the first rotation copies it to its versioned name
  and removes it. `key init` is unchanged, so installed edges and the install guides keep
  working.
- Every key file, `rotation.json` included, is written through
  `carto_common.crypto.write_private_file`: a temporary file created exclusively with mode 0600,
  written and fsynced, renamed over the target, and the directory fsynced (a no-op on Windows,
  where NTFS journals the rename). `WrappedKey.write` uses it too, and the local KMS master key is
  created with `os.open(O_CREAT | O_EXCL, 0o600)` instead of a default-mode create followed by
  `chmod`.

**Alternatives.** Keeping `tenant-key.json` as the active file and making `rotation.json` carry
the active key's fingerprint: one more field to keep in step, and a crash still leaves a file
that disagrees with the state. Migrating `tenant-key.json` on load: a load would write.
