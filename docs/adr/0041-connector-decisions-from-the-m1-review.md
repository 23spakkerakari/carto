# ADR 0041: Connector decisions from the M1 review (work package C)

Status: proposed, 2026-10-15 (plan `docs/plans/M1-review-fixes.md`, package C; spec 2.3
invariants 1 and 8, 8.1, 8.1.3, 8.1.4, Appendix A). Records the choices made while building
ADR 0035 (cursors on the query boundary) and the Splunk part of ADR 0037 (allowlists), and while
fixing the other connector findings. It does not repeat what those ADRs decide. The founder
reviews it with the package; section 9 lists where the build departs from ADR 0035.

## 1. Cursor-only checkpoints

**Decision.** ADR 0035's checkpoint is
`CursorCheckpoint(source_id: str, cursor: Mapping[str, Any])`, a frozen dataclass in
`pipeline/model.py`; `ReadItem = RawRecord | CursorCheckpoint` is what `ReadConnector.read`
yields and `IngestorLike.ingest` takes (backfill still yields records only). The ingestor
(`Ingestor._place_checkpoint`, `pipeline/batch.py`) skips the pipeline and only sets the
candidate cursor, so the existing rule applies: the cursor commits after the batch that holds
everything before it is acknowledged. A checkpoint after a refused batch, or for an unknown
source, is ignored. The scheduler does not count checkpoints as records; the offline analyzer
and the bench skip them. Records no longer carry `commit_cursor`.

## 2. Splunk windows on index time

**Decision.** In `connectors/splunk.py`:

- The cursor is `{"window_end": <iso>}`. Each poll reads `[window_end, upper)` with
  `index_earliest` (inclusive) and `index_latest` (exclusive), where `upper` is now minus
  `settle_seconds` (default 60, 0 to 3600), truncated to whole seconds. A cursor never moves
  back: when `upper` is not after it, nothing runs and no checkpoint is sent.
- `earliest_time` and `latest_time` are still sent, only to bound the index scan: window start
  minus `max_event_lag_hours` (default 24, 1 to 9600) and window end plus one day
  (`FUTURE_TOLERANCE`, the future bound of ADR 0036).
- Without a cursor the first window starts `backfill_days` before `upper`, else one
  `window_minutes`. A checkpoint follows every window, so a quiet search still advances.
- Backfill and the one-minute connection test stay on event time.

## 3. How the Splunk allowlist reads a search

**Decision.** `connectors/splunk_spl.py` checks a search at config load:

- Stages split on unquoted `|`. Double quotes quote; a backslash escapes the next character;
  single quotes do not quote, as in SPL.
- Every unquoted `[...]` subsearch is checked the same way, to a depth of 8. The first stage is
  an implicit `search` unless the text starts with `|`; inside brackets the first word is a
  command unless it reads as a search term.
- Refused outright: macros (backquotes, which expand to anything), unterminated quotes,
  unbalanced brackets, empty stages and NUL.
- Beyond ADR 0037 item 4's list, the read-only commands include the combining ones (`append`,
  `appendcols`, `appendpipe`, `format`, `join`, `multisearch`, `return`, `union`); their
  subsearches are checked too. Read capabilities are a fixed set plus the `list_` and `get_`
  prefixes. Errors name only the refused command.

As ADR 0037 says, no setting widens either list.

## 4. Splunk reads are bounded and backfill streams

**Decision.** Result lines are split from the byte stream as it arrives (`MAX_LINE_BYTES`,
4 MiB); a longer line is dropped while streaming, without being held, and counted as skipped.
JSON responses are read up to `MAX_JSON_BODY_BYTES` (4 MiB). Backfill runs at most
`max_concurrency` chunk searches (default 2, 1 to 8), each feeding a queue bounded at 1,000
records, and yields chunks in order; a failed chunk raises in its place and cancels the rest.

**Alternatives.** Reading `response.content` and checking its size after: the whole body is in
memory before the check.

## 5. Which Splunk statuses are retried

**Decision.** 429, 500, 502, 503 and 504 are retried three times with jittered backoff
(0.5 s base, 5 s cap); `Retry-After` is not read. The retries run inside one circuit-breaker
call, so a request that spends its retries counts as one breaker failure. 401, 403, 404 and other
statuses are not retried and do not count as breaker failures; the caller raises
`ConnectorError`. An open breaker is reported as a `ConnectorError`, so the connection test shows
it instead of raising.

