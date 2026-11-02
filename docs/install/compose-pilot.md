# Compose pilot install (M1)

This guide installs the carto pilot stack with Docker Compose on one virtual machine (spec 5.5
"Pilot: Docker Compose on one VM") and connects it to your systems (spec 4.2). It is written for
the platform engineer who runs the install and for the security reviewer who signs it off. Every
command runs from the root of the repository checkout unless a step says otherwise.

## What M1 delivers

M1 is the ingest half of the product: connectors read your systems read-only, `edge-gateway`
parses, classifies, redacts and tokenizes every record, keeps raw identifier values only in the
encrypted reveal vault on the edge, and forwards canonical events over mutual TLS to
`ingest-api`, which writes them to ClickHouse. Nothing in M1 shows you those events yet.

| Capability | Milestone |
| --- | --- |
| Connectors, edge pipeline, disk buffer, forwarding, `ingest-api`, offline analyzer | M1 (this guide; the analyzer has [its own guide](offline-analyzer.md)) |
| Map discovery, link review queue API and UI, field policy UI | M2 |
| Trace search and timeline, map graph UI, reveal flow (the core side of `/internal/tokenize` and `/internal/reveal`) | M3 |
| Watch: expectations, detectors, alerts, channels, status board | M4 |
| Manual hops and cost | M5 |
| SSO and RBAC, Helm, image signatures, TLS to the databases, cloud KMS, backups and restore drill | M6 |

The reverse proxy, `api`, `web` and the workers join the Compose stack from M2 on. The
[known gaps](#known-gaps-in-this-build) at the end of this guide list what the M1 build does not
do yet although this guide or the spec expects it. Read them before you start.

## Prerequisites

| Item | Requirement |
| --- | --- |
| Host | One VM with 8 vCPU, 32 GB RAM and 500 GB SSD (spec 5.5) |
| Container runtime | Docker Engine with the Compose v2 plugin (`docker compose`); BuildKit for `make images` (the Dockerfile uses `RUN --mount=type=cache`; BuildKit is the default builder since Docker Engine 23) |
| Tools | GNU make and `openssl` (the `make` targets use both), `git` |
| Disk encryption | Encryption at rest of the volumes that hold Docker's data is your responsibility and an install requirement (spec 14.4), in addition to carto's own encryption of the vault and keys |
| Clock | NTP-synchronised; certificates, webhook timestamps (300 s window) and internal assertions (at most 5 minutes) depend on it |
| Outbound internet | Not needed to run the stack. Building the images in M1 pulls base images and locked Python packages; build on a connected machine and move the images if the VM is offline (step 2) |
| Vendor access | None. Nothing phones home and no telemetry leaves the VM (spec 2.3 invariant 4; `CARTO_TELEMETRY__ENABLED` defaults to `false`). We never need inbound access |
| Source accounts | Read-only accounts per source, created with the scripts and guides listed under step 7 |

## Step 1: Obtain the release

M1 has no published release artifact. You receive the repository at the M1 tag (or a source
archive of it) and build the images yourself. Signed images in a registry, verified with
`carto-ctl verify-signatures`, arrive in M6 (spec 5.5, 14.8); that command prints a
not-implemented notice in this build.

## Step 2: Build the images

```sh
make images
```

This builds four images from `deploy/docker/base.Dockerfile` (spec 6, 14.6):

| Image | Contents | Used by |
| --- | --- | --- |
| `carto-edge:dev` | `carto-edge` | `edge-gateway` |
| `carto-core:dev` | `carto-core` | `migrate`, `ingest-api` |
| `carto-ctl:dev` | `carto-ctl` | `pki-init`, `key-init` |
| `carto-simulator:dev` | `carto-sim` | `simulator` (dev profile only) |

Each runs as uid 10001 with the apt tooling removed; the entrypoint is a `/bin/sh` shim that runs
the member's console script with the container arguments. Compose pulls three third-party images on
first start: `postgres:16.6-bookworm`, `clickhouse/clickhouse-server:24.8` and
`otel/opentelemetry-collector-contrib:0.111.0`.

On an offline VM, build elsewhere and transfer the images with the standard Docker commands:

```sh
docker save carto-edge:dev carto-core:dev carto-ctl:dev postgres:16.6-bookworm \
  clickhouse/clickhouse-server:24.8 otel/opentelemetry-collector-contrib:0.111.0 \
  -o carto-images.tar
docker load -i carto-images.tar          # on the VM
```

## Step 3: Create the database password files

`compose.yaml` passes the PostgreSQL and ClickHouse passwords as Docker secrets from two files,
never through the environment. `make dev` creates them when they are missing or empty; for a
pilot, create them yourself with the same commands:

```sh
test -s deploy/compose/secrets/pg_password || openssl rand -hex 24 > deploy/compose/secrets/pg_password
test -s deploy/compose/secrets/ch_password || openssl rand -hex 24 > deploy/compose/secrets/ch_password
```

Both files are git-ignored (`deploy/compose/secrets/.gitignore`). Compose mounts them read-only
at `/run/secrets/pg_password` and `/run/secrets/ch_password` in the services that list them, and
the core settings read them through `CARTO_POSTGRES__PASSWORD_FILE` and
`CARTO_CLICKHOUSE__PASSWORD_FILE`. Anyone who can read these files or run `docker` on the VM can
reach the databases: restrict who can log in to the host. The PostgreSQL image applies the
password only when it initialises an empty `pg-data` volume; changing the file later does not
change the database password.

## Step 4: Create the internal PKI (`carto-ctl pki init`)

Every internal connection is mutual TLS with install-generated private CAs (spec 14.4), one per
listener (ADR 0031). On every start the one-shot `volumes-init` job gives the named volumes to
uid 10001, then `pki-init` runs:

```
carto-ctl pki init --out /etc/carto/pki --ca-key-dir /etc/carto/pki-ca \
  --services ingest-api,edge-gateway,otel-collector --if-missing
```

Each listener (`ingest-api`, `edge-gateway`) gets its own CA. That CA signs the listener's server
certificate and the client certificates of the services allowed to call it, and the listener
trusts nothing else, so a caller meant for another listener is refused during the TLS handshake:

| Listener | Its CA signs client certificates for |
| --- | --- |
| `ingest-api` | `edge-gateway` |
| `edge-gateway` | `otel-collector`, collectors on other hosts (`collector`), the reverse proxy (`proxy`), core's API (`api`) |

The command writes:

| File | Volume | Content |
| --- | --- | --- |
| `<listener>-ca.crt`, `<listener>-ca.key` | `pki-ca` | Each listener's CA certificate (valid 3,650 days) and key, on a volume that no running service mounts |
| `listener.crt`, `listener.key`, `listener-ca.crt` | `pki-<listener>` | The listener's server certificate (valid 90 days, SANs for the service name, `localhost` and any `--dns` or `--ip` given; it also serves as the client certificate for the service's own health probe) and the CA it trusts for its callers |
| `to-<listener>.crt`, `to-<listener>.key`, `to-<listener>-ca.crt` | `pki-<service>` | The service's client certificate for calling `<listener>` (valid 90 days) and that listener's CA, to verify the server |

Each service mounts only its own volume, so it can read only its own keys: `edge-gateway` has
its listener files and `to-ingest-api.*`, `otel-collector` has `to-edge-gateway.*` only, and
`ingest-api` has its listener files only.

Each certificate also names its service role (`urn:carto:role:<service>`), and each route
accepts only the roles listed for it: `ingest-api` takes batches and heartbeats only from
`edge-gateway`; `edge-gateway` takes OTLP only from `otel-collector` and `collector`,
`/internal/tokenize` and `/internal/reveal` only from `api`, and webhooks only from `proxy`.
`/healthz`, `/readyz` and `/metrics` accept any certificate the listener's CA signed. Anything
else is answered 403 and counted in `carto_edge_mtls_refused_total` or
`carto_ingest_mtls_refused_total`. A service name that is not a role (a scraper, say) gets a
client certificate without a role for every listener: enough for health and metrics only.

