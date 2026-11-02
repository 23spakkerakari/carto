import { describe, expect, it } from 'vitest';

import {
  formatAgo,
  formatDuration,
  formatInteger,
  formatMagnitude,
  formatPercent,
  formatTime,
} from './format';

describe('format', () => {
  it('formats shares as percentages', () => {
    expect(formatPercent(0.92)).toBe('92%');
    expect(formatPercent(0.004)).toBe('0.4%');
    expect(formatPercent(0.9997)).toBe('100%');
    expect(formatPercent(0.992)).toBe('99.2%');
    expect(formatPercent(1)).toBe('100%');
    expect(formatPercent(0)).toBe('0%');
  });

  it('formats integers with separators', () => {
    expect(formatInteger(30975)).toBe('30,975');
  });

  it('formats magnitudes in words', () => {
    expect(formatMagnitude(6.76e9)).toBe('6.8 billion');
    expect(formatMagnitude(2.6e6)).toBe('2.6 million');
    expect(formatMagnitude(10000)).toBe('10,000');
  });

  it('formats durations in plain units', () => {
    expect(formatDuration(45)).toBe('45 s');
    expect(formatDuration(1380)).toBe('23 min');
    expect(formatDuration(7320)).toBe('2 h 2 min');
    expect(formatDuration(93600)).toBe('1 d 2 h');
    expect(formatDuration(-93600)).toBe('1 d 2 h');
  });

  it('formats times in UTC and relative times', () => {
    expect(formatTime('2026-10-18T06:05:00Z')).toBe('2026-10-18 06:05 UTC');
    expect(formatTime(null)).toBe('—');
    expect(formatTime('garbage')).toBe('—');
    const now = Date.parse('2026-10-18T06:00:00Z');
    expect(formatAgo('2026-10-18T05:57:00Z', now)).toBe('3 min ago');
    expect(formatAgo('2026-10-18T05:59:50Z', now)).toBe('just now');
    expect(formatAgo('2026-10-18T07:00:00Z', now)).toBe('in 1 h');
  });
});
