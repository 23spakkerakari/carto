// Runs after `vite build`: the production bundle must carry neither the mock API nor its
// service worker (they exist only in `--mode mock` and `--mode e2e`), the index must carry the
// strict CSP meta tag and no inline script or style, and every asset must be same-origin.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.join(here, '..', 'dist');
const failures = [];
const strict = /STRICT_CSP =\s*"([^"]+)"/.exec(
  readFileSync(path.join(here, '..', 'src/csp.ts'), 'utf8'),
)?.[1];
// The meta tag carries the policy without the header-only frame-ancestors directive.
const csp = strict?.replace(/;\s*frame-ancestors 'none'/, '');
const nginx = readFileSync(path.join(here, '..', 'deploy/nginx.conf'), 'utf8');
if (!strict || !nginx.includes(`"${strict}"`)) {
  failures.push('deploy/nginx.conf does not send the strict policy of src/csp.ts');
}

const files = [];
const walk = (d) => {
  for (const entry of readdirSync(d)) {
    const p = path.join(d, entry);
    if (statSync(p).isDirectory()) walk(p);
    else files.push(p);
  }
};
walk(dist);

if (existsSync(path.join(dist, 'mockServiceWorker.js'))) {
  failures.push('dist/mockServiceWorker.js is present');
}
for (const file of files.filter((f) => f.endsWith('.js'))) {
  const text = readFileSync(file, 'utf8');
  if (/mockServiceWorker|setupWorker|\bmsw\b/.test(text)) {
    failures.push(`${path.relative(dist, file)} references the mock API`);
  }
}
// Vite writes attribute values with HTML entities (&#39; for the quote); compare decoded text.
const index = readFileSync(path.join(dist, 'index.html'), 'utf8').replace(/&#39;|&apos;/g, "'");
if (!index.includes(`content="${csp}"`)) failures.push('index.html lacks the strict CSP meta tag');
if (/<script(?![^>]*\bsrc=)[^>]*>[^<]/.test(index))
  failures.push('index.html has an inline script');
if (/<style[\s>]/.test(index)) failures.push('index.html has an inline style element');
if (/(src|href)="(https?:)?\/\//.test(index)) failures.push('index.html references another origin');

if (failures.length > 0) {
  console.error('check-build: FAILED');
  for (const f of failures) console.error(`  - ${f}`);
  process.exit(1);
}
console.log(`check-build: ok (${files.length} files, no mock API, strict CSP meta tag present)`);
