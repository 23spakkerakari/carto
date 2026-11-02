/* msw handlers for the M2 API (spec 12, package E's routes). They honour cursors, If-Match
 * (412 on mismatch, 428 when missing), the CSRF header (403 without it), the roles of spec 14.2
 * (403 without the permission) and the logged-out flag (401 everywhere). Errors are RFC 7807. */
import { HttpResponse, http, type HttpResponseInit, type JsonBodyType } from 'msw';

import type {
  DecisionRequest,
  EntityPatch,
  LinkStatus,
  Page,
  Permission,
  ProblemDetail,
  RejectReason,
  SystemPatch,
} from '../api/types';
import { appendAudit, type MockDb } from './db';

const BASE = '/api/v1';
const MAX_LIMIT = 500;
const DEFAULT_LIMIT = 50;

function problem(status: number, title: string, detail = ''): Response {
  const body: ProblemDetail = { type: 'about:blank', title, status, detail, instance: null };
  return HttpResponse.json(body, {
    status,
    headers: { 'Content-Type': 'application/problem+json' },
  });
}

function json(body: JsonBodyType, init: HttpResponseInit = {}): Response {
  return HttpResponse.json(body, init);
}

function page<T>(items: T[], url: URL): Page<T> {
  const limitRaw = Number(url.searchParams.get('limit') ?? DEFAULT_LIMIT);
  const limit = Math.min(
    Math.max(1, Number.isFinite(limitRaw) ? limitRaw : DEFAULT_LIMIT),
    MAX_LIMIT,
  );
  const cursorRaw = url.searchParams.get('cursor');
  const offset = cursorRaw ? Number(cursorRaw) : 0;
  const start = Number.isFinite(offset) && offset >= 0 ? offset : 0;
  const slice = items.slice(start, start + limit);
  const next = start + limit < items.length ? String(start + limit) : null;
  return { items: slice, next_cursor: next };
}

function versionOf(etag: string): number {
  return Number(/^(?:W\/)?"(\d+)"$/.exec(etag)?.[1] ?? 0);
}

function bumped(etag: string): string {
  return `W/"${versionOf(etag) + 1}"`;
}

const LINK_STATUSES: readonly LinkStatus[] = [
  'proposed',
  'accepted',
  'rejected',
  'auto_accepted',
  'stale',
];
const REJECT_REASONS: readonly RejectReason[] = [
  'different_meaning',
  'coincidence',
  'wrong_direction',
];

