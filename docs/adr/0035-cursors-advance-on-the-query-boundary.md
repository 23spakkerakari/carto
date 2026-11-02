# ADR 0035: Connector cursors advance on the query boundary, not on the records found

Status: accepted by the founder, 2026-10-12 (spec 8.1.3, 8.1.4, Appendix A). Changes the spec's
SQL query template and the Splunk window field.

## Context

The review found that cursors only moved when a read returned records:

- **Splunk** commits `window_end` on the last record of a window. A search that matches nothing
  for longer than one read's span re-searches the same span on every poll, forever.
- **Splunk** filters on event time (`earliest_time`, `latest_time`). An event indexed more than
  the two-minute overlap after its own timestamp, such as forwarder backlog, is never read.
- **SQL** advances to the largest watermark on the page. When more rows share one value than a
  page holds, the next query (`> :watermark`) skips the rest of them.
- **SQL** DATE and NUMERIC watermarks never advance: the initial cursor is a datetime, and a
  Decimal comes back from JSON as a string.
- **PostgreSQL** compares a naive UTC watermark in the session time zone.
- Rows committed late by long transactions fall behind the watermark and are missed.

## Decision

1. **Checkpoints.** A connector may yield a `CursorCheckpoint(cursor)` between records. The
   ingestor commits it once every earlier record is durable in the buffer, as it does for a
   record's `commit_cursor`. Checkpoints carry no event data.
2. **Splunk on index time.** Windows filter on `index_earliest` and `index_latest`, which only
   move forward. A window ends no later than `now - settle_seconds` (default 60). Each fully
   searched window is followed by a checkpoint, even when it was empty. `earliest_time` is set
   to `window start - max_event_lag` (default 24 hours) only to bound the buckets Splunk scans.
   An event whose own time is older than that lag is not read; the lag is configurable per
   source.
3. **SQL closed ranges.** For time-typed watermarks (timestamp, timestamptz, datetime, date) the
   template becomes
   `WHERE <col> > :watermark AND <col> <= :upper ORDER BY <col> LIMIT :batch`. `:upper` is the
   database's own clock (`CURRENT_TIMESTAMP` read at the start of the poll) minus
   `settle_seconds` (default 30), so edge clock skew does not matter and rows from transactions
   that commit within the settle delay are still read. When the range is fully read, the cursor
   moves to `:upper` with a checkpoint, even if the range was empty. Integer and decimal
   watermarks keep the two-parameter template and advance on rows found; with no clock, a quiet
   period skips nothing.
4. **Ties.** On a full page, the cursor stops at the largest value strictly below the page
   maximum and the trailing tie group is read again; duplicates collapse by `event_id`. A page
   that is a single tie group is re-run with a doubled `:batch`, up to 16 times the configured
   batch, and then fails with a connector error that names the query.
5. **Typed cursors.** A watermark is stored as `{"t": "datetime" | "date" | "int" | "decimal",
   "v": <ISO text or digits>}`. A source sets `watermark_type`; the initial value is the epoch,
   `0001-01-01` or `0` to match. A full page whose watermark cannot be compared is a connector
   error, never a warning.
6. **Session time zone.** PostgreSQL sessions set `TimeZone=UTC`, as MySQL already does.

## Alternatives

- A keyset cursor `(watermark, primary key) > (:wm, :pk)`: exact for ties, but it needs the
  primary key of every custom query and a third parameter for every dialect. The tie re-read
  gives the same result with the template the spec already describes.
- `>=` instead of `>`: re-reads forever once a tie group fills a page.
- A synthetic "window done" record: pollutes event data and the analyzer.

## Consequences

- Spec 8.1.3 and Appendix A gain a note that points here.
- The simulator's SQL source configuration gains `:upper`.
- Rows committed later than `settle_seconds` after their own timestamp are still missed; the
  setting is documented for sources with long transactions.
