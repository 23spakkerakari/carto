import { Link, useParams } from 'react-router';

import { useFields, useSources, useSystem } from '../../api/hooks';
import { formatDuration, formatTime } from '../../lib/format';
import {
  DefinitionList,
  ErrorLine,
  Loading,
  Mono,
  orDash,
  PageHeader,
  SectionTitle,
  StateMarker,
  stateOf,
  Untrusted,
} from '../../ui/primitives';

export function SystemDetailPage() {
  const { id = '' } = useParams();
  const system = useSystem(id);
  const sources = useSources();
  const fields = useFields(id);

  if (system.isPending) return <Loading what="Loading system" />;
  if (system.isError) return <ErrorLine error={system.error} />;
  const s = system.data;
  const own = (sources.data ?? []).filter((source) => source.system_id === s.id);
  const fieldCount = fields.data?.pages.reduce((n, page) => n + page.items.length, 0) ?? 0;

  return (
    <>
      <PageHeader
        title={s.name}
        kicker="System"
        block={[
          { label: 'Id', value: <Untrusted text={s.id} />, mono: true },
          { label: 'Owner', value: <Untrusted text={s.owner_group ?? '—'} /> },
          { label: 'Criticality', value: <Untrusted text={s.criticality} /> },
          { label: 'Sources', value: String(s.source_count), mono: true },
          { label: 'Updated', value: formatTime(s.updated_at), mono: true },
        ]}
      />
      {s.description ? (
        <p className="max-w-3xl">
          <Untrusted text={s.description} />
        </p>
      ) : null}
      <SectionTitle>Sources</SectionTitle>
      {own.length === 0 ? (
        <p className="text-muted">No source reports for this system.</p>
      ) : (
        <table className="table-ruled">
          <thead>
            <tr>
              <th scope="col">Source</th>
              <th scope="col">Type</th>
              <th scope="col">Enabled</th>
              <th scope="col">Health</th>
              <th scope="col">Lag</th>
              <th scope="col">Last error</th>
            </tr>
          </thead>
          <tbody>
            {own.map((source) => (
              <tr key={source.id}>
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
                <td>
                  <Untrusted text={source.type} />
                </td>
                <td>{source.enabled ? 'yes' : 'no'}</td>
                <td>
                  <StateMarker state={stateOf(source.health?.status)} />
                </td>
                <td>
                  <Mono>{source.health ? formatDuration(source.health.lag_seconds) : '—'}</Mono>
                </td>
                <td className="max-w-md">
                  <Untrusted text={orDash(source.health?.message)} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <SectionTitle>Fields</SectionTitle>
      <DefinitionList
        rows={[
          {
            label: 'Known fields',
            value: fields.isPending ? 'loading…' : String(fieldCount),
            mono: true,
          },
          {
            label: 'Policies',
            value: (
              <Link
                to={`/setup/fields?system=${encodeURIComponent(s.id)}`}
                className="underline underline-offset-2"
              >
                Open field policies for this system
              </Link>
            ),
          },
        ]}
      />
    </>
  );
}
