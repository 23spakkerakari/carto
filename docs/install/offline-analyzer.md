# Offline analyzer (`carto-edge analyze`)

The offline analyzer is the pilot that needs no install (spec 4.1). You export logs and tables
from three to five systems, run `carto-edge analyze` on a machine you control, review what it
wrote, and send us one directory, the bundle. The analyzer parses, classifies, redacts and
tokenizes exactly as an installed edge would, with a key generated on your machine that never
leaves it. We load the bundle into a core instance and walk through the proposed map with you;
we cannot turn its tokens back into your values.

## The journey

1. Export one file or set of files per source into one input directory (below).
2. Describe the sources in `analyze.yaml`.
3. Run `carto-edge analyze`. It opens no network connection.
4. Review `MANIFEST.md` and, if you want, the events themselves; pin fields and re-run as needed.
5. Send the bundle directory, and only that. Keep the state directory.

## What to export

Export the same period (for example 14 days) from every system, so the links between them can be
found. Put each system's files in its own subdirectory of one input directory; the
configuration names files relative to it.

| What | How to export |
| --- | --- |
| Application logs | The log files as written, one file per day or one file in total; NDJSON, logfmt, XML lines, access logs and plain text all work |
| Database tables | One CSV per table with a header row that includes the primary key column; prefer the columns you would grant carto in a view (see the [read-only role scripts](read-only-roles/postgresql.sql)) |
| File drops (SFTP, shares) | A copy of the drop directory with modification times preserved (`cp -p` or `rsync -t`); only names, sizes and times are used, contents are never read |

Accepted inputs and limits (spec 8.1.1; `edge/carto_edge/connectors/upload.py`):

| Rule | Value |
| --- | --- |
| Extensions read as records | `.log`, `.txt`, `.json`, `.ndjson`, `.csv`, `.xml`, each optionally compressed as `.gz`, or inside a `.zip` |
| File arrivals (`kind: files`) | Any file; only name, size and modification time are used |
| Per file | 2 GB, counted uncompressed while streaming (a `.gz` or a zip entry that expands past it stops the run) |
| Per source | 20 GB of input files on disk |
| Zip archives | At most 10,000 entries; compression ratio at most 100:1 per entry and overall; at most 50 GB uncompressed; no absolute paths, no `..`, no symlink entries; entries are streamed, never extracted to disk; entries with other extensions are skipped |
| Symlinks on disk | Never followed |
| Lines | Cut at 16 MiB; records larger than `parse.max_record_bytes` (default 1 MiB) are dropped and counted |

`max_file_bytes` and `max_upload_bytes` in a source's `config` lower the per-file and per-source
limits for that source. Do not include files that hold secrets or that you would not send in any
form; the analyzer drops secret-like fields, but an export that never contained them is safer.

## Write `analyze.yaml`

`analyze.yaml` has the schema of the edge sources file (`carto_edge.config.SourcesFile`, spec
Appendix A): `systems`, `sources`, `field_policies` and `network`. Every enabled source must be
of type `upload`; the analyzer refuses any other type. The worked example is
[`simulator/analyze.shop.yaml`](../../simulator/analyze.shop.yaml), the configuration for the
simulator's scenario A (five systems, seven sources); the sections below walk through it.

### Systems

```yaml
systems:
  - id: sys_warehouse
    name: Warehouse
    owner_group: WMS Support
    criticality: high
```

Ids match `^[a-z][a-z0-9_]{0,63}$`; `criticality` is `low`, `medium` (default) or `high`.

### Sources and the three kinds

```yaml
  - id: src_wms_db
    system: sys_warehouse
    type: upload
    config:
      kind: rows
      paths: ["warehouse/purchase_orders*.csv"]
      table: purchase_orders
      primary_key: id
    parse:
      format: csv
      # The time the row came into being, not its last status change: a row whose updated_at
      # is set by a nightly job would place every record on the night it shipped.
      timestamp_field: created_at
      timestamp_format: "%Y-%m-%d %H:%M:%S"
      timezone: America/New_York
      actor_field: created_by
```

