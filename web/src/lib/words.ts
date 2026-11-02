import type {
  Direction,
  FieldClassName,
  LinkRole,
  LinkStatus,
  LinkType,
  Permission,
  PolicyName,
  RejectReason,
  Role,
} from '../api/types';

/** Business-language copy for the vocabularies of carto_schema.api (spec 13). Technical names
 * stay available behind "Details". */

export const LINK_TYPE_WORDS: Record<LinkType, string> = {
  exact: 'same identifier',
  bridge: 'paired in one system',
  composite: 'matched on several weak fields',
};

export const LINK_ROLE_WORDS: Record<LinkRole, string> = {
  transaction: 'one record to one record',
  association: 'one to many (association)',
  batch: 'one file or batch to many records',
};

export const LINK_STATUS_WORDS: Record<LinkStatus, string> = {
  proposed: 'Waiting for review',
  accepted: 'Confirmed',
  rejected: 'Rejected',
  auto_accepted: 'Confirmed automatically',
  stale: 'Gone stale',
};

export const LINK_STATUS_TAB: Record<LinkStatus, string> = {
  proposed: 'Proposed',
  accepted: 'Accepted',
  rejected: 'Rejected',
  stale: 'Stale',
  auto_accepted: 'Auto-accepted',
};

export const REJECT_REASON_WORDS: Record<RejectReason, string> = {
  different_meaning: 'Different meaning',
  coincidence: 'Coincidence',
  wrong_direction: 'Wrong direction',
};

export const REJECT_REASON_HELP: Record<RejectReason, string> = {
  different_meaning: 'The two fields identify different things.',
  coincidence: 'The values overlap by chance.',
  wrong_direction: 'They are related, but the order of events is the other way round.',
};

export const ROLE_WORDS: Record<Role, string> = {
  admin: 'Administrator',
  integration_engineer: 'Integration engineer',
  business_viewer: 'Business viewer',
  auditor: 'Auditor',
};

export const FIELD_CLASS_WORDS: Record<FieldClassName, string> = {
  identifier: 'identifier',
  low_card_attribute: 'attribute (few values)',
  timestamp: 'timestamp',
  amount: 'amount',
  date: 'date',
  person_name: 'person name',
  contact: 'contact detail',
  government_id: 'government id',
  financial: 'financial',
  health: 'health',
  free_text: 'free text',
  secret_like: 'looks like a secret',
  unknown: 'unknown',
};

export const POLICY_WORDS: Record<PolicyName, string> = {
  keep: 'kept in clear',
  tokenize: 'tokenized',
  drop: 'dropped',
};

export interface DirectionWords {
  glyph: '→' | '←' | '↔';
  text: string;
}

/** The arrow and the words for a direction, given the two system names. */
export function directionWords(
  direction: Direction,
  fromName: string,
  toName: string,
): DirectionWords {
  switch (direction) {
    case 'a_to_b':
      return { glyph: '→', text: `${fromName} first` };
    case 'b_to_a':
      return { glyph: '←', text: `${toName} first` };
    case 'undirected':
      return { glyph: '↔', text: 'no fixed order' };
  }
}

export function hasPermission(
  permissions: readonly Permission[] | undefined,
  needed: Permission,
): boolean {
  return permissions?.includes(needed) ?? false;
}
