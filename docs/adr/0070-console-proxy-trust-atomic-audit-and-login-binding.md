# ADR 0070: Console proxy trust, one transaction per decision and its audit row, and the login binding cookie

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, package A, findings
M5, M6, M7, L1 and L2; spec 2.3 invariant 6, 12, 14.1, 14.7, 14.9, 14.12; ADR 0051, 0052,
0058 section 1)

The security review of 2026-11-01 found four console weaknesses in the auth and audit path.
Each fix is small; they are recorded together because they share one picture of the shipped
topology: a browser, the install's own nginx (`web`), and the api listener behind it under
mutual TLS.

## 1. `X-Forwarded-For` is trusted from the `web` certificate role only (M5)

**Context.** `client_ip` returned the socket peer and ignored `X-Forwarded-For` because there
was no trusted-proxy allowlist (that arrives with the ingress work in M6). Behind nginx every
request has the same peer, so the login limiter (`api.login_rate_per_minute_per_ip`) was one
bucket for the whole install: ten anonymous requests a minute to `/auth/login` locked every
user out, and the audit `ip` column recorded nginx.

**Decision.** `carto_core.api.context.client_ip` returns the last hop of `X-Forwarded-For`
when the connection presented the `web` role certificate (`PeerRoleMiddleware`,
`carto_common.mtls.peer_role`), else the socket peer. The last element is the one nginx itself
appended with `$proxy_add_x_forwarded_for`; anything before it is client-supplied and ignored.
The header is split on commas first and only the last element is bounded (45 characters, the
longest IP literal) and parsed with `ipaddress`; a malformed last element falls back to the
peer. The header as a whole is not bounded: uvicorn bounds header size, and a bound on the
whole value would be a lever for the client (Consequences). Nothing else trusts the header:
uvicorn keeps `proxy_headers=False`.

**Why the certificate role is the right signal.** Under `api.require_client_cert` the only
caller that reaches the listener with the `web` role is the nginx the install's own PKI issued
that certificate to (ADR 0031, 0052); a client that could present it already sits inside the
trust boundary. An address allowlist would add a second thing to configure and to get wrong,
and it would still have to be reconciled with the certificate. The M6 ingress allowlist may
narrow this further; it does not need to replace it. Under `api.insecure_http` there is no
certificate, so the header is ignored and the limiter is per peer, as before.

## 2. A decision and its audit row commit in one transaction (M6)

**Context.** The decision routes committed the state change through the port and then wrote
the audit row in a separate `engine.begin()`. An append failure (lock timeout, pool
exhausted) left the decision in place without its row, against spec 2.3 invariant 6, and the
client saw a 500 for a change that had happened.

**Decision.** The route builds the row and the port appends it inside the transaction of the
state change:

- `carto_core.audit.TransactionalAuditRecorder.append_on(connection, entry)` runs the advisory
  lock, the head read and the insert on a connection the caller owns; `PostgresAuditLog` and
  `InMemoryAuditLog` implement it. `carto_core.audit.TransactionHook` is what a store runs
  inside its transaction with the connection and the stored row.
- `LinkRepository.decide`, `EntityRepository.set_name` and `MetadataStore.rename_system` take
  an optional `on_write` hook and call it after the write, before the commit; the in-memory
  implementations call it before the record lands, so a raise leaves the record unchanged.
  `ReviewService.accept`, `reject` and `mark_association` pass it through.
- The ports (`LinkPort.accept`/`reject`/`mark_association`, `EntityPort.patch`,
  `SystemPort.patch`) take `audit: AuditEntryFor[Record]`, a callable from the stored record
  to the `AuditEntry`. The adapters in `data_ports.py` wrap it as the hook and append through
  the audit log they are built with (`assemble_data_ports(..., audit=...)`;
  `build_data_ports` passes `PostgresAuditLog(engine)`); the in-memory ports append through
  the log `memory_ports` gives them.
- The routes build the entry with `carto_core.api.context.audit_entry` (request id, client
  address, user agent, `before` from the record they loaded, `after` from the stored one) and
  no longer call `record_audit` for links, entities and systems. `record_audit` remains for
  rows without a port transaction: login, logout and the refused login of section 4.

ADR 0058 section 1 stays true in substance: one decision produces exactly one row, carrying
the request context the service never sees, and the review service's own recorder in the api
is still the no-op one. What changes is where the row is written: by the port, on the
decision's connection, not by the route after the commit.

## 3. The OIDC login is bound to the browser that started it (M7)

