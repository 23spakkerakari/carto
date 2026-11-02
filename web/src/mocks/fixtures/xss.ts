/* The XSS corpus (threat model row 4): a sixth, hostile system whose every customer-controlled
 * string carries a payload. Merged into the mock when the page is opened with `?corpus=xss`.
 * The Playwright suite asserts that nothing executes, nothing loads from a canary host, every
 * string appears in the DOM as text, and no element gains an unexpected href or src. */
import type {
  AuditEntryView,
  EntityView,
  FieldView,
  LinkView,
  SourceView,
  SystemView,
} from '../../api/types';
import { auditRow, days, end, evidence, hours, iso, type LinkSeed, materialise } from './shop';

export const CANARY = 'http://canary.invalid';

/** Every payload the corpus plants, so the test can look for each one as text. */
export const PAYLOADS = {
  script: '<script>window.__xss_executed = true</script>',
  imgOnerror: `<img src="${CANARY}/pixel.gif" onerror="window.__xss_executed = true">`,
  svgOnload: '"><svg onload="window.__xss_executed = true">',
  jsUrl: 'javascript:window.__xss_executed = true',
  mdLink: `[claim your refund](${CANARY}/phish)`,
  template: '{{ constructor.constructor("window.__xss_executed = true")() }}',
  entity: '&lt;b&gt;bold&lt;/b&gt; &amp;amp; &#x3C;i&#x3E;',
  rtl: 'invoice‮fdp.exe',
  iframe: `<iframe src="${CANARY}/frame"></iframe>`,
  anchor: `<a href="${CANARY}/x">x</a>`,
  cssUrl: `url(${CANARY}/bg.png)`,
  long: 'A'.repeat(1024),
  longPath: 'x'.repeat(256),
} as const;

export const xssSystem: SystemView = {
  id: 'hostile',
  name: PAYLOADS.imgOnerror,
  description: `${PAYLOADS.mdLink} ${PAYLOADS.template}`,
  owner_group: PAYLOADS.svgOnload,
  criticality: 'high',
  source_count: 1,
  updated_at: iso(-hours(1)),
  etag: 'W/"1"',
};

export const xssSource: SourceView = {
  id: 'hostile_logs',
  system_id: 'hostile',
  type: 'file_tail',
  enabled: true,
  config: {
    path: PAYLOADS.script,
    url: PAYLOADS.jsUrl,
    note: PAYLOADS.mdLink,
    template: PAYLOADS.template,
    entities: PAYLOADS.entity,
    rtl: PAYLOADS.rtl,
    iframe: PAYLOADS.iframe,
    anchor: PAYLOADS.anchor,
    css: PAYLOADS.cssUrl,
    long: PAYLOADS.long,
    nested: { deeper: PAYLOADS.imgOnerror, list: [PAYLOADS.svgOnload, 1, true, null] },
  },
  secret_ref: PAYLOADS.jsUrl,
  backfill_days: 7,
  schedule: PAYLOADS.template,
  health: {
    status: 'warn',
    last_success_at: iso(-hours(3)),
    lag_seconds: 10800,
    error_count: 1,
    buffer_depth: 0,
    oldest_buffered_at: null,
    message: PAYLOADS.imgOnerror,
    received_at: iso(-30),
  },
  last_test: PAYLOADS.anchor,
  updated_at: iso(-hours(1)),
};

const hostileTemplate = PAYLOADS.svgOnload;

