import type { LinkView } from '../../api/types';
import { evidenceRows, technicalRows } from '../../lib/evidence';
import { Mono, SectionTitle, Untrusted } from '../../ui/primitives';

/** The spec 9.3 features in plain language with the raw name and value beside each, and the
 * technical identity of the link. Everything is text. */
export function EvidenceList({ link }: { link: LinkView }) {
  const rows = evidenceRows(link);
  return (
    <div data-testid="evidence">
      <table className="table-ruled">
        <caption className="sr-only">Evidence features</caption>
        <thead>
          <tr>
            <th scope="col">Feature</th>
            <th scope="col">Value</th>
            <th scope="col">Raw</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} data-feature={row.key}>
              <td className="text-break">{row.label}</td>
              <td>
                <Untrusted text={row.value} />
              </td>
              <td>
                <Mono wrap className="text-muted">
                  {row.raw}
                </Mono>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <SectionTitle>Identity</SectionTitle>
      <table className="table-ruled">
        <caption className="sr-only">Technical identity</caption>
        <tbody>
          {technicalRows(link).map((row) => (
            <tr key={row.label}>
              <th
                scope="row"
                className="w-40 border-b border-hairline font-cond text-2xs font-medium"
              >
                {row.label}
              </th>
              <td>
                <Mono wrap>{row.value}</Mono>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