Keys are written with owner-only permissions and never overwritten, and the output lists
certificate fingerprints only. With `--if-missing` a complete earlier run is a success that
writes nothing, so restarting the stack leaves the PKI alone; a partial set (a key without its
certificate) is refused. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--out DIR` | `pki` | Directory for the per-service directories |
| `--ca-key-dir DIR` | `--out` | Directory for the listener CAs' certificates and keys |
| `--if-missing` | off | Succeed without writing when every file is already present |
| `--services NAME[,NAME...]` | `edge-gateway,ingest-api,otel-collector,api` | Services to issue for; a service that is neither a listener nor allowed to call one of the listeners named is refused |
| `--dns NAME` | none | Extra DNS SAN on every listener certificate (repeatable) |
| `--ip ADDR` | none | Extra IP SAN on every listener certificate (repeatable) |
| `--days N` | `90` | Service certificate lifetime, 1 to 3,650 |

If a client will reach `edge-gateway` or `ingest-api` by a name other than the service name, or
you want a certificate for a scraper of your own, run the command yourself before the first
start so the names are in the certificates:

```sh
docker compose -f deploy/compose/compose.yaml run --rm pki-init \
  pki init --out /etc/carto/pki --ca-key-dir /etc/carto/pki-ca \
  --services ingest-api,edge-gateway,otel-collector --dns edge-gateway.example.internal
```

**Rotation.** Service certificates expire after 90 days and this build has no command that
reissues them from the existing CAs; automated rotation (spec 14.4) arrives with M6. Before day
90, remove the containers with `docker compose ... down` (never with `-v`, which deletes every
volume), remove the PKI volumes with `docker volume rm carto_pki-ingest-api
carto_pki-edge-gateway carto_pki-otel-collector carto_pki-ca`, and start again so `pki-init`
creates new CAs and certificates.

## Step 5: Create the tokenization key (`carto-ctl key init`)

Tokens are HMACs under the tenant key (spec 8.4). The key is generated once, exists at rest only
wrapped by a KMS, is unwrapped into `edge-gateway` memory at startup, and never leaves the
`edge-state` volume: it is never in an environment variable, a config file, an image or a log.
On first start the one-shot `key-init` service runs:

```
carto-ctl key init --state-dir /var/lib/carto --tenant-id default --kms local
```

(plus `--if-missing`, so a restart leaves existing keys alone and a partial set is refused).
It writes, under `/var/lib/carto/keys/`:

| File | Content |
| --- | --- |
| `local-kms.key` | The local KMS master key, 32 bytes, owner-only (`--kms local` only) |
| `tenant-key.json` | Version 1 of the tenant key, wrapped by the KMS and bound to the tenant id and purpose |
| `rotation.json` | Active version 1, no previous versions, 30-day overlap |

`edge-gateway` adds `vault-data-key.json` (the wrapped reveal vault data key) on first start and
`secrets-data-key.json` on the first `local://` secret lookup. The command prints key ids and
fingerprints only. Options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--state-dir DIR` | `/var/lib/carto-edge` | Edge state directory; keys go under `DIR/keys`. Compose passes `/var/lib/carto` |
| `--tenant-id ID` | `default` | Tenant key domain; keep `default` (v1 is single-tenant) |
| `--kms local\|vault` | `local` | Local master key file or HashiCorp Vault Transit (ADR 0012) |
| `--vault-url URL` | none | Vault address (https) |
| `--vault-transit-key NAME` | `carto` | Transit key name |
| `--vault-mount PATH` | `transit` | Transit mount |
| `--vault-token-file FILE` | none | File holding the Vault token; never an option value or an environment variable |

**Local KMS (pilots).** The master key file sits in the same volume as the keys it wraps, so the
protection is the volume itself: disk encryption, host access control and how you handle
backups. Use it for a pilot only.

**Vault Transit (production).** Create a derived Transit key so Vault enforces the context that
carto binds into every wrap, give the edge a token limited to that key, and initialise with
`--kms vault`:

```sh
vault secrets enable transit
vault write transit/keys/carto derived=true
```

```hcl
# Vault policy for the edge token
path "transit/encrypt/carto" { capabilities = ["update"] }
path "transit/decrypt/carto" { capabilities = ["update"] }
path "kv/data/carto/*"       { capabilities = ["read"] }   # only if secret_ref uses vault://kv/carto/...
```

The command below needs the override file of step 8 with `key-init` on the `sources` network and
given the `vault_token` secret:

```sh
docker compose -f deploy/compose/compose.yaml -f deploy/compose/compose.pilot.yaml run --rm \
  key-init key init --state-dir /var/lib/carto --tenant-id default --kms vault \
  --vault-url https://vault.example.internal:8200 --vault-token-file /run/secrets/vault_token
```

Then set the same KMS on `edge-gateway` in the override file: `CARTO_KMS__PROVIDER` to `vault`,
`CARTO_KMS__VAULT_URL`, `CARTO_KMS__VAULT_TRANSIT_KEY` and `CARTO_KMS__VAULT_MOUNT` when they
differ from `carto` and `transit`, and `CARTO_KMS__VAULT_TOKEN_FILE` pointing at a token file
mounted as a Docker secret. The gateway reads the token file at startup to unwrap the keys.
Vault's certificate must verify against the system trust store of the image; this build has no
setting for a private Vault CA bundle. AWS KMS, Azure Key Vault and GCP KMS arrive in M6
(ADR 0012).

**Rotation and loss.** Key rotation and KMS rewrapping follow
[the key rotation runbook](../runbooks/key-rotation.md) (`carto-edge key status`, `key rotate`,
`key rewrap`, run inside the `edge-gateway` container). Losing every copy of a key version makes
events tokenized under it unjoinable with new events and their vault entries unreadable
(spec 14.11): back the keys up as described under [Operations](#operations).

## Step 6: Database migrations

The one-shot `migrate` service runs `carto-core migrate` once PostgreSQL and ClickHouse are
healthy. It creates the ClickHouse tables `events` and `event_identifiers` with the retention TTL
(`CARTO_RETENTION__EVENTS_DAYS`, default 30, range 7 to 400; spec 14.10) and brings PostgreSQL to
the head revision (the batch ledger and source health tables). It is idempotent: a second run
prints `clickhouse: up to date` and `postgres: at head`. Migrations 0003 to 0006 drop and
recreate both event tables as `ReplacingMergeTree` (ADR 0034), so a stack installed before them
loses the events it held; load or replay them again after the upgrade. To run it by hand:

```sh
docker compose -f deploy/compose/compose.yaml run --rm migrate
```

`carto-core migrate --clickhouse-only` and `--postgres-only` limit it to one store. The M1 stack
uses one database user, `carto`, for every core service and connects without TLS on the internal
`core` network (`CARTO_POSTGRES__SSLMODE: disable`, ClickHouse over `http://clickhouse:8123`).
Per-service least-privilege users and TLS to both databases (spec 14.4) arrive in M6.

## Step 7: Configure your sources

The gateway reads one sources file (`CARTO_SOURCES_FILE`, mounted at `/etc/carto/sources.yaml`).
Start from the dev file, which is wired to the simulator, and replace its systems and sources:

```sh
cp deploy/compose/sources.dev.yaml deploy/compose/sources.yaml
```

`sources.yaml` holds no secrets (credentials are references, below) but it names internal hosts;
keep it out of public repositories. Step 8 mounts it in place of `sources.dev.yaml`.

### File structure

The file is YAML read with `safe_load`, at most 1 MiB, with unknown keys rejected
(`carto_edge.config.SourcesFile`; spec Appendix A). Validation errors name the location, never
the value.

| Key | Content |
| --- | --- |
| `systems[]` | `id` (`^[a-z][a-z0-9_]{0,63}$`), `name`, `owner_group`, `criticality` (`low`, `medium` default, `high`) |
| `sources[]` | `id` (same pattern), `system`, `type` (`upload`, `otlp`, `splunk`, `sql`, `sftp`, `webhook`), `enabled` (default `true`), `config` (validated by the connector), `secret_ref`, `parse`, `backfill_days` (0 to 400, accepted by the schema) |
| `field_policies[]` | Admin pins, below |
| `network.allowed_source_cidrs` | Private ranges connectors may reach, below |

A `config` key whose name contains `password`, `passwd`, `token`, `secret` or `api_key` is
refused: credentials go in `secret_ref`. Parse hints (spec 8.2) apply to every connector:

