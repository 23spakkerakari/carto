import * as Tabs from '@radix-ui/react-tabs';
import { useCallback, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router';

import { ApiError } from '../../api/client';
import { useDecision, useLinks, useMe, useSystems } from '../../api/hooks';
import { LINK_STATUSES, type LinkStatus, type LinkView } from '../../api/types';
import { hasPermission, LINK_STATUS_TAB } from '../../lib/words';
import { Dialog } from '../../ui/Dialog';
import { EmptyState, ErrorLine, LoadMore, Loading, PageHeader } from '../../ui/primitives';
import { DECIDABLE, LinkDetail } from './LinkDetail';
import { Queue, type QueueAction } from './Queue';
import { RejectForm, type RejectValues } from './RejectForm';

const EMPTY: Record<LinkStatus, string> = {
  proposed: 'No links are waiting for review.',
  accepted: 'No links have been accepted yet.',
  rejected: 'No links have been rejected.',
  stale: 'No accepted link has gone stale.',
  auto_accepted: 'No links were accepted automatically.',
};

function isStatus(value: string | null): value is LinkStatus {
  return value !== null && (LINK_STATUSES as readonly string[]).includes(value);
}

export function ReviewPage() {
  const [params, setParams] = useSearchParams();
  const status: LinkStatus = isStatus(params.get('status'))
    ? (params.get('status') as LinkStatus)
    : 'proposed';
  const me = useMe();
  const systems = useSystems();
  const links = useLinks(status);
  const decision = useDecision();
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [queueFocused, setQueueFocused] = useState(false);
  const [rejecting, setRejecting] = useState<LinkView | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lastIndex, setLastIndex] = useState(0);

  const items = useMemo(() => links.data?.pages.flatMap((page) => page.items) ?? [], [links.data]);
  const canReview = hasPermission(me.data?.permissions, 'review_links');

  const systemName = useCallback(
    (id: string) => systems.data?.find((system) => system.id === id)?.name ?? id,
    [systems.data],
  );

  // The current item follows the list: the first item by default, the neighbour after a
  // decision removes the current one. Derived, not synchronised: the stored id may be stale.
  const found = items.find((item) => item.id === currentId);
  const current = found ?? items[Math.min(lastIndex, Math.max(0, items.length - 1))] ?? null;
  const effectiveId = current?.id ?? null;
  const select = useCallback(
    (id: string) => {
      setCurrentId(id);
      const index = items.findIndex((item) => item.id === id);
      if (index >= 0) setLastIndex(index);
    },
    [items],
  );

  const decide = (link: LinkView, kind: QueueAction, values?: RejectValues) => {
    setNotice(null);
    decision.mutate(
      {
        link,
        kind,
        body:
          kind === 'reject' && values ? { reason: values.reason, note: values.note } : { note: '' },
      },
      {
        onSuccess: () => {
          setRejecting(null);
        },
        onError: (error) => {
          if (error instanceof ApiError && error.isStale) {
            setRejecting(null);
            setNotice('This link changed while you were looking at it. Reloaded.');
          }
        },
      },
    );
  };

  const onAction = (action: QueueAction) => {
    if (!current || !canReview || !DECIDABLE.has(current.status) || decision.isPending) return;
    if (action === 'reject') {
      setRejecting(current);
      return;
    }
    decide(current, action);
  };

  const selectTab = (value: string) => {
    if (!isStatus(value)) return;
    setCurrentId(null);
    setLastIndex(0);
    setNotice(null);
    setParams(value === 'proposed' ? {} : { status: value }, { replace: true });
  };

  const staleError = decision.error instanceof ApiError && decision.error.isStale;
  const detailError = staleError ? undefined : (decision.error ?? undefined);

  return (
    <>
      <PageHeader
        title="Review"
        kicker="Map"
        meta={[
          { label: 'Showing', value: LINK_STATUS_TAB[status] },
          { label: 'Loaded', value: String(items.length), mono: true },
          { label: 'Order', value: 'confidence × volume' },
        ]}
      />
      <Tabs.Root value={status} onValueChange={selectTab}>
        <Tabs.List aria-label="Link status" className="mb-4 flex gap-1 border-b border-hairline">
          {LINK_STATUSES.map((value) => (
            <Tabs.Trigger
              key={value}
              value={value}
              className="-mb-px border-b-2 border-transparent px-3 py-1.5 font-cond text-[14px] font-medium text-ink-2 data-[state=active]:border-ink data-[state=active]:text-ink"
            >
              {LINK_STATUS_TAB[value]}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        {LINK_STATUSES.map((value) => (
          <Tabs.Content key={value} value={value} className="outline-none">
            {value !== status ? null : links.isPending ? (
              <Loading what="Loading links" />
            ) : links.isError ? (
              <ErrorLine error={links.error} />
            ) : items.length === 0 ? (
              <EmptyState>{EMPTY[status]}</EmptyState>
            ) : (
              <div className="flex items-start gap-8">
                <div className="min-w-0 flex-1">
                  <Queue
                    items={items}
                    currentId={effectiveId}
                    onSelect={select}
                    onAction={onAction}
                    actionsEnabled={canReview && current !== null && DECIDABLE.has(current.status)}
                    systemName={systemName}
                    onFocusChange={setQueueFocused}
                    label={`${LINK_STATUS_TAB[status]} links`}
                  />
                  <LoadMore
                    hasMore={links.hasNextPage}
                    loading={links.isFetchingNextPage}
                    onClick={() => {
                      void links.fetchNextPage();
                    }}
                  />
                </div>
                <div className="w-[26rem] shrink-0 xl:w-[30rem]">
                  {current ? (
                    <LinkDetail
                      link={current}
                      systemName={systemName}
                      onAction={onAction}
                      canReview={canReview}
                      busy={decision.isPending}
                      showHints={queueFocused}
                      error={detailError}
                      notice={notice}
                    />
                  ) : null}
                </div>
              </div>
            )}
          </Tabs.Content>
        ))}
      </Tabs.Root>
      <Dialog
        open={rejecting !== null}
        onClose={() => {
          setRejecting(null);
        }}
        title="Reject this link"
        labelledBy="reject-title"
      >
        {rejecting ? (
          <RejectForm
            key={rejecting.id}
            busy={decision.isPending}
            error={detailError}
            onCancel={() => {
              setRejecting(null);
            }}
            onSubmit={(values) => {
              decide(rejecting, 'reject', values);
            }}
          />
        ) : null}
      </Dialog>
    </>
  );
}
