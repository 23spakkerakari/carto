/* The shop scenario (spec 19, scenario A) as the console sees it: five systems, seven sources
 * with health, thirty fields with shapes, fourteen links of every type and status, three
 * entities, an audit page and the /me of each role. Nothing here is a value or a token: shapes,
 * counts, shares and sentences only (spec 2.3 invariant 2). All strings are the simulator's
 * own; none is real customer data. */
import type {
  AuditEntryView,
  EntityView,
  EvidenceView,
  FieldView,
  LinkEnd,
  LinkView,
  MeView,
  SourceView,
  SystemView,
} from '../../api/types';

const T0 = Date.parse('2026-10-18T06:00:00Z');
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString();
const hours = (h: number) => h * 3600;
const days = (d: number) => d * 86400;

export const WINDOW_START = iso(-days(7));
export const WINDOW_END = iso(0);

export type Principal = 'integration_engineer' | 'admin' | 'auditor' | 'business_viewer';

export const me: Record<Principal, MeView> = {
  integration_engineer: {
    subject: 'oidc|7f3a2c',
    display_name: 'Mira Okafor',
    roles: ['integration_engineer'],
    permissions: ['view', 'review_links', 'manage_expectations', 'ack_alerts'],
    csrf_token: 'csrf-7f3a2c-5d1e9b0a4c8f2e61',
    session_expires_at: iso(hours(11)),
  },
  admin: {
    subject: 'oidc|a1b2c3',
    display_name: 'Tomas Reyes',
    roles: ['admin'],
    permissions: [
      'view',
      'review_links',
      'manage_expectations',
      'ack_alerts',
      'manage_sources',
      'manage_admin',
      'view_audit',
    ],
    csrf_token: 'csrf-a1b2c3-9e8d7c6b5a4f3e21',
    session_expires_at: iso(hours(11)),
  },
  auditor: {
    subject: 'oidc|d4e5f6',
    display_name: 'Priya Natarajan',
    roles: ['auditor'],
    permissions: ['view', 'view_audit'],
    csrf_token: 'csrf-d4e5f6-1f2e3d4c5b6a7980',
    session_expires_at: iso(hours(11)),
  },
  business_viewer: {
    subject: 'oidc|0a9b8c',
    display_name: 'Jonas Lindqvist',
    roles: ['business_viewer'],
    permissions: ['view', 'ack_alerts'],
    csrf_token: 'csrf-0a9b8c-aa11bb22cc33dd44',
    session_expires_at: iso(hours(11)),
  },
};

export const systems: SystemView[] = [
  {
    id: 'webstore',
    name: 'Webstore',
    description: 'Customer-facing shop; carts become orders at checkout.',
    owner_group: 'digital-commerce',
    criticality: 'high',
    source_count: 2,
    updated_at: iso(-hours(2)),
    etag: 'W/"3"',
  },
  {
    id: 'orders',
    name: 'Order system',
    description: 'Order management; the system of record for orders.',
    owner_group: 'order-ops',
    criticality: 'high',
    source_count: 2,
    updated_at: iso(-hours(2)),
    etag: 'W/"5"',
  },
  {
    id: 'payments',
    name: 'Payments',
    description: 'Payment service provider gateway; settlement files nightly.',
    owner_group: 'finance-systems',
    criticality: 'high',
    source_count: 1,
    updated_at: iso(-days(1)),
    etag: 'W/"2"',
  },
  {
    id: 'warehouse',
    name: 'Warehouse',
    description: 'Warehouse management; purchase orders keyed by hand from the order export.',
    owner_group: 'fulfilment',
    criticality: 'medium',
    source_count: 1,
    updated_at: iso(-days(1)),
    etag: 'W/"1"',
  },
  {
    id: 'shipping',
    name: 'Shipping',
    description: 'Carrier integration; manifests and tracking events.',
    owner_group: 'fulfilment',
    criticality: 'medium',
    source_count: 1,
    updated_at: iso(-days(3)),
    etag: 'W/"1"',
  },
];

function health(
  status: 'ok' | 'warn' | 'bad' | 'idle',
  lag: number,
  message = '',
  errors = 0,
): SourceView['health'] {
  return {
    status,
    last_success_at: status === 'idle' ? iso(-days(2)) : iso(-lag),
    lag_seconds: lag,
    error_count: errors,
    buffer_depth: status === 'bad' ? 4120 : 0,
    oldest_buffered_at: status === 'bad' ? iso(-hours(3)) : null,
    message,
    received_at: iso(-45),
  };
}