**Context.** `GET /auth/login` stored the attempt by `sha256(state)` and set no cookie; the
callback completed for any browser presenting a valid `state` and `code`. An attacker could
start a login, capture the callback URL from the provider redirect and have a victim open it,
signing the victim into the attacker's account (login CSRF, session swapping).

**Decision.** `GET /auth/login` sets `__Host-carto_login`: 32 random bytes (base64url),
`Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age` of the attempt's ten minutes. The
`LoginAttempt` stores `binding_hash = sha256(cookie)`, never the value; `PostgresSessionStore`
keeps it in the pending row's `roles_json` (`{"pending": true, "nonce": ..., "binding": ...}`),
so no migration is needed. The callback pops the attempt first (the state stays single-use),
then requires the cookie, bounds its length and compares its hash with
`hmac.compare_digest`; a missing or foreign cookie is refused through the existing `state`
failure path before the code is exchanged. Success and every failure clear the cookie. A
pending row written before this ADR has no binding and pops as `None`, so an in-flight login
across the upgrade starts over.

`SameSite=Lax` is the right strictness: the provider's redirect to the callback is a top-level
navigation, which Lax sends; a cross-site fetch or an iframe does not get the cookie.

## 4. No access log on the api listener; refused logins are audited (L1, L2)

- `build_uvicorn_config` sets `access_log=False`. uvicorn's access line prints the request
  target, and the callback's query string carries the OIDC `code` and `state` (spec 14.12);
  `RequestIdMiddleware`'s structured line already records every request without them.
- `_login_failed` writes an `auth.login_failed` row with the step as `target` and the detail
  string as `reason`: no claim value, no code, no state, no cookie. The actor is
  `carto_core.audit.ANONYMOUS_ACTOR` (`"anonymous"`), the convention from now on for a row with
  no signed-in principal; nothing before this ADR wrote one. The
  `carto_api_login_failures_total` and rate-limit metrics are unchanged.

## 5. The login limiter is per attempt on the callback, per address under a ceiling on login start (review 2026-11-02, Low)

**Context.** Section 1 made the login limiter key on the browser address, but the budget
stayed `api.login_rate_per_minute_per_ip` (ten a minute) on both `GET /auth/login` and
`GET /auth/callback`. Behind one corporate NAT or VPN egress every user shares one address, so
ten logins a minute across a whole office locked everyone out, and anyone on that NAT could do
it on purpose with ten junk requests a minute (threat-model row 12, availability).

**Decision.** `carto_core.api.ratelimit.LoginLimiter` holds three buckets and the routes use
them as follows:

- The callback is limited per login attempt, not per address. It carries the
  `__Host-carto_login` cookie (section 3) and the attempt it names is popped (single-use), so a
  callback with a valid cookie has a budget of exactly one per login start and needs no
  address limiter at all. Only a callback without a valid cookie (no state or code, an unknown
  or expired state, a missing or foreign cookie) takes from a small per-address budget,
  `api.login_rate_per_minute_per_ip` (ten a minute, as before), checked before the refusal is
  audited, so junk bounds the audit log too, and, when the request carries no cookie, before
  the store is read. The state is still popped in every case (section 3 stands).
- `GET /auth/login` keeps a per-address limiter with its own budget sized for shared egress,
  `api.login_starts_per_minute_per_ip` (default 60), under a ceiling across every address,
  `api.login_starts_per_minute_total` (default 600), so nobody can mint pending attempts in the
  store without bound. The address bucket is checked first, so one flooding address spends at
  most its own share of the ceiling. Both settings are positive integers and the ceiling must
  cover one address's budget (a model validator).
- Every `429` carries `Retry-After`. `carto_api_rate_limited_total` still counts every `429`;
  `carto_api_login_starts_rate_limited_total` and
  `carto_api_login_callbacks_rate_limited_total` tell the two login paths apart, since
  `ApiMetrics` has no labels.

A valid callback that fails later (token exchange, a bad ID token) is a refused login, audited
and counted as before; it has consumed its attempt, so it cannot be replayed to spend anything
twice.

## 6. A PATCH confirms a name only when it carries one (audit 2026-11-02, Medium; spec 2.3 invariant 5)

**Context.** `EntityRepository.set_name` took a name and set `name_confirmed = TRUE`
unconditionally, and `EntityPortAdapter.patch` filled a missing name with the current display
name, which for an unreviewed entity is the LLM's `suggested_name` (or `""`). A reviewer who
only wrote a description therefore confirmed the model's suggestion under their own identity,
and the audit row said so. The in-memory port did it right (`name_confirmed = current or name
is not None`), so the HTTP test passed while the PostgreSQL path did not.

