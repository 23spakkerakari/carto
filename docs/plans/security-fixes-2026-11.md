# Security fixes, November 2026

Status: in progress on branch `security-fixes` (cut from `m3-trace-spec` at e12b626), started
2026-11-02.
Source: the full-project security review of 2026-11-01 (six parallel code reads against spec 2.3
and the threat model, bandit, pip-audit, npm audit). The founder asked for the High and Medium
findings to be fixed in that order. Lows are not in this plan.

## Goal

Every High and Medium finding is fixed with a regression test, or listed under "Deferred" with
its reason. No security invariant of spec 2.3 is weakened. Engine changes (templates,
classifier) run `make eval` on both scenarios and the deltas are recorded in the PR.

## Decisions recorded with this plan

ADR numbers 0060 to 0066 belong to the M3 demo cut; this plan uses 0067 to 0072: one per package,
with H and T sharing 0067.

| Decision | ADR |
| --- | --- |
| Splunk `from` accepts only `datamodel:` and `lookup:` targets; an unquoted backslash before a stage or subsearch delimiter is refused; weakened transport on a source needs an explicit `insecure_transport: true` that is audited and warned | 0067 |
| A template token is a constant only once it appears in three distinct messages of its cluster; identical repeats count once. Cardinality and detector hits ratchet: a field that was ever high-cardinality or ever carried a detector hit never returns to `keep` without an admin pin | 0068 |
| Core refuses value-shaped attribute values and template text (email, UUID, long hex, digit runs, JWT, key shapes) at ingest and in the bundle loader, and masks the same shapes before any text reaches the LLM provider; inventory `secret_ref` and config values are validated and masked | 0069 |
| The console trusts `X-Forwarded-For` only from a peer that presented the `web` role certificate; a decision and its audit row commit in one transaction; the OIDC login is bound to the browser by a `__Host-carto_login` cookie; the api access log is off | 0070 |
| `carto_ingest` gains DELETE on `ingest_batches`; `carto_worker` loses the identity tables; `llm_calls` becomes append-only like `audit_log`; the api and worker use a read-only ClickHouse user, created by the Compose init with the others (the migrate-created users stay M6) | 0071 |
| Compose ships production defaults (no plain-HTTP issuer, no Dex back-channel, `groups` claim, no bootstrap bindings) and the dev values move to `deploy/compose/dev.env`; published ports bind loopback unless `CARTO_BIND` says otherwise; every image built in CI is scanned; actions and scanner images are pinned by digest; `.dockerignore` files; CI runs the simulator before the unit tests and the leak run over both scenarios | 0072 |

## Work packages

Each package lands as conventional commits with tests first. Packages run in parallel on
disjoint files; the integration run (`make check`, `make eval` on shop and people_ops) happens
once at the end.

### H. Highs and dependencies (`connectors/splunk_spl.py`, `uv.lock`)

- H1. `from` is accepted only with a `datamodel:` or `lookup:` target; `savedsearch:` and any
  other target are refused with the ADR 0037 message. Test: the three forms.
- M13. `_stages` and `_matching_bracket` refuse an unquoted backslash immediately before `|`,
  `[`, `]` or a backtick instead of treating it as an escape. Test: each delimiter.
- Dependencies: joserfc 1.7.7, filelock 4.2.6, certifi 2026.10.23, setuptools 84.2.1 in
  `uv.lock` (the latest at lock time); pip-audit clean with `--strict`.
- H2 is the `fix-c2` branch (ADR 0037 items 1 to 3, upload confinement, SFTP `config=None`).
  Merged here if it merges cleanly; otherwise recorded under Deferred with the conflict.

### E. Values in clear at the edge (`pipeline/templates.py`, `classify.py`, `stats.py`, `forward.py`, `test_edge_leak.py`, simulator markers)

- M1. The template store tracks the distinct message digests of each cluster (bounded, per
  cluster) and uses the mined template only once the cluster holds `MIN_CLUSTER_SIZE` distinct
  messages; until then every token is a parameter. The existing test that locks in a
  thrice-repeated message as constants is inverted. Parameters of such messages become fields
  and go through the classifier like any other value.
- M2. `FieldStats` persists two sticky flags, `high_cardinality_seen` and `detector_hits_seen`
  (snapshot version 2, version 1 loads with both false). Rule 5 treats a field as high
  cardinality when either the live estimate or the flag says so; rule 6 drops when hits were
  ever seen. An admin pin to `keep` still wins (existing behaviour); PII classes still cannot be
  pinned. Test: the flood scenario from the review (900 distinct names, then 4,000 `ok`) stays
  tokenized; a genuine enum that was never high cardinality still becomes `keep`.
