import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import AxeBuilder from '@axe-core/playwright';
import { expect, type Page, type TestInfo } from '@playwright/test';

const here = path.dirname(fileURLToPath(import.meta.url));

/** The spec 14.7 policy as src/csp.ts defines it (read as text: e2e is its own TS project). */
export const STRICT_CSP = (() => {
  const source = readFileSync(path.join(here, '..', 'src', 'csp.ts'), 'utf8');
  const match = /STRICT_CSP =\s*"([^"]+)"/.exec(source);
  if (!match?.[1]) throw new Error('src/csp.ts does not define STRICT_CSP');
  return match[1];
})();
/** The meta-tag form: frame-ancestors is header-only (see src/csp.ts). */
export const META_CSP = STRICT_CSP.replace(/;\s*frame-ancestors 'none'/, '');
/** The nginx draft that sends the full policy as a header. */
export const NGINX_CONF = readFileSync(path.join(here, '..', 'deploy', 'nginx.conf'), 'utf8');

export type Principal = 'integration_engineer' | 'admin' | 'auditor' | 'business_viewer';

export interface Flags {
  as?: Principal;
  corpus?: 'xss';
  loggedOut?: boolean;
}

/** Opens a console route with the mock's page-load flags. */
export async function gotoApp(page: Page, route: string, flags: Flags = {}): Promise<void> {
  const [pathname, existing = ''] = route.split('?');
  const params = new URLSearchParams(existing);
  if (flags.as && flags.as !== 'integration_engineer') params.set('as', flags.as);
  if (flags.corpus) params.set('corpus', flags.corpus);
  if (flags.loggedOut) params.set('logged_out', '1');
  const query = params.toString();
  await page.goto(query ? `${pathname ?? route}?${query}` : route);
}

export interface Watch {
  cspViolations: string[];
  pageErrors: string[];
  foreignRequests: string[];
  dialogs: string[];
}

/** Records what must never happen: CSP reports, page errors, requests to another host, dialogs. */
export function watch(page: Page): Watch {
  const record: Watch = { cspViolations: [], pageErrors: [], foreignRequests: [], dialogs: [] };
  page.on('console', (message) => {
    const text = message.text();
    if (/Content[- ]Security[- ]Policy|Refused to/i.test(text)) record.cspViolations.push(text);
  });
  page.on('pageerror', (error) => {
    record.pageErrors.push(error.message);
  });
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.hostname !== '127.0.0.1' && url.hostname !== 'localhost') {
      record.foreignRequests.push(request.url());
    }
  });
  page.on('dialog', (dialog) => {
    record.dialogs.push(dialog.message());
    void dialog.dismiss();
  });
  return record;
}

export function expectClean(record: Watch): void {
  expect(record.cspViolations, 'CSP violations').toEqual([]);
  expect(record.pageErrors, 'page errors').toEqual([]);
  expect(record.foreignRequests, 'requests to another host').toEqual([]);
  expect(record.dialogs, 'dialogs').toEqual([]);
}

/** WCAG 2.2 AA (and the A and 2.1 rules underneath) with zero violations. */
export async function expectNoAxeViolations(
  page: Page,
  testInfo: TestInfo,
  label: string,
): Promise<void> {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'])
    .analyze();
  if (results.violations.length > 0) {
    await testInfo.attach(`axe-${label}`, {
      body: JSON.stringify(results.violations, null, 2),
      contentType: 'application/json',
    });
  }
  const summary = results.violations.map(
    (v) => `${v.id} (${v.impact ?? 'n/a'}): ${v.nodes.map((n) => n.target.join(' ')).join(', ')}`,
  );
  expect(summary, `axe violations on ${label}`).toEqual([]);
}

/** The CSRF token the mock /me serves, fetched from inside the page. */
export async function csrfToken(page: Page): Promise<string> {
  return page.evaluate(async () => {
    const response = await fetch('/api/v1/me', { credentials: 'same-origin' });
    const me = (await response.json()) as { csrf_token: string };
    return me.csrf_token;
  });
}
