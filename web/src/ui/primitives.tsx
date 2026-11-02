import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { describeError } from '../api/client';

export type State = 'ok' | 'warn' | 'bad' | 'idle';

const STATE_CLASS: Record<State, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  bad: 'bg-bad',
  idle: 'bg-idle',
};

const STATE_WORD: Record<State, string> = {
  ok: 'healthy',
  warn: 'behind',
  bad: 'failing',
  idle: 'idle',
};

export function stateOf(status: string | null | undefined): State {
  switch (status) {
    case 'ok':
    case 'healthy':
      return 'ok';
    case 'warn':
    case 'warning':
    case 'degraded':
      return 'warn';
    case 'bad':
    case 'error':
    case 'failing':
      return 'bad';
    default:
      return 'idle';
  }
}

/** Colour is reserved for state: a 6px square and a word, never a pill. */
export function StateMarker({ state, label }: { state: State; label?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap">
      <span aria-hidden="true" className={`inline-block h-1.5 w-1.5 ${STATE_CLASS[state]}`} />
      <span>{label ?? STATE_WORD[state]}</span>
    </span>
  );
}

type Variant = 'primary' | 'default' | 'quiet' | 'confirm';

const VARIANT_CLASS: Record<Variant, string> = {
  primary: 'border-ink bg-ink text-paper hover:bg-ink-2 disabled:bg-idle disabled:border-idle',
  default:
    'border-ink bg-paper text-ink hover:bg-paper-2 disabled:text-muted disabled:border-hairline',
  quiet: 'border-transparent bg-transparent text-ink hover:bg-paper-2 disabled:text-muted',
  confirm: 'border-ink bg-hi text-ink hover:bg-hi-2 disabled:bg-paper-2 disabled:text-muted',
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  /** A short hint shown after the label, e.g. a keyboard key, only when `showHint` is true. */
  hint?: string;
  showHint?: boolean;
}

export function Button({
  variant = 'default',
  hint,
  showHint = false,
  className = '',
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`inline-flex h-8 items-center gap-2 rounded border px-3 font-cond text-[13px] font-semibold tracking-wide uppercase disabled:cursor-not-allowed ${VARIANT_CLASS[variant]} ${className}`}
      {...rest}
    >
      <span>{children}</span>
      {hint ? (
        <kbd
          aria-hidden="true"
          className={`inline-flex h-4 min-w-4 items-center justify-center border border-current px-1 font-mono text-[10px] leading-none font-medium normal-case ${showHint ? '' : 'invisible'}`}
        >
          {hint}
        </kbd>
      ) : null}
    </button>
  );
}

/** Identifiers and numbers set in Plex Mono. `wrap` lets long customer paths break. */
export function Mono({
  children,
  wrap = false,
  className = '',
}: {
  children: ReactNode;
  wrap?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`font-mono text-[13px] ${wrap ? 'text-break' : 'whitespace-nowrap'} ${className}`}
    >
      {children}
    </span>
  );
}

export interface Cell {
  label: string;
  value: ReactNode;
  mono?: boolean;
}

/** A page opens with a title block: a 2px ink rule, the title, and labelled cells, like the
 * title block of a drawing. `meta` cells sit on the right for list pages. */
export function PageHeader({
  title,
  kicker,
  block = [],
  meta = [],
  actions,
}: {
  title: string;
  kicker?: string;
  block?: Cell[];
  meta?: Cell[];
  actions?: ReactNode;
}) {
  return (
    <header className="rule-2 mb-5">
      <div className="flex items-end justify-between gap-6 pt-2 pb-3">
        <div>
          {kicker ? <div className="label-cond text-muted">{kicker}</div> : null}
          <h1 className="font-cond text-[26px] leading-8 font-semibold tracking-tight">{title}</h1>
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      {block.length > 0 || meta.length > 0 ? (
        <div className="flex flex-wrap items-stretch justify-between gap-y-2 border-y border-hairline">
          <TitleCells cells={block} />
          <TitleCells cells={meta} align="right" />
        </div>
      ) : null}
    </header>
  );
}

