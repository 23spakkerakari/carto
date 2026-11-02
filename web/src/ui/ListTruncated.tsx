import { LIST_LIMIT, LIST_MAX_PAGES, useListTruncated, type ListName } from '../api/hooks';
import { formatInteger } from '../lib/format';
import { Notice } from './primitives';

/** A plain line, rendered only when a whole-list read (`useSystems`, `useSources`) stopped at
 * its page bound: the screen shows the first part of the list and names of the rest fall back
 * to raw ids. */
export function ListTruncated({ list }: { list: ListName }) {
  const truncated = useListTruncated(list);
  if (!truncated) return null;
  return (
    <Notice>
      <span data-testid={`list-truncated-${list}`}>
        List truncated: only the first {formatInteger(LIST_LIMIT * LIST_MAX_PAGES)} {list} are
        loaded; the rest are not shown and appear by id.
      </span>
    </Notice>
  );
}
