import { Link } from 'react-router';

import { useSources, useSystems } from '../../api/hooks';
import { formatTime } from '../../lib/format';
import {
  EmptyState,
  ErrorLine,
  Loading,
  Mono,
  PageHeader,
  StateMarker,
  stateOf,
  Untrusted,
} from '../../ui/primitives';

/** Systems as the edge reported them (ADR 0050), read-only in M2. */
export function SystemsPage() {
  const systems = useSystems();
  const sources = useSources();

  const worstState = (systemId: string) => {
    const states = (sources.data ?? [])
      .filter((s) => s.system_id === systemId)
      .map((s) => stateOf(s.health?.status));
    if (states.includes('bad')) return 'bad';
    if (states.includes('warn')) return 'warn';
    if (states.includes('ok')) return 'ok';
    return 'idle';
  };

  return (
    <>
      <PageHeader
        title="Systems"
        meta={[
          { label: 'Systems', value: String(systems.data?.length ?? 0), mono: true },
          { label: 'Sources', value: String(sources.data?.length ?? 0), mono: true },
          { label: 'Reported by', value: 'the edge' },
        ]}
      />
      {systems.isPending ? (
        <Loading what="Loading systems" />
      ) : systems.isError ? (
        <ErrorLine error={systems.error} />
      ) : systems.data.length === 0 ? (
        <EmptyState>No system has reported yet.</EmptyState>
      ) : (
        <table className="table-ruled">
          <thead>
            <tr>
              <th scope="col">System</th>
              <th scope="col">Id</th>
              <th scope="col">Description</th>
              <th scope="col">Owner</th>
              <th scope="col">Criticality</th>
              <th scope="col" className="text-right">
                Sources
              </th>
              <th scope="col">Health</th>
              <th scope="col">Updated</th>
            </tr>
          </thead>
          <tbody>
            {systems.data.map((system) => (
              <tr key={system.id}>
                <td className="font-cond font-semibold">
                  <Link
                    to={`/systems/${encodeURIComponent(system.id)}`}
                    className="underline-offset-2 hover:underline"
                  >
                    <Untrusted text={system.name} />
                  </Link>
                </td>
                <td>
                  <Mono wrap>
                    <Untrusted text={system.id} />
                  </Mono>
                </td>
                <td className="max-w-md">
                  <Untrusted text={system.description} />
                </td>
                <td>
                  <Untrusted text={system.owner_group ?? '—'} />
                </td>
                <td>
                  <Untrusted text={system.criticality} />
                </td>
                <td className="text-right">
                  <Mono>{system.source_count}</Mono>
                </td>
                <td>
                  <StateMarker state={worstState(system.id)} />
                </td>
                <td className="whitespace-nowrap">
                  <Mono>{formatTime(system.updated_at)}</Mono>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
