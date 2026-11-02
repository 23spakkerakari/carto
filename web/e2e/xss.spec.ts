import { expect, test, type Page } from '@playwright/test';

import { expectClean, gotoApp, watch, type Flags } from './helpers';

/* The XSS corpus (threat model row 4). The payloads live in src/mocks/fixtures/xss.ts; they are
 * repeated here as text so the test does not depend on the module under test. */
const CANARY = 'canary.invalid';
const PAYLOADS = {
  script: '<script>window.__xss_executed = true</script>',
  imgOnerror: '<img src="http://canary.invalid/pixel.gif" onerror="window.__xss_executed = true">',
  imgShort: '<img src="http://canary.invalid/s.gif" onerror="x()">',
  svgOnload: '"><svg onload="window.__xss_executed = true">',
  short: '<svg onload=x>',
  sourceType: '<img src=x onerror=x>',
  jsUrl: 'javascript:window.__xss_executed = true',
  mdLink: '[claim your refund](http://canary.invalid/phish)',
  template: '{{ constructor.constructor("window.__xss_executed = true")() }}',
  entity: '&lt;b&gt;bold&lt;/b&gt; &amp;amp; &#x3C;i&#x3E;',
  rtl: 'invoice‮fdp.exe',
  iframe: '<iframe src="http://canary.invalid/frame"></iframe>',
  anchor: '<a href="http://canary.invalid/x">x</a>',
  cssUrl: 'url(http://canary.invalid/bg.png)',
  styleTag: '<style>body{background:url(http://canary.invalid/bg.png)}</style>',
  displayName: '<b onmouseover="window.__xss_executed = true">Mira</b>',
  subject: 'oidc|<script>window.__xss_executed = true</script>',
  problemTitle: '<svg onload="window.__xss_executed = true">Name refused</svg>',
  problemDetail:
    '[fix it here](http://canary.invalid/fix) {{ constructor.constructor("window.__xss_executed = true")() }}',
  long: 'A'.repeat(1024),
};

interface Snapshot {
  executed: unknown;
  text: string;
  scripts: number;
  styles: number;
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
      styles: document.querySelectorAll('style').length,
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
  expect(snap.styles, 'style elements').toBe(0);
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
  test('the shell shows the signed-in display name as text', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review', xss);
    const nav = page.getByRole('navigation', { name: 'Console' });
    await expect(nav).toContainText(PAYLOADS.displayName);
    expectInert(await snapshot(page), [PAYLOADS.displayName]);
    expectClean(record);
  });

  test('review queue, detail, Details and the reject dialog', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review', xss);
    const hostile = page.locator('[role="row"][data-link-id="lnk_xss_01"]');
    await expect(hostile).toBeVisible();
    await expect(hostile).toHaveAttribute('aria-selected', 'true');
    await page.locator('summary', { hasText: 'Details' }).click();
    await expect(page.getByTestId('evidence')).toBeVisible();
    // The top shapes of both ends, the system name, the sentence and the identity rows.
    expectInert(await snapshot(page), [
      PAYLOADS.script,
      PAYLOADS.imgOnerror,
      PAYLOADS.imgShort,
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

  test('a decided link names its decider as text', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review?status=accepted', xss);
    const decided = page.locator('[role="row"][data-link-id="lnk_xss_03"]');
    await expect(decided).toBeVisible();
    await decided.click();
    await expect(page.getByText(PAYLOADS.subject).first()).toBeVisible();
    expectInert(await snapshot(page), [PAYLOADS.subject, PAYLOADS.displayName]);
    expectClean(record);
  });

  test('entities: the suggested name, and the problem document of a refused rename', async ({
    page,
  }) => {
    const record = watch(page);
    await gotoApp(page, '/map/entities', xss);
    const hostile = page.locator('[data-entity-id="ent_hostile"]');
    await expect(hostile).toBeVisible();
    // The entity has no name yet: the heading is empty and the suggestion fills the form.
    await expect(hostile.getByRole('heading', { level: 2 })).toHaveText('');
    await expect(hostile.getByText('Suggested')).toBeVisible();
    expectInert(await snapshot(page), [PAYLOADS.mdLink, PAYLOADS.script, PAYLOADS.iframe]);
    // The suggested name is the field's value, not markup.
    const input = hostile.getByLabel('Name');
    await expect(input).toHaveValue(PAYLOADS.anchor);
    // The mock refuses the rename with a hostile problem document; the error line is text.
    await hostile.getByRole('button', { name: 'Confirm name' }).click();
    const alert = hostile.getByRole('alert');
    await expect(alert).toContainText(PAYLOADS.problemTitle);
    await expect(alert).toContainText(PAYLOADS.problemDetail);
    expectInert(await snapshot(page), [PAYLOADS.problemTitle, PAYLOADS.problemDetail]);
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
      PAYLOADS.short,
    ]);
    await page.goto('/systems/hostile?corpus=xss');
    await expect(page.getByRole('heading', { level: 1 })).toContainText(PAYLOADS.imgOnerror);
    expect(await page.title()).toBe('carto');
    expectInert(await snapshot(page), [PAYLOADS.imgOnerror, PAYLOADS.svgOnload, PAYLOADS.short]);
    expectClean(record);
  });

  test('sources and the hostile source configuration', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/setup/sources', xss);
    await expect(page.getByTestId('source-row')).toHaveCount(8);
    expectInert(await snapshot(page), [PAYLOADS.imgOnerror, PAYLOADS.anchor, PAYLOADS.sourceType]);
    await page.goto('/setup/sources/hostile_logs?corpus=xss');
    await expect(page.getByRole('heading', { level: 1, name: 'hostile_logs' })).toBeVisible();
    // Type, health status, configuration keys and values, secret reference and last test.
    expectInert(await snapshot(page), [
      PAYLOADS.sourceType,
      PAYLOADS.short,
      PAYLOADS.styleTag,
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
      PAYLOADS.imgShort,
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
