/* Starts the msw service worker. Imported dynamically from main.tsx only when
 * `import.meta.env.VITE_MOCK_API === '1'` (modes `mock` and `e2e`), so the production bundle
 * contains none of this (scripts/check-build.mjs asserts it).
 *
 * Page-load flags, read once: `?as=admin|auditor|business_viewer` picks the principal,
 * `?logged_out` makes every call 401, `?corpus=xss` merges the hostile fixtures. */
import { setupWorker } from 'msw/browser';

import { buildDb, optionsFromSearch } from './db';
import { createHandlers } from './handlers';

export async function startMockApi(): Promise<void> {
  const db = buildDb(optionsFromSearch(window.location.search));
  const worker = setupWorker(...createHandlers(db));
  await worker.start({
    onUnhandledRequest: (request, print) => {
      if (new URL(request.url).pathname.startsWith('/api/')) print.error();
    },
    quiet: true,
    serviceWorker: { url: '/mockServiceWorker.js' },
  });
}
