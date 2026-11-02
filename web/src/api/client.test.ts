import { describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  ApiShapeError,
  createClient,
  describeError,
  etagForVersion,
  parseProblem,
  parseRetryAfter,
  versionFromEtag,
} from './client';

function response(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  const text = body === undefined ? null : typeof body === 'string' ? body : JSON.stringify(body);
  return new Response(text, { status, headers });
}

function clientWith(fetchImpl: typeof fetch, onUnauthorized = vi.fn()) {
  return { api: createClient({ fetchImpl, onUnauthorized }), onUnauthorized };
}

describe('problem+json parsing', () => {
  it('reads an RFC 7807 document', async () => {
    const problem = await parseProblem(
      response(
        412,
        {
          type: 'about:blank',
          title: 'The item changed',
          status: 412,
          detail: 'Reload it.',
          instance: '/links/1',
        },
        { 'content-type': 'application/problem+json; charset=utf-8' },
      ),
    );
    expect(problem).toEqual({
      type: 'about:blank',
      title: 'The item changed',
      status: 412,
      detail: 'Reload it.',
      instance: '/links/1',
    });
  });

  it('synthesises a problem for a non-JSON error body without echoing it', async () => {
    const problem = await parseProblem(
      response(502, '<html>Bad gateway from nginx</html>', { 'content-type': 'text/html' }),
    );
    expect(problem.status).toBe(502);
    expect(problem.title).toBe('Server error');
    expect(problem.detail).toBe('');
    expect(JSON.stringify(problem)).not.toContain('nginx');
  });

  it('falls back when the JSON is not a problem document', async () => {
    const problem = await parseProblem(
      response(404, { message: 'nope' }, { 'content-type': 'application/json' }),
    );
    expect(problem.title).toBe('Not found');
  });
});

describe('ETag helpers', () => {
  it('round-trips a weak ETag', () => {
    expect(etagForVersion(3)).toBe('W/"3"');
    expect(versionFromEtag('W/"3"')).toBe(3);
    expect(versionFromEtag('"7"')).toBe(7);
    expect(versionFromEtag('garbage')).toBeNull();
    expect(versionFromEtag(null)).toBeNull();
  });

  it('parses Retry-After in seconds or as a date', () => {
    expect(parseRetryAfter('30')).toBe(30);
    expect(parseRetryAfter(null)).toBeNull();
    expect(parseRetryAfter('not a date')).toBeNull();
    const soon = new Date(Date.now() + 10_000).toUTCString();
    const seconds = parseRetryAfter(soon);
    expect(seconds).not.toBeNull();
    expect(seconds).toBeGreaterThanOrEqual(8);
    expect(seconds).toBeLessThanOrEqual(11);
  });
});

describe('request shape', () => {
  it('sends same-origin credentials, JSON, If-Match and the CSRF header', async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve(
        response(200, { id: 'lnk_1' }, { 'content-type': 'application/json', etag: 'W/"2"' }),
      ),
    );
    const { api } = clientWith(fetchImpl);
    const result = await api.post<{ id: string }>('/links/lnk_1/accept', {
      body: { note: '' },
      ifMatch: 'W/"1"',
      csrfToken: 'csrf-token',
    });
    expect(result.data.id).toBe('lnk_1');
    expect(result.etag).toBe('W/"2"');
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/v1/links/lnk_1/accept');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect(init.body).toBe('{"note":""}');
    const headers = init.headers as Headers;
    expect(headers.get('if-match')).toBe('W/"1"');
    expect(headers.get('x-csrf-token')).toBe('csrf-token');
    expect(headers.get('content-type')).toBe('application/json');
  });

  it('builds query strings and drops empty values', async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve(
        response(200, { items: [], next_cursor: null }, { 'content-type': 'application/json' }),
      ),
    );
    const { api } = clientWith(fetchImpl);
    await api.get('/links', { query: { status: 'proposed', cursor: null, limit: 50, system: '' } });
    const [url] = fetchImpl.mock.calls[0] as unknown as [string];
    expect(url).toBe('/api/v1/links?status=proposed&limit=50');
  });

  it('rejects a non-JSON success body', async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve(response(200, '<html></html>', { 'content-type': 'text/html' })),
    );
    const { api } = clientWith(fetchImpl);
    await expect(api.get('/me')).rejects.toBeInstanceOf(ApiShapeError);
  });
});

describe('status handling', () => {
  it('calls the unauthorized hook on 401 and throws', async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve(
        response(
          401,
          { title: 'Signed out', status: 401 },
          { 'content-type': 'application/problem+json' },
        ),
      ),
    );
    const { api, onUnauthorized } = clientWith(fetchImpl);
    await expect(api.get('/me')).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('marks 412 as stale and carries Retry-After on 429', async () => {
    const stale = createClient({
      fetchImpl: (() =>
        Promise.resolve(
          response(
            412,
            { title: 'The item changed', status: 412 },
            { 'content-type': 'application/problem+json' },
          ),
        )) as unknown as typeof fetch,
      onUnauthorized: vi.fn(),
    });
    const error = await stale
      .post('/links/x/accept', { ifMatch: 'W/"1"' })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).isStale).toBe(true);
    expect(describeError(error)).toBe('This item changed while you were looking at it. Reloaded.');

    const limited = createClient({
      fetchImpl: (() =>
        Promise.resolve(
          response(
            429,
            { title: 'Too many requests', status: 429 },
            { 'content-type': 'application/problem+json', 'retry-after': '12' },
          ),
        )) as unknown as typeof fetch,
      onUnauthorized: vi.fn(),
    });
    const limitedError = await limited.get('/links').catch((e: unknown) => e);
    expect((limitedError as ApiError).retryAfterSeconds).toBe(12);
    expect(describeError(limitedError)).toBe('Too many requests. Try again in 12 s.');
  });

  it('describes network failures without detail', () => {
    expect(describeError(new TypeError('Failed to fetch'))).toBe(
      'The server could not be reached.',
    );
    expect(describeError(new Error('x'))).toBe('Something went wrong.');
  });
});
