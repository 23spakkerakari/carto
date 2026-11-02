/* The XSS corpus (threat model row 4): a sixth, hostile system whose every customer-controlled
 * string carries a payload. Merged into the mock when the page is opened with `?corpus=xss`.
 * The Playwright suite asserts that nothing executes, nothing loads from a canary host, every
 * string appears in the DOM as text, and no element gains an unexpected href or src.
 *
 * Every value is one the contract (carto_schema/api.py) can carry: the pattern-bound ids
 * (system_id, template_id) are valid ids, because the API would never serve anything else, and
 * each free-text field holds a payload within its maximum length (criticality and health
 * status 16, source type 32, display_name 128, subject and decided_by 256). The signed-in
 * principal, one decided link and the problem document of one mutation are hostile too. */
import type {
  AuditEntryView,
  EntityView,
  FieldView,
  LinkView,
  MeView,
  ProblemDetail,
  SourceView,
  SystemView,
} from '../../api/types';
import { auditRow, days, end, evidence, hours, iso, type LinkSeed, materialise } from './shop';

export const CANARY = 'http://canary.invalid';

/** Every payload the corpus plants, so the test can look for each one as text. */
export const PAYLOADS = {
  script: '<script>window.__xss_executed = true</script>',
  imgOnerror: `<img src="${CANARY}/pixel.gif" onerror="window.__xss_executed = true">`,
  /** A shape (64 characters at most). */
  imgShort: `<img src="${CANARY}/s.gif" onerror="x()">`,
  svgOnload: '"><svg onload="window.__xss_executed = true">',
  /** Criticality and health status (16 characters at most). */
  short: '<svg onload=x>',
  /** A source type (32 characters at most). */
  sourceType: '<img src=x onerror=x>',
  jsUrl: 'javascript:window.__xss_executed = true',
  mdLink: `[claim your refund](${CANARY}/phish)`,
  template: '{{ constructor.constructor("window.__xss_executed = true")() }}',
  entity: '&lt;b&gt;bold&lt;/b&gt; &amp;amp; &#x3C;i&#x3E;',
  rtl: 'invoice‮fdp.exe',
  iframe: `<iframe src="${CANARY}/frame"></iframe>`,
  anchor: `<a href="${CANARY}/x">x</a>`,
  cssUrl: `url(${CANARY}/bg.png)`,
  styleTag: `<style>body{background:url(${CANARY}/bg.png)}</style>`,
  displayName: '<b onmouseover="window.__xss_executed = true">Mira</b>',
  subject: 'oidc|<script>window.__xss_executed = true</script>',
  problemTitle: '<svg onload="window.__xss_executed = true">Name refused</svg>',
  problemDetail: `[fix it here](${CANARY}/fix) {{ constructor.constructor("window.__xss_executed = true")() }}`,
  long: 'A'.repeat(1024),
  longPath: 'x'.repeat(256),
} as const;

/** What `/me` serves under the corpus: the Shell renders the display name on every page, and
 * the subject becomes `decided_by` and the audit actor of anything decided in the session. */
export const xssMe: Pick<MeView, 'subject' | 'display_name'> = {
  subject: PAYLOADS.subject,
  display_name: PAYLOADS.displayName,
};

/** The RFC 7807 document the mock answers to a rename of the hostile entity: the screens show
 * `title: detail` in their error line, as text. */
export const xssProblem: ProblemDetail = {
  type: 'about:blank',
  title: PAYLOADS.problemTitle,
  status: 422,
  detail: PAYLOADS.problemDetail,
  instance: null,
};

export const xssSystem: SystemView = {
  id: 'hostile',
  name: PAYLOADS.imgOnerror,
  description: `${PAYLOADS.mdLink} ${PAYLOADS.template}`,
  owner_group: PAYLOADS.svgOnload,
  criticality: PAYLOADS.short,
  source_count: 1,
  updated_at: iso(-hours(1)),
  etag: 'W/"1"',
};

export const xssSource: SourceView = {
  id: 'hostile_logs',
  system_id: 'hostile',
  type: PAYLOADS.sourceType,
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
    // Keys are customer strings too (the connector's own configuration names).
    [PAYLOADS.styleTag]: 'a hostile key',
    [PAYLOADS.svgOnload]: 2,
  },
  secret_ref: PAYLOADS.jsUrl,
  backfill_days: 7,
  schedule: PAYLOADS.template,
  health: {
    status: PAYLOADS.short,
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

/** A valid template id (`^[A-Za-z0-9_.:-]{1,64}$`): the ids are pattern-bound in the contract,
 * so a payload there is one the API could never serve. */
const hostileTemplate = 'tpl.hostile:1';

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
      { shape: PAYLOADS.imgShort, share: 0.8 },
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
    shapes: [
      { shape: PAYLOADS.svgOnload, share: 0.6 },
      { shape: PAYLOADS.anchor, share: 0.4 },
    ],
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
const longField = xssFields[1]?.id ?? '';
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
  // A decided link: `decided_by` is a subject string the identity provider chose.
  {
    id: 'lnk_xss_03',
    a: end(scriptField, 'exact', pool),
    b: end(longField, 'exact', pool),
    type: 'bridge',
    role: 'association',
    direction: 'b_to_a',
    score: 0.9,
    status: 'accepted',
    volume: 500,
    sentence: `${PAYLOADS.subject} ${PAYLOADS.displayName}`,
    evidence: evidence({ n_a: 500, n_b: 500, n_match: 450, c_ab: 0.9, c_ba: 0.9 }),
    entity: 'ent_hostile',
    decidedBy: PAYLOADS.subject,
  },
];

export const xssLinks: LinkView[] = materialise(seeds, 100);

/** Unnamed: the suggestion is what the name form shows, so it carries the payload. */
export const xssEntity: EntityView = {
  id: 'ent_hostile',
  name: '',
  suggested_name: PAYLOADS.anchor,
  name_confirmed: false,
  description: PAYLOADS.mdLink,
  fields: xssFields.map((f) => ({
    field_id: f.id,
    form: 'exact',
    system_id: f.system_id,
    path: f.path,
  })),
  link_ids: ['lnk_xss_01', 'lnk_xss_03'],
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
