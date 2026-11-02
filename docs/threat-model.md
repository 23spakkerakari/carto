# Threat model

Maintained per spec Section 15. Reviewed at the end of every milestone; the review log is at the
bottom. Each row names the mitigation and the automated check that proves it.

| # | Threat | Mitigations | Verified by | Status |
| --- | --- | --- | --- | --- |
| 1 | Stolen connector credentials used to read customer systems | Secret manager references, least privilege, read-only verification, rotation, egress policies | Secrets never present in DB, API responses or logs (tests) | M1 |
| 2 | Core database exfiltration | Core holds tokens, shapes and low-cardinality attributes only | Leak test (Section 18.3) | M1 |
| 3 | Dictionary attack on tokens through search | Rate limits, audit, self-alert on unusual search volume per user, key never leaves edge | Rate-limit and audit tests | M3 |
| 4 | Malicious log content causing XSS | Text-only rendering (React escaping, ESLint sink rules, no Markdown or auto-linking of customer strings), strict CSP in the build and on the web proxy | Playwright XSS corpus (`web/e2e/xss.spec.ts`), CSP watch (`web/e2e/csp.spec.ts`), build check | M2 (built 2026-10-18) |
| 5 | Prompt injection through log content | Payloads built from metadata tables only, nonce-fenced as data, no tools, strict output schema with enumerated label types, invalid output discarded and counted, suggestions shown as "Suggested" until confirmed, egress allowlist, audit per call | Injection corpus yields only schema-valid suggestions (`core/tests/test_core_llm_injection.py`), egress tests, provider conformance | M2 (built 2026-10-18, ADR 0054) |
| 6 | SSRF via connector or channel URLs | DNS resolution checks, IP pinning, allowlists | SSRF suite (metadata IPs, rebinding, redirects) | M1 |
| 7 | ReDoS, XXE, zip bombs | RE2, defusedxml, archive limits | Payload fixtures | M1 |
| 8 | Insider bulk-revealing values | Separate grant, step-up auth, rate limits, audit, alert on bulk reveal | Authz and rate tests | M3 |
| 9 | Supply chain compromise | Signed images, SBOM, pinned dependencies, scanners, CODEOWNERS | CI gates | M0 (lockfile with hashes, scanners, SBOM signing, CODEOWNERS, Renovate); images from M1 |
| 10 | Log flood or denial of service | Backpressure, per-source quotas, buffer caps, flood sampling that keeps errors | Load tests | M1 |
| 11 | Audit tampering | Hash chain, INSERT-only grants, external anchoring | `audit verify` test | M1 edge (hourly anchor, ADR 0033); M2 core chain, append-only trigger, `carto-core audit-verify` (ADR 0051); external anchoring of the core log M6 |
| 12 | Session hijack, CSRF | Cookie flags, CSRF tokens, short sessions, step-up | Security tests, ZAP | M2 cookie flags, idle and absolute timeouts, synchronizer token, login rate limit (`core/tests/test_core_api_auth_flow.py`, `test_core_api_security_headers.py`); step-up use and ZAP in M6 |
| 13 | Accidental write to a source system | No write paths, read-only sessions, grant checks, Semgrep rule | Read-only tests | M0 (Semgrep rule with self-test); connectors M1 |
| 14 | PHI leaking into chat or email | `include_identifiers` off by default, UI warning | Notification content tests | M4 |
| 15 | Loss of tokenization key | KMS-managed wrapping, documented backup and recovery | Restore drill | M6 |

## Assets

Raw identifier values and PII (edge only), the tokenization key (edge memory, KMS-wrapped at
rest), the reveal vault, connector credentials (customer secret manager), the customer's
integration architecture (core), the audit log.

## Trust boundaries

Customer systems to edge (read-only, least privilege), edge to core (mTLS, tokenized data only),
core to channels and LLM endpoint (allowlisted egress, metadata only), users to API (OIDC, RBAC,
step-up for reveal).

## Review log

- **M0 (2026-10-06):** no runtime components exist yet. Established: hashed lockfile, CI
  scanners failing on high and critical findings, SBOM generation and keyless signing on `main`,
  CODEOWNERS for the sensitive paths, Renovate with pinned action digests, the Semgrep read-only
  connector rule with a self-test, and a simulator that plants marker values for the M1 leak
  test. The simulator writes synthetic PII only (names, emails and addresses are generated and
  carry markers); no real customer data exists in the repository.
