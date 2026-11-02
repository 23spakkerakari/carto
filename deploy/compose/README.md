# deploy/compose

Docker Compose stack for pilots (spec 5.5): PostgreSQL, ClickHouse, `ingest-api`, `edge-gateway`,
the OpenTelemetry Collector, the console (`api`, `worker`, `web`; M2, ADR 0052), the one-shot
`volumes-init`, `pki-init`, `key-init` and `migrate` jobs, the simulator in live mode and Dex
as the development identity provider under the `dev` profile, and two push-route proxies under
the `push-otlp` and `push-webhooks` profiles (ADR 0032). The assembler, detector and notifier
join in M3 and M4; Helm in M6.

```
make dev                                   # build images, generate secrets, start the stack with the simulator
docker compose -f deploy/compose/compose.yaml logs -f edge-gateway ingest-api
docker compose -f deploy/compose/compose.yaml down -v
```

## One-shot jobs

They run on every `up` and are idempotent, so a restart of the stack never fails on them:

| Job | Does |
| --- | --- |
| `volumes-init` | `chown 10001:10001` on the named volumes the carto services write (a new named volume is root-owned). The only container that runs as root, with `CHOWN` and nothing else, and no network. |
| `pki-init` | `carto-ctl pki init --if-missing`: a CA per listener (`ingest-api`, `edge-gateway`), each signing its listener's server certificate and the client certificates of the services allowed to call it, written to one volume per service (`pki-ingest-api`, `pki-edge-gateway`, `pki-otel-collector`, `pki-proxy`) so a service can read only its own keys. Each certificate also names its service role, and each route accepts only the roles listed for it (ADR 0031). The CA keys go to the `pki-ca` volume, which no running service mounts (spec 14.4, 15). |
| `key-init` | `carto-ctl key init --if-missing --kms local`: the tenant key wrapped by the local KMS on the `edge-state` volume (ADR 0012). Production uses Vault Transit (`docs/install/compose-pilot.md`). |
| `migrate` | `carto-core migrate`: ClickHouse and PostgreSQL schemas and retention TTLs. |

## The console (M2, ADR 0052)

| Service | Does |
| --- | --- |
| `api` | `carto-core api`: the console API under `/api/v1` (spec 12), TLS from its own listener CA; only the `web` proxy holds a client certificate for it (`carto-ctl pki init` role `web`). Connects to PostgreSQL as `carto_app`. |
| `worker` | `carto-core worker`: leases the profiler and linker jobs from the `jobs` table (spec 5.3). Connects as `carto_worker`. No listener. |
| `web` | nginx serving the built console and proxying `/api/` to `api` over mutual TLS and, in the `dev` profile, `/dex/` to Dex. The one origin users open: `https://localhost:${CARTO_WEB_PORT:-8080}` with the certificate in `proxy-tls/` (`make dev` generates a self-signed one; a pilot installs its own). |
| `dex` | `dev` profile only: four static users, `admin@carto.local`, `engineer@carto.local`, `viewer@carto.local`, `auditor@carto.local`, password `password`, bound to the four spec 14.2 roles by email (`CARTO_API__BOOTSTRAP_GROUP_ROLES`). A customer install sets `CARTO_OIDC_ISSUER`, `CARTO_OIDC_CLIENT_ID`, the client secret file and leaves `CARTO_OIDC_GROUPS_CLAIM` at `groups` (see `docs/install/compose-pilot.md`). |

`postgres/init-roles.sh` creates `carto_ingest`, `carto_app` and `carto_worker` on the first
start of an empty data volume from `secrets/pg_*_password`; `migrate` (as the owner `carto`)
grants each role what migration 0004 lists. An existing M1 volume needs the three roles created
by hand once (the script runs only at initialization); the commands are in the install guide.

## Push routes (ADR 0032)

Off until their profile is enabled; the only services that publish a port.

| Service | Profile | Does |
| --- | --- | --- |
| `proxy-otlp` | `push-otlp` | nginx TCP passthrough on `CARTO_OTLP_PORT` (4318) to `edge-gateway`, which checks the collector's own certificate (`carto-ctl pki issue --listener edge-gateway`, role `collector`). |
| `proxy-webhooks` | `push-webhooks` | nginx HTTPS on `CARTO_WEBHOOK_PORT` (8443) with the certificate in `proxy-tls/`; only `POST /webhooks/<source id>`, rate- and size-limited, forwarded with the `proxy` role. |

## Hardening (spec 14.6)

Every service uses the `x-hardening` block: read-only root filesystem, all capabilities dropped,
`no-new-privileges`, a size-capped `/tmp` tmpfs and capped JSON logs. The carto images run as
uid 10001 and the collector as `10001:10001`. The PostgreSQL and ClickHouse images start as root,
fix the ownership of their data directory and drop to their own user, so they get back exactly
`CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID` (`x-db-capabilities`). ClickHouse gets a
small tmpfs over `users.d` because its entrypoint writes the user file there; `carto.xml` is
mounted into it read-only. Both networks are internal; only the push-route proxies, when their
profile is enabled, join the `ingress` network and publish a port (spec 14.5). The proxies run
`nginxinc/nginx-unprivileged` as uid 10001. The one-shot jobs and the simulator have no network
at all.

`ingest-api`'s healthcheck is `carto-core healthcheck`, which presents the service's own
certificate because the listener requires mutual TLS.

`sources.dev.yaml` points the edge at the simulator's output volume; copy it to `sources.yaml`
and edit for a real pilot (see `docs/install/compose-pilot.md`).

`secrets/pg_password`, `ch_password`, `pg_ingest_password`, `pg_app_password`,
`pg_worker_password` and `oidc_client_secret` are generated by `make dev` when missing and are
git-ignored, as is the `proxy-tls/` key pair.

CI's `compose` job starts this stack, replays one simulated day into it, waits for events in
ClickHouse and runs `tools/ci/leak_scan.py` over the stored rows and the service logs (spec 18.3).