export function createHandlers(db: MockDb) {
  const guard = (request: Request, permission: Permission): Response | null => {
    if (db.options.loggedOut) return problem(401, 'Signed out', 'Sign in to continue.');
    if (!db.me.permissions.includes(permission)) {
      return problem(403, 'Not allowed', 'Your role does not include this permission.');
    }
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      if (request.headers.get('x-csrf-token') !== db.me.csrf_token) {
        return problem(403, 'CSRF token missing or wrong');
      }
    }
    return null;
  };

  const precondition = (request: Request, etag: string): Response | null => {
    const ifMatch = request.headers.get('if-match');
    if (!ifMatch)
      return problem(428, 'Precondition required', 'Send If-Match with the current ETag.');
    if (ifMatch.trim() !== etag) {
      return problem(412, 'The item changed', 'The item was changed by someone else. Reload it.');
    }
    return null;
  };

  const readBody = async <T>(request: Request): Promise<T | Response> => {
    try {
      return (await request.json()) as T;
    } catch {
      return problem(400, 'Malformed JSON body');
    }
  };

  const decide = async (
    request: Request,
    id: string,
    kind: 'accept' | 'reject' | 'association',
  ): Promise<Response> => {
    const denied = guard(request, 'review_links');
    if (denied) return denied;
    const link = db.links.find((l) => l.id === id);
    if (!link) return problem(404, 'Not found');
    const stale = precondition(request, link.etag);
    if (stale) return stale;
    const body = await readBody<DecisionRequest>(request);
    if (body instanceof Response) return body;
    if (kind === 'reject' && (!body.reason || !REJECT_REASONS.includes(body.reason))) {
      return problem(422, 'A reject reason is required');
    }
    if (typeof body.note === 'string' && body.note.length > 512) {
      return problem(422, 'The note is too long');
    }
    if (link.status !== 'proposed' && link.status !== 'stale' && link.status !== 'auto_accepted') {
      return problem(
        409,
        'Already decided',
        'Only proposed, stale or auto-accepted links take a decision.',
      );
    }
    const before = { status: link.status, version: link.version, role: link.role };
    const now = new Date().toISOString();
    link.status = kind === 'reject' ? 'rejected' : 'accepted';
    link.role = kind === 'association' ? 'association' : link.role;
    link.reject_reason = kind === 'reject' ? (body.reason ?? null) : null;
    link.decided_by = db.me.subject;
    link.decided_at = now;
    link.updated_at = now;
    link.version += 1;
    link.etag = bumped(link.etag);
    appendAudit(
      db,
      `link.${kind}`,
      `link:${link.id}`,
      before,
      {
        status: link.status,
        version: link.version,
        role: link.role,
        reject_reason: link.reject_reason,
      },
      body.note,
    );
    return json(link, { headers: { ETag: link.etag } });
  };

  return [
    http.get(`${BASE}/me`, () => {
      if (db.options.loggedOut) return problem(401, 'Signed out', 'Sign in to continue.');
      return json(db.me);
    }),
    http.post(`${BASE}/auth/logout`, ({ request }) => {
      if (db.options.loggedOut) return problem(401, 'Signed out');
      if (request.headers.get('x-csrf-token') !== db.me.csrf_token) {
        return problem(403, 'CSRF token missing or wrong');
      }
      db.options.loggedOut = true;
      return new HttpResponse(null, { status: 204 });
    }),

    http.get(`${BASE}/links`, ({ request }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const url = new URL(request.url);
      const statusParam = url.searchParams.get('status') ?? 'proposed';
      if (!LINK_STATUSES.includes(statusParam as LinkStatus)) return problem(422, 'Unknown status');
      const items = db.links
        .filter((l) => l.status === statusParam)
        .sort((a, b) => b.score * b.affected_volume - a.score * a.affected_volume);
      return json(page(items, url));
    }),
    http.get(`${BASE}/links/:id`, ({ request, params }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const link = db.links.find((l) => l.id === params.id);
      if (!link) return problem(404, 'Not found');
      return json(link, { headers: { ETag: link.etag } });
    }),
    http.post(`${BASE}/links/:id/accept`, ({ request, params }) =>
      decide(request, String(params.id), 'accept'),
    ),
    http.post(`${BASE}/links/:id/reject`, ({ request, params }) =>
      decide(request, String(params.id), 'reject'),
    ),
    http.post(`${BASE}/links/:id/association`, ({ request, params }) =>
      decide(request, String(params.id), 'association'),
    ),

    http.get(`${BASE}/entities`, ({ request }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      return json(page(db.entities, new URL(request.url)));
    }),
    http.get(`${BASE}/entities/:id`, ({ request, params }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const entity = db.entities.find((e) => e.id === params.id);
      if (!entity) return problem(404, 'Not found');
      return json(entity, { headers: { ETag: entity.etag } });
    }),
    http.patch(`${BASE}/entities/:id`, async ({ request, params }) => {
      const denied = guard(request, 'review_links');
      if (denied) return denied;
      const entity = db.entities.find((e) => e.id === params.id);
      if (!entity) return problem(404, 'Not found');
      const stale = precondition(request, entity.etag);
      if (stale) return stale;
      const body = await readBody<EntityPatch>(request);
      if (body instanceof Response) return body;
      if (
        body.name !== undefined &&
        body.name !== null &&
        (body.name.length === 0 || body.name.length > 128)
      ) {
        return problem(422, 'The name must be 1 to 128 characters');
      }
      const before = {
        name: entity.name,
        name_confirmed: entity.name_confirmed,
        description: entity.description,
      };
      if (typeof body.name === 'string') {
        entity.name = body.name;
        entity.name_confirmed = true;
      }
      if (typeof body.description === 'string') entity.description = body.description;
      entity.updated_at = new Date().toISOString();
      entity.etag = bumped(entity.etag);
      appendAudit(db, 'entity.rename', `entity:${entity.id}`, before, {
        name: entity.name,
        name_confirmed: entity.name_confirmed,
        description: entity.description,
      });
      return json(entity, { headers: { ETag: entity.etag } });
    }),

    http.get(`${BASE}/systems`, ({ request }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      return json(page(db.systems, new URL(request.url)));
    }),
    http.get(`${BASE}/systems/:id`, ({ request, params }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const system = db.systems.find((s) => s.id === params.id);
      if (!system) return problem(404, 'Not found');
      return json(system, { headers: { ETag: system.etag } });
    }),
    http.patch(`${BASE}/systems/:id`, async ({ request, params }) => {
      const denied = guard(request, 'review_links');
      if (denied) return denied;
      const system = db.systems.find((s) => s.id === params.id);
      if (!system) return problem(404, 'Not found');
      const stale = precondition(request, system.etag);
      if (stale) return stale;
      const body = await readBody<SystemPatch>(request);
      if (body instanceof Response) return body;
      const before = { name: system.name, description: system.description };
      if (typeof body.name === 'string' && body.name.length > 0) system.name = body.name;
      if (typeof body.description === 'string') system.description = body.description;
      system.updated_at = new Date().toISOString();
      system.etag = bumped(system.etag);
      appendAudit(db, 'system.rename', `system:${system.id}`, before, {
        name: system.name,
        description: system.description,
      });
      return json(system, { headers: { ETag: system.etag } });
    }),

    http.get(`${BASE}/sources`, ({ request }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      return json(page(db.sources, new URL(request.url)));
    }),
    http.get(`${BASE}/sources/:id`, ({ request, params }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const source = db.sources.find((s) => s.id === params.id);
      if (!source) return problem(404, 'Not found');
      return json(source);
    }),
    http.get(`${BASE}/sources/:id/health`, ({ request, params }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const source = db.sources.find((s) => s.id === params.id);
      if (!source) return problem(404, 'Not found');
      if (!source.health) return problem(404, 'No heartbeat yet');
      return json(source.health);
    }),

    http.get(`${BASE}/fields`, ({ request }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const url = new URL(request.url);
      const system = url.searchParams.get('system');
      const items = system ? db.fields.filter((f) => f.system_id === system) : db.fields;
      return json(page(items, url));
    }),
    http.get(`${BASE}/fields/:id`, ({ request, params }) => {
      const denied = guard(request, 'view');
      if (denied) return denied;
      const field = db.fields.find((f) => f.id === decodeURIComponent(String(params.id)));
      if (!field) return problem(404, 'Not found');
      return json(field);
    }),

    http.get(`${BASE}/audit`, ({ request }) => {
      const denied = guard(request, 'view_audit');
      if (denied) return denied;
      const items = [...db.audit].sort((a, b) => b.id - a.id);
      return json(page(items, new URL(request.url)));
    }),
  ];
}