- **M1 (2026-10-10):** the edge pipeline, the six connectors, `edge-gateway`, `ingest-api`, the
  offline analyzer and the Compose pilot stack exist (plan M1, ADRs 0011 to 0026).
  - *Assets added:* tokenization key material under the edge state directory (`keys/`: the
    KMS-wrapped tenant key versions, the reveal vault and local secret store data keys, and with
    the local KMS the master key file itself, owner-only); the reveal vault (`vault.sqlite`,
    AES-256-GCM per value, AAD tenant plus token); the disk buffer (`buffer.sqlite`, batches
    already tokenized, up to 20 GiB); connector credentials, referenced by `secret_ref` and
    resolved at use (`vault://` against Vault KV v2; `local://` against the KMS-wrapped
    `secrets.sqlite`); the edge audit file (`audit.ndjson`, hash-chained; ADR 0014; anchored
    hourly and at shutdown in the product log and optionally the customer's syslog, ADR 0033); the internal
    service keys, each in its own Compose volume mounted only by its service (ADR 0031), and the
    listener CA keys in the separate `pki-ca` volume that only the `pki-init` job mounts; the analyzer's state directory, as
    sensitive as an installed edge (ADR 0025).
  - *Push routes (ADR 0032):* `proxy-webhooks` is the one service meant to face the internet: it
    terminates HTTPS, admits only `POST /webhooks/<source id>` with per-IP rate and connection
    limits and a 1 MiB body cap, and forwards with the `proxy` role, so a webhook sender reaches
    no other route and is still authenticated by its HMAC signature at the gateway.
- **M2 (2026-10-18):** the map: profiler, linker, composite matcher, the console API and web
  app, the worker and the optional LLM gateway (plan M2, ADRs 0046 to 0057).
  - *Assets added:* the customer's integration architecture as metadata in PostgreSQL
    (`fields`, `field_sketches`, `key_links` with their evidence, `entities`), sessions
    (`sessions`, keyed by the SHA-256 of the cookie value, never the value), the core audit
    chain (`audit_log`, append-only by trigger, INSERT and SELECT only for the application
    roles), the OIDC client secret and, when enabled, the LLM provider credential (files only,
    `api_key_file`), and the per-service database passwords (`carto_ingest`, `carto_app`,
    `carto_worker`).
  - *Trust boundary, users to API (spec 5.2):* one origin (`web` nginx) terminates TLS and is
    the only holder of a client certificate for the `api` listener (role `web`, ADR 0052);
    login is OIDC with PKCE, sessions are server-side behind the `__Host-` cookie, every
    mutation needs the CSRF header and `If-Match`, and every route declares one permission
    (`require`), which the authorization matrix test derives from the committed OpenAPI
    document and checks as every role and unauthenticated. `reveal` is granted to nobody in
    M2. Rate limits are per process; a multi-replica limiter is an M6 item.
  - *Trust boundary, core to the LLM endpoint:* the gateway is off by default; the client
    refuses any host but the configured endpoint (and ignores proxy variables); payloads are
    built from metadata only and a test proves they carry no token, no identifier attribute
    and nothing from `events`; the per-call audit holds the payload hash, never the payload.
  - *What a compromised core database yields:* tokens, shapes, counts, lags, field paths,
    template text and the names reviewers gave things; no raw value (unchanged from M1; the
    sketches hold 64-bit hashes of tokens, not tokens).
  - *Residual risks noted:* the development identity provider (Dex, static passwords) exists
    only in the Compose `dev` profile and CI; the client IP in audit rows is the socket peer
    (proxy headers untrusted) until the ingress forwarding is decided in M6; proposals are not
    audited (they are engine output), decisions and linker-made transitions are.
    `proxy-otlp` passes TLS through, so collectors on customer hosts authenticate to the gateway
    with their own `collector` certificates; a stolen collector key reaches `/v1/logs` and health
    on the gateway only. Both are off unless their Compose profile is enabled.
  - *Trust boundary changes:* collector to `edge-gateway` over mutual TLS (OTLP/HTTP, client
    certificate from the edge-gateway CA with the `otel-collector` or `collector` role, ADR
    0031); `edge-gateway` to `ingest-api` over mutual TLS (client certificate from the ingest-api
    CA, role `edge-gateway`; each listener trusts only its own CA; TLS 1.2
    floor, client certificate required; `carto_core.ingest.server`); core to edge through
    `/internal/tokenize` and `/internal/reveal`, which accept only Ed25519-signed internal
    assertions (audience, permission, purpose, nonce, at most 5 minutes; plan decision 13;
    `carto_edge.reveal`). The core signer that calls them is M3. Bundles cross from the
    customer to us as files: integrity by signature, origin by hand-over (ADR 0022).
  - *Threats and mitigations that landed:* SSRF through connector hosts: resolution, rejection of
    loopback, link-local, metadata and unlisted private ranges, and address pinning for HTTP,
    PostgreSQL and SFTP (ADR 0018, `carto_edge.net.ssrf`); DNS rebinding: a name with any refused
    address is refused and the validated address is the one connected to; zip bombs and oversized
    inputs: the spec 8.1.1 archive limits, streamed bounded readers and per-record caps
    (`carto_edge.connectors.upload`, `carto_edge.pipeline.parser`), the webhook 1 MB body cap and
    depth limit (`carto_edge.connectors.webhook`), the OTLP decompressed-size cap
    (`carto_edge.gateway.otlp`), the `ingest-api` wire and decompressed caps
    (`carto_core.ingest.app`) and the bundle loader's manifest and line caps
    (`carto_core.bundle`); assertion replay: single use per nonce plus the 5-minute lifetime
    (`carto_edge.reveal`); write-capable credentials: the SQL grant checks and the Splunk
    capability check report `WRITE_CAPABLE`, and the gateway's scheduler runs every pull
    connector's test before polling and never polls a source that cannot be enabled
    (`carto_edge.gateway.scheduler`; `carto-edge source test` runs it on demand), read-only
    sessions and the single-SELECT statement check (`carto_edge.connectors.sql_dialects`), SFTP
    list and stat only; log leakage: the redaction processor on every output path
    (`carto_common.logging`, ADR 0010), the HTTP client loggers held at WARNING because their INFO
    lines carry URLs, the gateway's own access line with route templates instead of raw paths, and
    the leak test over bundle, forwarded batches, product logs and ClickHouse rows (see
    `edge/tests/test_edge_leak.py`, `make leak`, and the CI `compose` job, which replays the
    simulator through the stack and scans ClickHouse and the service logs); low-cardinality
    identifiers (batch keys) kept in clear by spec 8.3 rule 6 unless pinned: pinned in the scenario
    configs, flagged in the install guides, default safeguard open for the founder (ADR 0026); push
    acknowledgement before durability: the push routes seal their events into the disk buffer
    before answering 2xx and answer 503 when the buffer refused them (`carto_edge.gateway.app`);
    key extraction: keys exist at rest only wrapped
    by the KMS, never in environment variables, images or logs, and `carto-ctl key init` prints
    fingerprints only (`carto_edge.keys`, `carto_ctl.keyinit`); buffer exhaustion: the 20 GiB cap
    and the 80% backpressure threshold (`carto_edge.pipeline.buffer`), mapped to 429 and 503 on
    the push routes (see `carto_edge.gateway.app`); bundle tampering: signature and per-file
    digests verified before any event is read, extra files refused (`carto_core.bundle`).
  - *Residual risks and deferrals:* the SQL Server connector is tested against a fake
    connection, not a live server (ADR 0019), and the edge image lacks the ODBC driver; the leak
    run over scenario B waits for scenario B in M4 (ADR 0011); cloud KMS and cloud secret
    managers arrive in M6 (ADR 0012, 0013); the Compose stack talks to PostgreSQL and ClickHouse
    without TLS and with one database user on the internal network until M6; the OTLP endpoint has
    no per-source quota or rate limit, only body caps and backpressure (spec 15 row 10 is partly
    met); with the local KMS the master key sits in the same volume as the keys it wraps, so the
    protection is the volume and disk encryption (Vault Transit for production); the nonce cache
    and the reveal rate-limit windows are in memory and reset on a gateway restart (see
    `carto_edge.reveal`); MySQL and SQL Server connect by name after validation, leaving a
    rebinding window (ADR 0018); the listeners trust the role a certificate names, so whoever
    holds a listener's CA key (the `pki-ca` volume, the operator) can issue any role for that
    listener; webhook replay protection depends on
    the sender's timestamp header; the Vault address is an operator setting and does not pass the
    SSRF policy (`carto_edge.secrets`); certificate rotation is manual until M6; the full
    gateway path measured 2,398 to 3,600 events/s in one process on a development laptop, above
    the spec 17 target of 2,000 but with about 20% margin on a slow core (ADR 0028; the reference
    node figure is the founder's).
  - *Spec 15 rows touched:* 1 (secret references, read-only verification), 2 (tokenized events,
    leak test), 3 and 8 (edge side: tokenize and reveal rate limits, assertions and audit; core
    side M3), 6 (SSRF suite), 7 (archive limits, `defusedxml`, RE2), 9 (first service images,
    Trivy image scan), 10 (buffer caps and backpressure), 13 (read-only connectors), 15 (wrapped
    key files, backup guidance in `docs/install/compose-pilot.md`; restore drill M6).