| `config` key | Default | Meaning |
| --- | --- | --- |
| `paths` | required | Files, directories or globs (`*`, `?`, `[...]`, `**`), relative to `--input` |
| `kind` | `log` | `log`: one record per line, locator `<file name>:line:<n>`. `rows`: one `row_change` record per CSV row, locator `<table>:row:<primary key>`, template `row_change <table>`. `files`: one `file_arrived` record per file from its name, size and modification time, template `file_arrived <name with digit runs as *>` |
| `table`, `primary_key` | none | Required for `rows` |
| `include` | none | Shell patterns a file name must match |
| `encoding` | `utf-8` | Any Python codec name |
| `max_file_bytes`, `max_upload_bytes` | 2 GB, 20 GB | Lower the limits for this source |

The shop example uses `log` for its five log sources, `rows` for the warehouse table export
(two CSV files, before and after a column rename, matched by one glob) and `files` for the
nightly `SHIP_*.csv` drops. It also sets `timestamp_column` and `actor_column` on the table
source; the upload connector accepts those keys, but the pipeline reads the parse hints, so set
`parse.timestamp_field` and `parse.actor_field` as the example does.

### Parse hints

| `parse` key | Default | Meaning |
| --- | --- | --- |
| `format` | `auto` | `auto`, `ndjson`, `json`, `xml`, `logfmt`, `csv`, `access_log`, `text` (Drain3 template mining) |
| `timestamp_field` | tries `ts`, `timestamp`, `@timestamp`, `time`, `_time`, `datetime`, `date`, `eventTime`, `event_time`, `created_at`, `updated_at` | Field holding the timestamp |
| `timestamp_format` | auto-detect | `iso8601`, `epoch_s`, `epoch_ms` or a strftime pattern such as `"%Y-%m-%d %H:%M:%S"` |
| `timezone` | `UTC` | IANA zone for timestamps **without** an offset; timestamps that carry an offset keep it |
| `message_field` | tries `msg`, `message`, `log`, `body`, `text` | Free text mined for a template; its parameters become `msg.param_0..n` and are classified like any field |
| `severity_field` | none | Level field |
| `actor_field` | none | The user or service that acted; becomes a tokenized `actor` (spec 7.1), the signal for manual work (spec 11.1) |
| `csv_columns`, `csv_has_header`, `csv_delimiter` | none, `true`, `,` | CSV layout |
| `access_log_pattern` | none | RE2 pattern with named groups |
| `max_record_bytes` | 1 MiB | Larger records are dropped |

Timezones matter for linking. In the shop example the webstore, order and shipping logs are UTC,
the payment messages carry their own offset (the `timezone` there applies only to timestamps
without one), and the two warehouse sources write naive local time, so they declare
`America/New_York`. Clock skew is a property of the source and is left as it is.

### Field policy pins

The classifier decides per field (spec 8.3): secrets are always dropped; names, contact data,
government ids, financial and health data are dropped by default; amounts are tokenized;
high-cardinality values are tokenized with forms (spec 8.4); low-cardinality attributes such as
status codes are kept in clear; free text keeps only its template constants. A pin overrides the
decision for one field, matched by `field_ref` (`system/template/path`) or with `*` for the
system or template:

```yaml
field_policies:
  - field: sys_warehouse/*/customer_name
    field_class: person_name
    policy: tokenize
    forms: ["phonetic"]
    reason: "L06 composite link: clerks key the cardholder name into the PO; phonetic forms survive their typos"
```

The shop example pins the two name fields of a manual hop to phonetic tokens (Double Metaphone
codes, keyed) so the link can be found without any name leaving your machine in clear. Rules
the file enforces: `secret_like` is always `drop`, and the PII classes can never be `keep`.
`forms` takes `raw`, `norm`, `alnum`, `digits`, `date`, `amount`, `phonetic` or exact names such
as `digits.0`. Every change to the pins changes the bundle's `policy_version`.

### Network

```yaml
network:
  allowed_source_cidrs: []
```

The analyzer opens no network connection, so no range needs to be allowed.

### Why `ground_truth/` is never an input

The simulator writes `ground_truth/` next to its per-system directories; it holds the synthetic
PII in clear (`markers.json`) and the transaction mapping that the evaluation must not see
through the engine. The shop configuration therefore names only `webstore/`, `orders/`,
`payments/`, `warehouse/` and `shipping/`. The same rule applies to your exports: point `paths`
at the files you mean to analyze, never at a whole tree that may contain other material.

## Run

