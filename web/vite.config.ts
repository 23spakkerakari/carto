import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig, type Plugin } from 'vite';

const here = path.dirname(fileURLToPath(import.meta.url));

// The policy lives in src/csp.ts (a plain string module) so the app, the build and the tests
// read one definition. It is read as text here to keep the Vite config free of app imports.
const STRICT_CSP = /STRICT_CSP =\s*"([^"]+)"/.exec(
  readFileSync(path.join(here, 'src/csp.ts'), 'utf8'),
)?.[1];
if (!STRICT_CSP) throw new Error('src/csp.ts does not define STRICT_CSP');
// frame-ancestors is header-only (src/csp.ts META_CSP); the meta tag leaves it out.
const META_CSP = STRICT_CSP.replace(/;\s*frame-ancestors 'none'/, '');

/** Injects the spec 14.7 policy as a meta tag. The production build gets it verbatim (minus the
 * header-only frame-ancestors directive). The dev
 * server needs a nonce for the tags Vite itself injects (React refresh preamble, CSS HMR), so
 * `serve` adds one nonce to script-src and style-src and a websocket allowance for HMR; nothing
 * else changes. */
function csp(command: 'build' | 'serve', nonce: string): Plugin {
  const policy =
    command === 'build'
      ? META_CSP
      : `default-src 'self'; script-src 'self' 'nonce-${nonce}'; style-src 'self' 'nonce-${nonce}'; ` +
        `connect-src 'self' ws://localhost:* ws://127.0.0.1:*; object-src 'none'; base-uri 'none'`;
  return {
    name: 'carto-csp',
    transformIndexHtml: {
      order: 'pre',
      handler: () => [
        {
          tag: 'meta',
          attrs: { 'http-equiv': 'Content-Security-Policy', content: policy },
          injectTo: 'head-prepend',
        },
      ],
    },
  };
}

/** Serves msw's worker script at /mockServiceWorker.js in mock and e2e modes only, so the
 * production build never carries it (a copy under public/ would land in every build). */
function mockWorker(enabled: boolean): Plugin {
  const require = createRequire(import.meta.url);
  const workerPath = path.join(
    path.dirname(require.resolve('msw/package.json')),
    'lib/mockServiceWorker.js',
  );
  const source = () => readFileSync(workerPath, 'utf8');
  return {
    name: 'carto-mock-worker',
    apply: () => enabled,
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url?.split('?')[0] !== '/mockServiceWorker.js') {
          next();
          return;
        }
        res.setHeader('Content-Type', 'application/javascript');
        res.setHeader('Service-Worker-Allowed', '/');
        res.end(source());
      });
    },
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'mockServiceWorker.js', source: source() });
    },
  };
}

export default defineConfig(({ command, mode }) => {
  const mock = mode === 'mock' || mode === 'e2e' || process.env.VITE_MOCK_API === '1';
  const nonce = 'carto-dev';
  return {
    plugins: [react(), tailwindcss(), csp(command, nonce), mockWorker(mock)],
    define: {
      'import.meta.env.VITE_MOCK_API': JSON.stringify(mock ? '1' : ''),
    },
    html: command === 'serve' ? { cspNonce: nonce } : {},
    build: {
      outDir: mode === 'e2e' ? 'dist-e2e' : 'dist',
      sourcemap: false,
      target: 'es2022',
      modulePreload: { polyfill: false },
    },
    server: {
      port: 5173,
      strictPort: true,
      // Against the Compose stack the API is the same origin (ADR 0052); in development Vite
      // proxies /api to it so the browser still sees one origin, http://localhost:5173.
      proxy: mock
        ? undefined
        : {
            '/api': {
              target: process.env.CARTO_API_URL ?? 'https://localhost:8443',
              changeOrigin: false,
              secure: false,
            },
          },
    },
    preview: { port: 4173, strictPort: true },
  };
});
