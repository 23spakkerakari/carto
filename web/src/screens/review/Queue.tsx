import { useCallback, useEffect, useRef, type KeyboardEvent } from 'react';

import type { LinkView } from '../../api/types';
import { formatInteger, formatPercent } from '../../lib/format';
import { directionWords, LINK_TYPE_WORDS } from '../../lib/words';
import { Mono, Untrusted } from '../../ui/primitives';

export type QueueAction = 'accept' | 'reject' | 'association';

const COLS =
  'grid-cols-[minmax(0,1.3fr)_minmax(0,1.3fr)_minmax(0,1fr)_5.5rem_6rem_minmax(0,0.9fr)]';

export interface QueueProps {
  items: LinkView[];
  currentId: string | null;
  onSelect: (id: string) => void;
  onAction: (action: QueueAction) => void;
  actionsEnabled: boolean;
  systemName: (id: string) => string;
  onFocusChange: (focused: boolean) => void;
  label: string;
}

/** The queue is an ARIA grid with row-level focus: Up and Down move the current item, Home and
 * End jump, A, R and S act on it (shortcuts only while focus is in the grid). Rows are CSS
 * grids sharing one column template, so the sentence can span the row as a second line. */
export function Queue({
  items,
  currentId,
  onSelect,
  onAction,
  actionsEnabled,
  systemName,
  onFocusChange,
  label,
}: QueueProps) {
  const rowRefs = useRef(new Map<string, HTMLDivElement>());
  const gridRef = useRef<HTMLDivElement>(null);
  // Whether focus was inside the grid: when a decision removes the focused row, the document
  // would otherwise drop focus to the body and the keyboard flow would stop.
  const focusInside = useRef(false);

  const focusRow = useCallback((id: string) => {
    rowRefs.current.get(id)?.focus();
  }, []);

  useEffect(() => {
    // Keep the focused row in step with the current item after a decision removes a row.
    const grid = gridRef.current;
    if (!grid || currentId === null) return;
    const active = document.activeElement;
    const inside = grid.contains(active);
    const lostToBody = focusInside.current && (!active || active === document.body);
    if ((inside || lostToBody) && active !== rowRefs.current.get(currentId)) {
      focusRow(currentId);
    }
  }, [currentId, focusRow]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = items.findIndex((item) => item.id === currentId);
    const move = (next: number) => {
      const target = items[Math.max(0, Math.min(items.length - 1, next))];
      if (!target) return;
      event.preventDefault();
      onSelect(target.id);
      focusRow(target.id);
    };
    switch (event.key) {
      case 'ArrowDown':
        move(index + 1);
        return;
      case 'ArrowUp':
        move(index - 1);
        return;
      case 'Home':
        move(0);
        return;
      case 'End':
        move(items.length - 1);
        return;
      default:
        break;
    }
    if (!actionsEnabled || event.altKey || event.ctrlKey || event.metaKey) return;
    const key = event.key.toLowerCase();
    if (key === 'a' || key === 'r' || key === 's') {
      event.preventDefault();
      onAction(key === 'a' ? 'accept' : key === 'r' ? 'reject' : 'association');
    }
  };

  return (
    <div
      ref={gridRef}
      role="grid"
      aria-label={label}
      aria-rowcount={items.length + 1}
      className="text-[13px]"
      tabIndex={-1}
      onFocus={() => {
        focusInside.current = true;
        onFocusChange(true);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          focusInside.current = false;
          onFocusChange(false);
        }
      }}
    >
      <div role="rowgroup">
        <div role="row" aria-rowindex={1} className={`grid ${COLS} rule-15 gap-x-3`}>
          <Header>From</Header>
          <Header>To</Header>
          <Header>Kind</Header>
          <Header align="right">Confidence</Header>
          <Header align="right">Volume</Header>
          <Header>Direction</Header>
        </div>
      </div>
      <div role="rowgroup">
        {items.map((link, index) => {
          const current = link.id === currentId;
          const direction = directionWords(
            link.direction,
            systemName(link.a.system_id),
            systemName(link.b.system_id),
          );
          return (
            <div
              key={link.id}
              ref={(node) => {
                if (node) rowRefs.current.set(link.id, node);
                else rowRefs.current.delete(link.id);
              }}
              role="row"
              aria-rowindex={index + 2}
              aria-selected={current}
              tabIndex={current || (currentId === null && index === 0) ? 0 : -1}
              data-link-id={link.id}
              onClick={() => {
                onSelect(link.id);
              }}
              onKeyDown={onKeyDown}
              onFocus={() => {
                if (!current) onSelect(link.id);
              }}
              className={`grid ${COLS} cursor-pointer gap-x-3 border-b border-hairline py-2 ${current ? 'bg-hi' : 'hover:bg-paper-2'}`}
            >
              <End
                name={systemName(link.a.system_id)}
                path={link.a.path}
                shape={link.a.top_shape}
              />
              <End
                name={systemName(link.b.system_id)}
                path={link.b.path}
                shape={link.b.top_shape}
              />
              <div role="gridcell" className="px-2">
                {LINK_TYPE_WORDS[link.link_type]}
              </div>
              <div role="gridcell" className="px-2 text-right">
                <Mono>{formatPercent(link.score)}</Mono>
              </div>
              <div role="gridcell" className="px-2 text-right">
                <Mono>{formatInteger(link.affected_volume)}</Mono>
              </div>
              <div role="gridcell" className="px-2 whitespace-nowrap">
                <span aria-hidden="true" className="mr-1 font-mono">
                  {direction.glyph}
                </span>
                <Untrusted text={direction.text} />
              </div>
              <div role="gridcell" className="col-span-full px-2 pt-1 text-ink-2">
                <Untrusted text={link.sentence} />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Header({ children, align = 'left' }: { children: string; align?: 'left' | 'right' }) {
  return (
    <div
      role="columnheader"
      className={`label-cond px-2 pb-1.5 ${align === 'right' ? 'text-right' : ''}`}
    >
      {children}
    </div>
  );
}

function End({ name, path, shape }: { name: string; path: string; shape: string | null }) {
  return (
    <div role="gridcell" className="min-w-0 px-2">
      <div className="font-cond font-semibold">
        <Untrusted text={name} />
      </div>
      <div>
        <Mono wrap>
          <Untrusted text={path} />
        </Mono>
      </div>
      {shape ? (
        <div className="text-muted">
          <Mono wrap className="text-[12px]">
            <Untrusted text={shape} />
          </Mono>
        </div>
      ) : null}
    </div>
  );
}