| `parse` key | Default | Meaning |
| --- | --- | --- |
| `format` | `auto` | `auto`, `ndjson`, `json`, `xml`, `logfmt`, `csv`, `access_log`, `text` |
| `timestamp_field` | tries `ts`, `timestamp`, `@timestamp`, `time`, `_time`, `datetime`, `date`, `eventTime`, `event_time`, `created_at`, `updated_at` | Field holding the source timestamp |
| `timestamp_format` | auto-detect | `iso8601`, `epoch_s`, `epoch_ms` or a strftime pattern |
| `timezone` | `UTC` | IANA zone applied only to timestamps without an offset |
| `message_field` | tries `msg`, `message`, `log`, `body`, `text` | Free-text field mined for a template |
| `severity_field` | none | Level field |
| `actor_field` | none | User or service field; becomes the tokenized `actor` |
| `csv_columns`, `csv_has_header`, `csv_delimiter` | none, `true`, `,` | CSV layout |
| `access_log_pattern` | none | RE2 pattern with named groups |
| `max_record_bytes` | 1 MiB | Larger records are dropped (256 B to 16 MiB) |

### Credentials: `secret_ref`

Connectors receive credentials only as references, resolved at use, cached in memory for at
most 15 minutes, never written to disk in clear and never logged (spec 14.3, ADR 0013):

| Scheme | In this build |
| --- | --- |
| `vault://<mount>/<path>[#<field>]` | HashiCorp Vault KV v2, field `value` by default. Needs `CARTO_KMS__VAULT_URL` and `CARTO_KMS__VAULT_TOKEN_FILE` on `edge-gateway`, also when the KMS provider is `local`. **The pilot path.** |
| `local://<name>` | `secrets.sqlite` in the edge state volume: AES-256-GCM under a KMS-wrapped data key (ADR 0013). For Compose pilots without Vault. Write values with `carto-edge secret set` (below). |
| `aws-sm://`, `azure-kv://`, `gcp-sm://` | Refused with a message naming M6 |

A login (SQL, SFTP password) is one secret value: a JSON object `{"username": ..., "password":
...}` or `user:password` text. Write it from a file so it stays out of shell history:

```sh
vault kv put kv/carto/wms-readonly value=@wms-readonly.json
```

Without Vault, store it in the edge's local store instead (`$COMPOSE` as defined in step 8). The
value comes from a file or standard input, never from an argument, and only the name is printed
and audited:

```sh
$COMPOSE exec -T edge-gateway carto-edge secret set wms-readonly < wms-readonly.json
$COMPOSE exec edge-gateway carto-edge secret list
```

and reference it as `secret_ref: local://wms-readonly`. `carto-edge secret delete <name>` removes
one.

A rotated credential takes effect within 15 minutes or at the next gateway restart.

### Network allow-list: `network.allowed_source_cidrs`

Every connector host is resolved and checked before each connection (spec 14.7, ADR 0018):

- Always refused: loopback, unspecified, multicast and reserved addresses, link-local including
  the cloud metadata address `169.254.169.254` and `fe80::/10`, `fd00:ec2::254`,
  `100.100.100.200` and `0.0.0.0/8`.
- Refused unless inside a listed CIDR: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
  `100.64.0.0/10`, `fc00::/7` and the other ranges Python classes as private.
- A name with any refused address is refused entirely. HTTP (Splunk), PostgreSQL and SFTP
  connect to the validated address while TLS or SSH still verifies the name. MySQL and SQL
  Server validate at each connect but connect by name, so a rebinding window remains: give
  those hosts fixed entries with `extra_hosts` in the override file (step 8).

List only your source subnets. Never list the Docker networks of this stack (usually inside
`172.16.0.0/12`): that would let a connector be pointed at `ingest-api` or the databases.

```yaml
network:
  allowed_source_cidrs: ["10.20.0.0/16"]
```

### Field policy pins

Pins override the classifier for one field (spec 8.3) until the field policy UI arrives (M2).
`field` is a `field_ref` (`system/template/path`) or a pattern with `*` for the system or the
template (`sys_orders/*/order_id`).

```yaml
field_policies:
  - field: sys_warehouse/*/customer_name
    field_class: person_name
    policy: tokenize
    forms: ["phonetic"]
    reason: "composite link: clerks key the cardholder name into the PO"
```

`policy` is `keep`, `tokenize` or `drop`; `forms` takes the families `raw`, `norm`, `alnum`,
`digits`, `date`, `amount`, `phonetic` or exact form names such as `digits.0`. `secret_like`
fields are always dropped and the PII classes (`person_name`, `contact`, `government_id`,
`financial`, `health`) can never be kept in clear; the file is refused otherwise. A `keep` pin on
an identifier sends its values to core in clear: agree it with your security reviewer. Every
change to the pins changes `policy_version`, which each event carries.

### One example per connector type

Each example below is a `sources[]` entry. Config keys are those of the connector modules in
`edge/carto_edge/connectors/`; files they name (CA bundles, mounted directories) must exist
inside the `edge-gateway` container, so mount them in the override file.

#### SQL polling: PostgreSQL, MySQL, SQL Server (spec 8.1.4)

Create a dedicated login with the script for your engine, run it on a **read replica** (SQL
Server: through the availability group listener; carto connects with
`ApplicationIntent=ReadOnly`), and prefer a view that exposes only the columns carto needs:
[PostgreSQL](read-only-roles/postgresql.sql), [MySQL](read-only-roles/mysql.sql),
[SQL Server](read-only-roles/sqlserver.sql).

```yaml
  - id: src_wms_db
    system: sys_warehouse
    type: sql
    config:
      dialect: postgresql                 # postgresql, mysql or mssql
      host: wms-replica.example.internal
      port: 5432
      database: wms
      sslmode: verify-full
      ca_file: /etc/carto/ca/wms-replica-ca.pem
      poll_seconds: 60
      queries:
        - name: purchase_orders
          sql: >
            SELECT id, po_num, order_ref, status, warehouse_code, created_by, updated_at
            FROM carto_purchase_orders
            WHERE updated_at > :watermark
            AND updated_at <= :upper
            ORDER BY updated_at
            LIMIT :batch
          watermark_column: updated_at
          primary_key: id
    secret_ref: vault://kv/carto/wms-readonly
    parse:
      timestamp_field: updated_at
      timezone: America/New_York          # the column holds naive local time
      actor_field: created_by
```

| Key | Default | Meaning |
| --- | --- | --- |
| `dialect` | required | `postgresql`, `mysql`, `mssql` |
| `host`, `port`, `database` | port 5432, 3306 or 1433 by dialect | Bare host name or address |
| `sslmode` | `verify-full` | PostgreSQL libpq mode |
| `ssl` | `true` | MySQL: TLS with certificate verification |
| `encrypt`, `trust_server_certificate`, `driver` | `true`, `false`, `ODBC Driver 18 for SQL Server` | SQL Server |
| `ca_file` | none | CA bundle for the server certificate |
| `poll_seconds` | 60 | 5 to 86,400 |
| `query_timeout_seconds`, `connect_timeout_seconds` | 30, 10 | Timeouts |
| `batch`, `max_pages_per_read` | 1,000, 100 | Rows per page bound to `:batch`; pages per poll |
| `failure_threshold`, `breaker_reset_seconds` | 5, 60 | Circuit breaker |
| `queries[]` | 1 to 64 | `name`, `sql`, `watermark_column`, `primary_key` (default: the first column), `timestamp_column`, `actor_column` |

Read-only enforcement has three layers: the grants, read-only sessions (PostgreSQL
`default_transaction_read_only` and `BEGIN READ ONLY`, MySQL `START TRANSACTION READ ONLY`, SQL
Server `ApplicationIntent=ReadOnly`) and a parser check that accepts a single `SELECT` or
`WITH ... SELECT` with no bind parameters other than `:watermark` and `:batch`. The connector's
test refuses a login that can INSERT, UPDATE, DELETE or alter anything. The gateway runs every
pull connector's test before its first poll and never polls a source whose test fails or finds a
write-capable credential (`scheduler.source_not_enabled` in its log, with the failed check
names); it retries with backoff, so a corrected grant starts the source without a restart. Run
the test yourself (`$COMPOSE` as defined in step 8) before you enable a source, and after any
grant change:

```sh
$COMPOSE exec edge-gateway carto-edge source test --source src_wms_db
```

It prints each check and exits 1 when a source cannot be enabled. The Test button of Setup ›
Sources (spec 4.2) arrives with the UI.

A query's `timestamp_column` and `actor_column` (spec Appendix A) name the row's clock and actor;
they take precedence over the source's `parse.timestamp_field` and `parse.actor_field`. The actor
column is tokenized into the event's `actor`, never kept as an attribute (spec 7.1). SQL Server support
is implemented and verified against a live server at the first deployment that needs it
(ADR 0019); see the known gaps for the image.

