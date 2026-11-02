import {
  QueryClient,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from '@tanstack/react-query';
import { createContext, useContext } from 'react';

import { ApiError, createClient, isCsrfRefusal, type ApiClient } from './client';
import type {
  AuditEntryView,
  DecisionKind,
  DecisionRequest,
  EntityPatch,
  EntityView,
  FieldView,
  LinkStatus,
  LinkView,
  MeView,
  Page,
  SourceHealthView,
  SourceView,
  SystemPatch,
  SystemView,
} from './types';

/** API data lives in TanStack Query's memory only: no persister, no browser storage (spec 13). */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        gcTime: 5 * 60_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status < 500 && error.status !== 429) return false;
          return failureCount < 2;
        },
        refetchOnWindowFocus: false,
      },
      mutations: { retry: false },
    },
  });
}

export const ApiContext = createContext<ApiClient | null>(null);

export function useApi(): ApiClient {
  const api = useContext(ApiContext);
  if (!api) throw new Error('ApiContext is missing');
  return api;
}

export const defaultApiClient = createClient();

export const keys = {
  me: ['me'] as const,
  links: (status: LinkStatus) => ['links', status] as const,
  link: (id: string) => ['link', id] as const,
  entities: ['entities'] as const,
  entity: (id: string) => ['entity', id] as const,
  systems: ['systems'] as const,
  system: (id: string) => ['system', id] as const,
  sources: ['sources'] as const,
  source: (id: string) => ['source', id] as const,
  sourceHealth: (id: string) => ['source', id, 'health'] as const,
  fields: (system: string | null) => ['fields', system ?? ''] as const,
  field: (id: string) => ['field', id] as const,
  audit: ['audit'] as const,
};

const PAGE = 50;
/** Page size for the lists the console reads whole (systems, sources). */
export const LIST_LIMIT = 500;
/** How many pages of `LIST_LIMIT` a whole-list read follows before it stops and says so. */
export const LIST_MAX_PAGES = 20;

function meQuery(api: ApiClient) {
  return {
    queryKey: keys.me,
    queryFn: async () => (await api.get<MeView>('/me')).data,
  };
}

export function useMe() {
  const api = useApi();
  return useQuery({ ...meQuery(api), staleTime: 5 * 60_000 });
}

/** The synchroniser token from the cached `/me` (14.1). Mutations call this at send time. */
export function csrfTokenOf(client: QueryClient): string | null {
  return client.getQueryData<MeView>(keys.me)?.csrf_token ?? null;
}

/** Sends a mutation with the cached token. When the API refuses it as a CSRF failure (the
 * session or its token was rotated server-side while `/me` sat in the cache), `/me` is fetched
 * once, the cache updated, and the request sent once more with the fresh token; a second
 * refusal, or any other error, surfaces as it is. A role 403 is never retried. */
export async function withCsrfRetry<T>(
  api: ApiClient,
  client: QueryClient,
  send: (csrfToken: string | null) => Promise<T>,
): Promise<T> {
  try {
    return await send(csrfTokenOf(client));
  } catch (error) {
    if (!isCsrfRefusal(error)) throw error;
    let fresh: MeView;
    try {
      fresh = await client.query({ ...meQuery(api), staleTime: 0, retry: false });
    } catch {
      // The session itself is gone (401 already sent the browser to login) or /me failed:
      // report the refusal the mutation got, not the refetch.
      throw error;
    }
    return await send(fresh.csrf_token);
  }
}

/** A whole list read page by page: `items` is every item up to `LIST_MAX_PAGES` pages and
 * `truncated` says whether the API still had more when the bound stopped the read. */
export interface Listing<T> {
  items: T[];
  truncated: boolean;
}

async function fetchListing<T>(api: ApiClient, path: string): Promise<Listing<T>> {
  const items: T[] = [];
  let cursor: string | null = null;
  for (let pages = 1; ; pages += 1) {
    const page: Page<T> = (await api.get<Page<T>>(path, { query: { cursor, limit: LIST_LIMIT } }))
      .data;
    items.push(...page.items);
    if (!page.next_cursor) return { items, truncated: false };
    if (pages >= LIST_MAX_PAGES) return { items, truncated: true };
    cursor = page.next_cursor;
  }
}

export type ListName = 'systems' | 'sources';

const LIST_PATH: Record<ListName, string> = { systems: '/systems', sources: '/sources' };

function listingQuery<T>(api: ApiClient, list: ListName) {
  return {
    queryKey: keys[list],
    queryFn: () => fetchListing<T>(api, LIST_PATH[list]),
  };
}

const selectItems = <T>(listing: Listing<T>) => listing.items;
const selectTruncated = (listing: Listing<unknown>) => listing.truncated;

/** True when the matching list read hit `LIST_MAX_PAGES` and the console shows only part of
 * it. Shares the list's cache entry: no second request. */
export function useListTruncated(list: ListName): boolean {
  const api = useApi();
  const query = useQuery({ ...listingQuery<unknown>(api, list), select: selectTruncated });
  return query.data ?? false;
}

type LinkPages = InfiniteData<Page<LinkView>, string | null>;

export function useLinks(status: LinkStatus) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: keys.links(status),
    queryFn: async ({ pageParam }) =>
      (
        await api.get<Page<LinkView>>('/links', {
          query: { status, cursor: pageParam, limit: PAGE },
        })
      ).data,
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useLink(id: string | null) {
  const api = useApi();
  return useQuery({
    queryKey: keys.link(id ?? ''),
    queryFn: async () => (await api.get<LinkView>(`/links/${encodeURIComponent(id ?? '')}`)).data,
    enabled: id !== null,
  });
}

