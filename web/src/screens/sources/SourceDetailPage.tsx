import { Link, useParams } from 'react-router';

import { useSource, useSourceHealth, useSystems } from '../../api/hooks';
import { formatDuration, formatInteger, formatTime } from '../../lib/format';
import {
  DefinitionList,
  ErrorLine,
  JsonText,
  Loading,
  Mono,
  orDash,
  PageHeader,
  SectionTitle,
  StateMarker,
  stateOf,
  Untrusted,
} from '../../ui/primitives';

/** A configuration value as text. Scalars print as they are; lists and objects as indented
 * JSON text. Nothing is interpreted: no links, no markup. */
function ConfigValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted">—</span>;
  if (typeof value === 'string') {
    return (
      <Mono wrap>
        <Untrusted text={value} />
      </Mono>
    );
  }
  if (typeof value === 'number' || typeof value === 'boolean') return <Mono>{String(value)}</Mono>;
  return <JsonText value={value} />;
}

export function SourceDetailPage() {
  const { id = '' } = useParams();
  const source = useSource(id);
  const health = useSourceHealth(id);
  const systems = useSystems();

  if (source.isPending) return <Loading what="Loading source" />;
  if (source.isError) return <ErrorLine error={source.error} />;
  const s = source.data;
  const h = health.data ?? s.health;
  const systemName = systems.data?.find((sys) => sys.id === s.system_id)?.name ?? s.system_id;

  return (
    <>
      <PageHeader
        title={s.id}
        kicker="Source"
        block={[
          {
            label: 'System',
            value: (
              <Link
                to={`/systems/${encodeURIComponent(s.system_id)}`}
                className="underline-offset-2 hover:underline"
              >
                <Untrusted text={systemName} />
              </Link>
            ),
          },
          { label: 'Type', value: <Untrusted text={s.type} /> },
          { label: 'Enabled', value: s.enabled ? 'yes' : 'no' },
          { label: 'Health', value: <StateMarker state={stateOf(h?.status)} /> },
          { label: 'Updated', value: formatTime(s.updated_at), mono: true },
        ]}
      />
      <div className="grid gap-8 xl:grid-cols-2">
        <section aria-labelledby="src-health">
          <SectionTitle>
            <span id="src-health">Health</span>
          </SectionTitle>
          {h ? (
            <DefinitionList
              rows={[
                {
                  label: 'Status',
                  value: <StateMarker state={stateOf(h.status)} label={h.status} />,
                },
                { label: 'Lag', value: formatDuration(h.lag_seconds), mono: true },
                { label: 'Last success', value: formatTime(h.last_success_at), mono: true },
                { label: 'Errors', value: formatInteger(h.error_count), mono: true },
                { label: 'Buffered', value: formatInteger(h.buffer_depth), mono: true },
                { label: 'Oldest buffered', value: formatTime(h.oldest_buffered_at), mono: true },
                { label: 'Last error', value: <Untrusted text={orDash(h.message)} /> },
                { label: 'Heartbeat', value: formatTime(h.received_at), mono: true },
              ]}
            />
          ) : (
            <p className="text-muted">No heartbeat yet.</p>
          )}
          <SectionTitle>
            <span>Last read-only test</span>
          </SectionTitle>
          <p>
            <Untrusted text={s.last_test ?? 'Not tested yet.'} />
          </p>
        </section>
        <section aria-labelledby="src-config">
          <SectionTitle>
            <span id="src-config">Configuration</span>
          </SectionTitle>
          <DefinitionList
            rows={[
              {
                label: 'Secret',
                value: s.secret_ref ? (
                  <Mono wrap>
                    <Untrusted text={s.secret_ref} />
                  </Mono>
                ) : (
                  'none'
                ),
              },
              {
                label: 'Backfill',
                value: s.backfill_days === null ? '—' : `${s.backfill_days} days`,
                mono: true,
              },
              { label: 'Schedule', value: <Untrusted text={s.schedule ?? '—'} /> },
              ...Object.entries(s.config).map(([key, value]) => ({
                label: key,
                value: <ConfigValue value={value} />,
              })),
            ]}
          />
          <p className="mt-3 text-[13px] text-muted">
            Secrets never reach the console; the edge sends the name of the secret reference only.
            Editing arrives with the control plane.
          </p>
        </section>
      </div>
    </>
  );
}
