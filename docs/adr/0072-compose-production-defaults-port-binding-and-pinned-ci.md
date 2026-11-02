# ADR 0072: Compose ships production defaults, published ports bind loopback, every image is scanned and every action and image is pinned

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, package D; spec 2.3
invariants 4 and 9, 14.5, 14.8, threat-model row 9)

## Context

The security review of 2026-11-01 found the deployment and CI files weaker than the install
guide said they were. `deploy/compose/compose.yaml` gave the `api` service, which runs in every
install, the development identity-provider values as defaults: a plain-HTTP issuer allowed, a
back-channel to Dex, roles bound by the `email` claim and four `@carto.local` bindings seeded
into `role_bindings`, while the guide told the pilot to "leave those unset". The three
published ports bound every interface while the guide's "Ports" note said no port was
published. CI scanned one of the five images it builds, used floating action tags and the
`latest` tags of two scanner images, and built images from a context with no `.dockerignore`.
The unit tests ran before the simulator, so the nine simulator-gated test files skipped on
every run, and the leak test covered one scenario. The collector template listened in clear
and trusted the `carto.source_id` an OTLP sender set.

## Decision

1. **Production defaults, development values in a file.** `compose.yaml` defaults to
   `CARTO_OIDC_ALLOW_HTTP=false`, the `groups` claim and `CARTO_OIDC_GROUP_ROLES={}` (no
   seeded binding). The settings refuse an empty back-channel, so `CARTO_OIDC_BACKCHANNEL_URL`
   defaults to the issuer itself, which rewrites nothing: the api reaches the issuer directly.
   The Dex values (`http://dex:5556/dex`, `ALLOW_HTTP=true`, `email`, the four bindings) move to
   `deploy/compose/dev.env`; `make dev`, `make dev-down` and CI's compose job pass
   `--env-file deploy/compose/dev.env`. A pilot never uses the file. The variable keeps its
   documented name, `CARTO_OIDC_GROUP_ROLES`.
2. **Loopback binding.** `web`, `proxy-otlp` and `proxy-webhooks` publish
   `${CARTO_BIND:-127.0.0.1}:<port>:<port>`. A pilot sets `CARTO_BIND` to the interface it
   means and filters it in the `DOCKER-USER` chain or the cloud security group, because Docker
   programs published ports into `nat` and `FORWARD`, past the host's `INPUT` rules. The guide's
   "Ports" note says what is published and how to open it. CI reaches the stack from the
   runner itself, so the default holds there; `make dev` keeps it too.
3. **Every image scanned.** The `images` job scans `carto-core:ci` beside `carto-edge:ci`; the
   `compose` job scans `carto-ctl:dev`, `carto-simulator:dev` and `carto-web:dev` after
   `make images`, with the same severity and exit-code settings. Threat-model row 9 lists them.
