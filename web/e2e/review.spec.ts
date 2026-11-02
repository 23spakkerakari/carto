import { expect, test } from '@playwright/test';

import { csrfToken, expectClean, gotoApp, watch } from './helpers';

const row = (id: string) => `[role="row"][data-link-id="${id}"]`;

test.describe('Map > Review', () => {
  test('lists proposed links as served, with the sentence as a second line', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await expect(page.getByRole('heading', { level: 1, name: 'Review' })).toBeVisible();
    const grid = page.getByRole('grid', { name: 'Proposed links' });
    const ids = await grid
      .locator('[data-link-id]')
      .evaluateAll((rows) => rows.map((r) => r.getAttribute('data-link-id')));
    // score x affected_volume, descending, as the API serves it.
    expect(ids).toEqual(['lnk_03', 'lnk_01', 'lnk_05', 'lnk_04', 'lnk_02', 'lnk_06', 'lnk_07']);
    await expect(grid.locator(row('lnk_01'))).toContainText('Order system');
    await expect(grid.locator(row('lnk_01'))).toContainText('merchant_ref');
    await expect(grid.locator(row('lnk_01'))).toContainText('A9-9999');
    await expect(grid.locator(row('lnk_01'))).toContainText('same identifier');
    await expect(grid.locator(row('lnk_01'))).toContainText('97%');
    await expect(grid.locator(row('lnk_01'))).toContainText('29,610');
    await expect(grid.locator(row('lnk_01'))).toContainText('Order system first');
    await expect(grid.locator(row('lnk_01'))).toContainText('96% of Payments settlement rows');
    await expect(grid.locator(row('lnk_03'))).toContainText('paired in one system');
    await expect(grid.locator(row('lnk_06'))).toContainText('matched on several weak fields');
    // The first item is current and the detail pane shows it.
    await expect(grid.locator(row('lnk_03'))).toHaveAttribute('aria-selected', 'true');
    const detail = page.getByTestId('link-detail');
    await expect(detail.getByTestId('sentence')).toContainText(
      'every order row carries both a cart id',
    );
    // Details disclosure lists every feature with raw names.
    await detail.locator('summary', { hasText: 'Details' }).click();
    const evidence = detail.getByTestId('evidence');
    for (const key of [
      'n_a',
      'n_b',
      'n_match',
      'c_ab',
      'c_ba',
      'lag_median_seconds',
      'pairing_consistency',
      'domain_size',
    ]) {
      await expect(evidence.locator(`[data-feature="${key}"]`)).toBeVisible();
    }
    await expect(evidence).toContainText('pairing_consistency = 0.998');
    expectClean(record);
  });

  test('accepts a link optimistically and it appears under Accepted', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await page.locator(row('lnk_01')).click();
    await expect(page.locator(row('lnk_01'))).toHaveAttribute('aria-selected', 'true');
    await page.getByRole('button', { name: 'Accept' }).click();
    await expect(page.locator(row('lnk_01'))).toHaveCount(0);
    await expect(
      page.getByRole('grid', { name: 'Proposed links' }).locator('[data-link-id]'),
    ).toHaveCount(6);
    await page.getByRole('tab', { name: 'Accepted', exact: true }).click();
    await expect(page).toHaveURL(/status=accepted/);
    await expect(page.locator(row('lnk_01'))).toBeVisible();
    await page.locator(row('lnk_01')).click();
    await expect(page.getByTestId('link-detail')).toContainText('Confirmed');
    await expect(page.getByTestId('link-detail')).toContainText('by oidc|7f3a2c');
    expectClean(record);
  });

  test('rejecting requires a reason and records it', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await page.locator(row('lnk_05')).click();
    const rejectButton = page.getByRole('button', { name: 'Reject', exact: true });
    await rejectButton.click();
    const dialog = page.getByRole('dialog', { name: 'Reject this link' });
    await expect(dialog).toBeVisible();
    // Focus lands inside the dialog (native showModal).
    await expect(dialog.getByRole('radio').first()).toBeFocused();
    await dialog.getByRole('button', { name: 'Reject link' }).click();
    await expect(dialog.getByRole('alert')).toHaveText('Choose a reason before rejecting.');
    await dialog.getByRole('radio', { name: /Coincidence/ }).click();
    await dialog.getByLabel(/Note/).fill('Abandoned carts, nothing more.');
    await dialog.getByRole('button', { name: 'Reject link' }).click();
    await expect(dialog).toBeHidden();
    await expect(page.locator(row('lnk_05'))).toHaveCount(0);
    await page.getByRole('tab', { name: 'Rejected' }).click();
    await page.locator(row('lnk_05')).click();
    await expect(page.getByTestId('link-detail')).toContainText('Rejected (Coincidence)');
    expectClean(record);
  });

  test('cancelling the reject dialog returns focus to the Reject button', async ({ page }) => {
    await gotoApp(page, '/map/review');
    const rejectButton = page.getByRole('button', { name: 'Reject', exact: true });
    await rejectButton.click();
    const dialog = page.getByRole('dialog', { name: 'Reject this link' });
    await expect(dialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(rejectButton).toBeFocused();
    await expect(page.locator(row('lnk_03'))).toBeVisible();
  });

  test('marks a link as association', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await page.locator(row('lnk_04')).click();
    await page.getByRole('button', { name: 'Mark as association' }).click();
    await expect(page.locator(row('lnk_04'))).toHaveCount(0);
    await page.getByRole('tab', { name: 'Accepted', exact: true }).click();
    await page.locator(row('lnk_04')).click();
    await expect(page.getByTestId('link-detail')).toContainText('one to many (association)');
    expectClean(record);
  });

  test('a 412 rolls the item back, reloads it and says so in one line', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    await expect(page.locator(row('lnk_01'))).toBeVisible();
    // Another reviewer decides first: the same link, the same ETag, from outside the UI.
    const token = await csrfToken(page);
    const status = await page.evaluate(async (csrf) => {
      const response = await fetch('/api/v1/links/lnk_01/accept', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'If-Match': 'W/"1"', 'X-CSRF-Token': csrf },
        body: '{}',
      });
      return response.status;
    }, token);
    expect(status).toBe(200);
    await page.locator(row('lnk_01')).click();
    await page.getByRole('button', { name: 'Accept' }).click();
    await expect(page.getByRole('status').filter({ hasText: 'This link changed' })).toHaveText(
      'This link changed while you were looking at it. Reloaded.',
    );
    // Reloaded: the proposed list no longer carries it, the accepted list does.
    await expect(page.locator(row('lnk_01'))).toHaveCount(0);
    await page.getByRole('tab', { name: 'Accepted', exact: true }).click();
    await expect(page.locator(row('lnk_01'))).toBeVisible();
    expectClean(record);
  });

  test('the mock refuses a mutation without the CSRF header or If-Match', async ({ page }) => {
    await gotoApp(page, '/map/review');
    await expect(page.getByRole('heading', { level: 1, name: 'Review' })).toBeVisible();
    const statuses = await page.evaluate(async () => {
      const noCsrf = await fetch('/api/v1/links/lnk_02/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'If-Match': 'W/"1"' },
        body: '{}',
      });
      const me = (await (await fetch('/api/v1/me')).json()) as { csrf_token: string };
      const noIfMatch = await fetch('/api/v1/links/lnk_02/accept', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': me.csrf_token },
        body: '{}',
      });
      return [noCsrf.status, noCsrf.headers.get('content-type'), noIfMatch.status];
    });
    expect(statuses).toEqual([403, 'application/problem+json', 428]);
  });

  test('keyboard: arrows move, hints appear only with focus in the queue, A and R act', async ({
    page,
  }) => {
    const record = watch(page);
    await gotoApp(page, '/map/review');
    const first = page.locator(row('lnk_03'));
    const second = page.locator(row('lnk_01'));
    // No hints while focus is elsewhere.
    await expect(page.getByRole('button', { name: 'Accept' }).locator('kbd')).toBeHidden();
    await first.focus();
    await expect(page.getByRole('button', { name: 'Accept' }).locator('kbd')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Accept' }).locator('kbd')).toHaveText('A');
    await page.keyboard.press('ArrowDown');
    await expect(second).toHaveAttribute('aria-selected', 'true');
    await expect(second).toBeFocused();
    await expect(page.getByTestId('link-detail')).toContainText('96% of Payments settlement rows');
    await page.keyboard.press('ArrowUp');
    await expect(first).toHaveAttribute('aria-selected', 'true');
    await page.keyboard.press('End');
    await expect(page.locator(row('lnk_07'))).toHaveAttribute('aria-selected', 'true');
    await page.keyboard.press('Home');
    await expect(first).toHaveAttribute('aria-selected', 'true');
    // R opens the reject dialog; Escape returns to the queue row.
    await page.keyboard.press('r');
    const dialog = page.getByRole('dialog', { name: 'Reject this link' });
    await expect(dialog).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(first).toBeFocused();
    // A accepts the current item; focus and selection move to the next row.
    await page.keyboard.press('a');
    await expect(first).toHaveCount(0);
    await expect(second).toHaveAttribute('aria-selected', 'true');
    await expect(second).toBeFocused();
    // Blur the queue: hints disappear.
    await page.getByRole('tab', { name: 'Proposed', exact: true }).focus();
    await expect(page.getByRole('button', { name: 'Accept' }).locator('kbd')).toBeHidden();
    expectClean(record);
  });

  test('shows every status tab and the empty sentence', async ({ page }) => {
    await gotoApp(page, '/map/review');
    for (const name of ['Proposed', 'Accepted', 'Rejected', 'Stale', 'Auto-accepted']) {
      await expect(page.getByRole('tab', { name, exact: true })).toBeVisible();
    }
    await page.getByRole('tab', { name: 'Stale' }).click();
    await expect(page.locator(row('lnk_13'))).toContainText('fell from 96% to 4%');
    await page.getByRole('tab', { name: 'Auto-accepted' }).click();
    await expect(page.locator(row('lnk_14'))).toBeVisible();
    await page.locator(row('lnk_14')).click();
    await expect(page.getByTestId('link-detail')).toContainText('Confirmed automatically');
    // Accept everything proposed and see the empty state.
    await page.getByRole('tab', { name: 'Proposed', exact: true }).click();
    for (let i = 0; i < 7; i += 1) {
      await page.getByRole('button', { name: 'Accept' }).click();
      await expect(
        page.getByRole('grid', { name: 'Proposed links' }).locator('[data-link-id]'),
      ).toHaveCount(6 - i);
    }
    await expect(page.getByText('No links are waiting for review.')).toBeVisible();
  });

  test('a business viewer sees the queue but cannot decide', async ({ page }) => {
    await gotoApp(page, '/map/review', { as: 'business_viewer' });
    await expect(page.getByRole('grid', { name: 'Proposed links' })).toBeVisible();
    await expect(page.getByText('Your role can view links but not decide them.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Accept' })).toHaveCount(0);
    await expect(page.getByRole('navigation', { name: 'Console' })).not.toContainText('Audit');
  });

  test('a signed-out session goes to the login route', async ({ page }) => {
    await gotoApp(page, '/map/review', { loggedOut: true });
    await page.waitForURL(/\/api\/v1\/auth\/login/);
    expect(new URL(page.url()).pathname).toBe('/api/v1/auth/login');
  });

  test('sign out posts to the logout route with the CSRF header', async ({ page }) => {
    await gotoApp(page, '/map/review');
    const requestPromise = page.waitForRequest(
      (request) => request.url().endsWith('/api/v1/auth/logout') && request.method() === 'POST',
    );
    await page.getByRole('button', { name: 'Sign out' }).click();
    const request = await requestPromise;
    expect(request.headers()['x-csrf-token']).toMatch(/^csrf-/);
    await page.waitForURL((url) => url.pathname === '/' || url.pathname === '/map/review');
  });
});
