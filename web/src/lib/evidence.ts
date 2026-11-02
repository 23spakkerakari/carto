import type { CompositeComponentView, EvidenceView, LinkView } from '../api/types';
import {
  formatDuration,
  formatInteger,
  formatMagnitude,
  formatNumber,
  formatPercent,
  formatTime,
} from './format';

/** One row of the "Details" disclosure: the plain-language label, the formatted value and the
 * raw feature name with its raw value, so a reviewer and an engineer read the same table. */
export interface EvidenceRow {
  key: keyof EvidenceView | `component_${number}`;
  label: string;
  value: string;
  raw: string;
}

const NOT_MEASURED = 'not measured';

function pct(value: number | null): string {
  return value === null ? NOT_MEASURED : formatPercent(value);
}

function num(value: number | null): string {
  return value === null ? NOT_MEASURED : formatNumber(value);
}

function rawOf(name: string, value: number | string | null): string {
  return `${name} = ${value === null ? 'null' : String(value)}`;
}

function lagWords(seconds: number | null): string {
  if (seconds === null) return NOT_MEASURED;
  if (seconds === 0) return 'at the same time';
  const text = formatDuration(seconds);
  return seconds > 0 ? `${text} later` : `${text} earlier`;
}

function fanoutWords(median: number | null, p99: number | null): string {
  if (median === null && p99 === null) return NOT_MEASURED;
  return `typically ${num(median)}, worst 1% ${num(p99)}`;
}

function componentWords(component: CompositeComponentView): string {
  return (
    `${component.a.path} with ${component.b.path} ` +
    `(${component.a.form}): agrees in ${formatPercent(component.m)} of true matches ` +
    `and ${formatPercent(component.u)} of random pairs; weight ${formatNumber(component.weight)}`
  );
}

/** Every feature of spec 9.3 (and 9.4, 9.5) as a row, in the order of the table, whether or
 * not it was measured for this link, so the disclosure always has the same shape. */
export function evidenceRows(link: LinkView): EvidenceRow[] {
  const e = link.evidence;
  const from = link.a.system_id;
  const to = link.b.system_id;
  const rows: EvidenceRow[] = [
    {
      key: 'n_a',
      label: `Distinct identifiers in ${from}`,
      value: formatInteger(e.n_a),
      raw: rawOf('n_a', e.n_a),
    },
    {
      key: 'n_b',
      label: `Distinct identifiers in ${to}`,
      value: formatInteger(e.n_b),
      raw: rawOf('n_b', e.n_b),
    },
    {
      key: 'n_match',
      label: 'Identifiers seen in both',
      value: formatInteger(e.n_match),
      raw: rawOf('n_match', e.n_match),
    },
    {
      key: 'c_ab',
      label: `Share of ${from} identifiers that match`,
      value: formatPercent(e.c_ab),
      raw: rawOf('c_ab', e.c_ab),
    },
    {
      key: 'c_ba',
      label: `Share of ${to} identifiers that match`,
      value: formatPercent(e.c_ba),
      raw: rawOf('c_ba', e.c_ba),
    },
    {
      key: 'lag_median_seconds',
      label: `Typical delay: ${to} appears`,
      value: lagWords(e.lag_median_seconds),
      raw: rawOf('lag_median_seconds', e.lag_median_seconds),
    },
    {
      key: 'lag_iqr_seconds',
      label: 'Spread of that delay (middle half)',
      value: e.lag_iqr_seconds === null ? NOT_MEASURED : formatDuration(e.lag_iqr_seconds),
      raw: rawOf('lag_iqr_seconds', e.lag_iqr_seconds),
    },
    {
      key: 'lag_positive_share',
      label: `How often ${to} comes after ${from}`,
      value: pct(e.lag_positive_share),
      raw: rawOf('lag_positive_share', e.lag_positive_share),
    },
    {
      key: 'fanout_ab_median',
      label: `${to} events per ${from} identifier`,
      value: fanoutWords(e.fanout_ab_median, e.fanout_ab_p99),
      raw: `${rawOf('fanout_ab_median', e.fanout_ab_median)}; ${rawOf('fanout_ab_p99', e.fanout_ab_p99)}`,
    },
    {
      key: 'fanout_ba_median',
      label: `${from} events per ${to} identifier`,
      value: fanoutWords(e.fanout_ba_median, e.fanout_ba_p99),
      raw: `${rawOf('fanout_ba_median', e.fanout_ba_median)}; ${rawOf('fanout_ba_p99', e.fanout_ba_p99)}`,
    },
    {
      key: 'concentration',
      label: 'Share of matches from the ten busiest identifiers',
      value: pct(e.concentration),
      raw: rawOf('concentration', e.concentration),
    },
    {
      key: 'chance_ratio',
      label: 'Matches expected by chance, as a share of those found',
      value: pct(e.chance_ratio),
      raw: rawOf('chance_ratio', e.chance_ratio),
    },
    {
      key: 'domain_size',
      label: 'Possible values of the dominant shape',
      value: e.domain_size === null ? NOT_MEASURED : formatMagnitude(e.domain_size),
      raw: rawOf('domain_size', e.domain_size),
    },
    {
      key: 'pairing_consistency',
      label: `Share of ${from} values that always pair with one ${to} value`,
      value:
        link.link_type === 'bridge' || e.pairing_consistency !== null
          ? pct(e.pairing_consistency)
          : 'bridges only',
      raw: rawOf('pairing_consistency', e.pairing_consistency),
    },
    {
      key: 'window_start',
      label: 'Evidence window',
      value:
        e.window_start === null && e.window_end === null
          ? NOT_MEASURED
          : `${formatTime(e.window_start)} to ${formatTime(e.window_end)}`,
      raw: `${rawOf('window_start', e.window_start)}; ${rawOf('window_end', e.window_end)}`,
    },
  ];
  if (link.link_type === 'composite' || e.components.length > 0) {
    if (e.components.length === 0) {
      rows.push({
        key: 'components',
        label: 'Weak fields matched together',
        value: NOT_MEASURED,
        raw: 'components = []',
      });
    }
    e.components.forEach((component, index) => {
      rows.push({
        key: `component_${index}`,
        label: index === 0 ? 'Weak fields matched together' : '',
        value: componentWords(component),
        raw: `components[${index}] = {weight: ${component.weight}, m: ${component.m}, u: ${component.u}}`,
      });
    });
  }
  return rows;
}

/** Technical identity of a link for the bottom of the disclosure. */
export function technicalRows(link: LinkView): { label: string; value: string }[] {
  return [
    { label: 'Link id', value: link.id },
    { label: 'Version', value: String(link.version) },
    { label: 'Type', value: link.link_type },
    { label: 'Role', value: link.role },
    { label: 'Direction', value: link.direction },
    { label: 'Score', value: link.score.toFixed(4) },
    { label: 'Affected volume', value: String(link.affected_volume) },
    { label: 'Ambiguous merges', value: String(link.ambiguous_merges) },
    { label: 'From field', value: link.a.field_id },
    { label: 'From path', value: `${link.a.path} (${link.a.form})` },
    { label: 'From template', value: link.a.template_id },
    { label: 'To field', value: link.b.field_id },
    { label: 'To path', value: `${link.b.path} (${link.b.form})` },
    { label: 'To template', value: link.b.template_id },
    { label: 'Entity', value: link.entity_id ?? '—' },
    { label: 'First proposed', value: formatTime(link.first_proposed_at) },
    { label: 'Updated', value: formatTime(link.updated_at) },
  ];
}
