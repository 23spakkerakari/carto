/** Formatting for the console's numbers and times. Every input is a number or an ISO string from
 * the API; none of these functions ever receives customer text. Locale is fixed to en-GB so the
 * figures read the same in every browser. */

const LOCALE = 'en-GB';
const integer = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
const oneDecimal = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 1 });
const twoDecimals = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 });
const twoSignificant = new Intl.NumberFormat(LOCALE, { maximumSignificantDigits: 2 });

export function formatInteger(value: number): string {
  return integer.format(value);
}

/** 0.923 -> "92%"; under 1% two significant digits ("0.4%", "0.04%"); 99.x% keeps a decimal. */
export function formatPercent(share: number): string {
  const pct = share * 100;
  if (pct > 0 && pct < 1) return `${twoSignificant.format(pct)}%`;
  if (pct > 99 && pct < 100) return `${oneDecimal.format(pct)}%`;
  return `${integer.format(pct)}%`;
}

export function formatNumber(value: number): string {
  if (Number.isInteger(value)) return integer.format(value);
  return Math.abs(value) >= 100 ? oneDecimal.format(value) : twoDecimals.format(value);
}

/** Large counts in words: 6.76e9 -> "6.8 billion", 10000 -> "10,000". */
export function formatMagnitude(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e12) return `${oneDecimal.format(value / 1e12)} trillion`;
  if (abs >= 1e9) return `${oneDecimal.format(value / 1e9)} billion`;
  if (abs >= 1e6) return `${oneDecimal.format(value / 1e6)} million`;
  return formatNumber(value);
}

/** Seconds -> "45 s", "2 min", "2 h 10 min", "3 d 4 h". Sign is the caller's business. */
export function formatDuration(seconds: number): string {
  const s = Math.abs(Math.round(seconds));
  if (s < 60) return `${s} s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) return restMinutes ? `${hours} h ${restMinutes} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days} d ${restHours} h` : `${days} d`;
}

const dateTime = new Intl.DateTimeFormat(LOCALE, {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
  timeZone: 'UTC',
});

/** ISO timestamp -> "2026-10-18 09:41 UTC". The console shows UTC everywhere; the ISO string is
 * kept in a title attribute by the caller. */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '—';
  const parts = dateTime.formatToParts(at);
  const get = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((p) => p.type === type)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')} ${get('hour')}:${get('minute')} UTC`;
}

/** "3 min ago", "2 h ago", "4 d ago"; future times read "in …". */
export function formatAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return '—';
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return '—';
  const delta = (now - at) / 1000;
  const text = formatDuration(delta);
  if (Math.abs(delta) < 45) return 'just now';
  return delta >= 0 ? `${text} ago` : `in ${text}`;
}
