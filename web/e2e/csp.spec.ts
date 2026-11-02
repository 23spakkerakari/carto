import { expect, test } from '@playwright/test';

import { expectClean, gotoApp, META_CSP, NGINX_CONF, STRICT_CSP, watch } from './helpers';

test.describe('content security policy (spec 14.7)', () => {
  test('the production build carries the strict policy and runs under it without a report', async ({
    page,
  }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await expect(page.getByRole('heading', { level: 1, name: 'Review' })).toBeVisible();
    const meta = await page
      .locator('meta[http-equiv="Content-Security-Policy"]')
      .getAttribute('content');
    expect(meta).toBe(META_CSP);
    expect(META_CSP).toBe(
      "default-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'",
    );
    // The header form (deploy/nginx.conf) carries the full policy with frame-ancestors.
    expect(NGINX_CONF).toContain(`"${STRICT_CSP}"`);
    // Exercise every screen, the dialog and the disclosure: Radix, the router and the forms
    // all have to work with no inline script, no inline style element and no eval.
    await page.getByRole('button', { name: 'Reject', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.getByRole('radio').first().click();
    await page.keyboard.press('Escape');
    await page.locator('summary', { hasText: 'Details' }).click();
    for (const route of [
      '/map/entities',
      '/systems',
      '/setup/sources',
      '/setup/sources/orders_db',
      '/setup/fields',
    ]) {
      await page.getByRole('navigation', { name: 'Console' }).waitFor();
      await page.goto(route);
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    }
    await gotoApp(page, '/admin/audit', { as: 'admin' });
    await expect(page.getByTestId('audit-row').first()).toBeVisible();
    expectClean(record);
    // No inline script or style element exists in the document.
    const inline = await page.evaluate(() => ({
      scripts: [...document.querySelectorAll('script')].filter((s) => !s.src).length,
      styles: document.querySelectorAll('style').length,
      externalScripts: [...document.querySelectorAll('script[src]')].map(
        (s) => (s as HTMLScriptElement).src,
      ),
    }));
    expect(inline.scripts).toBe(0);
    expect(inline.styles).toBe(0);
    for (const src of inline.externalScripts)
      expect(new URL(src).origin).toBe(new URL(page.url()).origin);
  });

  test('the self-hosted Plex faces load under the policy', async ({ page }) => {
    await gotoApp(page, '/map/review');
    await expect(page.getByRole('heading', { level: 1, name: 'Review' })).toBeVisible();
    const loaded = await page.evaluate(async () => {
      await document.fonts.ready;
      return {
        cond: document.fonts.check('600 16px "IBM Plex Sans Condensed"'),
        sans: document.fonts.check('400 16px "IBM Plex Sans"'),
        mono: document.fonts.check('400 16px "IBM Plex Mono"'),
        faces: [...document.fonts]
          .filter((f) => f.status === 'loaded')
          .map((f) => `${f.family} ${f.weight}`),
      };
    });
    expect(loaded.cond, loaded.faces.join(', ')).toBe(true);
    expect(loaded.sans).toBe(true);
    expect(loaded.mono).toBe(true);
  });

  test('a violating inline handler would be reported (the watch is live)', async ({ page }) => {
    // Sanity check of the harness: inject a violation on purpose and see the report arrive,
    // so a green CSP run means the app produced none, not that reports went unseen.
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await expect(page.getByRole('heading', { level: 1, name: 'Review' })).toBeVisible();
    await page.evaluate(() => {
      const el = document.createElement('div');
      el.setAttribute('onclick', 'void 0');
      document.body.append(el);
      el.click();
    });
    await expect.poll(() => record.cspViolations.length).toBeGreaterThan(0);
  });
});
