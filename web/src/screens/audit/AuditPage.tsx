import { useMemo } from 'react';

import { useAudit } from '../../api/hooks';
import { formatTime } from '../../lib/format';
import {
  Disclosure,
  EmptyState,
  ErrorLine,
  JsonText,
  LoadMore,
  Loading,
  Mono,
  orDash,
  PageHeader,
  Untrusted,
} from '../../ui/primitives';

/** Admin > Audit (spec 14.9): the hash-chained log, newest first. Before and after are shown as
 * indented JSON text. */
export function AuditPage() {
  const audit = useAudit();
  const items = useMemo(() => audit.data?.pages.flatMap((p) => p.items) ?? [], [audit.data]);

  return (
    <>
      <PageHeader
        title="Audit"
        kicker="Admin"
        meta={[
          { label: 'Entries loaded', value: String(items.length), mono: true },
          { label: 'Order', value: 'newest first' },
          { label: 'Integrity', value: 'hash chain; verify with carto-ctl audit verify' },
        ]}
      />
      {audit.isPending ? (
        <Loading what="Loading the audit log" />
      ) : audit.isError ? (
        <ErrorLine error={audit.error} />
      ) : items.length === 0 ? (
        <EmptyState>The audit log is empty.</EmptyState>
      ) : (
        <table className="table-ruled">
          <thead>
            <tr>
              <th scope="col" className="text-right">
                #
              </th>
              <th scope="col">Time</th>
              <th scope="col">Actor</th>
              <th scope="col">Action</th>
              <th scope="col">Target</th>
              <th scope="col">Reason</th>
              <th scope="col">Change</th>
            </tr>
          </thead>
          <tbody>
            {items.map((entry) => (
              <tr key={entry.id} data-testid="audit-row">
                <td className="text-right">
                  <Mono>{entry.id}</Mono>
                </td>
                <td className="whitespace-nowrap">
                  <Mono>{formatTime(entry.at)}</Mono>
                </td>
                <td className="max-w-48">
                  <Mono wrap>
                    <Untrusted text={entry.actor} />
                  </Mono>
                </td>
                <td>
                  <Mono wrap>
                    <Untrusted text={entry.action} />
                  </Mono>
                </td>
                <td className="max-w-56">
                  <Mono wrap>
                    <Untrusted text={entry.target} />
                  </Mono>
                </td>
                <td className="max-w-xs">
                  <Untrusted text={orDash(entry.reason)} />
                </td>
                <td className="w-96">
                  {entry.before === null && entry.after === null ? (
                    '—'
                  ) : (
                    <Disclosure summary="Before and after">
                      <div className="grid grid-cols-2 gap-4">
                        <div>
                          <div className="label-cond text-muted">Before</div>
                          <JsonText value={entry.before} />
                        </div>
                        <div>
                          <div className="label-cond text-muted">After</div>
                          <JsonText value={entry.after} />
                        </div>
                      </div>
                      <div className="mt-2 text-2xs text-muted">
                        request <Mono>{entry.request_id ?? '—'}</Mono>; row hash{' '}
                        <Mono wrap>{entry.row_hash}</Mono>
                      </div>
                    </Disclosure>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <LoadMore
        hasMore={audit.hasNextPage}
        loading={audit.isFetchingNextPage}
        onClick={() => {
          void audit.fetchNextPage();
        }}
      />
    </>
  );
}