function TitleCells({ cells, align = 'left' }: { cells: Cell[]; align?: 'left' | 'right' }) {
  if (cells.length === 0) return <div />;
  return (
    <dl className={`flex flex-wrap ${align === 'right' ? 'justify-end' : ''}`}>
      {cells.map((cell) => (
        <div
          key={cell.label}
          className={`min-w-28 px-3 py-1.5 first:pl-0 ${align === 'right' ? 'border-l border-hairline' : 'border-r border-hairline last:border-r-0'}`}
        >
          <dt className="label-cond text-muted">{cell.label}</dt>
          <dd className={`text-break ${cell.mono ? 'font-mono text-[13px]' : ''}`}>{cell.value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return <h2 className="label-cond rule-15 mt-6 mb-2 pb-1">{children}</h2>;
}

/** Key and value pairs, ruled. Values are rendered as text by React; nothing is parsed. */
export function DefinitionList({
  rows,
}: {
  rows: { label: string; value: ReactNode; mono?: boolean }[];
}) {
  return (
    <dl className="dl-ruled">
      {rows.map((row, index) => (
        <Row key={`${row.label}-${index}`} row={row} />
      ))}
    </dl>
  );
}

function Row({ row }: { row: { label: string; value: ReactNode; mono?: boolean } }) {
  return (
    <>
      <dt>{row.label}</dt>
      <dd className={row.mono ? 'font-mono text-[13px]' : ''}>{row.value}</dd>
    </>
  );
}

/** "Details": technical detail behind a disclosure (spec 13). */
export function Disclosure({
  summary = 'Details',
  children,
  open,
}: {
  summary?: string;
  children: ReactNode;
  open?: boolean;
}) {
  return (
    <details className="group mt-3" open={open}>
      <summary className="label-cond inline-flex list-none items-center gap-1 rounded py-1 [&::-webkit-details-marker]:hidden">
        <span aria-hidden="true" className="inline-block w-3 group-open:rotate-90">
          ▸
        </span>
        {summary}
      </summary>
      <div className="mt-2">{children}</div>
    </details>
  );
}

export function ErrorLine({ error, text }: { error?: unknown; text?: string }) {
  const line = text ?? (error === undefined ? null : describeError(error));
  if (!line) return null;
  return (
    <p role="alert" className="my-2 border-l-2 border-bad pl-2 text-[13px] text-bad">
      {line}
    </p>
  );
}

export function Notice({ children }: { children: ReactNode }) {
  return (
    <p role="status" className="my-2 border-l-2 border-ink pl-2 text-[13px]">
      {children}
    </p>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="py-6 text-muted">{children}</p>;
}

export function Loading({ what = 'Loading' }: { what?: string }) {
  return (
    <p role="status" className="py-6 text-muted">
      {what}…
    </p>
  );
}

export function LoadMore({
  hasMore,
  loading,
  onClick,
}: {
  hasMore: boolean;
  loading: boolean;
  onClick: () => void;
}) {
  if (!hasMore) return null;
  return (
    <div className="py-3">
      <Button onClick={onClick} disabled={loading}>
        {loading ? 'Loading…' : 'Load more'}
      </Button>
    </div>
  );
}

/** Customer text, rendered as text. The component exists so the rule is visible at the call
 * site; React escapes everything it interpolates. Bidi isolation keeps an RTL override inside
 * the string from reordering the chrome around it. */
export function Untrusted({ text, className = '' }: { text: string; className?: string }) {
  return (
    <bdi dir="auto" className={`text-break ${className}`}>
      {text}
    </bdi>
  );
}

function stringify(value: unknown): string {
  // JSON.stringify returns undefined for undefined and functions; the types say string.
  const text = JSON.stringify(value, null, 2) as string | undefined;
  return text ?? 'null';
}

/** A customer string or a dash when it is empty or missing. */
export function orDash(text: string | null | undefined): string {
  if (text === null || text === undefined || text === '') return '—';
  return text;
}

/** A JSON value from the API as indented text inside <pre>. Never parsed, never linked. */
export function JsonText({ value }: { value: unknown }) {
  return (
    <pre className="m-0 font-mono text-[12px] leading-4 whitespace-pre-wrap text-break">
      {stringify(value)}
    </pre>
  );
}