```sh
carto-edge analyze --config analyze.yaml --input ./exports --out bundle.carto
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--config FILE` | required | The analyzer configuration |
| `--input DIR` | required | Directory the `paths` are relative to |
| `--out DIR` | required | Bundle directory to create; it must not exist or must be empty |
| `--state-dir DIR` | `<out>.edge-state` next to the bundle | Key, reveal vault and template state; must not be inside the bundle |
| `--tenant-id ID` | `default` | Tenant key domain written into the bundle |
| `--producer TEXT` | `carto-edge analyze <version>` | Free text recorded in the manifest |
| `--locator-map` | off | Evaluation only; never on your data (below) |
| `--as-of TIME` | the run time | Check event times against this ISO 8601 date or time with an offset, instead of now (below) |

The run makes two passes (ADR 0017): the first reads everything to gather field statistics and
templates, the second classifies every field with the statistics of the whole input and writes
the events, so one bundle has one consistent decision per field. It prints counts and paths
only, never a value. Exit codes: 0 the bundle was written, 1 the run failed (keys unusable, an
input over the limits, a write failed), 2 a usage or configuration error.

Event times must lie in a plausible range (ADR 0036): no older than event retention plus one
day (`CARTO_RETENTION__EVENTS_DAYS`, default 30), no more than one day ahead. An older record is
dropped with reason `too_old`, since core would delete it on arrival; a later one keeps its event
with the time it was read, marked `observed_at_quality: ingest`, because a time in the future is
almost always a misread date. The range is read against the run time. Exports analyzed long after
they were taken lose their old part; to analyze them as of the day they were exported, pass
`--as-of 2026-10-08` (the manifest notes it). `MANIFEST.md` and the summary give both counts.

The analyzer also reads the `CARTO_` settings of the edge (`carto_edge.config.EdgeSettings`).
Offline, the ones that matter are the classifier thresholds (`CARTO_CLASSIFY__DISTINCT_THRESHOLD`,
default 1,000; `CARTO_CLASSIFY__DISTINCT_RATIO`, default 0.2) and PII detection
(`CARTO_PII__ENABLED`, default `true`; `CARTO_PII__SCORE_THRESHOLD`, default 0.5). The `local`
KMS is the default and the only provider the analyzer creates keys for (ADR 0025).

### The worked example

From a checkout of the repository with the workspace installed (`make setup`):

```sh
make sim SCENARIO=shop DAYS=14
make analyze SCENARIO=shop
```

`make analyze` runs:

```sh
uv run carto-edge analyze --config simulator/analyze.shop.yaml --input sim-out/shop \
  --out sim-out/shop.carto --state-dir sim-out/shop.edge-state --locator-map \
  --as-of "$(uv run carto-sim as-of --out sim-out/shop)"
```

It passes `--locator-map` because the simulator run is scored by the evaluation harness; a
customer run never does. Simulator runs have fixed dates (from 2026-09-23 by default), so it
passes `--as-of` with the start of the day after the run, which `carto-sim as-of` reads from the
run's manifest. `make bench` and `make bench-gateway` do the same.

### From the container image

`make images` builds `carto-edge:dev` (signed release images arrive in M6). Its entrypoint runs
`carto-edge` with the container arguments, as uid 10001. Run it with no network, a read-only
root filesystem, the input and configuration mounted read-only, and separate writable
directories for the bundle and the state:

```sh
mkdir -p out state
docker run --rm --network none --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  -v "$PWD/analyze.yaml:/work/analyze.yaml:ro" \
  -v "$PWD/exports:/work/exports:ro" \
  -v "$PWD/out:/work/out" \
  -v "$PWD/state:/work/state" \
  carto-edge:dev analyze --config /work/analyze.yaml --input /work/exports \
  --out /work/out/bundle.carto --state-dir /work/state
```

`--user` makes the files yours on the host. Keeping `state/` outside `out/` means archiving
`out/` can never pick up the key.

## What the bundle contains

```
bundle.carto/
  manifest.json       run metadata, per-source counts, totals, sha256 and size of every data file
  MANIFEST.md         the same for a person to review, with the field and template tables
  events.ndjson.zst   one canonical event per line (spec 7.1), zstd
  fields.json         every field with its class, policy, counts, shapes, forms; samples for kept fields
  templates.json      every template with its text, kind, counts and first and last time seen
  signature.json      Ed25519 signature over the exact bytes of manifest.json
```

Each event carries ids, timestamps, the template id and text, severity, the attributes kept in
clear, identifier tokens with the shape and length of each value (`AA-9999`, 7), an actor token,
and the names of the fields dropped. Raw message text is never sent.

