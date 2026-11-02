import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { links, me as principals, sources, systems } from '../mocks/fixtures/shop';
import { ListTruncated } from '../ui/ListTruncated';
import { ApiError, createClient, describeError, isCsrfRefusal } from './client';
import {
  ApiContext,
  csrfTokenOf,
  keys,
  LIST_LIMIT,
  LIST_MAX_PAGES,
  makeQueryClient,
  useDecision,
  useListTruncated,
  usePatchSystem,
  useSources,
  useSystems,
} from './hooks';
import type { MeView, ProblemDetail, SourceView, SystemView } from './types';

/* The bodies the API sends (core sessions.gate and auth.require): same type and title, only
 * the detail differs. */
const CSRF_REFUSAL: ProblemDetail = {
  type: 'about:blank',
  title: 'forbidden',
  status: 403,
  detail: "send the session's CSRF token in x-csrf-token",
  instance: 'urn:carto:request:r1',
};
const ROLE_REFUSAL: ProblemDetail = {
  type: 'about:blank',
  title: 'forbidden',
  status: 403,
  detail: 'this account may not edit_systems',
  instance: 'urn:carto:request:r2',
};

interface Call {
  method: string;
  url: string;
  csrf: string | null;
}

function json(status: number, body: unknown, problem = false): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': problem ? 'application/problem+json' : 'application/json' },
  });
}

const notFound = () => json(404, { title: 'not found', status: 404 }, true);

function meWith(csrf: string): MeView {
  return { ...principals.integration_engineer, csrf_token: csrf };
}

/** A query client and API client over a scripted fetch; `calls` records every request. */
function harness(route: (call: Call) => Response) {
  const calls: Call[] = [];
  const fetchImpl = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    const call: Call = {
      method: init?.method ?? 'GET',
      url: typeof input === 'string' ? input : input instanceof URL ? input.href : input.url,
      csrf: headers.get('x-csrf-token'),
    };
    calls.push(call);
    return Promise.resolve(route(call));
  });
  const client = makeQueryClient();
  const api = createClient({ fetchImpl, onUnauthorized: vi.fn() });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ApiContext.Provider value={api}>{children}</ApiContext.Provider>
    </QueryClientProvider>
  );
  return { calls, client, wrapper };
}

const trace = (calls: Call[]) => calls.map((c) => `${c.method} ${c.url} ${c.csrf ?? '-'}`);

const system: SystemView = systems[0]!;
const link = links[0]!;

describe('CSRF refusal detection', () => {
  it('tells the CSRF 403 from the role 403 by the detail the API sets', () => {
    const csrf = new ApiError(CSRF_REFUSAL);
    const role = new ApiError(ROLE_REFUSAL);
    expect(isCsrfRefusal(csrf)).toBe(true);
    expect(isCsrfRefusal(role)).toBe(false);
    expect(isCsrfRefusal(new ApiError({ ...CSRF_REFUSAL, status: 401 }))).toBe(false);
    expect(isCsrfRefusal(new Error("send the session's CSRF token"))).toBe(false);
    expect(describeError(csrf)).toBe('Your session changed. Reload the page and try again.');
    expect(describeError(role)).toBe('Your role does not allow this.');
  });
});

