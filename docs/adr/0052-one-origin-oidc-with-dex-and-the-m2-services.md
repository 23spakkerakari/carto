# ADR 0052: One origin for the console, OIDC from the first screen with Dex in development, and the api, worker and web services

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 10 and question 1; founder
decision 1 of 2026-10-12; spec 5.3, 6, 13, 14.1, 14.2, 14.7)

## Context

The first screen (Map > Review) needs people to sign in with roles. The spec's main path is
single sign-on through OIDC; the break-glass local admin exists only for setup and identity
provider outages. The `__Host-` session cookie, the CSRF header and the
`default-src 'self'` content security policy all assume the web app and the API share an
origin.

## Decision

- **Login.** OIDC authorization code flow with PKCE through Authlib (BSD-3). Groups come from
  the ID token claim named by `api.oidc.groups_claim` and map to roles through `role_bindings`;
  `api.bootstrap_group_roles` seeds that table once when it is empty. Server-side sessions in
  PostgreSQL behind the `__Host-carto_session` cookie (`HttpOnly`, `Secure`, `SameSite=Lax`,
  path `/`), idle timeout 30 minutes, absolute 12 hours; a synchronizer CSRF token returned by
  `GET /me` and required in `X-CSRF-Token` on every state-changing request; step-up
  (`max_age`) is wired but required by no M2 action. The break-glass admin is M6.
- **Development identity provider.** Dex (Apache-2.0) runs in the Compose `dev` profile and in
  CI with static users, one per role, so the authorization matrix runs end to end. Dex is never
  part of a customer install; the install guide names the customer's own provider.
- **One origin.** The web app is static files served by nginx (`web` service) behind the
  customer's ingress; the same nginx proxies `/api/` to the `api` service over mutual TLS on the
  internal network. Cookies, CSRF and the CSP hold without exceptions. In Compose the origin is
  `https://localhost` through the existing proxy image with a `carto-ctl pki` certificate.
- **Services.** Compose gains `api` (role `api`, TLS from `carto-ctl pki init`), `worker` (no
  listener; its own database user `carto_worker`) and `web`; the migrate job creates the
  `carto_app` and `carto_worker` database roles when it runs as the owner.
- **Authorization** is `carto_core.api.auth.require(permission)` only; the matrix test is
  generated from the committed OpenAPI document and calls every route as every role.

## Alternatives

- Break-glass local admin now, OIDC in M6: the partner pilot would run on a local password and
  the sessions, roles and matrix test would be built twice.
- A separate API host with CORS: the `__Host-` cookie cannot cross hosts; CSRF and CSP would
  need exceptions from the first day.

## Consequences

- `authlib` is a core dependency; Dex is a container image in the dev profile only.
- The web scaffold (ADR 0009) is built in this milestone with the review screen.