#### Splunk pull (spec 8.1.3)

Create the search-only role and token with the [Splunk role guide](splunk-role.md) and store the
token in Vault as the whole secret value.

```yaml
  - id: src_orders_splunk
    system: sys_orders
    type: splunk
    config:
      base_url: https://splunk.example.internal:8089
      search: "search index=orders sourcetype=order_svc"
      window_minutes: 5
      overlap_minutes: 2
      max_concurrency: 2
      ca_file: /etc/carto/ca/splunk-ca.pem
    secret_ref: vault://kv/carto/splunk-orders-token
```

| Key | Default | Meaning |
| --- | --- | --- |
| `base_url` | required | Origin only (`https://host:port`, no path, no credentials) |
| `search` | required | The search string run through `/services/search/jobs/export` |
| `window_minutes`, `overlap_minutes` | 5, 2 | Sliding windows of **index time** and their overlap; results are deduplicated on `_cd`, `_indextime` and a hash. Every searched window moves the cursor, even an empty one (ADR 0035) |
| `settle_seconds` | 60 | A window ends no later than now minus this, so events still being indexed are read on the next poll |
| `max_event_lag_hours` | 24 | Bounds the event time searched (`earliest_time` = window start minus this). An event whose own timestamp is older than this when it is indexed is not read; raise it for sources with long forwarder backlogs |
| `max_concurrency`, `backfill_chunk_minutes` | 2, 60 | Backfill protection for the search head |
| `verify_tls`, `ca_file` | `true`, none | Certificate verification and a custom CA |
| `allow_plaintext` | `false` | Admin flag to allow `http://`; leave it off |
| `timeout_seconds`, `requests_per_second` | 120, 2 | Request timeout and rate limit |
| `dedupe_size`, `max_windows_per_read` | 100,000, 288 | Deduplication memory; windows per poll |
| `failure_threshold`, `breaker_reset_seconds` | 5, 60 | Circuit breaker |

Splunk's extracted fields are parsed; `_raw`, `linecount` and `splunk_server` are dropped and
`_time` is among the default timestamp fields. The search may use only built-in read-only
commands (ADR 0037): every pipeline stage and subsearch is checked when the configuration loads,
and commands that write, send, call out or start other searches (`outputlookup`, `collect`,
`delete`, `sendemail`, `outputcsv`, `rest`, `map`, `script`, custom commands) and macros are
refused with the command's name. If you need a read-only command that is refused, ask for it to
be added; there is no setting to widen the list. The test reads the token's roles and
capabilities and reports the token as write-capable when any capability is not on the read list:
`search`, `rtsearch`, `list_*`, `get_*`, `rest_properties_get`, `rest_apps_view`,
`export_results_is_visible` and `pattern_detect`.

#### SFTP or file-drop watch (spec 8.1.5)

The connector lists directories and reads `stat` results only: it never opens, writes, renames or
deletes a file, and emits `file_arrived` and `file_removed` events from names, sizes and times.
SFTP exposes no grant query, so the read-only status cannot be verified from carto: make the
account read-only on the server (no write permission on the listed directories, ideally a
chroot). The host key is pinned; obtain its SHA-256 fingerprint and confirm it with the server's
administrator out of band:

```sh
ssh-keyscan -t ed25519 sftp.example.internal | ssh-keygen -lf -
```

```yaml
  - id: src_ship_sftp
    system: sys_shipping
    type: sftp
    config:
      host: sftp.example.internal
      port: 22
      username: carto_ro
      host_key_sha256: "SHA256:REPLACE_WITH_THE_43_CHARACTER_FINGERPRINT"
      directories: ["/outbound/shipping"]
      filename_patterns: ["SHIP_*.csv"]
      poll_seconds: 60
    secret_ref: vault://kv/carto/sftp-readonly
```

| Key | Default | Meaning |
| --- | --- | --- |
| `host`, `port`, `username` | port 22 | Required for a remote source |
| `host_key_sha256` | required for a remote source | `SHA256:` plus 43 base64 characters; any other key is refused |
| `directories` | required | Remote paths, or all `local:///absolute/path` for a directory mounted into the container (SMB or NFS mounts included) |
| `filename_patterns` | `["*"]` | Shell patterns |
| `poll_seconds` | 60 | 5 to 86,400 |
| `connect_timeout_seconds`, `max_entries_per_directory` | 15, 100,000 | Limits |

The secret is a private key (PEM text, or JSON `{"private_key": ..., "passphrase": ...}`) or a
password (`{"username": ..., "password": ...}` or `user:password`). The dev file watches a local
directory instead: `directories: ["local:///data/sim/shipping/outbound"]`, no host, no secret.

#### Webhook receiver (spec 8.1.6)

Systems that can POST JSON send it to `POST /webhooks/<source_id>` on `edge-gateway`.

```yaml
  - id: src_carrier_webhook
    system: sys_shipping
    type: webhook
    config:
      signature_header: X-Carto-Signature
      timestamp_header: X-Carto-Timestamp
      timestamp_window_seconds: 300
      event_type_field: event
    secret_ref: vault://kv/carto/carrier-webhook-hmac
```

The shared secret must be at least 16 characters. The sender puts the hex HMAC-SHA256 of the raw
body under the secret in `X-Carto-Signature`, as `v1=<hex>`, `sha256=<hex>` or bare hex. When it
also sends `X-Carto-Timestamp` (epoch seconds), the signed text is `<timestamp>.<body>` and the
timestamp must be within `timestamp_window_seconds`; send it, because without it a captured
request can be replayed. Unsigned requests are rejected. The body is a JSON object or an array of
at most `max_records` (1,000) objects, at most `max_body_bytes` (1 MB) and `max_json_depth` (32)
deep; `event_type_field` names the field whose value becomes the template. The gateway answers
401 for a bad or missing signature, 404 for an unknown or non-webhook source, 413 above the size
limit, 400 for a bad body, 503 when the secret cannot be resolved, and 429 or 503 under
backpressure. Senders reach the gateway only through `proxy-webhooks` (role `proxy`, ADR 0031,
0032), which is off until you enable it; see "Push routes" in step 8.

#### OTLP through the OpenTelemetry Collector (spec 8.1.2)

Log files, syslog, Splunk HEC and OTLP reach the edge through the upstream Collector, which keeps
a persistent queue and forwards OTLP/HTTP (protobuf, zstd) to `edge-gateway` over mutual TLS.
Each source is an `otlp` entry with an empty `config`; the parse hints stay here, because the
edge parses and the collector does not:

```yaml
  - id: src_orders_log
    system: sys_orders
    type: otlp
    config: {}
    parse:
      format: logfmt
      timestamp_field: ts
      timestamp_format: iso8601
      timezone: UTC
      message_field: msg
      severity_field: level
```

Copy the template [`otel/collector.yaml`](../../otel/collector.yaml) to
`deploy/compose/collector.yaml` and edit it:

- One receiver per source, each setting the `carto.source_id` resource attribute to the source
  id above; applications that send OTLP must set that resource attribute themselves. Records
  without it are dropped by the `filter/require_source` processor, and the gateway rejects
  records whose id is not an enabled `otlp` source.
- Keep the receivers you use in `service.pipelines.logs.receivers` and delete the others.
- The `otlphttp/edge` exporter uses `ca_file`, `cert_file` and `key_file` from the
  `pki-otel-collector` volume (`/etc/carto/pki/to-edge-gateway-ca.crt`, `to-edge-gateway.crt`,
  `to-edge-gateway.key`: a client certificate from the edge-gateway CA with the
  `otel-collector` role) and the endpoint
  `https://edge-gateway:8443` (the collector appends `/v1/logs`).

The gateway accepts `application/x-protobuf` with `gzip` or `zstd`, at most 4 MiB after
decompression (`CARTO_GATEWAY__OTLP_MAX_BODY_BYTES`) and 10,000 log records per request. The
in-stack `otel-collector` publishes no port, so tail files by mounting their directories
read-only into it (step 8); its syslog (5140), Splunk HEC (8088) and OTLP (4317, 4318) receivers
are reachable only from inside the `edge` network. A collector on another host (next to the
systems whose files it tails) sends through `proxy-otlp` instead; see "Push routes" in step 8.

