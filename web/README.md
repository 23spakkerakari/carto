# web

The carto console (spec section 13): Map > Review, Map > Entities, Systems, Setup > Sources,
Setup > Field policies and Admin > Audit. Vite + React 19 + TypeScript strict, TanStack Query,
Radix primitives, Tailwind 4, React Router 7; Vitest and Testing Library for units, Playwright
with axe for the browser. Built in M2 (plan `docs/plans/M2.md` package F; ADRs 0009, 0050,
0052, 0057).

## Run

Node 20.15 or later (`.nvmrc`), npm 10. `npm ci` installs from the committed lockfile.

| Command                | What it does                                                                                                 |
| ---------------------- | ------------------------------------------------------------------------------------------------------------ |
| `npm run dev:mock`     | Vite dev server at <http://localhost:5173> with the msw mock API (`VITE_MOCK_API=1`). No backend needed.     |
| `npm run dev`          | Dev server proxying `/api` to the Compose stack (`CARTO_API_URL`, default `https://localhost:8443`).         |
| `npm run build`        | Production build to `dist/` plus `scripts/check-build.mjs` (no mock code, strict CSP meta tag, same origin). |
| `npm run preview`      | Serves `dist/` at <http://localhost:4173>.                                                                   |
| `npm run lint`         | ESLint: typescript-eslint strict type-checked, react-hooks, jsx-a11y strict, the spec 13 sink rules.         |
| `npm run typecheck`    | `tsc -b` over the app, the config files and the e2e suite; includes the OpenAPI contract check.              |
| `npm run test`         | Vitest (jsdom): API client, evidence formatter, reject form, formatting.                                     |
| `npm run e2e`          | `vite build --mode e2e` (production bundle plus the mock) then Playwright against `vite preview`.            |
| `npm run e2e:install`  | Downloads the Chromium build Playwright needs (once per machine; CI runs it every job).                      |
| `npm run format:check` | Prettier.                                                                                                    |
| `npm run fonts`        | Copies the IBM Plex faces from `@ibm/plex-*` into `public/fonts` (committed; rerun after a Renovate bump).   |
| `npm run gen:api`      | `openapi-typescript` over `core/openapi/openapi.v1.json` into `src/api/generated.d.ts`.                      |

Mock API flags, read once at page load: `?as=admin|auditor|business_viewer` (default:
integration engineer), `?logged_out=1` (every call answers 401), `?corpus=xss` (adds the hostile
fixtures of the XSS corpus). The mock honours cursors, `If-Match` (412 on mismatch, 428 when
missing), the CSRF header (403 without it) and the spec 14.2 permissions.

Against the stack, `GET /me` answering 401 sends the browser to `/api/v1/auth/login`; the API's
OIDC flow sets the `__Host-carto_session` cookie and returns to `/`.

## API layer

`src/api/types.ts` mirrors `packages/carto-schema/carto_schema/api.py` by hand (names, enums,
nullability). `src/api/generated.d.ts` is what `npm run gen:api` produces from the committed
OpenAPI document, and `src/api/contract.ts` asserts at compile time that every hand-written view,
patch and enum is assignable both ways with its generated schema, so `npm run typecheck` fails
on drift. TODO (orchestrator): when package E's document is stable, switch the hooks to import
the generated schemas directly and delete the hand-written copies; until then both files are
committed and the contract check keeps them equal. Note: FastAPI's 422 validation errors arrive
as `HTTPValidationError`, not problem+json; the client treats them as a generic "Request failed".

`src/api/client.ts`: `fetch` with `credentials: 'same-origin'`, JSON only, RFC 7807
`application/problem+json` parsed into a typed `ApiError` (never echoing request content), 401
redirects to the login route, `If-Match` from the item's `etag`, `X-CSRF-Token` from the cached
`GET /me`, `Retry-After` on 429. `src/api/hooks.ts`: TanStack Query hooks per resource, in
memory only (no persister, no browser storage), optimistic decisions with rollback.

## Security statement

- Every string that comes from customer data (field paths, template ids, system and entity
  names, sentences, configuration values, audit payloads) is rendered as text through React,
  wrapped in `<Untrusted>` (a `<bdi>` so an RTL override cannot reorder the chrome). No
  `dangerouslySetInnerHTML`, no `innerHTML`, no Markdown, no auto-linking, no `eval`: ESLint
  forbids the sinks (`no-restricted-syntax`) and the libraries (`no-restricted-imports`).
- The production build runs under the spec 14.7 policy exactly:
  `default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`
  (one definition in `src/csp.ts`; `deploy/nginx.conf` sends it as a header; the build injects
  it as a meta tag without `frame-ancestors`, which browsers ignore in a meta element and which
  the header and `X-Frame-Options: DENY` enforce; `scripts/check-build.mjs` and
  `e2e/csp.spec.ts` assert both forms). Tailwind
  compiles to one stylesheet; nothing injects styles or scripts at runtime. The dev server adds
  one nonce for Vite's own refresh preamble and CSS HMR, nothing else.
- Fonts are self-hosted (`public/fonts`, OFL-1.1); no font or script CDN.
- API data lives in TanStack Query's memory. The session is the HttpOnly `__Host-` cookie the
  API sets; the console never reads or stores a credential.
- The XSS corpus (threat model row 4) plants `<script>`, `<img onerror>`, `<svg onload>`,
  `javascript:` URLs, Markdown links, `{{ }}` templates, HTML entities, an RTL override, an
  `<iframe>`, an anchor, a CSS `url()` and 1,024-character strings in template ids, field paths,
  shapes, forms, system names, descriptions, sentences, entity names, configuration values and
  audit payloads; `e2e/xss.spec.ts` asserts on every screen that nothing executes, no request
  leaves the origin, no dialog opens, no element gains an `href`/`src`/handler, and every payload
  is present as text.

