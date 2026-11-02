import { expect, test } from '@playwright/test';

import { expectClean, gotoApp, watch } from './helpers';

test.describe('inventory screens', () => {
  test('Entities: suggested names are confirmed with PATCH and If-Match', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/map/entities');
    await expect(page.getByRole('heading', { level: 1, name: 'Entities' })).toBeVisible();
    await expect(page.getByTestId('entity')).toHaveCount(3);
    const po = page.locator('[data-entity-id="ent_po"]');
    await expect(po).toContainText('Suggested');
    await expect(po).toContainText('Warehouse');
    await expect(po).toContainText('Shipping');
    await expect(po).toContainText('po_num');
    const patch = page.waitForRequest(
      (r) => r.method() === 'PATCH' && r.url().endsWith('/entities/ent_po'),
    );
    await po.getByLabel('Name').fill('Purchase order (WMS)');
    await po.getByRole('button', { name: 'Confirm name' }).click();
    const request = await patch;
    expect(request.headers()['if-match']).toBe('W/"1"');
    expect(request.headers()['x-csrf-token']).toMatch(/^csrf-/);
    expect(request.postDataJSON()).toEqual({ name: 'Purchase order (WMS)', description: '' });
    await expect(po.getByRole('status')).toHaveText('Saved.');
    await expect(po).toContainText('Confirmed');
    await expect(po.getByRole('heading', { level: 2 })).toHaveText('Purchase order (WMS)');
    // The audit log recorded it.
    await gotoApp(page, '/admin/audit', { as: 'admin' });
    await expect(page.getByTestId('audit-row').first()).toBeVisible();
    expectClean(record);
  });

  test('Systems list and detail', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/systems');
    await expect(page.getByRole('heading', { level: 1, name: 'Systems' })).toBeVisible();
    const rows = page.locator('tbody tr');
    await expect(rows).toHaveCount(5);
    await expect(rows.filter({ hasText: 'Payments' })).toContainText('failing');
    await expect(rows.filter({ hasText: 'Order system' })).toContainText('behind');
    await expect(rows.filter({ hasText: 'Webstore' })).toContainText('healthy');
    await page.getByRole('link', { name: 'Warehouse' }).click();
    await expect(page).toHaveURL(/\/systems\/warehouse$/);
    await expect(page.getByRole('heading', { level: 1, name: 'Warehouse' })).toBeVisible();
    await expect(page.getByText('warehouse_mssql')).toBeVisible();
    await expect(page.getByText('Known fields')).toBeVisible();
    await expect(page.getByText('5', { exact: true })).toBeVisible();
    expectClean(record);
  });

  test('Sources list shows type, enabled, health marker, lag, last error and last test', async ({
    page,
  }) => {
    const record = watch(page);
    await gotoApp(page, '/setup/sources');
    const rows = page.getByTestId('source-row');
    await expect(rows).toHaveCount(7);
    const payments = rows.filter({ hasText: 'payments_sftp' });
    await expect(payments).toContainText('sftp');
    await expect(payments).toContainText('failing');
    await expect(payments).toContainText('1 d 2 h');
    await expect(payments).toContainText('Authentication failed');
    await expect(payments).toContainText('Host key matched');
    const shipping = rows.filter({ hasText: 'shipping_webhooks' });
    await expect(shipping).toContainText('no');
    await expect(shipping).toContainText('idle');
    await page.getByRole('link', { name: 'orders_db' }).click();
    await expect(page.getByRole('heading', { level: 1, name: 'orders_db' })).toBeVisible();
    // Configuration as a definition list of text; the secret is a reference name only.
    await expect(page.getByText('orders-db.internal')).toBeVisible();
    await expect(page.getByText('vault:kv/carto/orders-db')).toBeVisible();
    await expect(page.getByText('verify-full')).toBeVisible();
    await expect(page.getByText('Cursor 31 min behind')).toBeVisible();
    await expect(page.getByText('Read-only session confirmed', { exact: false })).toBeVisible();
    expectClean(record);
  });

  test('Field policies show class, policy, pins, shapes and quality, never values', async ({
    page,
  }) => {
    const record = watch(page);
    await gotoApp(page, '/setup/fields');
    await expect(page.getByTestId('field-row')).toHaveCount(30);
    const session = page.getByTestId('field-row').filter({ hasText: 'session_id' });
    await expect(session).toContainText('dropped');
    await expect(session).toContainText('pinned');
    const order = page.getByTestId('field-row').filter({ hasText: 'merchantRef' });
    await expect(order).toContainText('A9-9999');
    await expect(order).toContainText('99%');
    await expect(order).toContainText('tokenized');
    await expect(order).toContainText('Merchant reference (suggested)');
    await page.getByLabel('System').selectOption('warehouse');
    await expect(page).toHaveURL(/system=warehouse/);
    await expect(page.getByTestId('field-row')).toHaveCount(5);
    expectClean(record);
  });

  test('Audit is for admins and auditors; others get a plain refusal', async ({ page }) => {
    const record = watch(page);
    await gotoApp(page, '/admin/audit', { as: 'admin' });
    const rows = page.getByTestId('audit-row');
    await expect(rows).toHaveCount(16);
    await expect(rows.first()).toContainText('session.login');
    const reject = rows.filter({ hasText: 'link:lnk_10' });
    await expect(reject).toContainText('Staff accounts, not customers.');
    await reject.getByText('Before and after').click();
    await expect(reject).toContainText('"reject_reason": "different_meaning"');
    await gotoApp(page, '/admin/audit', { as: 'auditor' });
    await expect(page.getByTestId('audit-row').first()).toBeVisible();
    await gotoApp(page, '/admin/audit');
    await expect(page.getByRole('alert')).toHaveText('Your role does not allow this.');
    expectClean(record);
  });
});
