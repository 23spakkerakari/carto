/**
 * Hand-written mirror of `packages/carto-schema/carto_schema/api.py` (field names, enums and
 * optionality exactly as the pydantic models). The committed OpenAPI document
 * (`core/openapi/openapi.v1.json`) replaces this file through `npm run gen:api` once package E
 * lands it; until then every change to api.py is mirrored here by hand.
 *
 * Every string in a view that comes from customer data (paths, template text, names, config
 * values, sentences) is untrusted and rendered as text only (spec 13).
 */

export const API_VERSION = 'v1';
export const MAX_PAGE_SIZE = 500;
export const MAX_NAME_LEN = 128;
export const MAX_DESCRIPTION_LEN = 1024;
export const MAX_REASON_LEN = 512;

/** ISO 8601 timestamp in UTC, as `UtcTimestamp` serialises. */
export type UtcTimestamp = string;

export type LinkType = 'exact' | 'bridge' | 'composite';
export type LinkRole = 'transaction' | 'association' | 'batch';
export type Direction = 'a_to_b' | 'b_to_a' | 'undirected';
export type LinkStatus = 'proposed' | 'accepted' | 'rejected' | 'auto_accepted' | 'stale';
export type RejectReason = 'different_meaning' | 'coincidence' | 'wrong_direction';
export type Role = 'admin' | 'integration_engineer' | 'business_viewer' | 'auditor';
export type Permission =
  | 'view'
  | 'review_links'
  | 'manage_expectations'
  | 'ack_alerts'
  | 'manage_sources'
  | 'manage_admin'
  | 'view_audit'
  | 'reveal';

export const LINK_STATUSES: readonly LinkStatus[] = [
  'proposed',
  'accepted',
  'rejected',
  'stale',
  'auto_accepted',
];
export const REJECT_REASONS: readonly RejectReason[] = [
  'different_meaning',
  'coincidence',
  'wrong_direction',
];

/** `carto_schema.bundle.FieldClassName`. */
export type FieldClassName =
  | 'identifier'
  | 'low_card_attribute'
  | 'timestamp'
  | 'amount'
  | 'date'
  | 'person_name'
  | 'contact'
  | 'government_id'
  | 'financial'
  | 'health'
  | 'free_text'
  | 'secret_like'
  | 'unknown';

/** `carto_schema.bundle.PolicyName`. */
export type PolicyName = 'keep' | 'tokenize' | 'drop';

/** `carto_schema.bundle.ShapeShare`. */
export interface ShapeShare {
  shape: string;
  share: number;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string | null;
}

export interface MeView {
  subject: string;
  display_name: string;
  roles: Role[];
  permissions: Permission[];
  csrf_token: string;
  session_expires_at: UtcTimestamp;
}

export interface SystemView {
  id: string;
  name: string;
  description: string;
  owner_group: string | null;
  criticality: string;
  source_count: number;
  updated_at: UtcTimestamp;
  etag: string;
}

export interface SystemPatch {
  name?: string | null;
  description?: string | null;
}

export interface SourceHealthView {
  status: string;
  last_success_at: UtcTimestamp | null;
  lag_seconds: number;
  error_count: number;
  buffer_depth: number;
  oldest_buffered_at: UtcTimestamp | null;
  message: string;
  received_at: UtcTimestamp | null;
}

export interface SourceView {
  id: string;
  system_id: string;
  type: string;
  enabled: boolean;
  config: Record<string, unknown>;
  secret_ref: string | null;
  backfill_days: number | null;
  schedule: string | null;
  health: SourceHealthView | null;
  last_test: string | null;
  updated_at: UtcTimestamp;
}

export interface FieldView {
  id: string;
  system_id: string;
  template_id: string;
  path: string;
  field_class: FieldClassName;
  policy: PolicyName;
  pinned: boolean;
  count: number;
  distinct_estimate: number;
  null_rate: number;
  shapes: ShapeShare[];
  forms: string[];
  quality: number | null;
  label: string | null;
  suggested_label: string | null;
  first_seen: UtcTimestamp | null;
  last_seen: UtcTimestamp | null;
}

export interface LinkEnd {
  field_id: string;
  system_id: string;
  template_id: string;
  path: string;
  form: string;
  label: string | null;
  top_shape: string | null;
}

export interface CompositeComponentView {
  a: LinkEnd;
  b: LinkEnd;
  weight: number;
  m: number;
  u: number;
}

export interface EvidenceView {
  n_a: number;
  n_b: number;
  n_match: number;
  c_ab: number;
  c_ba: number;
  lag_median_seconds: number | null;
  lag_iqr_seconds: number | null;
  lag_positive_share: number | null;
  fanout_ab_median: number | null;
  fanout_ab_p99: number | null;
  fanout_ba_median: number | null;
  fanout_ba_p99: number | null;
  concentration: number | null;
  chance_ratio: number | null;
  domain_size: number | null;
  pairing_consistency: number | null;
  components: CompositeComponentView[];
  window_start: UtcTimestamp | null;
  window_end: UtcTimestamp | null;
}

export interface LinkView {
  id: string;
  a: LinkEnd;
  b: LinkEnd;
  link_type: LinkType;
  role: LinkRole;
  direction: Direction;
  score: number;
  status: LinkStatus;
  affected_volume: number;
  sentence: string;
  evidence: EvidenceView;
  entity_id: string | null;
  reject_reason: RejectReason | null;
  decided_by: string | null;
  decided_at: UtcTimestamp | null;
  ambiguous_merges: number;
  version: number;
  first_proposed_at: UtcTimestamp;
  updated_at: UtcTimestamp;
  etag: string;
}

export interface DecisionRequest {
  reason?: RejectReason | null;
  note: string;
}

export type DecisionKind = 'accept' | 'reject' | 'association';

export interface EntityFieldView {
  field_id: string;
  form: string;
  system_id: string;
  path: string;
}

export interface EntityView {
  id: string;
  name: string;
  suggested_name: string | null;
  name_confirmed: boolean;
  description: string;
  fields: EntityFieldView[];
  link_ids: string[];
  system_count: number;
  updated_at: UtcTimestamp;
  etag: string;
}

export interface EntityPatch {
  name?: string | null;
  description?: string | null;
}

export interface AuditEntryView {
  id: number;
  at: UtcTimestamp;
  actor: string;
  action: string;
  target: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  reason: string;
  request_id: string | null;
  row_hash: string;
}