export const sources: SourceView[] = [
  {
    id: 'webstore_app_logs',
    system_id: 'webstore',
    type: 'file_tail',
    enabled: true,
    config: {
      path: '/var/log/webstore/app-*.log',
      format: 'json',
      multiline: false,
      encoding: 'utf-8',
    },
    secret_ref: null,
    backfill_days: 14,
    schedule: null,
    health: health('ok', 12),
    last_test: 'Read 2,048 bytes from 3 files; JSON parsed; 0 errors.',
    updated_at: iso(-hours(2)),
  },
  {
    id: 'webstore_otlp',
    system_id: 'webstore',
    type: 'otlp',
    enabled: true,
    config: { listener: 'edge-gateway:4318', protocol: 'http/protobuf' },
    secret_ref: null,
    backfill_days: null,
    schedule: null,
    health: health('ok', 4),
    last_test: 'Listener up; last batch 38 records.',
    updated_at: iso(-hours(2)),
  },
  {
    id: 'orders_db',
    system_id: 'orders',
    type: 'postgres',
    enabled: true,
    config: {
      host: 'orders-db.internal',
      port: 5432,
      database: 'orders',
      user: 'carto_ro',
      query:
        'SELECT order_id, merchant_ref, cart_id, created_at FROM orders WHERE created_at > :cursor',
      cursor_column: 'created_at',
      ssl_mode: 'verify-full',
    },
    secret_ref: 'vault:kv/carto/orders-db',
    backfill_days: 30,
    schedule: 'every 5 minutes',
    health: health('warn', 1860, 'Cursor 31 min behind; last poll took 48 s.'),
    last_test:
      'Read-only session confirmed (default_transaction_read_only=on); SELECT returned 10 rows.',
    updated_at: iso(-hours(2)),
  },
  {
    id: 'orders_app_logs',
    system_id: 'orders',
    type: 'syslog',
    enabled: true,
    config: { listener: 'edge-gateway:6514', protocol: 'tls', format: 'logfmt' },
    secret_ref: null,
    backfill_days: null,
    schedule: null,
    health: health('ok', 9),
    last_test: 'Listener up; TLS handshake ok.',
    updated_at: iso(-hours(2)),
  },
  {
    id: 'payments_sftp',
    system_id: 'payments',
    type: 'sftp',
    enabled: true,
    config: {
      host: 'sftp.psp.example',
      port: 22,
      path: '/outbound/settlement-*.csv',
      host_key_fingerprint: 'SHA256:4b3f...',
      format: 'csv',
      delimiter: ';',
    },
    secret_ref: 'vault:kv/carto/psp-sftp',
    backfill_days: 60,
    schedule: 'daily at 02:30',
    health: health('bad', 93600, 'Authentication failed: permission denied (publickey).', 3),
    last_test: 'Host key matched; authentication failed (publickey).',
    updated_at: iso(-days(1)),
  },
  {
    id: 'warehouse_mssql',
    system_id: 'warehouse',
    type: 'mssql',
    enabled: true,
    config: {
      host: 'wms-sql.internal',
      port: 1433,
      database: 'WMS',
      query:
        'SELECT po_num, order_ref, created_by, created_at FROM dbo.PurchaseOrders WHERE created_at > :cursor',
      cursor_column: 'created_at',
      encrypt: true,
    },
    secret_ref: 'vault:kv/carto/wms-sql',
    backfill_days: 30,
    schedule: 'every 15 minutes',
    health: health('ok', 420),
    last_test: 'Read-only intent confirmed (ApplicationIntent=ReadOnly); SELECT returned 10 rows.',
    updated_at: iso(-days(1)),
  },
  {
    id: 'shipping_webhooks',
    system_id: 'shipping',
    type: 'webhook',
    enabled: false,
    config: { path: '/webhooks/shipping_webhooks', signature: 'hmac-sha256', provider: 'carrier' },
    secret_ref: 'vault:kv/carto/carrier-webhook',
    backfill_days: null,
    schedule: null,
    health: health('idle', 0, 'Disabled by the administrator on 2026-10-16.'),
    last_test: null,
    updated_at: iso(-days(3)),
  },
];

interface FieldSeed {
  system: string;
  template: string;
  path: string;
  cls: FieldView['field_class'];
  policy: FieldView['policy'];
  count: number;
  distinct: number;
  shapes: [string, number][];
  forms?: string[];
  quality?: number | null;
  label?: string | null;
  suggested?: string | null;
  pinned?: boolean;
  nullRate?: number;
}

const ID = ['exact', 'digits'];
const ORDER_SHAPE: [string, number][] = [['AA-9999999', 1]];
const CART_SHAPES: [string, number][] = [
  ['99-999', 0.97],
  ['9-999', 0.03],
];
const MREF_SHAPES: [string, number][] = [
  ['A9-9999', 0.99],
  ['A9-999', 0.01],
];
const AMOUNT_SHAPES: [string, number][] = [
  ['999.99', 0.9],
  ['9999.99', 0.1],
];

