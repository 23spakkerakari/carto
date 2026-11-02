// Copies the IBM Plex faces the console uses (OFL-1.1) from the @ibm/plex-* packages into
// public/fonts, so the build self-hosts them: the CSP (`font-src` falls under default-src 'self')
// and the air-gapped install forbid a font CDN. Run `npm run fonts` after a Renovate bump of
// @ibm/plex-*; the result is committed.
import { copyFileSync, mkdirSync, readdirSync, statSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.join(here, '..', 'public', 'fonts');
mkdirSync(out, { recursive: true });

/** Package, the file stem inside its woff2 directory, and the file name we publish. The
 * `split`/latin files cover the console's copy; identifiers are ASCII by construction. */
const faces = [
  ['@ibm/plex-sans', 'IBMPlexSans-Regular', 'plex-sans-400.woff2'],
  ['@ibm/plex-sans', 'IBMPlexSans-Italic', 'plex-sans-400i.woff2'],
  ['@ibm/plex-sans', 'IBMPlexSans-Medium', 'plex-sans-500.woff2'],
  ['@ibm/plex-sans', 'IBMPlexSans-SemiBold', 'plex-sans-600.woff2'],
  ['@ibm/plex-sans-condensed', 'IBMPlexSansCondensed-Medium', 'plex-sans-condensed-500.woff2'],
  ['@ibm/plex-sans-condensed', 'IBMPlexSansCondensed-SemiBold', 'plex-sans-condensed-600.woff2'],
  ['@ibm/plex-mono', 'IBMPlexMono-Regular', 'plex-mono-400.woff2'],
  ['@ibm/plex-mono', 'IBMPlexMono-Medium', 'plex-mono-500.woff2'],
];

function findFile(dir, stem) {
  // Prefer a latin subset ("-Latin1" or "latin") when the package ships split files.
  const candidates = [];
  const walk = (d) => {
    for (const entry of readdirSync(d)) {
      const p = path.join(d, entry);
      if (statSync(p).isDirectory()) walk(p);
      else if (entry.endsWith('.woff2') && entry.startsWith(stem)) candidates.push(p);
    }
  };
  walk(dir);
  const exact = candidates.find((p) => path.basename(p) === `${stem}.woff2`);
  const latin = candidates.find((p) => /latin1|-Latin1/i.test(path.basename(p)));
  const file = latin ?? exact ?? candidates[0];
  if (!file) throw new Error(`no woff2 for ${stem} under ${dir}`);
  return file;
}

for (const [pkg, stem, name] of faces) {
  const root = path.dirname(require.resolve(`${pkg}/package.json`));
  const src = findFile(root, stem);
  copyFileSync(src, path.join(out, name));
  console.log(`${name}  <-  ${path.relative(root, src)} (${statSync(src).size} bytes)`);
}