**Decision.** `set_name` takes `name: str | None`. With a name it sets `name`, `name_confirmed`,
`description` and `updated_at` as before. With `None` the UPDATE's SET list is `description`
and `updated_at` alone (`_SET_ENTITY_DESCRIPTION`), so `name` and `name_confirmed` are not
touched, whatever they hold. The adapter passes the request's `name` through unchanged. The
in-memory repository mirrors it. A description-only PATCH on an entity with a suggestion
leaves `name` NULL, `name_confirmed` false and `display_name` the suggestion, and its audit
row's `after` says `name_confirmed: false`; a PATCH with a name confirms it. The repository
test (`exercise_entities`, run against the in-memory and the PostgreSQL repository), the
data-ports test over the adapter and the HTTP test pin all three layers.

## 7. Key-file hygiene from the audit of 2026-11-02 (B to G)

Six smaller findings of the same audit, recorded together because each is one narrow rule:

- **`key rewrap` is resumable and never asks for `.new` to be removed (B, Medium-High).**
  Phase two of `KeyManager.rewrap` replaced the staged files outside the `try`, so an
  `OSError` or a crash left `local-kms.key.new` beside files already wrapped under it, the
  next run refused because `.new` existed and told the operator to remove it, which would have
  destroyed the only key for those files; and a half-rewrapped set could not be repaired
  because `rewrap` refused a file already under the new key id. Now: when the new KMS key id
  differs from the current one, a file already under the new id is verified (unwrapped) and
  left alone, a file under the current id is re-wrapped, and a file under neither is refused by
  name before anything is replaced; when the ids are the same (Vault Transit after a transit
  key rotation) every file is re-wrapped as before. Phase two runs inside a `try`: a failure
  names the file, removes only the staging files not yet installed, reports how many files are
  under each key and says `re-run carto-edge key rewrap to resume`. `_rewrap_local` opens an
  existing `.new` instead of refusing, never deletes it, and installs the new master only
  after every file is under it, copying the old master to `.retired` first and then renaming
  `.new` over `local-kms.key`, so a master file exists at every instant (the earlier two
  renames had a window with none). A `.retired` without a `.new` is an earlier, completed
  rewrap the operator has not finished and still refuses; that message tells them to verify
  the backups and destroy it per the runbook, not to remove anything. The runbook gains a
  "Rewrap interrupted" section. Tests cut the power on the second staging replace and show
  the next run finishing with every file under the new master.
- **`VaultTransitKms` builds its own isolated client (C, Medium).** `httpx.Client(timeout=...)`
  left `trust_env` on, so `HTTPS_PROXY` or `SSL_CERT_FILE` in the environment steered the
  Vault token and the plaintext key material to a proxy with an attacker CA. The client is now
  built like the KV resolver's: `trust_env=False`, an explicit TLS 1.2-minimum context from
  the system store or an optional `ca_file`, `follow_redirects=False`; and every request
  passes `follow_redirects=False` so a caller-supplied client cannot follow either. `ca_file`
  is a constructor parameter; the edge's `KmsSettings` has no `vault_ca_file` field yet and
  `build_kms` passes none (Consequences).
- **Certificates are created exclusively, after the key (D, Low).** `write_pem` and the CA
  copies in `carto-ctl pki` wrote certificates with `write_bytes`, following a planted symlink
  and overwriting, and wrote the certificate before the key, so the refusal path left a new
  certificate beside an old key. `crypto.create_public` is `_create_private` with mode 0644;
  `write_pem` checks that nothing (file, directory or symlink, dangling included) is at either
  path, writes the key, then the certificate, withdraws the key it just wrote when the
  certificate cannot be written, and fsyncs the directory. `pki init` checks every file it
  would write before touching anything (keys named first in the message); `pki issue` checks
  its two certificate paths too.
- **The local secret store's files are owner-only from creation (E, Low).** `sqlite3.connect`
  created `secrets.sqlite` under the umask and ran `PRAGMA journal_mode=WAL` before the
  `chmod`, so the `-wal` and `-shm` side files kept the umask mode. The file is now created
  exclusively with mode 0600 before SQLite opens it (an empty file is an empty database), and
  the database and every side file that exists are `chmod 600` after the first connection.
  `LocalKms.open` refuses, on POSIX, a master key file that group or others can read, naming
  the path and its mode.
- **Bounds (F, Low).** `carto-ctl pki init|issue --days` stops at 398 (`MAX_LEAF_DAYS`, the
  public-CA ceiling for a leaf; the default stays 90 per spec 14.4; CAs are not issued from
  the command line and keep ten years). The edge's Vault token file is read with the same
  4096-byte cap as `carto-ctl key init`. `carto-edge key rotate --overlap-days` stops at 366.
  The `secret.set.intent` audit row no longer carries the value's byte length.