/* prettier-ignore */
const fieldSeeds: FieldSeed[] = [
  { system: 'webstore', template: 'ws.cart.created', path: 'cart_id', cls: 'identifier', policy: 'tokenize', count: 48210, distinct: 47990, shapes: CART_SHAPES, forms: ID, quality: 0.94, suggested: 'Cart' },
  { system: 'webstore', template: 'ws.cart.created', path: 'customer.email', cls: 'contact', policy: 'tokenize', count: 48210, distinct: 21040, shapes: [['a@a.a', 1]], forms: ['exact', 'email_local'], nullRate: 0.02 },
  { system: 'webstore', template: 'ws.cart.created', path: 'cart_total', cls: 'amount', policy: 'keep', count: 48210, distinct: 9400, shapes: AMOUNT_SHAPES, forms: ['amount'] },
  { system: 'webstore', template: 'ws.checkout.done', path: 'cart_id', cls: 'identifier', policy: 'tokenize', count: 31400, distinct: 31390, shapes: CART_SHAPES, forms: ID, quality: 0.95, suggested: 'Cart' },
  { system: 'webstore', template: 'ws.checkout.done', path: 'order_id', cls: 'identifier', policy: 'tokenize', count: 31400, distinct: 31380, shapes: ORDER_SHAPE, forms: ID, quality: 0.98, label: 'Order' },
  { system: 'webstore', template: 'ws.checkout.done', path: 'session_id', cls: 'identifier', policy: 'drop', count: 31400, distinct: 31400, shapes: [['xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 1]], forms: ['exact'], quality: 0.2, pinned: true },
  { system: 'webstore', template: 'ws.checkout.done', path: 'customer.name', cls: 'person_name', policy: 'tokenize', count: 31400, distinct: 19800, shapes: [['Aaaaa Aaaaaa', 0.86], ['Aaaaa A. Aaaaaa', 0.14]], forms: ['exact', 'phonetic.k'] },
  { system: 'orders', template: 'orders.row', path: 'order_id', cls: 'identifier', policy: 'tokenize', count: 30980, distinct: 30980, shapes: ORDER_SHAPE, forms: ID, quality: 0.99, label: 'Order' },
  { system: 'orders', template: 'orders.row', path: 'merchant_ref', cls: 'identifier', policy: 'tokenize', count: 30980, distinct: 30975, shapes: MREF_SHAPES, forms: ID, quality: 0.96, suggested: 'Merchant reference' },
  { system: 'orders', template: 'orders.row', path: 'cart_id', cls: 'identifier', policy: 'tokenize', count: 30980, distinct: 30970, shapes: CART_SHAPES, forms: ID, quality: 0.94, suggested: 'Cart' },
  { system: 'orders', template: 'orders.row', path: 'status', cls: 'low_card_attribute', policy: 'keep', count: 30980, distinct: 6, shapes: [['aaaaaaa', 1]] },
  { system: 'orders', template: 'orders.row', path: 'created_at', cls: 'timestamp', policy: 'keep', count: 30980, distinct: 30900, shapes: [['9999-99-99T99:99:99Z', 1]], forms: ['date'] },
  { system: 'orders', template: 'orders.app:order-created', path: 'order_id', cls: 'identifier', policy: 'tokenize', count: 30990, distinct: 30980, shapes: ORDER_SHAPE, forms: ID, quality: 0.99, label: 'Order' },
  { system: 'orders', template: 'orders.app:order-created', path: 'merchant_ref', cls: 'identifier', policy: 'tokenize', count: 30990, distinct: 30975, shapes: MREF_SHAPES, forms: ID, quality: 0.96, suggested: 'Merchant reference' },
  { system: 'orders', template: 'orders.app:order-created', path: 'amount', cls: 'amount', policy: 'keep', count: 30990, distinct: 9100, shapes: AMOUNT_SHAPES, forms: ['amount'] },
  { system: 'orders', template: 'orders.app:order-created', path: 'user', cls: 'person_name', policy: 'tokenize', count: 30990, distinct: 38, shapes: [['aaaaa.aaaaaa', 1]], forms: ['exact'] },
  { system: 'payments', template: 'psp.settlement.csv', path: 'merchantRef', cls: 'identifier', policy: 'tokenize', count: 29870, distinct: 29860, shapes: MREF_SHAPES, forms: ID, quality: 0.96, suggested: 'Merchant reference' },
  { system: 'payments', template: 'psp.settlement.csv', path: 'pspTxnId', cls: 'identifier', policy: 'tokenize', count: 29870, distinct: 29870, shapes: [['aaaa_99999999999999', 1]], forms: ['exact'], quality: 0.91 },
  { system: 'payments', template: 'psp.settlement.csv', path: 'amount', cls: 'amount', policy: 'keep', count: 29870, distinct: 9050, shapes: AMOUNT_SHAPES, forms: ['amount'] },
  { system: 'payments', template: 'psp.settlement.csv', path: 'cardLast4', cls: 'financial', policy: 'drop', count: 29870, distinct: 8900, shapes: [['9999', 1]], pinned: true },
  { system: 'payments', template: 'psp.settlement.csv', path: 'settledOn', cls: 'date', policy: 'keep', count: 29870, distinct: 7, shapes: [['9999-99-99', 1]], forms: ['date'] },
  { system: 'warehouse', template: 'wms.po.row', path: 'po_num', cls: 'identifier', policy: 'tokenize', count: 27410, distinct: 27410, shapes: [['PO-9999999', 1]], forms: ID, quality: 0.97, suggested: 'Purchase order' },
  { system: 'warehouse', template: 'wms.po.row', path: 'order_ref', cls: 'identifier', policy: 'tokenize', count: 27410, distinct: 27300, shapes: [['AA-9999999', 0.91], ['AA 9999999', 0.06], ['9999999', 0.03]], forms: ID, quality: 0.88, suggested: 'Order' },
  { system: 'warehouse', template: 'wms.po.row', path: 'created_by', cls: 'person_name', policy: 'tokenize', count: 27410, distinct: 9, shapes: [['aaaaaa', 1]], forms: ['exact', 'phonetic.k'] },
  { system: 'warehouse', template: 'wms.po.row', path: 'warehouse_code', cls: 'low_card_attribute', policy: 'keep', count: 27410, distinct: 3, shapes: [['AAA9', 1]] },
  { system: 'warehouse', template: 'wms.po.row', path: 'created_at', cls: 'timestamp', policy: 'keep', count: 27410, distinct: 27000, shapes: [['9999-99-99 99:99:99', 1]], forms: ['date'] },
  { system: 'shipping', template: 'carrier.manifest', path: 'po_num', cls: 'identifier', policy: 'tokenize', count: 25100, distinct: 25100, shapes: [['PO-9999999', 1]], forms: ID, quality: 0.97, suggested: 'Purchase order' },
  { system: 'shipping', template: 'carrier.manifest', path: 'tracking_no', cls: 'identifier', policy: 'tokenize', count: 25100, distinct: 25100, shapes: [['AA999999999AA', 1]], forms: ID, quality: 0.93 },
  { system: 'shipping', template: 'carrier.manifest', path: 'recipient.postcode', cls: 'contact', policy: 'tokenize', count: 25100, distinct: 7600, shapes: [['AA9 9AA', 0.7], ['AA99 9AA', 0.3]], forms: ['exact'] },
  { system: 'shipping', template: 'carrier.manifest', path: 'weight_kg', cls: 'amount', policy: 'keep', count: 25100, distinct: 620, shapes: [['9.99', 0.8], ['99.99', 0.2]], forms: ['amount'] },
];

export const fields: FieldView[] = fieldSeeds.map((seed) => ({
  id: `${seed.system}/${seed.template}/${seed.path}`,
  system_id: seed.system,
  template_id: seed.template,
  path: seed.path,
  field_class: seed.cls,
  policy: seed.policy,
  pinned: seed.pinned ?? false,
  count: seed.count,
  distinct_estimate: seed.distinct,
  null_rate: seed.nullRate ?? 0,
  shapes: seed.shapes.map(([shape, share]) => ({ shape, share })),
  forms: seed.forms ?? [],
  quality: seed.quality ?? null,
  label: seed.label ?? null,
  suggested_label: seed.suggested ?? null,
  first_seen: iso(-days(14)),
  last_seen: iso(-hours(1)),
}));

export function end(fieldId: string, form = 'exact', pool: FieldView[] = fields): LinkEnd {
  const field = pool.find((f) => f.id === fieldId);
  if (!field) throw new Error(`fixture field ${fieldId} missing`);
  return {
    field_id: field.id,
    system_id: field.system_id,
    template_id: field.template_id,
    path: field.path,
    form,
    label: field.label ?? field.suggested_label,
    top_shape: field.shapes[0]?.shape ?? null,
  };
}

const baseEvidence: EvidenceView = {
  n_a: 0,
  n_b: 0,
  n_match: 0,
  c_ab: 0,
  c_ba: 0,
  lag_median_seconds: null,
  lag_iqr_seconds: null,
  lag_positive_share: null,
  fanout_ab_median: null,
  fanout_ab_p99: null,
  fanout_ba_median: null,
  fanout_ba_p99: null,
  concentration: null,
  chance_ratio: null,
  domain_size: null,
  pairing_consistency: null,
  components: [],
  window_start: WINDOW_START,
  window_end: WINDOW_END,
};

export function evidence(partial: Partial<EvidenceView>): EvidenceView {
  return { ...baseEvidence, ...partial };
}

export interface LinkSeed {
  id: string;
  a: LinkEnd;
  b: LinkEnd;
  type: LinkView['link_type'];
  role: LinkView['role'];
  direction: LinkView['direction'];
  score: number;
  status: LinkView['status'];
  volume: number;
  sentence: string;
  evidence: EvidenceView;
  entity?: string | null;
  rejectReason?: LinkView['reject_reason'];
  decidedBy?: string | null;
  version?: number;
  ambiguous?: number;
}

/* prettier-ignore */
const linkSeeds: LinkSeed[] = [
  {
    id: 'lnk_01', a: end('orders/orders.row/merchant_ref'), b: end('payments/psp.settlement.csv/merchantRef'),
    type: 'exact', role: 'transaction', direction: 'a_to_b', score: 0.97, status: 'proposed', volume: 29610,
    sentence: '96% of Payments settlement rows match an order in the Order system, typically 1 day 2 hours later; the references look like A9-9999 on both sides.',
    evidence: evidence({ n_a: 30975, n_b: 29860, n_match: 29610, c_ab: 0.956, c_ba: 0.992, lag_median_seconds: 93600, lag_iqr_seconds: 14400, lag_positive_share: 0.99, fanout_ab_median: 1, fanout_ab_p99: 2, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.0004, chance_ratio: 0.0012, domain_size: 2.6e6 }),
    entity: 'ent_order',
  },
  {
    id: 'lnk_02', a: end('orders/orders.row/order_id'), b: end('warehouse/wms.po.row/order_ref', 'digits'),
    type: 'exact', role: 'transaction', direction: 'a_to_b', score: 0.91, status: 'proposed', volume: 24830,
    sentence: '92% of Warehouse purchase orders match an order within 2 hours; IDs look like AA-9999999 on both sides, with 6% written with a space instead of a hyphen; the match uses the digits inside the order reference.',
    evidence: evidence({ n_a: 30980, n_b: 27300, n_match: 24830, c_ab: 0.8, c_ba: 0.91, lag_median_seconds: 7320, lag_iqr_seconds: 5400, lag_positive_share: 0.97, fanout_ab_median: 1, fanout_ab_p99: 3, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.0005, chance_ratio: 0.0001, domain_size: 6.76e9 }),
    entity: 'ent_order',
  },
  {
    id: 'lnk_03', a: end('orders/orders.row/cart_id'), b: end('orders/orders.row/order_id'),
    type: 'bridge', role: 'transaction', direction: 'undirected', score: 0.95, status: 'proposed', volume: 30970,
    sentence: 'In the Order system every order row carries both a cart id (99-999) and an order id (AA-9999999); 99.8% of cart ids always appear with the same order id.',
    evidence: evidence({ n_a: 30970, n_b: 30980, n_match: 30970, c_ab: 1, c_ba: 0.9997, lag_median_seconds: 0, lag_iqr_seconds: 0, lag_positive_share: 0.5, fanout_ab_median: 1, fanout_ab_p99: 1, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.0003, chance_ratio: 0, domain_size: 1e5, pairing_consistency: 0.998 }),
    entity: 'ent_order',
  },
  {
    id: 'lnk_04', a: end('warehouse/wms.po.row/po_num'), b: end('shipping/carrier.manifest/po_num'),
    type: 'exact', role: 'transaction', direction: 'a_to_b', score: 0.93, status: 'proposed', volume: 24900,
    sentence: '99% of Shipping manifests match a Warehouse purchase order, typically 1 day 5 hours later; purchase order numbers look like PO-9999999 on both sides.',
    evidence: evidence({ n_a: 27410, n_b: 25100, n_match: 24900, c_ab: 0.908, c_ba: 0.992, lag_median_seconds: 104400, lag_iqr_seconds: 28800, lag_positive_share: 0.995, fanout_ab_median: 1, fanout_ab_p99: 4, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.0004, chance_ratio: 0.0001, domain_size: 1e7 }),
    entity: 'ent_po',
  },
  {
    id: 'lnk_05', a: end('webstore/ws.cart.created/cart_id'), b: end('orders/orders.row/cart_id'),
    type: 'exact', role: 'transaction', direction: 'a_to_b', score: 0.89, status: 'proposed', volume: 30900,
    sentence: '64% of Webstore carts become an order in the Order system, typically 23 minutes later; cart ids look like 99-999 on both sides. Abandoned carts explain the rest.',
    evidence: evidence({ n_a: 47990, n_b: 30970, n_match: 30900, c_ab: 0.644, c_ba: 0.998, lag_median_seconds: 1380, lag_iqr_seconds: 2100, lag_positive_share: 0.98, fanout_ab_median: 1, fanout_ab_p99: 2, fanout_ba_median: 1, fanout_ba_p99: 2, concentration: 0.0003, chance_ratio: 0.14, domain_size: 1e5 }),
    entity: 'ent_order',
  },
  {
    id: 'lnk_06', a: end('orders/orders.app:order-created/amount', 'amount'), b: end('payments/psp.settlement.csv/amount', 'amount'),
    type: 'composite', role: 'transaction', direction: 'a_to_b', score: 0.71, status: 'proposed', volume: 2140,
    sentence: 'For 2,140 orders without a merchant reference, the amount and the settlement date together match one Payments row, typically 1 day later; the combination is right about 9 times in 10.',
    evidence: evidence({
      n_a: 2300, n_b: 2410, n_match: 2140, c_ab: 0.93, c_ba: 0.888, lag_median_seconds: 86400, lag_iqr_seconds: 21600, lag_positive_share: 0.96, fanout_ab_median: 1, fanout_ab_p99: 2, fanout_ba_median: 1, fanout_ba_p99: 2, concentration: 0.01, chance_ratio: 0.08, domain_size: 9400,
      components: [
        { a: end('orders/orders.app:order-created/amount', 'amount'), b: end('payments/psp.settlement.csv/amount', 'amount'), weight: 4.1, m: 0.97, u: 0.0012 },
        { a: end('orders/orders.row/created_at', 'date'), b: end('payments/psp.settlement.csv/settledOn', 'date'), weight: 1.9, m: 0.94, u: 0.14 },
      ],
    }),
    entity: null,
  },
  {
    id: 'lnk_07', a: end('webstore/ws.checkout.done/customer.name', 'phonetic.k'), b: end('warehouse/wms.po.row/created_by', 'phonetic.k'),
    type: 'composite', role: 'association', direction: 'undirected', score: 0.52, status: 'proposed', volume: 410,
    sentence: '410 Webstore customer names sound like a Warehouse clerk name; nine clerks key every purchase order, so this is most likely a coincidence.',
    evidence: evidence({
      n_a: 19800, n_b: 9, n_match: 9, c_ab: 0.0005, c_ba: 1, fanout_ab_median: 46, fanout_ab_p99: 410, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 1, chance_ratio: 0.29, domain_size: 2.6e4,
      components: [
        { a: end('webstore/ws.checkout.done/customer.name', 'phonetic.k'), b: end('warehouse/wms.po.row/created_by', 'phonetic.k'), weight: 0.8, m: 0.6, u: 0.21 },
      ],
    }),
    entity: null,
  },
  {
    id: 'lnk_08', a: end('webstore/ws.checkout.done/order_id'), b: end('orders/orders.app:order-created/order_id'),
    type: 'exact', role: 'transaction', direction: 'a_to_b', score: 0.99, status: 'accepted', volume: 31370,
    sentence: '99.9% of Webstore checkouts match an order-created log line in the Order system within 2 seconds; order ids look like AA-9999999 on both sides.',
    evidence: evidence({ n_a: 31380, n_b: 30980, n_match: 30970, c_ab: 0.987, c_ba: 0.9997, lag_median_seconds: 2, lag_iqr_seconds: 1, lag_positive_share: 0.999, fanout_ab_median: 1, fanout_ab_p99: 1, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.0003, chance_ratio: 0, domain_size: 6.76e9 }),
    entity: 'ent_order', decidedBy: 'oidc|7f3a2c', version: 2,
  },
  {
    id: 'lnk_09', a: end('orders/orders.row/order_id'), b: end('orders/orders.row/merchant_ref'),
    type: 'bridge', role: 'transaction', direction: 'undirected', score: 0.96, status: 'accepted', volume: 30975,
    sentence: 'In the Order system every order row carries both an order id (AA-9999999) and a merchant reference (A9-9999); 99.9% of order ids always appear with the same merchant reference.',
    evidence: evidence({ n_a: 30980, n_b: 30975, n_match: 30975, c_ab: 0.9998, c_ba: 1, lag_median_seconds: 0, lag_iqr_seconds: 0, lag_positive_share: 0.5, fanout_ab_median: 1, fanout_ab_p99: 1, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.0003, chance_ratio: 0, domain_size: 6.76e9, pairing_consistency: 0.999 }),
    entity: 'ent_order', decidedBy: 'oidc|7f3a2c', version: 2,
  },
  {
    id: 'lnk_10', a: end('webstore/ws.cart.created/customer.email', 'email_local'), b: end('orders/orders.app:order-created/user'),
    type: 'exact', role: 'association', direction: 'undirected', score: 0.61, status: 'rejected', volume: 120,
    sentence: '120 Webstore customer email names match an Order system user name; those users are 38 staff accounts, not customers.',
    evidence: evidence({ n_a: 21040, n_b: 38, n_match: 12, c_ab: 0.0006, c_ba: 0.32, fanout_ab_median: 3, fanout_ab_p99: 48, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 1, chance_ratio: 0.27, domain_size: 3.1e5 }),
    entity: null, rejectReason: 'different_meaning', decidedBy: 'oidc|7f3a2c', version: 2,
  },
  {
    id: 'lnk_11', a: end('webstore/ws.cart.created/cart_total', 'amount'), b: end('orders/orders.app:order-created/amount', 'amount'),
    type: 'exact', role: 'association', direction: 'a_to_b', score: 0.55, status: 'rejected', volume: 8900,
    sentence: '95% of cart totals equal some order amount; with 9,400 distinct amounts this is expected by chance.',
    evidence: evidence({ n_a: 9400, n_b: 9100, n_match: 8900, c_ab: 0.947, c_ba: 0.978, lag_median_seconds: 1500, lag_iqr_seconds: 40000, lag_positive_share: 0.62, fanout_ab_median: 3, fanout_ab_p99: 61, fanout_ba_median: 5, fanout_ba_p99: 70, concentration: 0.21, chance_ratio: 0.3, domain_size: 1e5 }),
    entity: null, rejectReason: 'coincidence', decidedBy: 'oidc|a1b2c3', version: 3,
  },
  {
    id: 'lnk_12', a: end('shipping/carrier.manifest/po_num'), b: end('warehouse/wms.po.row/po_num'),
    type: 'exact', role: 'transaction', direction: 'b_to_a', score: 0.9, status: 'rejected', volume: 24900,
    sentence: '99% of Shipping manifests match a Warehouse purchase order; the warehouse writes the number first, so the proposed order was reversed.',
    evidence: evidence({ n_a: 25100, n_b: 27410, n_match: 24900, c_ab: 0.992, c_ba: 0.908, lag_median_seconds: -104400, lag_iqr_seconds: 28800, lag_positive_share: 0.005, fanout_ab_median: 1, fanout_ab_p99: 1, fanout_ba_median: 1, fanout_ba_p99: 4, concentration: 0.0004, chance_ratio: 0.0001, domain_size: 1e7 }),
    entity: null, rejectReason: 'wrong_direction', decidedBy: 'oidc|7f3a2c', version: 2,
  },
  {
    id: 'lnk_13', a: end('orders/orders.row/merchant_ref'), b: end('payments/psp.settlement.csv/pspTxnId'),
    type: 'exact', role: 'transaction', direction: 'a_to_b', score: 0.88, status: 'stale', volume: 1200,
    sentence: 'Matches between Order system merchant references and Payments transaction ids fell from 96% to 4% this week against the 7-day baseline; the settlement file format may have changed.',
    evidence: evidence({ n_a: 30975, n_b: 29870, n_match: 1200, c_ab: 0.039, c_ba: 0.04, lag_median_seconds: 93600, lag_iqr_seconds: 14400, lag_positive_share: 0.99, fanout_ab_median: 1, fanout_ab_p99: 1, fanout_ba_median: 1, fanout_ba_p99: 1, concentration: 0.008, chance_ratio: 0.02, domain_size: 2.6e6 }),
    entity: 'ent_order', decidedBy: 'oidc|7f3a2c', version: 3,
  },
  {
    id: 'lnk_14', a: end('webstore/ws.checkout.done/cart_id'), b: end('webstore/ws.cart.created/cart_id'),
    type: 'bridge', role: 'transaction', direction: 'b_to_a', score: 0.98, status: 'auto_accepted', volume: 31390,
    sentence: 'Within the Webstore, every checkout carries the cart id of a cart created earlier (99-999); 100% of checkouts pair with exactly one cart.',
    evidence: evidence({ n_a: 31390, n_b: 47990, n_match: 31390, c_ab: 1, c_ba: 0.654, lag_median_seconds: -1380, lag_iqr_seconds: 2100, lag_positive_share: 0.01, fanout_ab_median: 1, fanout_ab_p99: 1, fanout_ba_median: 1, fanout_ba_p99: 2, concentration: 0.0003, chance_ratio: 0, domain_size: 1e5, pairing_consistency: 1 }),
    entity: 'ent_order', decidedBy: 'system:auto-accept', version: 2,
  },
];

export function materialise(seeds: LinkSeed[], offset = 0): LinkView[] {
  return seeds.map((seed, i) => {
    const index = i + offset;
    const version = seed.version ?? 1;
    const decided = seed.status !== 'proposed';
    return {
      id: seed.id,
      a: seed.a,
      b: seed.b,
      link_type: seed.type,
      role: seed.role,
      direction: seed.direction,
      score: seed.score,
      status: seed.status,
      affected_volume: seed.volume,
      sentence: seed.sentence,
      evidence: seed.evidence,
      entity_id: seed.entity ?? null,
      reject_reason: seed.rejectReason ?? null,
      decided_by: decided ? (seed.decidedBy ?? null) : null,
      decided_at: decided ? iso(-days(1) - hours(index)) : null,
      ambiguous_merges: seed.ambiguous ?? 0,
      version,
      first_proposed_at: iso(-days(5) - hours(index)),
      updated_at: iso(-hours(index + 1)),
      etag: `W/"${version}"`,
    };
  });
}

export const links: LinkView[] = materialise(linkSeeds);

const member = (fieldId: string, form = 'exact') => {
  const e = end(fieldId, form);
  return { field_id: e.field_id, form, system_id: e.system_id, path: e.path };
};

export const entities: EntityView[] = [
  {
    id: 'ent_order',
    name: 'Order',
    suggested_name: 'Order',
    name_confirmed: true,
    description: 'A customer order from cart to settlement.',
    fields: [
      member('webstore/ws.cart.created/cart_id'),
      member('webstore/ws.checkout.done/cart_id'),
      member('webstore/ws.checkout.done/order_id'),
      member('orders/orders.row/order_id'),
      member('orders/orders.row/cart_id'),
      member('orders/orders.row/merchant_ref'),
      member('orders/orders.app:order-created/order_id'),
      member('payments/psp.settlement.csv/merchantRef'),
      member('warehouse/wms.po.row/order_ref'),
    ],
    link_ids: ['lnk_08', 'lnk_09', 'lnk_14'],
    system_count: 4,
    updated_at: iso(-days(1)),
    etag: 'W/"4"',
  },
  {
    id: 'ent_po',
    name: 'Purchase order',
    suggested_name: 'Purchase order',
    name_confirmed: false,
    description: '',
    fields: [member('warehouse/wms.po.row/po_num'), member('shipping/carrier.manifest/po_num')],
    link_ids: [],
    system_count: 2,
    updated_at: iso(-hours(6)),
    etag: 'W/"1"',
  },
  {
    id: 'ent_shipment',
    name: 'Shipment',
    suggested_name: 'Shipment',
    name_confirmed: false,
    description: '',
    fields: [member('shipping/carrier.manifest/tracking_no')],
    link_ids: [],
    system_count: 1,
    updated_at: iso(-hours(6)),
    etag: 'W/"1"',
  },
];

const HASH = '0'.repeat(56);
let auditSeq = 0;
export function auditRow(
  offset: number,
  actor: string,
  action: string,
  target: string,
  before: Record<string, unknown> | null,
  after: Record<string, unknown> | null,
  reason = '',
): AuditEntryView {
  auditSeq += 1;
  return {
    id: auditSeq,
    at: iso(offset),
    actor,
    action,
    target,
    before,
    after,
    reason,
    request_id: `req_${String(auditSeq).padStart(6, '0')}`,
    row_hash: `${HASH}${String(auditSeq).padStart(8, '0')}`,
  };
}

/* prettier-ignore */
export const auditEntries: AuditEntryView[] = [
  auditRow(-days(6), 'system:migrate', 'schema.migrate', 'postgres:0004', null, { revision: '0004' }),
  auditRow(-days(5), 'edge:edge-01', 'inventory.replace', 'edge:edge-01', null, { systems: 5, sources: 7, pins: 2 }),
  auditRow(-days(4), 'oidc|a1b2c3', 'session.login', 'user:oidc|a1b2c3', null, { roles: ['admin'] }),
  auditRow(-days(4) + 600, 'oidc|a1b2c3', 'role_binding.create', 'group:integration-engineers', null, { role: 'integration_engineer' }),
  auditRow(-days(3), 'oidc|7f3a2c', 'session.login', 'user:oidc|7f3a2c', null, { roles: ['integration_engineer'] }),
  auditRow(-days(3) + 300, 'oidc|7f3a2c', 'link.accept', 'link:lnk_08', { status: 'proposed', version: 1 }, { status: 'accepted', version: 2 }),
  auditRow(-days(3) + 420, 'oidc|7f3a2c', 'link.accept', 'link:lnk_09', { status: 'proposed', version: 1 }, { status: 'accepted', version: 2 }),
  auditRow(-days(3) + 900, 'oidc|7f3a2c', 'entity.rename', 'entity:ent_order', { name: 'Order', name_confirmed: false }, { name: 'Order', name_confirmed: true }),
  auditRow(-days(2), 'oidc|7f3a2c', 'link.reject', 'link:lnk_10', { status: 'proposed', version: 1 }, { status: 'rejected', version: 2, reject_reason: 'different_meaning' }, 'Staff accounts, not customers.'),
  auditRow(-days(2) + 120, 'oidc|a1b2c3', 'link.reject', 'link:lnk_11', { status: 'proposed', version: 2 }, { status: 'rejected', version: 3, reject_reason: 'coincidence' }),
  auditRow(-days(2) + 240, 'oidc|7f3a2c', 'link.reject', 'link:lnk_12', { status: 'proposed', version: 1 }, { status: 'rejected', version: 2, reject_reason: 'wrong_direction' }, 'The warehouse writes the number first.'),
  auditRow(-days(1) - hours(2), 'system:linker', 'link.auto_accept', 'link:lnk_14', { status: 'proposed', version: 1 }, { status: 'auto_accepted', version: 2 }),
  auditRow(-days(1), 'system:linker', 'link.stale', 'link:lnk_13', { status: 'accepted', version: 2 }, { status: 'stale', version: 3 }),
  auditRow(-hours(20), 'oidc|a1b2c3', 'settings.update', 'settings:auto_accept', { enabled: false }, { enabled: true, min_score: 0.95 }),
  auditRow(-hours(6), 'edge:edge-01', 'inventory.replace', 'edge:edge-01', { sources: 7 }, { sources: 7, changed: ['shipping_webhooks'] }),
  auditRow(-hours(2), 'oidc|7f3a2c', 'session.login', 'user:oidc|7f3a2c', null, { roles: ['integration_engineer'] }),
];

export { iso, hours, days };