4. **Pinned actions and images.** Every `uses:` in `ci.yml` and `release.yml` names a commit
   SHA with the tag in a comment, at the latest patch of the major in use: `actions/checkout`
   v4.4.0, `astral-sh/setup-uv` v6.8.0, `actions/upload-artifact` v4.6.2, `actions/setup-node`
   v4.4.0, `docker/setup-buildx-action` v3.12.0, `docker/build-push-action` v6.19.2,
   `aquasecurity/trivy-action` v0.36.0, `gitleaks/gitleaks-action` v2.3.9, `anchore/sbom-action`
   v0.24.3, `sigstore/cosign-installer` v3.10.1. The scanner images run from a version tag and
   its digest: `semgrep/semgrep:1.183.0` and `ghcr.io/google/osv-scanner:v2.7.0`. The base
   images carry their digest: `python:3.12-slim-bookworm`, `ghcr.io/astral-sh/uv:0.9.30` (the
   0.9 line the Dockerfile already used), `node:22.23.4-alpine3.24` (Node 20 is end of life;
   `package.json` `engines` allows 22) and `nginxinc/nginx-unprivileged:1.30.4-alpine`.
   The gitleaks job names one scanner release in a job-level `GITLEAKS_VERSION` (8.24.3, the
   action's own default at v2.3.9): the action downloads it without a checksum of its own, and
   the annotate step, which re-runs the binary for a JSON report, fetches the same asset and
   verifies its sha256 (recorded in the job) before running it (review 2026-11-02, finding 15).
   `renovate.json` keeps `helpers:pinGitHubActionDigests` and adds `docker:pinDigests`, so
   Renovate moves the SHAs and digests with the patch releases and pins the images
   `compose.yaml` still names by tag on its first run; a rule stops it from crossing a major
   line of a Docker image on its own.
5. **Build context.** A root `.dockerignore` keeps `.git`, `.venv`, caches, every `tests/`
   directory, `sim-out`, `eval/reports`, `legacy/`, `docs/`, the console's `node_modules` and
   build output, the Compose secrets and TLS files, bundles, databases and `.env*` out of the
   context of `deploy/docker/base.Dockerfile`, which still finds `pyproject.toml`, `uv.lock`,
   `.python-version` and the member directories. `web/.dockerignore` drops `node_modules`, the
   build and test output, `e2e` (not an input of `npm run build`) and env files.
6. **CI order.** The python job runs `make sim` for `shop` (14 days) and `people_ops` (28 days)
   before pytest, so the `needs_sim` tests execute. The integration job runs the analyzer and
   the leak test over both scenarios.
7. **Collector template.** `otel/collector.yaml` carries REPLACE-marked TLS blocks with a
   `client_ca_file` on the syslog, HEC and OTLP receivers, a `bearertokenauth` extension wired
   through `auth:` on the OTLP receivers, and a header stating that the collector runs inside
   the edge trust zone. The filelog and syslog receivers stamp `carto.source_id` through their
   `resource` key. The Splunk HEC and OTLP receivers have no such key (the collector's config
   loader is strict: a `resource:` under `splunk_hec` fails the whole file at start; review
   2026-11-02, finding 8), so each gets a pipeline of its own (`logs/hec`, `logs/otlp`) with a
   `resource/hec` or `resource/otlp` processor that upserts the attribute. Either way the
   collector names the source and a sender's value is overwritten; `filter/require_source`
   stays as the backstop.
8. **Ride-alongs.** `web/.npmrc` sets `ignore-scripts=true` (the image build already installed
   that way; Playwright's browser is installed explicitly). `deploy/compose/secrets/.gitignore`
   and `proxy-tls/.gitignore` ignore everything but themselves and the README. `make dev`
   creates the secret files under `umask 077` inside a `0700` directory. `make dev` and the CI
   compose job also generate `ch_reader_password` for the ClickHouse reader of ADR 0071.
   `core/tests/test_core_compose_limits.py` asserts, for every service, the hardening anchor
   (`read_only`, `cap_drop: ALL`, `no-new-privileges`, no `cap_add` beyond the documented
   database set, `KILL` for ClickHouse and `CHOWN` for `volumes-init`), a non-root user (a
   `user:` of uid 10000 or more, or a carto image whose Dockerfile switches to 10001, with the
   two database images and `volumes-init` as the documented root starters) and that `ports:`
   appears only on `web`, `proxy-otlp` and `proxy-webhooks`, bound through
   `${CARTO_BIND:-127.0.0.1}`, which are also the only members of the `ingress` network.

## Alternatives

- Omitting the back-channel variable when unset: Compose cannot drop a key conditionally, and
  the settings refuse an empty string, so the issuer is the default.
- A `0.0.0.0` default with a firewall note: the review's point was that the note was untrue;
  loopback is the only default that is safe without reading it.
- Scanning every image in the `images` job: it would rebuild three images the compose job
  builds anyway; scanning them where they are built keeps one build per image.
- `0600` secret files: Compose bind-mounts each secret file as it is on the host (its
  `uid`, `gid` and `mode` keys are ignored for file secrets, verified in Compose 2.26 and 2.40),
  so the container uids (postgres 999, clickhouse 101, carto 10001) could not read a `0600` file
  owned by the developer and the stack would not start on Linux. The files are `0644` inside a
  `0700` directory instead: the mount reaches the file by inode, the directory keeps other
  host users out.

## Consequences

- A pilot that did not set `CARTO_OIDC_ISSUER` gets the console with a login that goes nowhere
  (the default issuer is still the dev Dex URL over HTTPS) instead of a working plain-HTTP
  login against the wrong provider; the guide's Login table is now true as written.
- `make dev` on a machine that reaches the console from another host needs `CARTO_BIND`.
- `web/.nvmrc` and CI's `setup-node` stay on Node 20.15.1 while the image builds on 22.23.4;
  moving the toolchain is a one-line change outside this package (the web CI job owns it).
- The uv binary in the image stays on the 0.9 line; the lockfile format must stay readable by
  it. Renovate proposes the 0.10+ lines as separate PRs since the major rule blocks them.
- The third-party images in `compose.yaml` (PostgreSQL, ClickHouse, Dex, the collector, the
  nginx proxies) are still pinned by tag only until Renovate's first `docker:pinDigests` run.
- The GitHub-hosted runner's `docker run` of the osv-scanner image pulls by digest; a scanner
  release now arrives as a Renovate PR, not silently. The gitleaks release is the exception:
  Renovate does not move `GITLEAKS_VERSION` or its sha256, so a gitleaks upgrade is a manual
  change of both values (the sha256 from the release's `gitleaks_<version>_checksums.txt`).
- The console origin is `CARTO_PUBLIC_URL` (default `https://localhost:${CARTO_WEB_PORT:-8080}`),
  from which the dev profile's Dex redirect URI is derived; the first cut hard-coded
  `https://localhost:<port>` in both places, so a pilot reached from any other host name got a
  redirect URI it could not register (review 2026-11-02, finding 13). The pilot guide's Login
  table carries the variable and names the `.env` Compose actually reads
  (`deploy/compose/.env`, or `--env-file`; finding 14).
- `otel/collector.dev.yaml` (file receivers only, no network listener) is unchanged. The
  template's `bearertokenauth` extension is server-side and needs a token file on the collector
  host; a sender that only holds a client certificate deletes the `auth:` lines.
- The hardening test fails on any new service that lacks the anchor, publishes a port, joins
  `ingress` or runs as root without being listed as a documented exception.