#### Upload: files mounted into the gateway

An `upload` source reads local files inside the `edge-gateway` container, polled every
`CARTO_GATEWAY__POLL_SECONDS` (60) and resumed from a `{file, line}` cursor. Its keys are those of
the offline analyzer ([offline-analyzer.md](offline-analyzer.md)) with absolute `paths`; the dev
file uses one for the simulator's warehouse row stream.

## Step 8: The pilot override file

As shipped, `compose.yaml` mounts `sources.dev.yaml`, mounts `otel/collector.dev.yaml`, and puts
every service on two `internal: true` networks, which have no route outside Docker. A pilot
needs to change all three. The repository does not ship a pilot override in M1; create
`deploy/compose/compose.pilot.yaml` (relative paths in it resolve against `deploy/compose/`):

```yaml
# deploy/compose/compose.pilot.yaml: your pilot's changes to compose.yaml (you create this file).
networks:
  sources: {}                     # not internal: edge-gateway's route to your source hosts

services:
  edge-gateway:
    networks: [edge, sources]
    volumes:
      - ./sources.yaml:/etc/carto/sources.yaml:ro              # replaces sources.dev.yaml
      - /etc/carto-pilot/ca:/etc/carto/ca:ro                    # CA bundles your sources need
    # extra_hosts:                                             # fixed entries for MySQL and SQL Server
    #   - "wms-replica.example.internal:10.20.4.17"
    environment:                                               # for vault:// secret_ref values
      CARTO_KMS__VAULT_URL: https://vault.example.internal:8200
      CARTO_KMS__VAULT_TOKEN_FILE: /run/secrets/vault_token
    secrets: [vault_token]

  otel-collector:
    volumes:
      - ./collector.yaml:/etc/otelcol-contrib/config.yaml:ro    # replaces collector.dev.yaml
      - /var/log/order-svc:/var/log/order-svc:ro                # one line per directory you tail

secrets:
  vault_token:
    file: /etc/carto-pilot/vault_token                         # outside the checkout
```

Compose mounts secret files with their host permissions; the services run as uid 10001, so make
the token file readable by that uid only (`chown 10001 /etc/carto-pilot/vault_token` and `chmod
0400`). The `sources` network gives the gateway unrestricted egress at the Docker level; restrict
it to your source hosts and Vault with host firewall rules, as spec 14.5 asks of the edge
("egress to configured source hosts only"; NetworkPolicies do this on Kubernetes in M6). With the
Vault KMS, also put `key-init` on the `sources` network and give it the `vault_token` secret.

Use both files for every command from here on:

```sh
COMPOSE="docker compose -f deploy/compose/compose.yaml -f deploy/compose/compose.pilot.yaml"
```

### Push routes (ADR 0032)

Two proxies, each off until you enable its profile, are the only services that publish a port
(spec 14.5). Both run `nginxinc/nginx-unprivileged` as uid 10001 with a read-only root
filesystem, and both join a non-internal `ingress` network so the port can be published: decide
who reaches them with host firewall rules.

**`proxy-otlp`** (`--profile push-otlp`, port `CARTO_OTLP_PORT`, default 4318) passes the TLS
connection through to `edge-gateway` unopened, so the gateway checks the collector's own
certificate: signed by the edge-gateway CA, with the `collector` role, which may call `/v1/logs`
and health only. Issue one certificate per collector host from the CA volume:

```sh
$COMPOSE run --rm --no-deps -v /etc/carto-pilot/collector-a:/out pki-init \
  pki issue --listener edge-gateway --name collector-a.example.internal \
  --ca-key-dir /etc/carto/pki-ca --out /out
```

It writes `to-edge-gateway.crt`, `to-edge-gateway.key` and `to-edge-gateway-ca.crt` (the
directory must be writable by uid 10001, and the key is readable by that uid only; copy the three
files to the collector host over a channel you trust). `pki issue` refuses a role the listener
does not accept and never overwrites a key. In `otel/collector.yaml` point the `otlphttp/edge`
exporter at `https://<this host>:4318` with those three files and `server_name_override:
edge-gateway`, as the template shows.

**`proxy-webhooks`** (`--profile push-webhooks`, port `CARTO_WEBHOOK_PORT`, default 8443)
terminates HTTPS for SaaS webhook senders with the certificate and key you put in
`deploy/compose/proxy-tls/` (`tls.crt` with its chain, `tls.key` readable by uid 10001; the stack
cannot obtain a certificate by itself without outbound internet). It accepts only `POST
/webhooks/<source id>` (anything else is 404, other methods 403), limits each client IP to 10
requests per second with bursts of 50 (429 beyond) and 20 open connections, caps bodies at 1 MiB,
and forwards over mutual TLS with its own certificate (role `proxy`, issued by `pki-init` on the
`pki-proxy` volume). The gateway still verifies each request's signature. Set each webhook
source's HMAC secret in the gateway's local store before you enable the route (or use a
`vault://` reference):

```sh
$COMPOSE run --rm --no-deps -T edge-gateway secret set shop_webhook_secret < hmac-secret.txt
```

Enable either or both routes by adding the profiles to every command, for example
`$COMPOSE --profile push-otlp --profile push-webhooks up -d`, or set
`COMPOSE_PROFILES=push-otlp,push-webhooks`. Their access logs carry the method, path (without the
query string), status and timing for webhooks, and connection counters for OTLP; never a header
or a body.

## Step 9: Start

```sh
$COMPOSE up -d
$COMPOSE ps
```

Start-up order is fixed by `depends_on`: `volumes-init` runs first; `migrate` runs once
`postgres` and `clickhouse` are healthy; `pki-init` and `key-init` run after `volumes-init`; `ingest-api` starts after `migrate` and
`pki-init` have completed; `edge-gateway` starts when `ingest-api` is healthy and `key-init` has
completed; `otel-collector` follows `edge-gateway`. The four one-shot jobs show as exited with
code 0 on every start. The simulator does not start without the `dev` profile. `ingest-api`'s
health check is `carto-core healthcheck --url https://localhost:8443/healthz`, which presents
the service's own certificate.

## Step 10: Verify

**Health and metrics.** Both services listen with mutual TLS on port 8443 inside the stack, so
check them from inside each container with its own certificate (the images have no curl; Python
is there):

```sh
for svc in ingest-api edge-gateway; do
$COMPOSE exec -T -e SVC=$svc $svc python - <<'EOF'
import os, ssl, urllib.request
svc, pki = os.environ["SVC"], "/etc/carto/pki/"
ctx = ssl.create_default_context(cafile=pki + "listener-ca.crt")
ctx.load_cert_chain(pki + "listener.crt", pki + "listener.key")
for path in ("/healthz", "/readyz"):
    print(svc, path, urllib.request.urlopen("https://localhost:8443" + path, context=ctx).status)
text = urllib.request.urlopen("https://localhost:8443/metrics", context=ctx).read().decode()
print("\n".join(line for line in text.splitlines() if line.startswith("carto_")))
EOF
done
```

| Service | Metrics (Prometheus text, `GET /metrics`, internal network only; spec 16) |
| --- | --- |
| `edge-gateway` | Counters `carto_edge_records_total`, `carto_edge_events_total`, `carto_edge_dropped_total` (by source and reason), `carto_edge_events_out_of_range_total` (by source and reason: `too_old` records dropped, `future` times replaced by the ingest time; ADR 0036), `carto_edge_vault_entries_total`, `carto_edge_vault_purged_total` (expired reveal vault entries removed by the hourly housekeeping), `carto_edge_batches_sealed_total`, `carto_edge_batches_sent_total`, `carto_edge_batches_parked_total`, `carto_edge_send_errors_total`, `carto_edge_heartbeats_total`, `carto_edge_requests_total`, `carto_edge_mtls_refused_total` (by route), `carto_edge_connector_errors_total`; gauges `carto_edge_buffer_bytes`, `carto_edge_buffer_depth`, `carto_edge_buffer_oldest_age_seconds`, `carto_edge_backpressure` (0 ok, 1 above 80%, 2 full), `carto_edge_connector_lag_seconds`, `carto_edge_key_versions` |
| `ingest-api` | `carto_ingest_batches_total`, `carto_ingest_events_total`, `carto_ingest_duplicates_total`, `carto_ingest_errors_total`, `carto_heartbeats_total`, `carto_ingest_mtls_refused_total`, `carto_ingest_busy_total` (429, too many batches in flight), `carto_ingest_claims_busy_total` (503, another delivery is writing the batch), `carto_ingest_claims_taken_over_total` (stale claims rewritten), `carto_ingest_out_of_range_total` (422, an `observed_at` out of range), `carto_ingest_refused_total` (422, ClickHouse refused the data) |