export const xssFields: FieldView[] = [
  {
    id: `hostile/${hostileTemplate}/${PAYLOADS.script}`,
    system_id: 'hostile',
    template_id: hostileTemplate,
    path: PAYLOADS.script,
    field_class: 'identifier',
    policy: 'tokenize',
    pinned: false,
    count: 1200,
    distinct_estimate: 1190,
    null_rate: 0,
    shapes: [
      { shape: PAYLOADS.imgOnerror, share: 0.8 },
      { shape: PAYLOADS.entity, share: 0.2 },
    ],
    forms: ['exact', PAYLOADS.jsUrl],
    quality: 0.7,
    label: null,
    suggested_label: PAYLOADS.mdLink,
    first_seen: iso(-days(3)),
    last_seen: iso(-hours(1)),
  },
  {
    id: `hostile/${hostileTemplate}/${PAYLOADS.longPath}`,
    system_id: 'hostile',
    template_id: hostileTemplate,
    path: PAYLOADS.longPath,
    field_class: 'free_text',
    policy: 'drop',
    pinned: true,
    count: 1200,
    distinct_estimate: 1200,
    null_rate: 0.5,
    shapes: [{ shape: PAYLOADS.rtl, share: 1 }],
    forms: [],
    quality: null,
    label: PAYLOADS.template,
    suggested_label: null,
    first_seen: iso(-days(3)),
    last_seen: iso(-hours(1)),
  },
  {
    id: `hostile/${hostileTemplate}/${PAYLOADS.iframe}`,
    system_id: 'hostile',
    template_id: hostileTemplate,
    path: PAYLOADS.iframe,
    field_class: 'identifier',
    policy: 'tokenize',
    pinned: false,
    count: 900,
    distinct_estimate: 880,
    null_rate: 0,
    shapes: [{ shape: PAYLOADS.anchor, share: 1 }],
    forms: ['exact'],
    quality: 0.6,
    label: null,
    suggested_label: null,
    first_seen: iso(-days(3)),
    last_seen: iso(-hours(1)),
  },
];

const pool = [...xssFields];
const scriptField = xssFields[0]?.id ?? '';
const iframeField = xssFields[2]?.id ?? '';

const seeds: LinkSeed[] = [
  {
    id: 'lnk_xss_01',
    a: end(scriptField, 'exact', pool),
    b: end(iframeField, 'exact', pool),
    type: 'exact',
    role: 'transaction',
    direction: 'a_to_b',
    score: 0.995,
    status: 'proposed',
    volume: 99999,
    sentence: `${PAYLOADS.script} ${PAYLOADS.mdLink} ${PAYLOADS.template} ${PAYLOADS.entity} ${PAYLOADS.rtl} ${PAYLOADS.long}`,
    evidence: evidence({
      n_a: 1190,
      n_b: 880,
      n_match: 870,
      c_ab: 0.73,
      c_ba: 0.99,
      lag_median_seconds: 60,
      lag_iqr_seconds: 10,
      lag_positive_share: 0.9,
      fanout_ab_median: 1,
      fanout_ab_p99: 1,
      fanout_ba_median: 1,
      fanout_ba_p99: 1,
      concentration: 0.01,
      chance_ratio: 0.01,
      domain_size: 1e4,
    }),
    entity: 'ent_hostile',
  },
  {
    id: 'lnk_xss_02',
    a: end(scriptField, PAYLOADS.jsUrl, pool),
    b: end(iframeField, 'exact', pool),
    type: 'composite',
    role: 'transaction',
    direction: 'undirected',
    score: 0.99,
    status: 'proposed',
    volume: 99998,
    sentence: `${PAYLOADS.imgOnerror} ${PAYLOADS.svgOnload} ${PAYLOADS.anchor} ${PAYLOADS.cssUrl}`,
    evidence: evidence({
      n_a: 100,
      n_b: 100,
      n_match: 90,
      c_ab: 0.9,
      c_ba: 0.9,
      components: [
        {
          a: end(scriptField, PAYLOADS.script, pool),
          b: end(iframeField, PAYLOADS.iframe, pool),
          weight: 2,
          m: 0.9,
          u: 0.1,
        },
      ],
    }),
    entity: null,
  },
];

export const xssLinks: LinkView[] = materialise(seeds, 100);

export const xssEntity: EntityView = {
  id: 'ent_hostile',
  name: PAYLOADS.anchor,
  suggested_name: PAYLOADS.script,
  name_confirmed: false,
  description: PAYLOADS.mdLink,
  fields: xssFields.map((f) => ({
    field_id: f.id,
    form: 'exact',
    system_id: f.system_id,
    path: f.path,
  })),
  link_ids: ['lnk_xss_01'],
  system_count: 1,
  updated_at: iso(-hours(1)),
  etag: 'W/"1"',
};

export const xssAudit: AuditEntryView[] = [
  auditRow(
    -hours(1),
    PAYLOADS.imgOnerror,
    'entity.rename',
    `entity:${PAYLOADS.script}`,
    { name: PAYLOADS.anchor, note: PAYLOADS.iframe },
    { name: PAYLOADS.template, note: PAYLOADS.jsUrl },
    PAYLOADS.mdLink,
  ),
];
