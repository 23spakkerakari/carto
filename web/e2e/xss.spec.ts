import { expect, test, type Page } from '@playwright/test';

import { expectClean, gotoApp, watch, type Flags } from './helpers';

/* The XSS corpus (threat model row 4). The payloads live in src/mocks/fixtures/xss.ts; they are
 * repeated here as text so the test does not depend on the module under test. */
const CANARY = 'canary.invalid';
const PAYLOADS = {
  script: '<script>window.__xss_executed = true</script>',
  imgOnerror: '<img src="http://canary.invalid/pixel.gif" onerror="window.__xss_executed = true">',
  svgOnload: '"><svg onload="window.__xss_executed = true">',
  jsUrl: 'javascript:window.__xss_executed = true',
  mdLink: '[claim your refund](http://canary.invalid/phish)',
  template: '{{ constructor.constructor("window.__xss_executed = true")() }}',
  entity: '&lt;b&gt;bold&lt;/b&gt; &amp;amp; &#x3C;i&#x3E;',
  rtl: 'invoice‮fdp.exe',
  iframe: '<iframe src="http://canary.invalid/frame"></iframe>',
  anchor: '<a href="http://canary.invalid/x">x</a>',
  cssUrl: 'url(http://canary.invalid/bg.png)',
  long: 'A'.repeat(1024),
};

interface Snapshot {
  executed: unknown;
  text: string;
  scripts: number;
  media: number;
  handlers: number;
  jsLinks: number;
  hrefs: string[];
  srcs: string[];
  styleAttrsWithUrl: number;
}

