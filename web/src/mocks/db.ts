/* In-memory state behind the mock API. Built once per page load from the fixtures; mutations
 * change it the way the real API would (version, etag, decided_by, audit row). */
import type {
  AuditEntryView,
  EntityView,
  FieldView,
  LinkView,
  MeView,
  SourceView,
  SystemView,
} from '../api/types';
import * as shop from './fixtures/shop';
import * as xss from './fixtures/xss';

export interface MockOptions {
  principal: shop.Principal;
  loggedOut: boolean;
  corpus: 'xss' | null;
}

export interface MockDb {
  options: MockOptions;
  me: MeView;
  systems: SystemView[];
  sources: SourceView[];
  fields: FieldView[];
  links: LinkView[];
  entities: EntityView[];
  audit: AuditEntryView[];
  nextAuditId: number;
}

const clone = <T>(value: T): T => structuredClone(value);

export function optionsFromSearch(search: string): MockOptions {
  const params = new URLSearchParams(search);
  const as = params.get('as');
  const principal: shop.Principal =
    as === 'admin' || as === 'auditor' || as === 'business_viewer' ? as : 'integration_engineer';
  return {
    principal,
    loggedOut: params.has('logged_out'),
    corpus: params.get('corpus') === 'xss' ? 'xss' : null,
  };
}

export function buildDb(options: MockOptions): MockDb {
  const db: MockDb = {
    options,
    me: clone(shop.me[options.principal]),
    systems: clone(shop.systems),
    sources: clone(shop.sources),
    fields: clone(shop.fields),
    links: clone(shop.links),
    entities: clone(shop.entities),
    audit: clone(shop.auditEntries),
    nextAuditId: shop.auditEntries.length + 1,
  };
  if (options.corpus === 'xss') {
    db.systems.push(clone(xss.xssSystem));
    db.sources.push(clone(xss.xssSource));
    db.fields.push(...clone(xss.xssFields));
    db.links.push(...clone(xss.xssLinks));
    db.entities.push(clone(xss.xssEntity));
    db.audit.push(...clone(xss.xssAudit));
    db.nextAuditId = db.audit.length + 1;
  }
  return db;
}

export function appendAudit(
  db: MockDb,
  action: string,
  target: string,
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
  reason = '',
): void {
  const id = db.nextAuditId;
  db.nextAuditId += 1;
  db.audit.push({
    id,
    at: new Date().toISOString(),
    actor: db.me.subject,
    action,
    target,
    before,
    after,
    reason,
    request_id: `req_${String(id).padStart(6, '0')}`,
    row_hash: `${'f'.repeat(56)}${String(id).padStart(8, '0')}`,
  });
}
