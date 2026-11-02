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
The value must parse with `ipaddress` and the header is bounded at 256 bytes; a malformed
header falls back to the peer. Nothing else trusts the header: uvicorn keeps
`proxy_headers=False`.

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

## Alternatives

- A trusted-proxy address allowlist for M5: deferred to the M6 ingress work; the certificate
  role is already the stronger identity (section 1).
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
- `assemble_data_ports` and the in-memory port constructors take the audit log; the committed
  OpenAPI document changed only in the two auth operation descriptions.
- The integration tests of the Postgres path (`test_core_api_postgres.py::
  test_append_on_a_callers_connection_chains_and_rolls_back_with_it` and the rollback step in
  `test_core_api_data_ports.py`) need Docker and ran in CI, not on the laptop that built this.
- Not built: the worker's own transitions (`auto_accept`, `refresh_staleness`, `_repropose`)
  still write their audit row after the decision's transaction through `ReviewService._record`;
  they are engine transitions, not human actions under invariant 6, and the hook is in place
  for a later change. The M6 ingress allowlist can further restrict which `web` peers are
  trusted; the edge gateway's own listeners are unchanged.
