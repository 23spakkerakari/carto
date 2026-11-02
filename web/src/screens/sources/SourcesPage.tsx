import { Link } from 'react-router';

import { useSources, useSystems } from '../../api/hooks';
import { formatAgo, formatDuration, formatTime } from '../../lib/format';
import {
  EmptyState,
  ErrorLine,
  Loading,
  Mono,
  orDash,
  PageHeader,
  StateMarker,
  stateOf,
  Untrusted,
} from '../../ui/primitives';

/** Setup > Sources, read-only (ADR 0050): what is connected and how healthy it is. */
export function SourcesPage() {
  const sources = useSources();
  const systems = useSystems();
  const systemName = (id: string) => systems.data?.find((s) => s.id === id)?.name ?? id;
  const failing = (sources.data ?? []).filter((s) => stateOf(s.health?.status) === 'bad').length;

  return (
    <>
      <PageHeader
        title="Sources"
        kicker="Setup"
        meta={[
          { label: 'Sources', value: String(sources.data?.length ?? 0), mono: true },
          { label: 'Failing', value: String(failing), mono: true },
          { label: 'Configured', value: 'on the edge (read-only here)' },
        ]}
      />
      {sources.isPending ? (
        <Loading what="Loading sources" />
      ) : sources.isError ? (
        <ErrorLine error={sources.error} />
      ) : sources.data.length === 0 ? (
        <EmptyState>No source has reported yet.</EmptyState>
      ) : (
        <table className="table-ruled">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">System</th>
              <th scope="col">Type</th>
              <th scope="col">Enabled</th>
              <th scope="col">Health</th>
              <th scope="col">Lag</th>
              <th scope="col">Last seen</th>
              <th scope="col">Last error</th>
              <th scope="col">Last test</th>
            </tr>
          </thead>
          <tbody>
            {sources.data.map((source) => {
              const state = stateOf(source.health?.status);
              return (
                <tr key={source.id} data-testid="source-row">
                  <td>
                    <Link
                      to={`/setup/sources/${encodeURIComponent(source.id)}`}
                      className="underline-offset-2 hover:underline"
                    >
                      <Mono wrap>
                        <Untrusted text={source.id} />
                      </Mono>
                    </Link>
                  </td>
                  <td className="font-cond font-semibold">
                    <Untrusted text={systemName(source.system_id)} />
                  </td>
                  <td>
                    <Untrusted text={source.type} />
                  </td>
                  <td>{source.enabled ? 'yes' : 'no'}</td>
                  <td>
                    <StateMarker state={state} />
                  </td>
                  <td>
                    <Mono>{source.health ? formatDuration(source.health.lag_seconds) : '—'}</Mono>
                  </td>
                  <td className="whitespace-nowrap">
                    <Mono>{formatAgo(source.health?.last_success_at)}</Mono>
                  </td>
                  <td className="max-w-xs">
                    <Untrusted text={state === 'ok' ? '—' : orDash(source.health?.message)} />
                  </td>
                  <td className="max-w-xs">
                    <Untrusted text={source.last_test ?? '—'} />
                    {source.updated_at ? (
                      <span className="block text-2xs text-muted">
                        {formatTime(source.updated_at)}
                      </span>
                    ) : null}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}
