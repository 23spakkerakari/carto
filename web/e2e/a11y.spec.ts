import { expect, test } from '@playwright/test';

import { expectNoAxeViolations, gotoApp, type Flags } from './helpers';

const ROUTES: { route: string; flags?: Flags; ready: string }[] = [
  { route: '/map/review', ready: 'Review' },
  { route: '/map/review?status=rejected', ready: 'Review' },
  { route: '/map/entities', ready: 'Entities' },
  { route: '/systems', ready: 'Systems' },
  { route: '/systems/orders', ready: 'Order system' },
  { route: '/setup/sources', ready: 'Sources' },
  { route: '/setup/sources/payments_sftp', ready: 'payments_sftp' },
  { route: '/setup/fields', ready: 'Field policies' },
  { route: '/admin/audit', flags: { as: 'admin' }, ready: 'Audit' },
];

test.describe('accessibility (axe, WCAG 2.2 AA)', () => {
  for (const { route, flags, ready } of ROUTES) {
    test(`${route} has no violations`, async ({ page }, testInfo) => {
      await gotoApp(page, route, flags);
      await expect(page.getByRole('heading', { level: 1, name: ready })).toBeVisible();
      await expectNoAxeViolations(page, testInfo, route);
    });
  }

  test('the review page with Details open and the reject dialog open', async ({
    page,
  }, testInfo) => {
    await gotoApp(page, '/map/review');
    await page.locator('summary', { hasText: 'Details' }).click();
    await expect(page.getByTestId('evidence')).toBeVisible();
    await expectNoAxeViolations(page, testInfo, 'review-details');
    await page.getByRole('button', { name: 'Reject', exact: true }).click();
    await expect(page.getByRole('dialog', { name: 'Reject this link' })).toBeVisible();
    await page.getByRole('button', { name: 'Reject link' }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    await expectNoAxeViolations(page, testInfo, 'review-reject-dialog');
  });

  test('the whole console is reachable by keyboard', async ({ page }) => {
    await gotoApp(page, '/map/review');
    await expect(page.getByRole('heading', { level: 1, name: 'Review' })).toBeVisible();
    // The first Tab from a fresh document lands on the skip link (a second press covers a
    // browser that spends the first on its own chrome).
    const skip = page.getByRole('link', { name: 'Skip to content' });
    await page.keyboard.press('Tab');
    if (!(await skip.evaluate((el) => el === document.activeElement))) {
      await page.keyboard.press('Tab');
    }
    await expect(skip).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#main')).toBeVisible();
    // Tab order reaches the tabs, the grid row and the actions.
    let reachedGrid = false;
    for (let i = 0; i < 20 && !reachedGrid; i += 1) {
      await page.keyboard.press('Tab');
      reachedGrid = await page.evaluate(
        () => document.activeElement?.getAttribute('role') === 'row',
      );
    }
    expect(reachedGrid).toBe(true);
    await page.keyboard.press('Tab');
    await expect(page.getByRole('button', { name: 'Accept' })).toBeFocused();
  });
});
