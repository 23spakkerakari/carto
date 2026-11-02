/* Compile-time check that the hand-written types in ./types match the schemas generated from
 * the committed OpenAPI document (`npm run gen:api` writes ./generated.d.ts). Each pair must be
 * assignable both ways; a drift fails `npm run typecheck`. Nothing here is emitted. */
import type { components } from './generated';
import type * as T from './types';

type S = components['schemas'];

type Same<A, B> = [A] extends [B] ? ([B] extends [A] ? true : false) : false;

// Defaulted fields are optional in the generated schemas (pydantic defaults) but the API always
// serialises them, so the hand-written views declare them required; `Deep` on the generated
// side expresses that at every level (link ends inside evidence, health inside a source).
type Deep<X> = X extends (infer U)[]
  ? Deep<U>[]
  : X extends object
    ? { [K in keyof X]-?: Deep<X[K]> }
    : X;
type Checks = [
  Same<T.MeView, Deep<S['MeView']>>,
  Same<T.SystemView, Deep<S['SystemView']>>,
  Same<T.SourceHealthView, Deep<S['SourceHealthView']>>,
  Same<T.SourceView, Deep<S['SourceView']>>,
  Same<T.FieldView, Deep<S['FieldView']>>,
  Same<T.LinkEnd, Deep<S['LinkEnd']>>,
  Same<T.CompositeComponentView, Deep<S['CompositeComponentView']>>,
  Same<T.EvidenceView, Deep<S['EvidenceView']>>,
  Same<T.LinkView, Deep<S['LinkView']>>,
  Same<T.EntityFieldView, Deep<S['EntityFieldView']>>,
  Same<T.EntityView, Deep<S['EntityView']>>,
  Same<T.AuditEntryView, Deep<S['AuditEntryView']>>,
  Same<T.DecisionRequest, S['DecisionRequest']>,
  Same<T.EntityPatch, S['EntityPatch']>,
  Same<T.SystemPatch, S['SystemPatch']>,
  Same<T.LinkType, S['LinkType']>,
  Same<T.LinkRole, S['LinkRole']>,
  Same<T.Direction, S['Direction']>,
  Same<T.LinkStatus, S['LinkStatus']>,
  Same<T.RejectReason, S['RejectReason']>,
  Same<T.Role, S['Role']>,
  Same<T.Permission, S['Permission']>,
];

export type ContractHolds = Checks extends true[] ? true : never;
export const contractHolds: ContractHolds = true;