export interface DecisionVariables {
  link: LinkView;
  kind: DecisionKind;
  body: DecisionRequest;
}

const DECIDED_STATUS: Record<DecisionKind, LinkStatus> = {
  accept: 'accepted',
  reject: 'rejected',
  association: 'accepted',
};

function removeFromPages(pages: LinkPages | undefined, id: string): LinkPages | undefined {
  if (!pages) return pages;
  return {
    ...pages,
    pages: pages.pages.map((page) => ({
      ...page,
      items: page.items.filter((item) => item.id !== id),
    })),
  };
}

/** Accept, reject or mark as association, optimistically: the item leaves the list it sits in
 * at once and comes back on failure. If-Match carries the ETag the list served; a 412 means
 * someone else decided first, and the caller reloads. */
export function useDecision() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ link, kind, body }: DecisionVariables) =>
      withCsrfRetry(
        api,
        client,
        async (csrfToken) =>
          (
            await api.post<LinkView>(`/links/${encodeURIComponent(link.id)}/${kind}`, {
              body,
              ifMatch: link.etag,
              csrfToken,
            })
          ).data,
      ),
    onMutate: async ({ link, kind, body }) => {
      const listKey = keys.links(link.status);
      await client.cancelQueries({ queryKey: listKey });
      const previousList = client.getQueryData<LinkPages>(listKey);
      const previousLink = client.getQueryData<LinkView>(keys.link(link.id));
      client.setQueryData<LinkPages>(listKey, (pages) => removeFromPages(pages, link.id));
      client.setQueryData<LinkView>(keys.link(link.id), {
        ...link,
        status: DECIDED_STATUS[kind],
        role: kind === 'association' ? 'association' : link.role,
        reject_reason: kind === 'reject' ? (body.reason ?? null) : null,
      });
      return { previousList, previousLink, listKey };
    },
    onError: (_error, { link }, context) => {
      if (!context) return;
      client.setQueryData(context.listKey, context.previousList);
      client.setQueryData(keys.link(link.id), context.previousLink);
    },
    onSuccess: (updated) => {
      client.setQueryData(keys.link(updated.id), updated);
    },
    onSettled: async (_data, _error, { link }) => {
      await client.invalidateQueries({ queryKey: ['links'] });
      await client.invalidateQueries({ queryKey: keys.link(link.id) });
      await client.invalidateQueries({ queryKey: keys.entities });
    },
  });
}

export function useEntities() {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: keys.entities,
    queryFn: async ({ pageParam }) =>
      (await api.get<Page<EntityView>>('/entities', { query: { cursor: pageParam, limit: PAGE } }))
        .data,
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function usePatchEntity() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ entity, patch }: { entity: EntityView; patch: EntityPatch }) =>
      withCsrfRetry(
        api,
        client,
        async (csrfToken) =>
          (
            await api.patch<EntityView>(`/entities/${encodeURIComponent(entity.id)}`, {
              body: patch,
              ifMatch: entity.etag,
              csrfToken,
            })
          ).data,
      ),
    onSettled: async () => {
      await client.invalidateQueries({ queryKey: keys.entities });
    },
  });
}

/** Every system, read page by page (see `fetchListing`); `useListTruncated('systems')` says
 * whether the read stopped short. */
export function useSystems() {
  const api = useApi();
  return useQuery({ ...listingQuery<SystemView>(api, 'systems'), select: selectItems });
}

export function useSystem(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: keys.system(id),
    queryFn: async () => (await api.get<SystemView>(`/systems/${encodeURIComponent(id)}`)).data,
  });
}

export function usePatchSystem() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ system, patch }: { system: SystemView; patch: SystemPatch }) =>
      withCsrfRetry(
        api,
        client,
        async (csrfToken) =>
          (
            await api.patch<SystemView>(`/systems/${encodeURIComponent(system.id)}`, {
              body: patch,
              ifMatch: system.etag,
              csrfToken,
            })
          ).data,
      ),
    onSettled: async () => {
      await client.invalidateQueries({ queryKey: keys.systems });
    },
  });
}

/** Every source, read page by page; `useListTruncated('sources')` says whether it stopped short. */
export function useSources() {
  const api = useApi();
  return useQuery({ ...listingQuery<SourceView>(api, 'sources'), select: selectItems });
}

export function useSource(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: keys.source(id),
    queryFn: async () => (await api.get<SourceView>(`/sources/${encodeURIComponent(id)}`)).data,
  });
}

export function useSourceHealth(id: string) {
  const api = useApi();
  return useQuery({
    queryKey: keys.sourceHealth(id),
    queryFn: async () =>
      (await api.get<SourceHealthView>(`/sources/${encodeURIComponent(id)}/health`)).data,
    refetchInterval: 30_000,
  });
}

export function useFields(system: string | null) {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: keys.fields(system),
    queryFn: async ({ pageParam }) =>
      (
        await api.get<Page<FieldView>>('/fields', {
          query: { system: system ?? undefined, cursor: pageParam, limit: PAGE },
        })
      ).data,
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useAudit() {
  const api = useApi();
  return useInfiniteQuery({
    queryKey: keys.audit,
    queryFn: async ({ pageParam }) =>
      (await api.get<Page<AuditEntryView>>('/audit', { query: { cursor: pageParam, limit: PAGE } }))
        .data,
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
  });
}

export function useLogout() {
  const api = useApi();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await withCsrfRetry(api, client, (csrfToken) =>
        api.post<undefined>('/auth/logout', { csrfToken }),
      );
    },
    onSuccess: () => {
      // A full reload: the API has ended the session, so /me answers 401 and the client goes
      // to the login route. The cache is not cleared first, which would refetch /me and race
      // this navigation with the 401 redirect.
      window.location.assign('/');
    },
  });
}