describe('mutations after a CSRF rotation', () => {
  it('refetches /me once and resends once with the fresh token', async () => {
    const { calls, client, wrapper } = harness((call) => {
      if (call.method === 'GET' && call.url === '/api/v1/me') return json(200, meWith('fresh'));
      if (call.method === 'PATCH' && call.url === `/api/v1/systems/${system.id}`) {
        return call.csrf === 'fresh'
          ? json(200, { ...system, description: 'rewritten', etag: 'W/"4"' })
          : json(403, CSRF_REFUSAL, true);
      }
      return notFound();
    });
    client.setQueryData(keys.me, meWith('stale'));
    const { result } = renderHook(() => usePatchSystem(), { wrapper });

    const updated = await act(() =>
      result.current.mutateAsync({ system, patch: { description: 'rewritten' } }),
    );

    expect(updated.description).toBe('rewritten');
    expect(trace(calls)).toEqual([
      `PATCH /api/v1/systems/${system.id} stale`,
      'GET /api/v1/me -',
      `PATCH /api/v1/systems/${system.id} fresh`,
    ]);
    expect(csrfTokenOf(client)).toBe('fresh');
  });

  it('shows the refusal when the resend is refused too, and asks /me only once', async () => {
    const { calls, client, wrapper } = harness((call) => {
      if (call.method === 'GET' && call.url === '/api/v1/me') return json(200, meWith('fresh'));
      if (call.method === 'PATCH') return json(403, CSRF_REFUSAL, true);
      return notFound();
    });
    client.setQueryData(keys.me, meWith('stale'));
    const { result } = renderHook(() => usePatchSystem(), { wrapper });

    const error: unknown = await act(() =>
      result.current.mutateAsync({ system, patch: { description: 'x' } }).catch((e: unknown) => e),
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(isCsrfRefusal(error)).toBe(true);
    expect(describeError(error)).toBe('Your session changed. Reload the page and try again.');
    expect(trace(calls)).toEqual([
      `PATCH /api/v1/systems/${system.id} stale`,
      'GET /api/v1/me -',
      `PATCH /api/v1/systems/${system.id} fresh`,
    ]);
  });

  it('never retries a role 403', async () => {
    const { calls, client, wrapper } = harness((call) => {
      if (call.method === 'GET' && call.url === '/api/v1/me') return json(200, meWith('fresh'));
      if (call.method === 'PATCH') return json(403, ROLE_REFUSAL, true);
      return notFound();
    });
    client.setQueryData(keys.me, meWith('stale'));
    const { result } = renderHook(() => usePatchSystem(), { wrapper });

    const error: unknown = await act(() =>
      result.current.mutateAsync({ system, patch: { description: 'x' } }).catch((e: unknown) => e),
    );

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect(describeError(error)).toBe('Your role does not allow this.');
    expect(trace(calls)).toEqual([`PATCH /api/v1/systems/${system.id} stale`]);
    expect(csrfTokenOf(client)).toBe('stale');
  });

  it('reports the refusal, not the refetch, when /me cannot be read again', async () => {
    const { calls, client, wrapper } = harness((call) => {
      if (call.method === 'GET' && call.url === '/api/v1/me') {
        return json(503, { title: 'unavailable', status: 503 }, true);
      }
      if (call.method === 'PATCH') return json(403, CSRF_REFUSAL, true);
      return notFound();
    });
    client.setQueryData(keys.me, meWith('stale'));
    const { result } = renderHook(() => usePatchSystem(), { wrapper });

    const error: unknown = await act(() =>
      result.current.mutateAsync({ system, patch: { description: 'x' } }).catch((e: unknown) => e),
    );

    expect(isCsrfRefusal(error)).toBe(true);
    expect(calls.filter((c) => c.method === 'PATCH')).toHaveLength(1);
    expect(calls.filter((c) => c.url === '/api/v1/me').length).toBeGreaterThanOrEqual(1);
  });

  it('applies to decisions as well', async () => {
    const { calls, client, wrapper } = harness((call) => {
      if (call.method === 'GET' && call.url === '/api/v1/me') return json(200, meWith('fresh'));
      if (call.method === 'POST' && call.url === `/api/v1/links/${link.id}/accept`) {
        return call.csrf === 'fresh'
          ? json(200, { ...link, status: 'accepted' })
          : json(403, CSRF_REFUSAL, true);
      }
      if (call.method === 'GET') return json(200, { items: [], next_cursor: null });
      return notFound();
    });
    client.setQueryData(keys.me, meWith('stale'));
    const { result } = renderHook(() => useDecision(), { wrapper });

    const decided = await act(() =>
      result.current.mutateAsync({ link, kind: 'accept', body: { note: '' } }),
    );

    expect(decided.status).toBe('accepted');
    expect(trace(calls).filter((line) => !line.startsWith('GET /api/v1/links'))).toEqual([
      `POST /api/v1/links/${link.id}/accept stale`,
      'GET /api/v1/me -',
      `POST /api/v1/links/${link.id}/accept fresh`,
    ]);
  });
});

describe('whole-list reads', () => {
  it('useSystems follows next_cursor until the API has no more', async () => {
    const { calls, wrapper } = harness((call) => {
      const url = new URL(call.url, 'http://console.test');
      if (call.method !== 'GET' || url.pathname !== '/api/v1/systems') return notFound();
      const cursor = url.searchParams.get('cursor');
      if (cursor === null) {
        return json(200, { items: systems.slice(0, 2), next_cursor: 'page-2' });
      }
      if (cursor === 'page-2') return json(200, { items: systems.slice(2), next_cursor: null });
      return notFound();
    });
    const { result } = renderHook(
      () => ({ systems: useSystems(), truncated: useListTruncated('systems') }),
      { wrapper },
    );

    await waitFor(() => {
      expect(result.current.systems.isSuccess).toBe(true);
    });

    expect(result.current.systems.data?.map((s) => s.id)).toEqual(systems.map((s) => s.id));
    expect(result.current.truncated).toBe(false);
    expect(calls.map((c) => c.url)).toEqual([
      `/api/v1/systems?limit=${LIST_LIMIT}`,
      `/api/v1/systems?cursor=page-2&limit=${LIST_LIMIT}`,
    ]);
  });

  it('stops at the page bound and the screen says the list is truncated', async () => {
    let served = 0;
    const { calls, wrapper } = harness((call) => {
      const url = new URL(call.url, 'http://console.test');
      if (call.method !== 'GET' || url.pathname !== '/api/v1/sources') return notFound();
      served += 1;
      const item: SourceView = { ...sources[0]!, id: `src_${served}` };
      return json(200, { items: [item], next_cursor: `page-${served + 1}` });
    });
    function Count() {
      const list = useSources();
      return <span data-testid="count">{list.data?.length ?? 'none'}</span>;
    }
    render(
      <>
        <Count />
        <ListTruncated list="sources" />
      </>,
      { wrapper },
    );

    const marker = await screen.findByTestId('list-truncated-sources');

    expect(marker).toHaveTextContent(/^List truncated: only the first 10,000 sources are loaded/);
    expect(screen.getByTestId('count')).toHaveTextContent(String(LIST_MAX_PAGES));
    expect(calls).toHaveLength(LIST_MAX_PAGES);
    expect(LIST_MAX_PAGES * LIST_LIMIT).toBe(10_000);
  });

  it('renders no marker when the list fits', async () => {
    const { wrapper } = harness(() => json(200, { items: systems, next_cursor: null }));
    render(<ListTruncated list="systems" />, { wrapper });
    await waitFor(() => {
      expect(screen.queryByTestId('list-truncated-systems')).toBeNull();
    });
  });
});