The stack has no Prometheus server in M1. A scraper needs a client certificate from each
listener's CA, so name it in `--services` when you run `pki init` (step 4); a name that is not a
service role gets certificates that reach health and metrics only.

**Events in ClickHouse.**

```sh
$COMPOSE exec clickhouse sh -c 'clickhouse-client --user carto --password "$(cat /run/secrets/ch_password)" \
  --database carto --query "SELECT source_id, count() FROM events GROUP BY source_id"'
```

**Logs.** Every carto service writes one JSON object per line through the redaction processor of
`carto_common.logging` (spec 14.12, ADR 0010): secret-named keys are `[REDACTED]`, body keys
`[DROPPED]`, and PEM blocks, JWTs, carto tokens, e-mail addresses and high-entropy strings are
masked; request and response bodies are never logged.

```sh
$COMPOSE logs --tail 100 edge-gateway ingest-api
```

A raw identifier, a name or a secret in these logs is a security defect: report it to us.

## The dev profile with the simulator

`make dev` is the development stack, not a pilot: it builds the images, creates the two password
files, and runs `docker compose -f deploy/compose/compose.yaml --profile dev up --remove-orphans`
in the foreground. The `simulator` service runs `carto-sim live --scenario shop --days 2 --speed
60 --out /data/sim`, replaying two simulated days of scenario A into the `sim-data` volume in
about 48 minutes (see "Live mode" in [simulator/README.md](../../simulator/README.md)). The
collector tails the five log sources with `otel/collector.dev.yaml`, the gateway watches the
`SHIP_*.csv` drops with the SFTP connector's `local://` mode, and the warehouse rows are read as
NDJSON log records because the dev stack has no source database (the header of
`deploy/compose/sources.dev.yaml` describes this gap). `make dev-down` stops the stack and
**removes every volume**, keys and vault included: never run it against a pilot.

## Step 11: The console (M2)

The stack now runs the console: `api` (spec 12), `worker` (the profiler and linker jobs) and
`web` (nginx serving the console and proxying `/api/` to `api` over mutual TLS). Users open
one origin, `https://<host>:${CARTO_WEB_PORT:-8080}`, served with the certificate in
`deploy/compose/proxy-tls/` (`make dev` generates a self-signed pair; install your own there).

### Secrets and database roles

`make dev` generates, next to `pg_password` and `ch_password`, the files `pg_ingest_password`,
`pg_app_password`, `pg_worker_password` and `oidc_client_secret` under
`deploy/compose/secrets/`. On the first start of an empty PostgreSQL volume,
`postgres/init-roles.sh` creates the roles `carto_ingest`, `carto_app` and `carto_worker` from
them; `migrate` (as the owner `carto`) grants each role what it needs (spec 14.4). A volume
created by an M1 install has no such roles: create them once by hand before the first M2 start:

```
docker compose -f deploy/compose/compose.yaml exec -T postgres psql -U carto -d carto -c \
  "CREATE ROLE carto_ingest LOGIN PASSWORD '$(cat deploy/compose/secrets/pg_ingest_password)'" \
  -c "CREATE ROLE carto_app LOGIN PASSWORD '$(cat deploy/compose/secrets/pg_app_password)'" \
  -c "CREATE ROLE carto_worker LOGIN PASSWORD '$(cat deploy/compose/secrets/pg_worker_password)'"
```

then run `migrate` again so the grants apply.

### Login

Login is single sign-on through your identity provider (OIDC authorization code flow with PKCE,
spec 14.1). Set in the pilot `.env`:

| Variable | Meaning |
| --- | --- |
| `CARTO_OIDC_ISSUER` | The provider's issuer URL (`https://login.example.com/realms/carto`). The redirect URI to register at the provider is `https://<host>:<port>/api/v1/auth/callback`. |
| `CARTO_OIDC_CLIENT_ID` | The client id registered at the provider (default `carto-console`). |
| `deploy/compose/secrets/oidc_client_secret` | The client secret, as a file (never an environment value). |
| `CARTO_OIDC_GROUPS_CLAIM` | The ID token claim that carries group names (default `groups`; ask the provider to include it). |
| `CARTO_OIDC_GROUP_ROLES` | JSON mapping group names to roles `admin`, `integration_engineer`, `business_viewer`, `auditor` (spec 14.2), applied once to `role_bindings` when the table is empty. |
| `CARTO_OIDC_BACKCHANNEL_URL`, `CARTO_OIDC_ALLOW_HTTP` | Leave unset for a real provider: the api then reaches the issuer directly over HTTPS. |

A user whose groups match no binding can sign in but may do nothing (deny by default); an
admin changes bindings in M6's Admin screen, for now in `role_bindings`. Sessions idle out after
30 minutes and end after 12 hours (`CARTO_API__SESSION_*`). The break-glass local admin arrives
in M6; until then the console needs the provider.

In the `dev` profile the stack runs Dex (ADR 0052) under the console origin at `/dex`, with
four users bound by email: `admin@carto.local`, `engineer@carto.local`, `viewer@carto.local`,
`auditor@carto.local`, password `password`. Dex is never part of a customer install.

### First look

After the edge has forwarded for a few minutes: Systems and Setup > Sources show what the edge
reported through `POST /internal/inventory` (configuration without secrets, health, last test,
ADR 0050); Setup > Field policies shows every field with its class, policy and shapes (never a
value). The worker profiles every five minutes and links after each pass; the nightly rebuild
runs at `CARTO_PROFILER__REBUILD_HOUR_UTC` (02:00) and the full link pass at
`CARTO_WORKER__FULL_PASS_HOUR_UTC` (03:00). Map > Review lists proposed links by confidence
times volume; accept, reject with a reason, or mark as association; accepted transaction links
and bridges form entity families that Entities lets you name. Every decision is audited
(`carto-core audit-verify` walks the chain; Admin > Audit shows it to admins and auditors).

### Optional LLM assist

Off by default (ADR 0004). To enable label and name suggestions through a provider you
approve: `CARTO_LLM__ENABLED=true`, `CARTO_LLM__PROVIDER=anthropic` or `openai` (any
OpenAI-compatible chat completions endpoint), `CARTO_LLM__ENDPOINT=https://...`,
`CARTO_LLM__MODEL=...`, and the credential in a file named by `CARTO_LLM__API_KEY_FILE` (mount
it as a Docker secret into `api` and `worker`). The gateway sends field paths, shapes and
template text only, never tokens or values; suggestions appear as "Suggested" until a person
confirms them; every call is audited in `llm_calls` (ADR 0054). The network policy should allow
`api` and `worker` egress to that endpoint only.

## Operations

### Back up the edge state volume

`carto_edge-state` (mounted at `/var/lib/carto`) is the only place the tokenization key, the
reveal vault and the edge audit trail exist (ADR 0014, 0025):

| Path | Content | Loss means |
| --- | --- | --- |
| `keys/` | `local-kms.key` (local KMS only), `tenant-key.json` until the first rotation, then `tenant-key.v<n>.json` for the active version and each version still in its overlap, `rotation.json` (names the active version), `vault-data-key.json`, `secrets-data-key.json`, `assertion-public.key` (M3) | Old tokens unjoinable with new ones; vault and local secrets unreadable |
| `vault.sqlite` | Reveal vault: token to AES-256-GCM ciphertext of the raw value, expiring with event retention | Reveal impossible for past events |
| `audit.ndjson` | Hash-chained record of every tokenize and reveal call | Loss of the edge's independent audit record |
| `cursors.sqlite` | Connector positions | Sources re-read; core deduplicates |
| `buffer.sqlite` | Batches not yet acknowledged by core | Those events are lost |
| `secrets.sqlite` | `local://` secrets | Re-enter them |
| `field_stats.json`, `templates/` | Field statistics and Drain3 state, so template ids survive restarts | Fields re-quarantined; templates re-learned |

With the local KMS a backup holds the master key next to the keys it wraps: encrypt the backup
and store it apart from the VM. With Vault Transit the backup is useless without Vault. Stop the
gateway so the SQLite files are consistent, copy, restart (the backup directory must be writable
by uid 10001):

