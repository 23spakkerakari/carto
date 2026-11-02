import type { LinkView } from '../../api/types';
import { formatDuration, formatInteger, formatPercent, formatTime } from '../../lib/format';
import {
  directionWords,
  LINK_ROLE_WORDS,
  LINK_STATUS_WORDS,
  LINK_TYPE_WORDS,
  REJECT_REASON_WORDS,
} from '../../lib/words';
import {
  Button,
  DefinitionList,
  Disclosure,
  ErrorLine,
  Mono,
  Notice,
  Untrusted,
} from '../../ui/primitives';
import { EvidenceList } from './EvidenceList';
import type { QueueAction } from './Queue';

export const DECIDABLE = new Set<LinkView['status']>(['proposed', 'stale', 'auto_accepted']);

export function LinkDetail({
  link,
  systemName,
  onAction,
  canReview,
  busy,
  showHints,
  error,
  notice,
}: {
  link: LinkView;
  systemName: (id: string) => string;
  onAction: (action: QueueAction) => void;
  canReview: boolean;
  busy: boolean;
  showHints: boolean;
  error?: unknown;
  notice?: string | null;
}) {
  const from = systemName(link.a.system_id);
  const to = systemName(link.b.system_id);
  const direction = directionWords(link.direction, from, to);
  const decidable = DECIDABLE.has(link.status);
  const lag = link.evidence.lag_median_seconds;

  return (
    <aside aria-labelledby="detail-title" className="rule-2 pt-2" data-testid="link-detail">
      <div className="label-cond text-muted">Selected link</div>
      <h2 id="detail-title" className="font-cond text-[20px] leading-7 font-semibold">
        <Untrusted text={from} /> <span aria-hidden="true">{direction.glyph}</span>{' '}
        <span className="sr-only">to</span>
        <Untrusted text={to} />
      </h2>
      <p className="mt-1 text-[13px] text-muted">
        <Mono wrap>
          <Untrusted text={link.a.path} />
        </Mono>{' '}
        with{' '}
        <Mono wrap>
          <Untrusted text={link.b.path} />
        </Mono>
      </p>
      <p className="mt-3 text-[15px] leading-6" data-testid="sentence">
        <Untrusted text={link.sentence} />
      </p>

      <div className="mt-4">
        <DefinitionList
          rows={[
            { label: 'Kind', value: LINK_TYPE_WORDS[link.link_type] },
            { label: 'Used as', value: LINK_ROLE_WORDS[link.role] },
            { label: 'Confidence', value: formatPercent(link.score), mono: true },
            {
              label: 'Matches',
              value: `${formatInteger(link.affected_volume)} identifiers in the window`,
              mono: true,
            },
            {
              label: 'Direction',
              value: <Untrusted text={`${direction.glyph} ${direction.text}`} />,
            },
            {
              label: 'Typical delay',
              value:
                lag === null
                  ? 'not measured'
                  : lag === 0
                    ? 'at the same time'
                    : `${formatDuration(lag)} ${lag > 0 ? 'later' : 'earlier'}`,
            },
            {
              label: 'Looks like',
              value: (
                <Mono wrap>
                  <Untrusted text={`${link.a.top_shape ?? '—'} and ${link.b.top_shape ?? '—'}`} />
                </Mono>
              ),
            },
            { label: 'Status', value: <Status link={link} /> },
          ]}
        />
      </div>

      {canReview ? (
        decidable ? (
          <div className="mt-4 flex flex-wrap gap-2" aria-label="Decision">
            <Button
              variant="primary"
              hint="A"
              showHint={showHints}
              disabled={busy}
              onClick={() => onAction('accept')}
            >
              Accept
            </Button>
            <Button
              hint="R"
              showHint={showHints}
              disabled={busy}
              onClick={() => onAction('reject')}
            >
              Reject
            </Button>
            <Button
              hint="S"
              showHint={showHints}
              disabled={busy}
              onClick={() => onAction('association')}
            >
              Mark as association
            </Button>
          </div>
        ) : (
          <p className="mt-4 text-[13px] text-muted">
            This link has been decided; it is shown for the record.
          </p>
        )
      ) : (
        <p className="mt-4 text-[13px] text-muted">Your role can view links but not decide them.</p>
      )}
      {showHints && canReview && decidable ? (
        <p className="mt-2 text-2xs text-muted">
          Keys: A accept, R reject, S association, arrows move.
        </p>
      ) : null}
      {notice ? <Notice>{notice}</Notice> : null}
      <ErrorLine error={error} />

      <Disclosure summary="Details">
        <EvidenceList link={link} />
      </Disclosure>
    </aside>
  );
}

function Status({ link }: { link: LinkView }) {
  const word = LINK_STATUS_WORDS[link.status];
  const confirmed = link.status === 'accepted' || link.status === 'auto_accepted';
  return (
    <span>
      <span className={confirmed ? 'bg-hi px-1' : ''}>{word}</span>
      {link.reject_reason ? <span> ({REJECT_REASON_WORDS[link.reject_reason]})</span> : null}
      {link.decided_by ? (
        <span className="text-muted">
          {' '}
          by <Untrusted text={link.decided_by} /> on {formatTime(link.decided_at)}
        </span>
      ) : null}
    </span>
  );
}