## Review it before you send it

`MANIFEST.md` lists everything that leaves your machine:

| Section | What to check |
| --- | --- |
| Run | Tenant, producer, key versions, policy version, counts, time range |
| Signature | What the signature means (below) |
| Sources | Records read, events, dropped and parse errors per source; a high drop or error count usually means a wrong `format` or timestamp hint |
| Fields kept in clear | **In clear in every event.** Low-cardinality attributes with up to five sample values each. Any sample that is an identifier, a name, an address or free text should be pinned to `tokenize` or `drop` |
| Fields tokenized | Forms and the top value shapes only (digits as `9`, letters as `A`); the values become keyed hashes |
| Fields dropped | Never sent; the events name the field only |
| Templates | Template text with parameters removed and PII detection applied to the constants; a template shows a value only when it never varied in your export |
| Notes | The PII detector used and anything the run wants you to know |

You can also search the events for values you know are sensitive (replace the example with an
order number or e-mail address from your own data; `zstd` is the Zstandard command-line tool).
There should be no match:

```sh
# replace SO-0004828 with an order reference that exists in your own export
zstd -dc bundle.carto/events.ndjson.zst | grep -c 'SO-0004828'
grep -c 'SO-0004828' bundle.carto/fields.json bundle.carto/templates.json bundle.carto/MANIFEST.md
```

Look hardest for identifiers that repeat across many records: a nightly batch number, a carrier
manifest id, a file name. They have few distinct values. When most of their values contain a run
of four or more digits (`MAN-20260923-01`, `SHIP_20260923.csv`) the classifier tokenizes them
anyway (ADR 0029); one without such digits looks like a low-cardinality attribute and is kept in
clear (spec 8.3 rule 6, ADR 0026). Pin each one to `field_class: identifier`, `policy:
tokenize`; `simulator/analyze.shop.yaml` pins `manifest_id`, `file` and `name` in the shipping
system as examples.

The rule errs on the safe side: a harmless field whose values have four digits (a year, a port,
a store number) is tokenized, and a four-digit value in a kept field is left out of the event.
Pin such a field to `field_class: low_card_attribute`, `policy: keep` to send it in clear.

Field names follow a similar rule (ADR 0030). A name appears in the bundle only if it occurs in
at least three records under the same parent and does not read as a person's name; otherwise it
is `*`. A JSON object keyed by values (customers, orders) with 256 or more distinct keys is
treated as a map: its keys appear as values in `<parent>.<n>.key` fields, which are tokenized or
dropped like any other value. If a real field shows as `*` in `MANIFEST.md`, it was rare in your
export; pinning its path makes the name trusted.

If a value shows up, pin the field (or remove the lines from the export) and re-run with the same
state directory. Tell us too: the classifier should have caught it.

## What never leaves your machine

- **The tokenization key** (`keys/` in the state directory). Tokens are
  HMAC-SHA256 under it (spec 8.4); without it they cannot be reversed or recomputed.
- **The reveal vault** (`vault.sqlite` in the state directory): the raw value behind each `raw`
  token, encrypted with AES-256-GCM under a KMS-wrapped data key.
- **Raw identifiers, PII and secrets**: tokenized, dropped, or reduced to phonetic tokens where you
  pinned it. Free text survives only as template constants.
- **Locators** with file names, line numbers and primary keys: they exist only in the optional
  locator map, which is for our simulator evaluation.

The bundle directory holds only the six files above: `carto-core` rejects a bundle with any other
entry.

## Protect the state directory

The state directory is as sensitive as an installed edge (ADR 0025). It holds the local KMS
master key next to the key it wraps, the reveal vault, and the template state.

- Keep it on an encrypted disk, readable only by you.
- Never send it, and never put it inside the bundle (the analyzer refuses `--state-dir` inside
  `--out`).
- Back it up if you want to reveal values later or re-run with the same tokens.
- Deleting it makes the bundle's tokens impossible to reveal; a later run with a new state
  directory produces different tokens.
- An edge you install later can start from this state directory (same key, same templates), so
  the pilot's tokens join with live data.

## How we load it

We verify first, then load (ADR 0022):

```sh
carto-core verify-bundle --bundle bundle.carto
carto-core load-bundle --bundle bundle.carto
```

