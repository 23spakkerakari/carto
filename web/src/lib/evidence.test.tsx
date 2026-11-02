import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { EvidenceView, LinkView } from '../api/types';
import { links } from '../mocks/fixtures/shop';
import { EvidenceList } from '../screens/review/EvidenceList';
import { evidenceRows } from './evidence';

const FEATURE_KEYS: (keyof EvidenceView)[] = [
  'n_a',
  'n_b',
  'n_match',
  'c_ab',
  'c_ba',
  'lag_median_seconds',
  'lag_iqr_seconds',
  'lag_positive_share',
  'fanout_ab_median',
  'fanout_ba_median',
  'concentration',
  'chance_ratio',
  'domain_size',
  'pairing_consistency',
  'window_start',
];

function link(id: string): LinkView {
  const found = links.find((l) => l.id === id);
  if (!found) throw new Error(`fixture ${id} missing`);
  return found;
}

describe('evidenceRows', () => {
  it('renders every spec 9.3 feature for an exact link, formatted', () => {
    const rows = evidenceRows(link('lnk_01'));
    const keys = rows.map((row) => row.key);
    for (const key of FEATURE_KEYS) expect(keys).toContain(key);
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(byKey.n_a?.value).toBe('30,975');
    expect(byKey.c_ab?.value).toBe('96%');
    expect(byKey.c_ba?.value).toBe('99.2%');
    expect(byKey.lag_median_seconds?.value).toBe('1 d 2 h later');
    expect(byKey.lag_iqr_seconds?.value).toBe('4 h');
    expect(byKey.fanout_ab_median?.value).toBe('typically 1, worst 1% 2');
    expect(byKey.concentration?.value).toBe('0.04%');
    expect(byKey.domain_size?.value).toBe('2.6 million');
    expect(byKey.pairing_consistency?.value).toBe('bridges only');
    expect(byKey.n_a?.raw).toBe('n_a = 30975');
    expect(byKey.fanout_ab_median?.raw).toBe('fanout_ab_median = 1; fanout_ab_p99 = 2');
  });

  it('shows pairing consistency for bridges and components for composites', () => {
    const bridge = Object.fromEntries(evidenceRows(link('lnk_03')).map((r) => [r.key, r]));
    expect(bridge.pairing_consistency?.value).toBe('99.8%');
    expect(bridge.lag_median_seconds?.value).toBe('at the same time');

    const composite = evidenceRows(link('lnk_06'));
    const components = composite.filter((r) => r.key.startsWith('component_'));
    expect(components).toHaveLength(2);
    expect(components[0]?.value).toContain('amount with amount (amount)');
    expect(components[0]?.value).toContain('weight 4.1');
    expect(components[0]?.raw).toBe('components[0] = {weight: 4.1, m: 0.97, u: 0.0012}');
  });

  it('says "not measured" instead of hiding an unmeasured feature', () => {
    const rows = Object.fromEntries(evidenceRows(link('lnk_07')).map((r) => [r.key, r]));
    expect(rows.lag_median_seconds?.value).toBe('not measured');
    expect(rows.lag_positive_share?.value).toBe('not measured');
    expect(rows.lag_median_seconds?.raw).toBe('lag_median_seconds = null');
  });

  it('reads earlier when the lag is negative', () => {
    const rows = Object.fromEntries(evidenceRows(link('lnk_12')).map((r) => [r.key, r]));
    expect(rows.lag_median_seconds?.value).toBe('1 d 5 h earlier');
  });
});

describe('EvidenceList', () => {
  it('renders every feature row and a planted script as text', () => {
    const hostile: LinkView = {
      ...link('lnk_01'),
      a: {
        ...link('lnk_01').a,
        path: '<script>window.__xss = 1</script>',
        template_id: '<img src=x onerror=alert(1)>',
      },
      sentence: '<b>bold</b>',
    };
    const { container } = render(<EvidenceList link={hostile} />);
    for (const key of FEATURE_KEYS) {
      expect(container.querySelector(`[data-feature="${key}"]`)).not.toBeNull();
    }
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
    expect(screen.getByText(/<script>window\.__xss = 1<\/script>/)).toBeInTheDocument();
    expect(screen.getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(container.textContent).toContain('n_a = 30975');
  });
});