- **Suggestion labels cannot look like links (G, Low).** The label pattern admitted
  `http://evil.co/x`. A `label` is refused when it contains `://`, a `www.` host, or a dot
  followed by two to six letters that end a word (`evil.co`, `Order.COM`); `rationale` gets the
  label's character class plus `,;'?!` (so still no `<>{}`, backslash or control character) and
  is refused when it carries `://` or `www.`, but may quote a dotted field path. Such answers
  fail the schema and are discarded and counted as invalid output like any other.

## Alternatives

- A trusted-proxy address allowlist for M5: deferred to the M6 ingress work; the certificate
  role is already the stronger identity (section 1).
- Raising `login_rate_per_minute_per_ip` alone for section 5: it would raise the junk budget
  of the callback with it, and no single number fits both a lone laptop and an office behind
  one address. Per-attempt is the right unit for the callback; the start budget and the ceiling
  bound the one thing a start costs, a pending attempt in the store.
- Refunding the address token on a valid callback instead of skipping the limiter: a refund
  needs a second lock round-trip and still refuses the office's eleventh valid callback in the
  burst before the first refund lands.
- A thread-local "current transaction" the audit log reads for M6: implicit, and wrong the
  first time a port runs an append outside the thread that opened the transaction. The
  connection is passed explicitly.
- Binding the login to the session cookie or to the state alone for M7: there is no session
  yet, and the state is exactly what the attacker holds.
- Keeping the access log with a redacting formatter for L1: the structured line exists; two
  logs of the same request are two places to get redaction wrong.

## Consequences

- `GET /audit` shows `auth.login_failed` rows by `anonymous`; an operator reads the step and the
  reason, never a value.
- The audit `ip` column holds the browser address behind the web proxy; in `insecure_http`
  deployments it still holds the peer.
- The first cut bounded the whole `X-Forwarded-For` at 256 bytes before parsing, so a browser
  that padded its own header past that made `client_ip` fall back to the nginx address: every
  such browser shared the proxy's login bucket and `audit.ip` recorded the proxy (review
  2026-11-02, finding 6). The bound now applies to the last element alone, after the split, and
  `test_core_api_app.py` pins that a padded header keeps the browser's own bucket.
- `assemble_data_ports` and the in-memory port constructors take the audit log; the committed
  OpenAPI document changed only in the two auth operation descriptions.
- Section 5: a browser behind a shared address is locked out of `/auth/login` only when that
  address starts more than sixty logins a minute, and never out of the callback; the limiter
  remains per replica (M6). `ApiContext.login_limiter` is a `LoginLimiter`, two settings and
  two counters are new, and the OpenAPI document is unchanged. A callback that carries a cookie
  still costs one attempt lookup before its junk budget is checked; that is the same cost as
  any anonymous request's session lookup and bounded the same way.
- The integration tests of the Postgres path (`test_core_api_postgres.py::
  test_append_on_a_callers_connection_chains_and_rolls_back_with_it` and the rollback step in
  `test_core_api_data_ports.py`) need Docker and ran in CI, not on the laptop that built this.
- Section 6: a description-only PATCH changes `updated_at` (and so the ETag) without touching
  the name columns; a client that reads `name_confirmed` from the response sees it unchanged.
  `EntityRepository.set_name` is a protocol change; both implementations and the one caller
  changed with it.
- Section 7: an operator who has a `local-kms.key.new` from a run interrupted before this
  change runs `carto-edge key rewrap` once and it finishes; nothing has to be removed by hand.
  `KmsSettings` still has no `vault_ca_file` field (it lives in `carto_edge.config`, outside
  this package), so a Vault Transit server signed by a private CA needs that CA in the
  container's system trust store until the field is added and threaded to
  `VaultTransitKms(ca_file=...)`; `SSL_CERT_FILE` no longer works for it, on purpose. On
  POSIX, an install whose `local-kms.key` is group- or world-readable stops starting until the
  mode is fixed; the message says how. `pki init` now also refuses a directory holding a stray
  certificate where it would write one; `--if-missing` is unchanged (a complete run is still
  a success that writes nothing). A rationale that quotes a URL is now invalid output.
- Not built: the worker's own transitions (`auto_accept`, `refresh_staleness`, `_repropose`)
  still write their audit row after the decision's transaction through `ReviewService._record`;
  they are engine transitions, not human actions under invariant 6, and the hook is in place
  for a later change. The M6 ingress allowlist can further restrict which `web` peers are
  trusted; the edge gateway's own listeners are unchanged.
