import * as RadioGroup from '@radix-ui/react-radio-group';
import { useId, useState, type SyntheticEvent } from 'react';

import { MAX_REASON_LEN, REJECT_REASONS, type RejectReason } from '../../api/types';
import { REJECT_REASON_HELP, REJECT_REASON_WORDS } from '../../lib/words';
import { Button, ErrorLine } from '../../ui/primitives';

export interface RejectValues {
  reason: RejectReason;
  note: string;
}

/** Reason required (spec 9.6), note optional and bounded. Validation is the form's own; the
 * API repeats it. */
export function RejectForm({
  onSubmit,
  onCancel,
  busy = false,
  error,
}: {
  onSubmit: (values: RejectValues) => void;
  onCancel: () => void;
  busy?: boolean;
  error?: unknown;
}) {
  const [reason, setReason] = useState<RejectReason | ''>('');
  const [note, setNote] = useState('');
  const [touched, setTouched] = useState(false);
  const idBase = useId();
  const missing = touched && reason === '';

  const submit = (event: SyntheticEvent) => {
    event.preventDefault();
    setTouched(true);
    if (reason === '') return;
    onSubmit({ reason, note: note.trim().slice(0, MAX_REASON_LEN) });
  };

  return (
    <form onSubmit={submit} noValidate>
      <p className="mt-1 text-[13px] text-muted">
        A reason is required. The link is remembered and not proposed again unless the evidence
        changes materially.
      </p>
      <fieldset className="mt-3" aria-describedby={missing ? `${idBase}-error` : undefined}>
        <legend className="label-cond mb-1">Reason</legend>
        <RadioGroup.Root
          value={reason}
          onValueChange={(value) => {
            setReason(value as RejectReason);
          }}
          aria-invalid={missing}
          aria-required="true"
          className="flex flex-col"
        >
          {REJECT_REASONS.map((value) => {
            const id = `${idBase}-${value}`;
            return (
              <div key={value} className="hairline flex items-start gap-3 py-2">
                <RadioGroup.Item
                  id={id}
                  value={value}
                  className="mt-0.5 inline-flex h-4 w-4 shrink-0 items-center justify-center rounded border border-ink bg-paper data-[state=checked]:bg-ink"
                >
                  <RadioGroup.Indicator className="block h-1.5 w-1.5 bg-hi" />
                </RadioGroup.Item>
                <label htmlFor={id} className="cursor-pointer">
                  <span className="block font-medium">{REJECT_REASON_WORDS[value]}</span>
                  <span className="block text-[13px] text-muted">{REJECT_REASON_HELP[value]}</span>
                </label>
              </div>
            );
          })}
        </RadioGroup.Root>
        {missing ? <ErrorLine text="Choose a reason before rejecting." /> : null}
      </fieldset>
      <div className="mt-3">
        <label htmlFor={`${idBase}-note`} className="label-cond block">
          Note{' '}
          <span className="font-normal normal-case tracking-normal text-muted">(optional)</span>
        </label>
        <textarea
          id={`${idBase}-note`}
          value={note}
          onChange={(event) => {
            setNote(event.target.value);
          }}
          maxLength={MAX_REASON_LEN}
          rows={3}
          className="mt-1 w-full rounded border border-ink px-2 py-1 text-[14px]"
        />
        <div className="text-right text-2xs text-muted">
          {note.length} / {MAX_REASON_LEN}
        </div>
      </div>
      <ErrorLine error={error} />
      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" variant="primary" disabled={busy}>
          {busy ? 'Rejecting…' : 'Reject link'}
        </Button>
      </div>
    </form>
  );
}
