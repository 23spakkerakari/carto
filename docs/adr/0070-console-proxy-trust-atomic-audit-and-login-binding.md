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
- Not built: the worker's own transitions (`auto_accept`, `refresh_staleness`, `_repropose`)
  still write their audit row after the decision's transaction through `ReviewService._record`;
  they are engine transitions, not human actions under invariant 6, and the hook is in place
  for a later change. The M6 ingress allowlist can further restrict which `web` peers are
  trusted; the edge gateway's own listeners are unchanged.