- M4 (edge half). `strip_config` runs every string value through `mask_string` so a URL with
  userinfo, an email or a token shape never leaves in the inventory. Test: `https://u:p@host`.
- M21. The simulator plants a `secrets` marker category (AWS access key id, bearer token,
  `password=` in prose, PEM header, DSN with userinfo) into noise lines of both scenarios and
  records them in `markers.json`; the leak test scans for them like the other categories.
- M22. `test_edge_leak.py` parametrises the end-to-end run over `shop` and `people_ops` (two
  days each) instead of hard-wiring shop. The leak test plants a thrice-repeated message probe.
- `make eval` on both scenarios before and after; deltas in the PR.

### C. Core backstops (`carto_schema/event.py`, `bundle.py`, `inventory.py`, `llm_gateway/payload.py`)

- M3. `AttributeValue` and `TemplateText` gain an `AfterValidator` that refuses value shapes:
  email, UUID, 16+ hex, 6+ digit run (already), JWT, AWS key id, PEM header, `key=value` with a
  secret key word. The bundle loader's `BundleField.path` and `BundleTemplate.template_text` use
  the same `FieldName` and `TemplateText` types as the live path. `clean_text` masks the same
  shapes before the payload goes to the provider. Tests: each shape is refused at ingest, in the
  bundle and masked in the payload; low-cardinality words (`approved`, `EUR`, `GET`) still pass.
  `make schema` regenerates the committed JSON Schemas.
- M4 (core half). `SourceInventory.secret_ref` uses `SecretRef`; config keys that
  `is_secret_key` flags are refused; config string values are masked with `mask_string` on the
  way into `sources.config_json`. Test: `secret_ref: hunter2` is a 422; `config.url` with
  userinfo is stored masked.

### A. Console auth and audit (`api/context.py`, `routes_auth.py`, `sessions.py`, `server.py`, `audit_postgres.py`, `data_ports.py`)

- M5. `client_ip` returns the last `X-Forwarded-For` hop when `request.state.peer_role == "web"`
  (the mTLS role nginx presents), else the socket peer. Test: with the `web` role the limiter
  and the audit `ip` see the browser address; without it the header is ignored.
- M6. The decision ports take the audit append inside their own transaction: the route builds
  the `AuditEntry` first and the port's `decide` runs the state change and the audit insert on
  one connection, rolling both back on failure. Same for entities and inventory PATCH. Test: an
  audit store that raises leaves the link unchanged and the response a 500.
- M7. `GET /auth/login` sets `__Host-carto_login` (random 32 bytes, Secure, HttpOnly,
  SameSite=Lax, 10 minutes); the attempt stores its SHA-256; the callback requires the cookie
  to match and clears it. Test: a callback without the cookie, or with another attempt's cookie,
  is refused.
- L1 and L2 ride along: `access_log=False` on the api listener; a failed login writes an
  `auth.login_failed` audit row with the step and no claim values.

### G. Database grants (`core/migrations/postgres/versions/0006_*.py`, `deploy/compose/clickhouse/users.d`)

- M9. Migration 0006 (0005 belongs to the M3 demo branch) grants `DELETE ON ingest_batches TO carto_ingest`; the grant test is
  amended; a role-connected integration test exercises release and prune as `carto_ingest`.
- M11. Migration 0006 revokes `sessions`, `login_attempts`, `users`, `role_bindings` from
  `carto_worker` (it keeps `SELECT` on `settings`), and puts the `audit_log` trigger pair on
  `llm_calls` with `SELECT, INSERT` only for both roles.
- M10. `users.d/carto.xml` defines a `carto_reader` user (readonly profile, password from the
  `ch_reader_password` secret file) alongside the owner; the api service uses it. The worker
  stays on the owner: the M3 demo cut makes it write the trace tables, and a worker user of
  its own is M6.
  ADR 0071 records that the migrate-created users remain M6 work. (The compose change itself
  is applied in package D to keep `compose.yaml` under one owner.)

### T. Connector transport and CI rules (`connectors/sql.py`, `splunk.py`, `gateway/scheduler.py`, `tools/semgrep`, `carto_common/pki.py`)

- M12. `SqlConfig` and `SplunkConfig` accept weakened transport (`sslmode` below `verify-ca`,
  `ssl: false`, `encrypt: false`, `trust_server_certificate: true`, `verify_tls: false`,
  `allow_plaintext: true`) only when `insecure_transport: true` is set; the scheduler writes an
  edge audit row `source.insecure_transport` and a warning when it enables such a source.
  Tests: config without the flag is refused; with it, one audit row and one warning.
