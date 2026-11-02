import { useId, useMemo, useState, type SyntheticEvent } from 'react';

import { useEntities, useMe, usePatchEntity, useSystems } from '../../api/hooks';
import { MAX_NAME_LEN, type EntityView } from '../../api/types';
import { formatTime } from '../../lib/format';
import { hasPermission } from '../../lib/words';
import {
  Button,
  EmptyState,
  ErrorLine,
  LoadMore,
  Loading,
  Mono,
  Notice,
  PageHeader,
  Untrusted,
} from '../../ui/primitives';

/** Entity families (spec 9.6): the name stays "Suggested" until a reviewer confirms it. */
export function EntitiesPage() {
  const entities = useEntities();
  const systems = useSystems();
  const me = useMe();
  const canReview = hasPermission(me.data?.permissions, 'review_links');
  const items = useMemo(() => entities.data?.pages.flatMap((p) => p.items) ?? [], [entities.data]);
  const systemName = (id: string) => systems.data?.find((s) => s.id === id)?.name ?? id;

  return (
    <>
      <PageHeader
        title="Entities"
        kicker="Map"
        meta={[
          { label: 'Families', value: String(items.length), mono: true },
          {
            label: 'Confirmed',
            value: String(items.filter((e) => e.name_confirmed).length),
            mono: true,
          },
        ]}
      />
      {entities.isPending ? (
        <Loading what="Loading entities" />
      ) : entities.isError ? (
        <ErrorLine error={entities.error} />
      ) : items.length === 0 ? (
        <EmptyState>No entity families yet; accept links to form them.</EmptyState>
      ) : (
        <ul className="divide-y divide-hairline">
          {items.map((entity) => (
            <li key={entity.id} className="py-4">
              <EntityRow entity={entity} systemName={systemName} canReview={canReview} />
            </li>
          ))}
        </ul>
      )}
      <LoadMore
        hasMore={entities.hasNextPage}
        loading={entities.isFetchingNextPage}
        onClick={() => {
          void entities.fetchNextPage();
        }}
      />
    </>
  );
}

function EntityRow({
  entity,
  systemName,
  canReview,
}: {
  entity: EntityView;
  systemName: (id: string) => string;
  canReview: boolean;
}) {
  const groups = useMemo(() => {
    const bySystem = new Map<string, EntityView['fields']>();
    for (const field of entity.fields) {
      const list = bySystem.get(field.system_id) ?? [];
      list.push(field);
      bySystem.set(field.system_id, list);
    }
    return [...bySystem.entries()];
  }, [entity.fields]);

  return (
    <article
      aria-labelledby={`entity-${entity.id}`}
      data-testid="entity"
      data-entity-id={entity.id}
    >
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 id={`entity-${entity.id}`} className="font-cond text-[20px] leading-7 font-semibold">
          <Untrusted text={entity.name} />
        </h2>
        {entity.name_confirmed ? (
          <span className="label-cond bg-hi px-1">Confirmed</span>
        ) : (
          <span className="label-cond text-muted">Suggested</span>
        )}
        <span className="text-[13px] text-muted">
          {entity.system_count} {entity.system_count === 1 ? 'system' : 'systems'},{' '}
          {entity.fields.length} {entity.fields.length === 1 ? 'field' : 'fields'},{' '}
          {entity.link_ids.length} {entity.link_ids.length === 1 ? 'link' : 'links'}; updated{' '}
          {formatTime(entity.updated_at)}
        </span>
      </div>
      {entity.description ? (
        <p className="mt-1 text-[13px]">
          <Untrusted text={entity.description} />
        </p>
      ) : null}
      <div className="mt-3 grid gap-x-8 gap-y-2 md:grid-cols-2 xl:grid-cols-3">
        {groups.map(([systemId, fields]) => (
          <div key={systemId}>
            <div className="label-cond hairline pb-0.5">
              <Untrusted text={systemName(systemId)} />
            </div>
            <ul className="mt-1">
              {fields.map((field) => (
                <li key={`${field.field_id}:${field.form}`} className="py-0.5">
                  <Mono wrap>
                    <Untrusted text={field.path} />
                  </Mono>
                  {field.form !== 'exact' ? (
                    <span className="text-[12px] text-muted"> ({field.form})</span>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
      {canReview ? <NameForm entity={entity} /> : null}
    </article>
  );
}

function NameForm({ entity }: { entity: EntityView }) {
  const patch = usePatchEntity();
  const id = useId();
  const [name, setName] = useState(
    entity.name !== '' ? entity.name : (entity.suggested_name ?? ''),
  );
  const [description, setDescription] = useState(entity.description);
  const [touched, setTouched] = useState(false);
  const trimmed = name.trim();
  const invalid = touched && (trimmed.length === 0 || trimmed.length > MAX_NAME_LEN);
  const unchanged =
    trimmed === entity.name && description === entity.description && entity.name_confirmed;

  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    setTouched(true);
    if (trimmed.length === 0 || trimmed.length > MAX_NAME_LEN) return;
    patch.mutate({ entity, patch: { name: trimmed, description } });
  };

  return (
    <form onSubmit={submit} noValidate className="mt-3 flex flex-wrap items-end gap-3">
      <div>
        <label htmlFor={`${id}-name`} className="label-cond block">
          Name
        </label>
        <input
          id={`${id}-name`}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
          maxLength={MAX_NAME_LEN}
          aria-invalid={invalid}
          aria-describedby={invalid ? `${id}-error` : undefined}
          className="mt-1 h-8 w-64 rounded border border-ink px-2"
        />
      </div>
      <div className="grow">
        <label htmlFor={`${id}-desc`} className="label-cond block">
          Description{' '}
          <span className="font-normal normal-case tracking-normal text-muted">(optional)</span>
        </label>
        <input
          id={`${id}-desc`}
          value={description}
          onChange={(event) => {
            setDescription(event.target.value);
          }}
          maxLength={1024}
          className="mt-1 h-8 w-full max-w-xl rounded border border-ink px-2"
        />
      </div>
      <Button
        type="submit"
        variant={entity.name_confirmed ? 'default' : 'confirm'}
        disabled={patch.isPending || unchanged}
      >
        {patch.isPending ? 'Saving…' : entity.name_confirmed ? 'Rename' : 'Confirm name'}
      </Button>
      <div className="basis-full">
        {invalid ? <ErrorLine text={`The name must be 1 to ${MAX_NAME_LEN} characters.`} /> : null}
        <ErrorLine error={patch.error ?? undefined} />
        {patch.isSuccess ? <Notice>Saved.</Notice> : null}
      </div>
    </form>
  );
}