async function snapshot(page: Page): Promise<Snapshot> {
  return page.evaluate(() => {
    const all = [...document.querySelectorAll('*')];
    return {
      executed: (window as unknown as { __xss_executed?: unknown }).__xss_executed,
      text: document.body.textContent,
      scripts: [...document.querySelectorAll('script')].filter((s) => !s.src).length,
      media: document.querySelectorAll('img, iframe, object, embed, svg, video, audio, frame')
        .length,
      handlers: all.filter((el) => [...el.attributes].some((a) => a.name.startsWith('on'))).length,
      jsLinks: all.filter((el) => /^\s*javascript:/i.test(el.getAttribute('href') ?? '')).length,
      hrefs: all.map((el) => el.getAttribute('href')).filter((h): h is string => h !== null),
      srcs: all.map((el) => el.getAttribute('src')).filter((s): s is string => s !== null),
      styleAttrsWithUrl: all.filter((el) => /url\(/i.test(el.getAttribute('style') ?? '')).length,
    };
  });
}

function expectInert(snap: Snapshot, expectedText: string[]): void {
  expect(snap.executed, 'a payload executed').toBeUndefined();
  expect(snap.scripts, 'inline script elements').toBe(0);
  expect(snap.media, 'media elements').toBe(0);
  expect(snap.handlers, 'inline event handlers').toBe(0);
  expect(snap.jsLinks, 'javascript: links').toBe(0);
  expect(snap.styleAttrsWithUrl, 'style attributes with url()').toBe(0);
  for (const href of snap.hrefs) {
    expect(href, `href ${href}`).toMatch(/^(\/[^/]|#main$|\/$)/);
    expect(href).not.toContain(CANARY);
  }
  for (const src of snap.srcs) {
    expect(src, `src ${src}`).toMatch(/^\/assets\//);
  }
  for (const text of expectedText) {
    expect(snap.text, `payload rendered as text: ${text.slice(0, 40)}`).toContain(text);
  }
}

const xss: Flags = { corpus: 'xss' };

test.describe('XSS corpus (threat 4): customer strings never become markup', () => {
  test('review queue, detail, Details and the reject dialog', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review', xss);
    const hostile = page.locator('[role="row"][data-link-id="lnk_xss_01"]');
    await expect(hostile).toBeVisible();
    await expect(hostile).toHaveAttribute('aria-selected', 'true');
    await page.locator('summary', { hasText: 'Details' }).click();
    await expect(page.getByTestId('evidence')).toBeVisible();
    expectInert(await snapshot(page), [
      PAYLOADS.script,
      PAYLOADS.imgOnerror,
      PAYLOADS.mdLink,
      PAYLOADS.template,
      PAYLOADS.entity,
      PAYLOADS.rtl,
      PAYLOADS.long,
      PAYLOADS.svgOnload,
    ]);
    // The composite item carries payloads in sentence, form and component paths.
    await page.locator('[role="row"][data-link-id="lnk_xss_02"]').click();
    await page.locator('summary', { hasText: 'Details' }).click();
    expectInert(await snapshot(page), [PAYLOADS.anchor, PAYLOADS.cssUrl, PAYLOADS.jsUrl]);
    await page.getByRole('button', { name: 'Reject', exact: true }).click();
    await expect(page.getByRole('dialog')).toBeVisible();
    expectInert(await snapshot(page), []);
    await page.keyboard.press('Escape');
    expectClean(record);
  });

  test('entities', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/entities', xss);
    await expect(page.locator('[data-entity-id="ent_hostile"]')).toBeVisible();
    expectInert(await snapshot(page), [
      PAYLOADS.anchor,
      PAYLOADS.mdLink,
      PAYLOADS.script,
      PAYLOADS.iframe,
    ]);
    // The name field carries the payload as its value, not as markup.
    const input = page.locator('[data-entity-id="ent_hostile"]').getByLabel('Name');
    await expect(input).toHaveValue(PAYLOADS.anchor);
    expectClean(record);
  });

  test('systems and the hostile system detail', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/systems', xss);
    await expect(page.locator('tbody tr')).toHaveCount(6);
    expectInert(await snapshot(page), [
      PAYLOADS.imgOnerror,
      PAYLOADS.mdLink,
      PAYLOADS.template,
      PAYLOADS.svgOnload,
    ]);
    await page.goto('/systems/hostile?corpus=xss');
    await expect(page.getByRole('heading', { level: 1 })).toContainText(PAYLOADS.imgOnerror);
    expect(await page.title()).toBe('carto');
    expectInert(await snapshot(page), [PAYLOADS.imgOnerror, PAYLOADS.svgOnload]);
    expectClean(record);
  });

  test('sources and the hostile source configuration', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/setup/sources', xss);
    await expect(page.getByTestId('source-row')).toHaveCount(8);
    expectInert(await snapshot(page), [PAYLOADS.imgOnerror, PAYLOADS.anchor]);
    await page.goto('/setup/sources/hostile_logs?corpus=xss');
    await expect(page.getByRole('heading', { level: 1, name: 'hostile_logs' })).toBeVisible();
    expectInert(await snapshot(page), [
      PAYLOADS.script,
      PAYLOADS.jsUrl,
      PAYLOADS.mdLink,
      PAYLOADS.template,
      PAYLOADS.entity,
      PAYLOADS.rtl,
      PAYLOADS.iframe,
      PAYLOADS.anchor,
      PAYLOADS.cssUrl,
      PAYLOADS.long,
    ]);
    // Nested configuration values are JSON text: the quotes of the payload are escaped.
    expect((await snapshot(page)).text).toContain('\\"><svg onload=');
    expectClean(record);
  });

  test('field policies', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/setup/fields?system=hostile', xss);
    await expect(page.getByTestId('field-row')).toHaveCount(3);
    expectInert(await snapshot(page), [
      PAYLOADS.script,
      PAYLOADS.imgOnerror,
      PAYLOADS.entity,
      PAYLOADS.jsUrl,
      PAYLOADS.mdLink,
      PAYLOADS.rtl,
      PAYLOADS.template,
      PAYLOADS.iframe,
      PAYLOADS.anchor,
      PAYLOADS.svgOnload,
      'x'.repeat(256),
    ]);
    expectClean(record);
  });

  test('audit log', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/admin/audit', { ...xss, as: 'admin' });
    const hostile = page.getByTestId('audit-row').filter({ hasText: 'entity:' + PAYLOADS.script });
    await expect(hostile).toHaveCount(1);
    await hostile.getByText('Before and after').click();
    expectInert(await snapshot(page), [PAYLOADS.imgOnerror, PAYLOADS.script, PAYLOADS.mdLink]);
    // JSON text keeps the payloads escaped as JSON strings, still text.
    await expect(hostile).toContainText('"note": "<iframe src=');
    expectClean(record);
  });
});
