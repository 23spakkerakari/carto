# Runbook: tokenization key rotation (spec 8.4)

Tokens are HMACs under the tenant key `K_tenant`. Rotation introduces a new key version, keeps
the old one for an overlap window so new events carry tokens under both versions, then retires
it. Nothing stored in core is rewritten: the linker and assembler treat links per key version.

## When

- On schedule (recommended yearly), or
- immediately when the edge host, its state directory backup or the KMS key may have been
  exposed.

## Before you start

Run the commands inside the edge container so they see its state volume and KMS settings:
`docker compose -f deploy/compose/compose.yaml exec edge-gateway carto-edge key status` (Compose)
or the same command on the edge host with `CARTO_STATE_DIR` set.

1. Confirm the KMS is reachable: `carto-edge key status` prints the active version, the KMS key
   id and the fingerprints (never the material).
2. Back up `<state_dir>/keys/` (wrapped keys only; useless without the KMS) and
   `<state_dir>/vault.sqlite`.
3. Decide the overlap window. Default 30 days; it must be at least the event retention period
   (`CARTO_RETENTION__EVENTS_DAYS`, default 30) so every stored event can still be joined with
   new ones.

## Rotate

```
carto-edge key rotate --overlap-days 30
```

This generates version `n+1`, wraps it with the KMS and writes it as `keys/tenant-key.v<n+1>.json`,
then replaces `keys/rotation.json`, which names the active version and is the one commit point:
a crash before that leaves version `n` active and the command can simply be run again; a crash
after it leaves `n+1` active (ADR 0042). Each file is written to a temporary file created
owner-only, fsynced, renamed and its directory fsynced. The first rotation also copies
`keys/tenant-key.json` (version 1 as `key init` wrote it) to `keys/tenant-key.v1.json` and then
removes it. It records `key.rotate.intent`
in the edge audit file before it writes anything and `key.rotate.done` (or `key.rotate.failed`)
after; when the audit file cannot be opened or written, nothing is rotated. Then restart edge-gateway (`docker compose ... restart edge-gateway`): it reads
the keyring at startup. The offline analyzer reads it at the start of every run. From then on
every new event carries one token per form per live version (ADR 0023) and the reveal vault
stores raw values under both.

## During the overlap

- Trace search tokenizes queries under every live version, so searches match old and new events.
- Expect event size to grow by the identifier share for the window; the ClickHouse budget in
  spec 17 allows it.

## After the overlap

`carto-edge key status` shows the previous version with its `retire_at`. After that instant the
edge stops tokenizing with it automatically; delete `keys/tenant-key.v<n>.json` once no event
older than the retention period remains (it is harmless to keep).

## If the KMS key itself must rotate

Rewrap, do not re-tokenize: `carto-edge key rewrap` unwraps every wrapped key under `keys/`
(tenant keys of every live version, the reveal vault data key, the local secret store data key)
and wraps the same material again, staging every file before replacing any, so a failure leaves
the old set intact. Tokens do not change.

- Vault Transit: rotate the transit key in Vault first (`vault write -f transit/keys/carto/rotate`),
  then run `carto-edge key rewrap`, which wraps with the latest key version.
- Local KMS: `carto-edge key rewrap` generates a new master key file, re-wraps everything under it,
  installs it as `keys/local-kms.key` and keeps the old one as `keys/local-kms.key.retired`.
  Take a new backup of `keys/`, verify it restores, then destroy the retired file; a second rewrap
  refuses to run while it exists.

Restart edge-gateway afterwards. `key.rewrap.intent` and then `key.rewrap.done` (or
`key.rewrap.failed`) are recorded in the edge audit file; without a writable audit file the
command changes nothing.

## Loss of the tenant key

If every copy of a key version is lost (state directory and backups), events tokenized under it
can no longer be joined with new events and their vault entries cannot be revealed. Spec 14.11:
the wrapped key backup is part of the backup drill; verify it every time you rotate.