## 6. A cancelled half-open probe frees the probe slot

**Decision.** `CircuitBreaker.call` catches anything that is not an `Exception` (cancellation)
and calls `release_probe()`, which clears the probe flag and counts neither success nor failure.
Half-open still admits exactly one probe. Before, a cancelled probe left the breaker refusing
every call for good.

## 7. SQL cursors are typed, and the bound comes from the database clock

**Decision.**

- `SqlQueryConfig.watermark_type` is `datetime` (default), `date`, `int` or `decimal`
  (`connectors/sql_watermarks.py`). The cursor stores `{"t": <type>, "v": <text>}`. A stored
  type that differs from the configured one is refused (the operator resets the cursor on
  purpose). An untyped cursor from the first M1 build is read as the configured type: an ISO
  string for the time types, a JSON integer for `int` and `decimal`; a float is refused. Errors
  name types, never values.
- New setting `watermark_timezone` (IANA, default `UTC`): row values with an offset are
  converted into it, and `:upper` is computed in it.
- `:upper` is the database clock minus `settle_seconds` (default 30, 0 to 86400), read once per
  poll and only when some query is time-typed: `CURRENT_TIMESTAMP` on PostgreSQL,
  `UTC_TIMESTAMP(6)` on MySQL, `SYSUTCDATETIME()` on SQL Server. ADR 0035 names
  `CURRENT_TIMESTAMP`; the UTC functions avoid the server's zone on MySQL and SQL Server. A
  naive clock value is UTC. For `date` the bound is yesterday, so a day is read once it is over.
- Time-typed templates must bind `:upper` and integer ones must not; the error shows
  `WHERE <column> > :watermark AND <column> <= :upper`. Spec 8.1.4, Appendix A and
  `docs/install/compose-pilot.md` carry the new form. When the clock cannot be read, the
  connection test fails a `database clock` check and skips the time-typed queries.
- PostgreSQL sessions get `-c TimeZone=UTC` in the libpq `options`, next to the read-only and
  timeout settings, so a `timestamptz` cursor is never shifted by the server's zone (MySQL
  already pins `+00:00`).

## 8. Tie-safe pages

**Decision.** On a full page only rows up to the last watermark value that is not the trailing
tie group are emitted; the tie group is read on the next page. When the whole page is one tie
group, it is read again with a doubled batch, up to 16 times the configured batch, then fails;
the next page returns to the configured batch. Rows out of ascending order, a full page with NULL
watermarks, or a value of the wrong type on any page fail the poll. A checkpoint is sent only when
the cursor moved.

## 9. Departures from ADR 0035, for the founder's review

- Item 1: the checkpoint carries `source_id` as well as the cursor.
- Item 3: MySQL and SQL Server use their UTC clock functions; the `date` bound and
  `watermark_timezone` are new.
- Item 4: the tie group is never emitted on the full page, so no duplicates arise for
  `event_id` to collapse.
- Item 5: stricter than an error on an incomparable full page only (section 8).

## Consequences

- `watermark_type` defaults to `datetime`, so an existing integer or decimal watermark query
  without `watermark_type` now fails `:upper` validation at load. The operator adds the type.
- A legacy cursor with an offset is decoded to UTC while new rows convert to
  `watermark_timezone`; the two agree only when the zone is UTC.
- `sql_watermarks.py` has no unit tests of its own; date, int and decimal decoding, the zone,
  batch doubling, the 16-fold limit and the order check are untested.
- Not built in package C, still open in the plan: ADR 0037 items 1 to 3 (the SQL statement
  allowlist still uses `DENIED_FUNCTIONS`; no SQL Server `sys.servers` or database-scope write
  check; MySQL grants through roles, and an unparsed grant line still counts as no finding);
  MySQL connecting to the validated address; SFTP ignoring the user's SSH configuration; upload
  confinement to its base directory, without following links or junctions; and the CSV field size
  limit with per-row errors skipped.

## Tests

`edge/tests/test_edge_batch.py`, `test_edge_scheduler.py`, `test_edge_conn_splunk.py`,
`test_edge_conn_splunk_spl.py`, `test_edge_net_retry.py`, `test_edge_conn_sql_check.py`,
`test_edge_conn_sql_mssql.py`, `test_edge_conn_sql_pg.py` (Docker only).