## Design

The founder's drawing-office console. Light only; monochrome chrome with colour reserved for
state (a 6 px square marker in ok, warn, bad or idle); IBM Plex throughout (Plex Sans Condensed
500/600 for headings, title-block labels, table headers, tabs and nav; Plex Sans for body copy;
Plex Mono for identifiers and figures); every radius 2 px. Rules carry hierarchy: a 2 px ink rule
opens each page's title block (`PageHeader` with labelled `block` cells and, on list pages, `meta`
cells), a 1.5 px rule closes a table header, hairlines separate rows; tables are dense and ruled,
never boxed in cards. One highlighter, `#F6DE63`, marks the current queue item and confirmed
states. Copy is business language ("same identifier", "matches", "last seen", "typical delay");
the technical detail (feature names and raw values, template ids, link ids, scores) sits behind a
"Details" disclosure. Tokens live in `src/styles.css` (`@theme`).

## Accessibility

WCAG 2.2 AA target. The review queue is an ARIA grid with row-level focus: Up and Down move
the current item, Home and End jump, A, R and S accept, reject and mark as association while
focus is in the grid, and the key hints are visible only then. The reject dialog is the native
`<dialog>` (focus trapped, Escape closes, focus returns to the opener). Every page, the open
disclosure and the open dialog pass axe (`wcag2a`, `wcag2aa`, `wcag21a`, `wcag21aa`,
`wcag22aa`) with zero violations in `e2e/a11y.spec.ts`.

## Dependencies and licenses

Runtime (bundled):

| Package                         | Version               | License |
| ------------------------------- | --------------------- | ------- |
| react, react-dom                | 19.1.10               | MIT     |
| react-router                    | 7.18.4                | MIT     |
| @tanstack/react-query           | 5.104.4               | MIT     |
| @radix-ui/react-tabs            | 1.2.1                 | MIT     |
| @radix-ui/react-radio-group     | 1.5.1                 | MIT     |
| @radix-ui/react-visually-hidden | 1.2.13                | MIT     |
| IBM Plex (font files)           | 1.1.0 / 2.0.0 / 2.5.0 | OFL-1.1 |

Development and test only (never in the bundle):

| Package                                | Version   | License             |
| -------------------------------------- | --------- | ------------------- |
| vite                                   | 6.4.4     | MIT                 |
| @vitejs/plugin-react                   | 4.7.0     | MIT                 |
| typescript                             | 5.9.3     | Apache-2.0          |
| tailwindcss, @tailwindcss/vite         | 4.3.3     | MIT                 |
| vitest                                 | 3.2.7     | MIT                 |
| jsdom                                  | 26.1.0    | MIT                 |
| @testing-library/react                 | 16.3.3    | MIT                 |
| @testing-library/jest-dom              | 6.9.1     | MIT                 |
| @testing-library/user-event            | 14.6.8    | MIT                 |
| @playwright/test                       | 1.64.1    | Apache-2.0          |
| @axe-core/playwright                   | 4.13.0    | MPL-2.0 (see below) |
| msw                                    | 2.15.0    | MIT                 |
| eslint, @eslint/js                     | 9.39.5    | MIT                 |
| typescript-eslint                      | 8.71.2    | MIT                 |
| eslint-plugin-react-hooks              | 7.1.1     | MIT                 |
| eslint-plugin-jsx-a11y                 | 6.10.2    | MIT                 |
| eslint-config-prettier                 | 10.1.8    | MIT                 |
| globals                                | 17.13.0   | MIT                 |
| prettier                               | 3.9.10    | MIT                 |
| openapi-typescript                     | 7.13.0    | MIT                 |
| @types/react, @types/react-dom         | 19.3.0    | MIT                 |
| @types/node                            | 20.19.43  | MIT                 |
| @ibm/plex-sans, -sans-condensed, -mono | see above | OFL-1.1             |

`@axe-core/playwright` (and `axe-core` under it) is MPL-2.0, a file-level weak copyleft. It is a
test-time dependency named by the spec (section 6, "Playwright with axe"), never bundled or
distributed, so the MIT/BSD/Apache rule for shipped code is not touched; recorded here so the
supply-chain review sees it. Versions are pinned exactly; Renovate bumps them. Node 20.15 is
the floor because of `@vitejs/plugin-react` 5+ and `@axe-core/playwright` 4.14+ requiring
Node 20.19 or 22; the next Node bump in CI lifts both.

## Deployment drafts

`deploy/nginx.conf` and `deploy/Dockerfile` are drafts for the orchestrator to wire into
`deploy/compose/compose.yaml` as the `web` service (ADR 0052): nginx serves `dist/` at `/`,
sends HSTS, the CSP and the spec 14.7 headers, proxies `/api/` to the `api` service over mutual
TLS with the `web` client certificate from `carto-ctl pki init` (paths under `/etc/carto/pki/`
are placeholders), and never proxies `/metrics` or `/internal`.

## What M3 adds

The map graph (`@xyflow/react`, MIT) with nodes per system, hops with volume and latency, stale
and clock-skew warnings; node naming; the trace screen (search by identifier, timeline with the
p50 to p95 band, batch links); example transactions and the Reveal button on the review item
(spec 4.3, ADR 0057), with revealed values in a distinct style, auto-hidden after 60 s and never
cached.