`verify-bundle` needs no settings. It checks, in order: `signature.json` parses; the sha256 of
`manifest.json` matches the signed digest; the Ed25519 signature verifies with the embedded public
key; the manifest parses and lists only bundle data files; the directory holds nothing else; and
every file has its recorded size and sha256. `load-bundle` repeats the verification, validates
every event against the canonical event schema (a raw value cannot arrive in a slot the contract
does not have) and writes them to ClickHouse in chunks of at most 5,000 (`--chunk-size`), with
batch ids derived from the bundle id: loading the same bundle twice writes nothing the second
time, and an interrupted load resumes. It reads the core settings (`CARTO_CLICKHOUSE__*`,
`CARTO_POSTGRES__*`) like `ingest-api`. M1 loads the events; the map and link proposals built
from them arrive with M2.

The signature key is generated for the run and discarded, so the signature proves the bundle was
not changed after it was written, not who wrote it. Origin comes from your hand-over: send the
bundle over a channel you trust and, separately (by phone or a second channel), the
`manifest_sha256` value from `signature.json` so we can confirm we received your bundle.

## Map discovery over a bundle (M2)

Loading a bundle gives core the events; discovery turns them into proposed links without an
edge install. With the stack's databases reachable (`CARTO_` settings as for `load-bundle`):

```
carto-core discover --bundle customer.carto --out discovery/
```

verifies and loads the bundle (a bundle already loaded is skipped by the ledger), runs one full
profile pass and one full link pass as of the bundle's last event, and writes `discovery/`:
`engine_links.json` (every proposed link with its type, role, direction, score, both ends as
system, template and field path, and the evidence features), `engine_entities.json` (the
families) and `discover_summary.json` (counts, the settings in effect, the weights version).
Nothing in these files is a token or a value. The same links appear in the console's
Map > Review when the `api` and `web` services run.

### The go/no-go gate (spec 21)

On the review call, fill a CSV with the links the partner confirms by hand, independently of
what the engine proposed, one row per link:

```
system_a,field_a,system_b,field_b
sys_warehouse,po_num,sys_shipping,po_num
sys_orders,merchant_ref,sys_payments,merchantRef
```

then

```
carto-core gate-report --predictions discovery/ --confirmed confirmed.csv --out gate.md
```

lists, for each confirmed link, the engine's top three proposals for `field_a` and whether
`field_b` is among them, and ends with the share found. The gate asks for at least half; below
that, stop and review with the founder before M3.

## Re-running

Re-run with the **same** `--state-dir` and a new or empty `--out`. The key and the template state
are reused, so the same input gives the same tokens and template ids, and a bundle from a
corrected run joins with the first one. A corrected bundle has a new bundle id; tell us which
bundle replaces which.

## The eval-only locator map

`--locator-map` writes `<out>.locator_map.ndjson` next to the bundle (never inside it), one
`{"key": "<source_id>:<locator>", "event_id": ...}` line per event (ADR 0024, ADR 0006). It exists
so the evaluation harness can join engine output to the simulator's ground truth. Locators carry
file names, line numbers and table primary keys in clear: never pass `--locator-map` on your
data, and never send the file.

## FAQ

**Does the analyzer need network access?** No. It reads local files and writes local files; run
it with `--network none` in the container to prove it.

**Can you recover our values from the tokens?** No. Tokens are keyed hashes and the key stays in
your state directory. Short numeric ids (four to six digits) could be guessed by someone who had
the key, which is one more reason the state directory never leaves your machine (spec 8.4).

**What do you see?** Token values, value shapes and lengths, template text, low-cardinality
attributes (the sample values you reviewed), timestamps, field names, and counts.

**A field I consider sensitive is in "Fields kept in clear".** Pin it to `tokenize` or `drop`
and re-run with the same state directory.

**Why are some fields `unknown`?** The analyzer classifies with the whole input, so the gateway's
quarantine of fields with fewer than 200 samples does not apply; a field with too little
evidence is still classified by the rules, and an unclassified one is dropped.

**The run stopped with an error about a limit.** A file, a zip entry or a source's total exceeded
the limits above; split the export into smaller files or into several sources.

**Can I see exactly what is in the events?** Yes: `zstd -dc bundle.carto/events.ndjson.zst | head`
prints the first events as JSON.

**Where is the data of the reveal vault used?** Only on your side, by a future installed edge
that starts from the state directory; the reveal flow of the product arrives in M3.
