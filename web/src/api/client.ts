import type { ProblemDetail } from './types';

export const API_BASE = '/api/v1';
export const LOGIN_PATH = `${API_BASE}/auth/login`;

/** A failed request: the RFC 7807 document the API sent (or one synthesised from the status
 * when the body was not a problem document) plus the headers the client acts on. */
export class ApiError extends Error {
  readonly status: number;
  readonly problem: ProblemDetail;
  /** Seconds from `Retry-After` on 429, when the header is a number. */
  readonly retryAfterSeconds: number | null;

  constructor(problem: ProblemDetail, retryAfterSeconds: number | null = null) {
    super(problem.title);
    this.name = 'ApiError';
    this.status = problem.status;
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** True when the item changed under the user (If-Match did not match). */
  get isStale(): boolean {
    return this.status === 412;
  }
}

/** Thrown when the response is not JSON where JSON was expected. */
export class ApiShapeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'ApiShapeError';
  }
}

export interface ApiResponse<T> {
  data: T;
  /** The `ETag` header as sent, e.g. `W/"3"`, or null. */
  etag: string | null;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  query?: Record<string, string | number | null | undefined>;
  body?: unknown;
  /** Sent as `If-Match`; required by the API on every mutation of a versioned resource. */
  ifMatch?: string | null;
  /** Sent as `X-CSRF-Token`; required by the API on every state-changing request (14.1). */
  csrfToken?: string | null;
  signal?: AbortSignal;
}

export interface ClientConfig {
  fetchImpl?: typeof fetch;
  /** Called on 401; the default sends the browser to the login route. */
  onUnauthorized?: () => void;
  base?: string;
}

const PROBLEM_TYPES = new Set(['application/problem+json', 'application/json']);

function titleFor(status: number): string {
  switch (status) {
    case 401:
      return 'Signed out';
    case 403:
      return 'Not allowed';
    case 404:
      return 'Not found';
    case 412:
      return 'The item changed';
    case 428:
      return 'Precondition required';
    case 429:
      return 'Too many requests';
    default:
      return status >= 500 ? 'Server error' : 'Request failed';
  }
}

function isProblem(value: unknown): value is Partial<ProblemDetail> & { status?: number } {
  return typeof value === 'object' && value !== null && 'title' in value;
}

/** Builds a `ProblemDetail` from a response whatever its body is. Never echoes request content
 * (that is the server's rule; the client just does not invent detail). */
export async function parseProblem(response: Response): Promise<ProblemDetail> {
  const fallback: ProblemDetail = {
    type: 'about:blank',
    title: titleFor(response.status),
    status: response.status,
    detail: '',
    instance: null,
  };
  const contentType = (response.headers.get('content-type') ?? '').split(';')[0]?.trim() ?? '';
  if (!PROBLEM_TYPES.has(contentType)) return fallback;
  try {
    const parsed: unknown = await response.json();
    if (!isProblem(parsed)) return fallback;
    return {
      type: typeof parsed.type === 'string' ? parsed.type : fallback.type,
      title: typeof parsed.title === 'string' ? parsed.title : fallback.title,
      status: typeof parsed.status === 'number' ? parsed.status : response.status,
      detail: typeof parsed.detail === 'string' ? parsed.detail : '',
      instance: typeof parsed.instance === 'string' ? parsed.instance : null,
    };
  } catch {
    return fallback;
  }
}

export function parseRetryAfter(value: string | null): number | null {
  if (value === null) return null;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds;
  const at = Date.parse(value);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.round((at - Date.now()) / 1000));
}

/** The weak ETag the API sends for a version: `W/"<version>"`. */
export function etagForVersion(version: number): string {
  return `W/"${version}"`;
}

/** The version inside an ETag (`W/"3"` or `"3"`), or null when it is not of that form. */
export function versionFromEtag(etag: string | null): number | null {
  if (etag === null) return null;
  const match = /^(?:W\/)?"(\d+)"$/.exec(etag.trim());
  return match?.[1] === undefined ? null : Number(match[1]);
}

function buildQuery(query: RequestOptions['query']): string {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === '') continue;
    params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : '';
}

export function defaultOnUnauthorized(): void {
  // Never loop: a page already at the login route is the identity provider's business.
  if (window.location.pathname.startsWith(LOGIN_PATH)) return;
  window.location.assign(LOGIN_PATH);
}

/** A thin fetch wrapper: same-origin credentials (the session is an HttpOnly cookie, ADR 0052),
 * JSON in and out, problem+json errors as `ApiError`, 401 to the login route. */
export function createClient(config: ClientConfig = {}) {
  const fetchImpl = config.fetchImpl ?? ((input, init) => fetch(input, init));
  const onUnauthorized = config.onUnauthorized ?? defaultOnUnauthorized;
  const base = config.base ?? API_BASE;

  async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiResponse<T>> {
    const method = options.method ?? 'GET';
    const headers = new Headers({ Accept: 'application/json' });
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    if (options.ifMatch) headers.set('If-Match', options.ifMatch);
    if (options.csrfToken) headers.set('X-CSRF-Token', options.csrfToken);

    const init: RequestInit = {
      method,
      headers,
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'manual',
    };
    if (options.body !== undefined) init.body = JSON.stringify(options.body);
    if (options.signal) init.signal = options.signal;
    const response = await fetchImpl(`${base}${path}${buildQuery(options.query)}`, init);

    if (response.status === 401) {
      onUnauthorized();
      throw new ApiError(await parseProblem(response));
    }
    if (!response.ok) {
      const problem = await parseProblem(response);
      const retryAfter =
        response.status === 429 ? parseRetryAfter(response.headers.get('retry-after')) : null;
      throw new ApiError(problem, retryAfter);
    }
    const etag = response.headers.get('etag');
    if (response.status === 204) return { data: undefined as T, etag };
    const contentType = (response.headers.get('content-type') ?? '').split(';')[0]?.trim();
    if (contentType !== 'application/json') {
      throw new ApiShapeError(
        `Expected JSON from ${method} ${path}, got ${contentType ?? 'nothing'}`,
      );
    }
    return { data: (await response.json()) as T, etag };
  }

  return {
    get: <T>(path: string, options: Omit<RequestOptions, 'method' | 'body'> = {}) =>
      request<T>(path, { ...options, method: 'GET' }),
    post: <T>(path: string, options: Omit<RequestOptions, 'method'> = {}) =>
      request<T>(path, { ...options, method: 'POST' }),
    patch: <T>(path: string, options: Omit<RequestOptions, 'method'> = {}) =>
      request<T>(path, { ...options, method: 'PATCH' }),
  };
}

export type ApiClient = ReturnType<typeof createClient>;

/** A short, plain line for an error, for the one-line error slots of the screens. */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 412) return 'This item changed while you were looking at it. Reloaded.';
    if (error.status === 429) {
      return error.retryAfterSeconds === null
        ? 'Too many requests. Try again shortly.'
        : `Too many requests. Try again in ${error.retryAfterSeconds} s.`;
    }
    if (error.status === 403) return 'Your role does not allow this.';
    return error.problem.detail
      ? `${error.problem.title}: ${error.problem.detail}`
      : error.problem.title;
  }
  if (error instanceof ApiShapeError) return 'The server answered in an unexpected format.';
  if (error instanceof TypeError) return 'The server could not be reached.';
  return 'Something went wrong.';
}
