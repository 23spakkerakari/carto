import { useMemo } from 'react';
import { useSearchParams } from 'react-router';

import { useFields, useSystems } from '../../api/hooks';
import { formatInteger, formatPercent, formatTime } from '../../lib/format';
import { FIELD_CLASS_WORDS, POLICY_WORDS } from '../../lib/words';
import {
  EmptyState,
  ErrorLine,
  LoadMore,
  Loading,
  Mono,
  PageHeader,
  Untrusted,
} from '../../ui/primitives';

/** Setup > Field policies, read-only (ADR 0050): class, policy, pins, shapes, forms, quality.
 * Shapes, never values. */
export function FieldsPage() {
  const [params, setParams] = useSearchParams();
  const system = params.get('system');
  const systems = useSystems();
  const fields = useFields(system);
  const items = useMemo(() => fields.data?.pages.flatMap((p) => p.items) ?? [], [fields.data]);
  const systemName = (id: string) => systems.data?.find((s) => s.id === id)?.name ?? id;

  return (
    <>
      <PageHeader
        title="Field policies"
        kicker="Setup"
        meta={[
          { label: 'Fields loaded', value: String(items.length), mono: true },
          { label: 'Pinned', value: String(items.filter((f) => f.pinned).length), mono: true },
          { label: 'Values', value: 'never shown; shapes only' },
        ]}
        actions={
          <label className="flex items-center gap-2 text-[13px]">
            <span className="label-cond">System</span>
            <select
              value={system ?? ''}
              onChange={(event) => {
                setParams(event.target.value ? { system: event.target.value } : {}, {
                  replace: true,
                });
              }}
              className="h-8 rounded border border-ink bg-paper px-2"
            >
              <option value="">All systems</option>
              {(systems.data ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        }
      />
      {fields.isPending ? (
        <Loading what="Loading fields" />
      ) : fields.isError ? (
        <ErrorLine error={fields.error} />
      ) : items.length === 0 ? (
        <EmptyState>No fields have been profiled for this selection.</EmptyState>
      ) : (
        <table className="table-ruled">
          <thead>
            <tr>
              <th scope="col">System</th>
              <th scope="col">Template</th>
              <th scope="col">Field</th>
              <th scope="col">Class</th>
              <th scope="col">Policy</th>
              <th scope="col">Pinned</th>
              <th scope="col">Shapes</th>
              <th scope="col">Forms</th>
              <th scope="col" className="text-right">
                Quality
              </th>
              <th scope="col" className="text-right">
                Seen
              </th>
              <th scope="col" className="text-right">
                Distinct
              </th>
              <th scope="col" className="text-right">
                Empty
              </th>
              <th scope="col">Label</th>
              <th scope="col">Last seen</th>
            </tr>
          </thead>
          <tbody>
            {items.map((field) => (
              <tr key={field.id} data-testid="field-row">
                <td className="font-cond font-semibold">
                  <Untrusted text={systemName(field.system_id)} />
                </td>
                <td className="max-w-40">
                  <Mono wrap className="text-[12px] text-muted">
                    <Untrusted text={field.template_id} />
                  </Mono>
                </td>
                <td className="max-w-48">
                  <Mono wrap>
                    <Untrusted text={field.path} />
                  </Mono>
                </td>
                <td>{FIELD_CLASS_WORDS[field.field_class]}</td>
                <td>{POLICY_WORDS[field.policy]}</td>
                <td>{field.pinned ? 'pinned' : '—'}</td>
                <td className="max-w-48">
                  {field.shapes.length === 0 ? (
                    '—'
                  ) : (
                    <ul>
                      {field.shapes.map((shape) => (
                        <li key={shape.shape} className="whitespace-nowrap">
                          <Mono wrap>
                            <Untrusted text={shape.shape} />
                          </Mono>{' '}
                          <span className="text-2xs text-muted">{formatPercent(shape.share)}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
                <td className="max-w-32">
                  <Mono wrap className="text-[12px]">
                    <Untrusted text={field.forms.length ? field.forms.join(', ') : '—'} />
                  </Mono>
                </td>
                <td className="text-right">
                  <Mono>{field.quality === null ? '—' : formatPercent(field.quality)}</Mono>
                </td>
                <td className="text-right">
                  <Mono>{formatInteger(field.count)}</Mono>
                </td>
                <td className="text-right">
                  <Mono>{formatInteger(field.distinct_estimate)}</Mono>
                </td>
                <td className="text-right">
                  <Mono>{formatPercent(field.null_rate)}</Mono>
                </td>
                <td>
                  {field.label ? (
                    <Untrusted text={field.label} />
                  ) : field.suggested_label ? (
                    <span className="text-muted">
                      <Untrusted text={field.suggested_label} /> (suggested)
                    </span>
                  ) : (
                    '—'
                  )}
                </td>
                <td className="whitespace-nowrap">
                  <Mono>{formatTime(field.last_seen)}</Mono>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <LoadMore
        hasMore={fields.hasNextPage}
        loading={fields.isFetchingNextPage}
        onClick={() => {
          void fields.fetchNextPage();
        }}
      />
    </>
  );
}