```sh
$COMPOSE stop edge-gateway
docker run --rm --network none --entrypoint tar -v carto_edge-state:/var/lib/carto:ro \
  -v /srv/carto-backup:/backup carto-edge:dev -czf /backup/edge-state.tgz -C /var/lib/carto .
$COMPOSE start edge-gateway
```

Back up after `key init` and every key change, and at least daily for the vault and audit file
(spec 14.11 targets an RPO of 24 hours). PostgreSQL and ClickHouse backup tooling and the
`carto-ctl drill restore` drill arrive in M6.

### Buffer, batching and backpressure

The gateway seals batches of at most 5,000 events or 5 MB (zstd), or after 2 seconds, appends them
to `buffer.sqlite`, and only then commits the source cursor (at-least-once; core claims each
batch id in its ledger before writing it, and the event tables merge a second copy of an event,
ADR 0034). The gateway drops a record whose time lies more than one day past event retention
and gives a record more than one day ahead its ingest time (ADR 0036), so a backfill deeper than
retention loads nothing past retention. `ingest-api` refuses a batch with such an event with
422; `carto-core load-bundle` skips such events and reports how many.
Ledger rows older than retention plus one day are pruned hourly. The forwarder posts the oldest
pending batch to `ingest-api`, retries 429,
5xx and network errors with jittered backoff up to 60 seconds, and parks batches that `ingest-api`
refuses for good (400, 403, 413, 415, 422). Parked batches count toward the cap and appear in
`carto_edge_batches_parked_total`; this build has no command to inspect or clear them.

The buffer is capped at 20 GiB (`CARTO_BUFFER__MAX_BYTES`). Above 80%
(`CARTO_BUFFER__BACKPRESSURE_RATIO`) pull sources pause and the push routes answer 429; when full
they answer 503. The collector then retries without a time limit (`max_elapsed_time: 0`) and
holds batches in its persistent queue on the `otel-queue` volume; size `sending_queue.queue_size`
and that volume for the longest core outage you want to ride out. Watch
`carto_edge_buffer_oldest_age_seconds` and `carto_edge_backpressure`.

### Edge settings reference

`edge-gateway` reads `CARTO_` environment variables with `__` between levels
(`carto_edge.config.EdgeSettings`). Settings never hold secrets or key material.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CARTO_STATE_DIR` | `/var/lib/carto-edge` (Compose: `/var/lib/carto`) | State directory |
| `CARTO_SOURCES_FILE` | none | Sources file |
| `CARTO_TENANT_ID` | `default` | Must match `key init --tenant-id` |
| `CARTO_RETENTION__EVENTS_DAYS` | 30 | Vault expiry; set the same value on the core services |
| `CARTO_CORE__URL`, `__CA_FILE`, `__CERT_FILE`, `__KEY_FILE` | none | `ingest-api` address (`https://` only) and mTLS files |
| `CARTO_CORE__BATCH_EVENTS`, `__BATCH_BYTES`, `__BATCH_FLUSH_SECONDS` | 5,000, 5 MiB, 2 | Batch limits |
| `CARTO_CORE__HEARTBEAT_SECONDS`, `__RETRY_MAX_SECONDS`, `__TIMEOUT_SECONDS` | 30, 60, 30 | Heartbeats, backoff cap, request timeout |
| `CARTO_KMS__PROVIDER` | `local` | `local` or `vault` |
| `CARTO_KMS__LOCAL_KEY_FILE` | `<state>/keys/local-kms.key` | Local master key |
| `CARTO_KMS__VAULT_URL`, `__VAULT_TRANSIT_KEY`, `__VAULT_MOUNT`, `__VAULT_TOKEN_FILE` | none, `carto`, `transit`, none | Vault Transit and KV |
| `CARTO_CLASSIFY__DISTINCT_THRESHOLD`, `__DISTINCT_RATIO`, `__QUARANTINE_SAMPLES` | 1,000, 0.2, 200 | Classifier thresholds (spec 8.3) |
| `CARTO_BUFFER__MAX_BYTES`, `__BACKPRESSURE_RATIO` | 20 GiB, 0.8 | Disk buffer |
| `CARTO_REVEAL__VALUES_PER_USER_PER_HOUR`, `__MAX_TOKENS_PER_REQUEST` | 100, 20 | Reveal limits (spec 8.4) |
| `CARTO_REVEAL__ASSERTION_PUBLIC_KEY_FILE` | `<state>/keys/assertion-public.key` | Core's public key; until it exists (M3) `/internal/tokenize` and `/internal/reveal` answer 503 |
| `CARTO_PII__ENABLED`, `__SPACY_MODEL`, `__SCORE_THRESHOLD` | `true`, `en_core_web_sm`, 0.5 | PII detection (ADR 0020) |
| `CARTO_GATEWAY__HOST`, `__PORT` | `127.0.0.1` (Compose: `0.0.0.0`), 8443 | Listener |
| `CARTO_GATEWAY__TLS_CERT_FILE`, `__TLS_KEY_FILE`, `__TLS_CLIENT_CA_FILE` | none | Listener mTLS files |
| `CARTO_GATEWAY__OTLP_MAX_BODY_BYTES`, `__WEBHOOK_MAX_BODY_BYTES` | 4 MiB, 1 MiB | Body caps |
| `CARTO_GATEWAY__POLL_SECONDS`, `__POLL_CONCURRENCY` | 60, 4 | Pull scheduling when a connector names no interval |

### Audit file

`audit.ndjson` is append-only and hash-chained (`prev_hash`, `row_hash`; ADR 0014). The gateway
refuses to start when the chain is broken. Check it with
`carto-edge audit verify --file /var/lib/carto/audit.ndjson` inside the `edge-gateway` container.

The chain alone cannot show that the last rows were deleted, or that a row was rewritten with
every later hash recomputed. The gateway therefore writes an **anchor** every hour and at
shutdown (ADR 0033): one log line with `"event": "audit.anchor"` and the last row's `seq`,
`row_hash` and `ts`. Keep the gateway's container logs somewhere the edge host cannot rewrite
(your log shipper, SIEM or syslog), or send the anchors straight to your syslog server:

| Variable | Default | Meaning |
| --- | --- | --- |
| `CARTO_AUDIT__ANCHOR_SYSLOG__HOST` | none (off) | Syslog or SIEM host that receives each anchor |
| `CARTO_AUDIT__ANCHOR_SYSLOG__PORT` | 6514 | TCP port |
| `CARTO_AUDIT__ANCHOR_SYSLOG__TLS` | `true` | RFC 5425 TLS (1.2 or later); `false` sends plain TCP with the same framing |
| `CARTO_AUDIT__ANCHOR_SYSLOG__CA_FILE` | system trust store | CA bundle that signed the syslog server's certificate |
| `CARTO_AUDIT__ANCHOR_SYSLOG__TIMEOUT_SECONDS` | 5 | Connect and send timeout |

Messages are RFC 5424 (facility `log audit`, severity informational, MSGID `audit.anchor`, text
`event=audit.anchor seq=<n> row_hash=<hex> ts=<time>`) with RFC 5425 octet-counting framing. A
failed send is logged as `audit.anchor_syslog_failed` and the next hourly anchor is sent as
usual; it never delays a reveal or an audit row.

To check the file against anchors you trust, pass them as `<seq>:<row_hash>` (repeatable):

```bash
$COMPOSE exec edge-gateway carto-edge audit verify --file /var/lib/carto/audit.ndjson \
  --anchor 1532:<row_hash from the 09:00 anchor> --anchor 1610:<row_hash from the 10:00 anchor>
```

Each anchor reports `holds`, `MISSING` (rows were removed after it) or `MISMATCH` (the chain was
rewritten); any of the last two makes the command exit 1. Rows written after the newest anchor
(at most an hour) are not covered.

A power cut during an append can leave a partial last line. On its next start the gateway (or
any `carto-edge` command that writes the audit file) moves that line to
`audit.ndjson.torn-<UTC time>` next to the file, keeps it for inspection, and appends an
`audit.recovered` row with its size. `audit verify` reports such a line without failing. Any
other mismatch still stops the gateway.

## Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `ingest-api` never becomes healthy, so `edge-gateway` never starts | Migrations failed, or the `pki` volume is incomplete | `$COMPOSE logs migrate ingest-api`; check `pki-init` exited 0 |
| `pki init` exits 1: `refusing to overwrite existing private key(s)` | A partial PKI on the `pki` or `pki-ca` volume | Remove both volumes and start again (step 4) |
| `edge-gateway` exits: `local KMS master key ... not found; run carto-ctl key init` | `key-init` did not run against this volume | Run step 5 |
| Unwrap fails after changing `CARTO_TENANT_ID` | The wrapped keys are bound to the tenant id | Keep `default` in v1 |
| `sources file is invalid: <location>: <message>` | Schema error | Fix the named location |
| `config key '...' looks like a credential; use secret_ref instead` | A credential in `config` | Move it to Vault and reference it |
| A connector error naming the network policy | Host resolves to a refused address or a private range not listed | Add the source subnet to `allowed_source_cidrs`; never loopback or metadata |
| `a remote source needs host, username and host_key_sha256` | SFTP config | Add the three keys |
| SFTP connection refused after a server change | Host key no longer matches the pin | Confirm the new key out of band, update `host_key_sha256` |
| `vault:// secret references need kms.vault_url and kms.vault_token_file` | Vault settings missing | Set both on `edge-gateway` (step 8) |
| `unknown local secret '...'` | The name is not in the local store | `carto-edge secret list`; store it with `carto-edge secret set` |
| `... secret references arrive in M6 (ADR 0013)` | Cloud secret manager scheme | Use `vault://` |
| `scheduler.source_not_enabled` in the gateway log, or `carto-edge source test` exits 1 | The source's test failed or the login can write | Read the failed checks with `carto-edge source test --source <id>`; fix the grants with the role script |
| Collector logs 429 or 503 from the edge | Backpressure: buffer above 80% or full | Check `ingest-api` health, `carto_edge_send_errors_total`, `carto_edge_buffer_oldest_age_seconds` |
| Collector TLS handshake errors | Endpoint name not in the `edge-gateway` certificate | Use `edge-gateway`, or reissue with `--dns` (step 4) |
| `carto_edge_dropped_total` grows with reason `unknown_source`, or OTLP partial success counts rejected records | `carto.source_id` does not match an enabled `otlp` source | Align the collector receivers with `sources.yaml` |
| Webhook 401, 404, 413 or 400 | Signature or timestamp, unknown source, body over 1 MB, malformed body | See the webhook section |
| PostgreSQL authentication fails after editing `pg_password` | The image applies the password only on first initialisation | Restore the original file or reset the password inside PostgreSQL |
| `audit chain broken at line N` and the gateway stops | `audit.ndjson` was edited | Treat as a security incident; restore from backup |
| `audit verify --anchor` reports `MISSING` or `MISMATCH` | Rows were removed or the chain rewritten after that anchor | Treat as a security incident; compare with a backup and the anchors in your log store |
| `audit.anchor_syslog_failed` in the gateway log | The syslog target is unreachable or its certificate does not verify | Check host, port and `CA_FILE`; the anchors are still in the product log |
| `/internal/tokenize` or `/internal/reveal` answer 503 | No assertion public key | Expected until M3 |

## Security notes

- **Read-only toward your systems** (spec 2.3 invariant 1). The connector framework has no write
  method, a Semgrep rule fails CI on mutating calls in connector modules, SQL runs in read-only
  sessions behind a single-SELECT check, Splunk uses only the search export endpoint, SFTP only
  lists and stats, and upload sources open local files for reading.
- **Containers** (spec 14.6). Every service runs with a read-only root filesystem,
  `no-new-privileges`, `cap_drop: ALL` and a `/tmp` tmpfs (64 MB; 256 MB for ClickHouse), with
  JSON log files rotated at 20 MB, five per service. The carto images run as uid 10001 and the
  collector as `10001:10001`. The PostgreSQL and ClickHouse entrypoints start as root to fix the
  ownership of their data directory and then drop to their own user, so those two get back
  `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID` only. `volumes-init` runs as root with
  `CHOWN` only and no network; the other one-shot jobs and the simulator have no network either.
- **Ports.** `compose.yaml` publishes no port on the host. All services sit on the `core` and
  `edge` networks, both `internal: true`; `ingest-api` is the only service on both. Inside the
  stack: `ingest-api` and `edge-gateway` on 8443 (mutual TLS), PostgreSQL 5432, ClickHouse 8123
  (HTTP, the interface carto uses), and the collector's health check (13133) and metrics (8888)
  on its loopback only. Your override
  adds egress for `edge-gateway` (step 8).
- **Mutual TLS.** Each listener requires a client certificate signed by its own CA (TLS 1.2
  minimum), so a caller meant for another listener fails the handshake. The CA keys live on the
  `pki-ca` volume, which only `pki-init` mounts, so no running service can mint certificates.
  Each certificate names its service role and each route accepts only the roles listed for it
  (ADR 0031, step 4); each service mounts only its own keys' volume. Kubernetes installs use cert-manager with one secret per service (M6).
- **Secrets and keys.** Database passwords come from files; connector credentials from Vault
  references; key material exists only wrapped, in the `edge-state` volume. None of them is in
  an environment variable, an image or a log.
- **What reaches core.** Tokens, shapes, templates with parameters removed, low-cardinality
  attributes and timestamps; raw identifier values only in the edge vault (spec 2.3 invariant 2).
  An identifier whose values repeat a lot (a nightly batch or manifest id) has few distinct
  values; when its values contain a run of four or more digits it is tokenized anyway (ADR
  0029). One without digits (`MAN-ALPHA`) is kept in clear unless pinned: review the kept fields
  and pin such identifiers to `tokenize` (Field policy pins, step 7). The reverse also holds: a
  harmless field with four digits (a year, a port) is tokenized until you pin it to `keep`.
  Field names travel only once seen in three records under their parent and not read as a
  person's name (ADR 0030), so the first two events of a new record shape show `*` for their
  field names; objects keyed by values (256 or more distinct keys) are sent as maps whose keys
  are values. The leak test (spec
  18.3, `make leak`) checks bundles, forwarded batches, logs and ClickHouse rows for planted
  marker values, and CI replays the simulator through this stack and scans ClickHouse and the
  service logs the same way.
- **Logs.** The gateway writes its own access line (method, route template, status, duration);
  the raw path and query string are never logged, and the HTTP client libraries are held at
  WARNING because their INFO lines carry full URLs (spec 14.12).
- **Images.** The runtime base is `python:3.12-slim-bookworm` with `/bin/sh` kept for the
  entrypoint shim. Distroless bases pinned by digest, cosign signatures and SLSA provenance
  arrive in M6.

## Deferred to M6

| Item | Spec |
| --- | --- |
| Helm chart, Kubernetes NetworkPolicies, Pod Security Standard `restricted` | 5.5, 14.5, 14.6 |
| Signed images and SBOMs with verification (`carto-ctl verify-signatures`) | 14.8 |
| TLS to PostgreSQL and ClickHouse; per-service database users | 14.4 |
| AWS KMS, Azure Key Vault, GCP KMS; `aws-sm://`, `azure-kv://`, `gcp-sm://` | 8.4, 14.3 (ADR 0012, 0013) |
| Automated certificate rotation (cert-manager on Kubernetes) | 14.4 |
| Backups, restore drill (`carto-ctl drill restore`), support bundle (`carto-ctl support-bundle`) | 14.11, 16 |
| Core audit log with anchoring (`carto-ctl audit verify`), customer security review checklist | 14.9, Appendix C |

## Known gaps in this build

These are things this guide or the spec expects that the M1 build does not do yet.

1. The edge image does not include unixODBC or the Microsoft ODBC Driver 18, so SQL Server
   sources cannot run from it (installing the driver means accepting Microsoft's licence terms in
   the image build; ADR 0019).
2. Only OTLP (from a collector holding a `pki issue` certificate) and webhooks reach the stack
   from other hosts, through the push-route proxies (ADR 0032). Syslog and Splunk HEC senders
   must send to a collector on their own network, which forwards over OTLP; the in-stack
   collector publishes no port.
3. No pilot override file ships with the repository, and there is no Prometheus scraper, no
   command to reissue service certificates from the existing CA, and no command to inspect or
   clear parked batches.
4. The reveal service keeps its nonce and rate-limit state in memory, so a gateway restart
   resets them; this matters from M3, when core calls the internal endpoints.
5. Identifiers with naturally low cardinality and no run of four digits are kept in clear
   unless pinned (ADR 0026, ADR 0029).