- M14. The Semgrep rule gains `$C.build_request("POST"|"PUT"|"PATCH"|"DELETE", ...)`,
  `$C.exec_driver_sql("=~/write/")`, `$C.execute(text("=~/write/"))`, `sftp.open(..., "w")`,
  `run()`/`create_process()` on asyncssh connections; the fixture file covers each; the Splunk
  export `nosemgrep` keeps its reason.
- M20. `write_pem` creates the key through `_create_private` (exclusive, 0600 from the first
  instant). Test on POSIX: mode is 0600 and the file is never world-readable.

### D. Deployment and CI (`deploy/compose/**`, `Makefile`, `.github/workflows/*.yml`, `otel/collector.yaml`, `.dockerignore`, `docs/install/compose-pilot.md`)

- M8. `compose.yaml` defaults: `ALLOW_HTTP_ISSUER` false, back-channel unset, groups claim
  `groups`, no bootstrap bindings. `deploy/compose/dev.env` holds the Dex values; `make dev` and
  the CI compose job pass `--env-file deploy/compose/dev.env`. The guide's Login table matches.
- M10 (compose half). `ch_reader_password` secret; the api uses `carto_reader`.
- M15. The CI image job scans `carto-core:ci` as well; the compose job scans `carto-ctl`,
  `carto-simulator` and `carto-web` after `make images`.
- M16. Every `uses:` is pinned to a commit SHA with the tag in a comment; `semgrep/semgrep` and
  `osv-scanner` run from a tagged version; the base image and uv are pinned by digest.
- M17. `.dockerignore` at the root and in `web/` (node_modules, dist, tests, caches, secrets).
- M18. Published ports become `${CARTO_BIND:-127.0.0.1}:<port>:<port>`; the guide's "Ports"
  note says what is published and how to open it.
- M19. `otel/collector.yaml` carries TLS and bearer-auth REPLACE blocks for the OTLP and syslog
  receivers and a comment that the collector must sit in the edge trust zone.
- M23. The CI python job runs `make sim` for both scenarios before pytest so the simulator-gated
  tests execute; the integration job already does. The leak step runs both scenarios.
- Ride-alongs: `web/.npmrc` `ignore-scripts=true`; the secrets and proxy-tls `.gitignore`
  files ignore everything but their README; `test_core_compose_limits.py` also asserts the
  hardening anchor and the absence of `ports:` on every service except the three proxies;
  `make dev` writes secret files with `umask 077`.

## Pull requests

Two PRs, both targeting `m3-trace-spec`, because the M3 demo cut (`m3-demo`) rewrites the
decision routes, `EntityRepository.set_name`, `compose.yaml` and CI at the same time.

- PR-1, branch `security-fixes-1`: packages H, T, E and C plus `uv.lock`, cherry-picked from
  `security-fixes`. Touches no file the demo changes. Opens as soon as `make check` and
  `make sec` are green.
- PR-2, branch `security-fixes`: packages A, D and G, rebased onto `m3-trace-spec` after the
  demo PR lands. Checklist for that rebase, none of which may be skipped:
  1. Migration 0006 `down_revision` becomes `"0005"` (the demo's trace migration); the linear
     history test's comment and the ADR 0071 note are updated to match.
  2. `routes_links._decide` and `EntityPortAdapter.patch`: the demo enqueues a full assembly
     inside the decision route; the audit hook of ADR 0070 must run in the same transaction as
     that enqueue.
  3. `EntityRepository.set_name` is rewritten by both branches and the merge is not mechanical:
     the merged signature is `set_name(tenant_id, entity_id, *, name: str | None, description,
     now, on_write=None, expected_version=None)`; the demo adds `expected_version`, `version + 1`
     and `StaleVersionError`, this branch adds `on_write` and the conditional name (a description-
     only write leaves `name` and `name_confirmed` alone, ADR 0070 section 6). `_SET_ENTITY_DESCRIPTION`
     must also bump `version`, carry the `expected_version` predicate and the `rowcount == 0`
     disambiguation; the in-memory repository keeps `version + 1` with the conditional name; the
     adapter passes `name=name` unchanged.
  4. `make eval-engine` on both scenarios re-run on the rebased branch; deltas recorded.
  5. ADR 0060, `docs/plans/M3-demo.md` and `CLAUDE.md` say `fix-c2` is already merged through
     PR-1 so nobody merges it twice.

## Deferred

Filled in as packages land: anything a package could not build, with its reason and ADR
Consequences entry.

## Acceptance

- `make check` green; `make sec` clean (bandit, pip-audit).
- `make eval SCENARIO=shop` and `make eval-engine SCENARIO=people_ops DAYS=28` within the spec
  18.4 targets, deltas recorded.
- Each High and Medium finding has a test that fails on `m3-trace-spec` and passes here.
- CI green on all jobs, including the new image scans and both leak runs.
