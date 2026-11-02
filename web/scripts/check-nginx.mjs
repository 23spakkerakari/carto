// Runs from `npm run build` (and `npm run check:nginx`): deploy/nginx.conf and
// deploy/security-headers.conf must agree with the spec 14.7 header set. nginx's add_header is
// not inherited by a location that declares any add_header of its own, so a location that adds
// one header (Cache-Control, say) loses every server-level security header unless it includes
// the snippet. This check reads the config as text (no nginx binary is needed) and fails when:
//   - a `location` block that contains `add_header` lacks `include <snippet>`;
//   - the `server` block lacks the include (locations that add no header inherit from it);
//   - the snippet does not declare each header of the set with `always`;
//   - a Cache-Control header with a max-age carries `always` (a 404 would be cached);
//   - a location sets Content-Type through add_header (two Content-Type headers);
//   - the Dockerfile does not copy the snippet to the path the includes name.
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const deploy = path.join(here, '..', 'deploy');
const SNIPPET = '/etc/nginx/security-headers.conf';
const HEADERS = [
  'Strict-Transport-Security',
  'Content-Security-Policy',
  'X-Content-Type-Options',
  'Referrer-Policy',
  'Permissions-Policy',
  'X-Frame-Options',
  'Cross-Origin-Opener-Policy',
  'Cross-Origin-Resource-Policy',
];

/** Strips `# ...` comments so a commented-out directive never counts. */
function stripComments(text) {
  return text
    .split('\n')
    .map((line) => line.replace(/#.*$/, ''))
    .join('\n');
}

/** Every `server { ... }` and `location <name> { ... }` block, with its body, at any depth. */
export function blocks(text) {
  const found = [];
  const re = /\b(server|location)\b([^{;]*)\{/g;
  let match;
  while ((match = re.exec(text)) !== null) {
    let depth = 1;
    let i = re.lastIndex;
    while (i < text.length && depth > 0) {
      if (text[i] === '{') depth += 1;
      else if (text[i] === '}') depth -= 1;
      i += 1;
    }
    found.push({ kind: match[1], name: match[2].trim(), body: text.slice(re.lastIndex, i - 1) });
  }
  return found;
}

/** The text of a block without its nested blocks: the directives at its own level. */
function ownDirectives(body) {
  let out = '';
  let depth = 0;
  for (const ch of body) {
    if (ch === '{') depth += 1;
    else if (ch === '}') depth -= 1;
    else if (depth === 0) out += ch;
  }
  return out;
}

export function check({ nginx, snippet, dockerfile }) {
  const failures = [];
  const conf = stripComments(nginx);
  const include = new RegExp(`\\binclude\\s+${SNIPPET.replace(/[.]/g, '\\.')}\\s*;`);

  const all = blocks(conf);
  const servers = all.filter((b) => b.kind === 'server');
  if (servers.length === 0) failures.push('nginx.conf has no server block');
  for (const server of servers) {
    const own = ownDirectives(server.body);
    if (!include.test(own)) failures.push('the server block does not include the snippet');
    if (/\badd_header\s+(Strict-Transport|Content-Security|X-Frame)/.test(own)) {
      failures.push(
        'the server block declares security headers itself; they belong in the snippet',
      );
    }
  }
  for (const location of all.filter((b) => b.kind === 'location')) {
    const label = `location ${location.name}`;
    if (/\badd_header\b/.test(location.body) && !include.test(location.body)) {
      failures.push(
        `${label} adds a header without including ${SNIPPET}; its security headers are dropped`,
      );
    }
    for (const line of location.body.match(/\badd_header\s+Cache-Control[^;]*;/g) ?? []) {
      if (/max-age=[1-9]/.test(line) && /\balways\b/.test(line)) {
        failures.push(`${label}: "${line.trim()}" would cache a 404 too; drop always`);
      }
    }
    if (/\badd_header\s+Content-Type\b/i.test(location.body)) {
      failures.push(`${label} sets Content-Type with add_header; use default_type`);
    }
  }

  const snip = stripComments(snippet);
  for (const header of HEADERS) {
    // The value may be a quoted string that itself contains `;` (HSTS).
    const re = new RegExp(`\\badd_header\\s+${header}\\s+(?:"[^"]*"|[^;"])*\\balways\\s*;`);
    if (!re.test(snip)) {
      failures.push(`security-headers.conf lacks "add_header ${header} ... always;"`);
    }
  }
  if (!/\badd_header\s+Content-Security-Policy\s+\$csp\s+always\s*;/.test(snip)) {
    failures.push('security-headers.conf must send the $csp map of nginx.conf');
  }
  if (!/^\s*map\s+\$sent_http_content_type\s+\$csp\s*\{/m.test(conf)) {
    failures.push('nginx.conf must define the $csp map the snippet sends');
  }

  if (!new RegExp(`COPY[^\\n]*deploy/security-headers\\.conf\\s+${SNIPPET}`).test(dockerfile)) {
    failures.push(`Dockerfile does not copy deploy/security-headers.conf to ${SNIPPET}`);
  }
  return failures;
}

const isMain =
  process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isMain) {
  const failures = check({
    nginx: readFileSync(path.join(deploy, 'nginx.conf'), 'utf8'),
    snippet: readFileSync(path.join(deploy, 'security-headers.conf'), 'utf8'),
    dockerfile: readFileSync(path.join(deploy, 'Dockerfile'), 'utf8'),
  });
  if (failures.length > 0) {
    console.error('check-nginx: FAILED');
    for (const f of failures) console.error(`  - ${f}`);
    process.exit(1);
  }
  console.log(
    'check-nginx: ok (every location that adds a header carries the security header set)',
  );
}
