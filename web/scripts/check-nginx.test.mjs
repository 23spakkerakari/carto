// `node --test scripts/check-nginx.test.mjs` (npm run test:scripts, run by `npm run build`):
// the checker of check-nginx.mjs must pass the shipped configuration and catch each of the
// five mutations it exists for. Every mutation is applied to the real deploy files, so a
// rewrite of nginx.conf that the checker can no longer read fails here, not in production.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { check } from './check-nginx.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const deploy = path.join(here, '..', 'deploy');
const shipped = {
  nginx: readFileSync(path.join(deploy, 'nginx.conf'), 'utf8'),
  snippet: readFileSync(path.join(deploy, 'security-headers.conf'), 'utf8'),
  dockerfile: readFileSync(path.join(deploy, 'Dockerfile'), 'utf8'),
};
const INCLUDE = 'include /etc/nginx/security-headers.conf;';

/** `text` with exactly one occurrence of `from` replaced; fails when the anchor is not there. */
function replaceOnce(text, from, to) {
  const first = text.indexOf(from);
  assert.notEqual(first, -1, `anchor not found in the shipped file: ${from}`);
  assert.equal(text.indexOf(from, first + 1), -1, `anchor is not unique: ${from}`);
  return text.slice(0, first) + to + text.slice(first + from.length);
}

/** The body of one `location <name> {` block of the shipped nginx.conf. */
function locationBody(name) {
  const start = shipped.nginx.indexOf(`location ${name} {`);
  assert.notEqual(start, -1, `no location ${name} in nginx.conf`);
  const open = shipped.nginx.indexOf('{', start);
  let depth = 1;
  let i = open + 1;
  while (depth > 0) {
    if (shipped.nginx[i] === '{') depth += 1;
    else if (shipped.nginx[i] === '}') depth -= 1;
    i += 1;
  }
  return shipped.nginx.slice(open + 1, i - 1);
}

test('the shipped configuration passes', () => {
  assert.deepEqual(check(shipped), []);
});

test('a location that adds a header without the include is caught', () => {
  const body = locationBody('/assets/');
  const mutated = replaceOnce(
    shipped.nginx,
    `location /assets/ {${body}`,
    `location /assets/ {${body.replace(INCLUDE, '')}`,
  );
  assert.notEqual(mutated, shipped.nginx);
  const failures = check({ ...shipped, nginx: mutated });
  assert.equal(failures.length, 1, failures.join('\n'));
  assert.match(failures[0], /^location \/assets\/ adds a header without including/);
});

test('a server block that declares a security header itself is caught', () => {
  const mutated = replaceOnce(
    shipped.nginx,
    '        index index.html;\n',
    '        index index.html;\n        add_header X-Frame-Options DENY always;\n',
  );
  const failures = check({ ...shipped, nginx: mutated });
  assert.deepEqual(failures, [
    'the server block declares security headers itself; they belong in the snippet',
  ]);
});

test('a snippet missing one header of the set is caught', () => {
  const mutated = replaceOnce(
    shipped.snippet,
    'add_header Referrer-Policy no-referrer always;\n',
    '',
  );
  const failures = check({ ...shipped, snippet: mutated });
  assert.deepEqual(failures, [
    'security-headers.conf lacks "add_header Referrer-Policy ... always;"',
  ]);
  // A commented-out directive counts as missing too.
  const commented = replaceOnce(
    shipped.snippet,
    'add_header Referrer-Policy no-referrer always;',
    '# add_header Referrer-Policy no-referrer always;',
  );
  assert.deepEqual(check({ ...shipped, snippet: commented }), failures);
});

test('a Cache-Control with a max-age that carries always is caught', () => {
  const mutated = replaceOnce(
    shipped.nginx,
    'add_header Cache-Control "public, max-age=604800";',
    'add_header Cache-Control "public, max-age=604800" always;',
  );
  const failures = check({ ...shipped, nginx: mutated });
  assert.equal(failures.length, 1, failures.join('\n'));
  assert.match(
    failures[0],
    /^location \/fonts\/: ".*max-age=604800" always;" would cache a 404 too; drop always$/,
  );
});

test('a Content-Type set through add_header is caught', () => {
  const mutated = replaceOnce(
    shipped.nginx,
    'default_type text/plain;',
    'add_header Content-Type text/plain;',
  );
  const failures = check({ ...shipped, nginx: mutated });
  assert.deepEqual(failures, [
    'location = /healthz sets Content-Type with add_header; use default_type',
  ]);
});

test('a Dockerfile that does not install the snippet is caught', () => {
  const mutated = shipped.dockerfile.replace(/^COPY[^\n]*security-headers\.conf[^\n]*\n/m, '');
  assert.notEqual(mutated, shipped.dockerfile);
  const failures = check({ ...shipped, dockerfile: mutated });
  assert.deepEqual(failures, [
    'Dockerfile does not copy deploy/security-headers.conf to /etc/nginx/security-headers.conf',
  ]);
});
