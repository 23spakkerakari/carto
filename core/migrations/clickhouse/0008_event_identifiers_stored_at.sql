-- ADR 0046: stored_at on event_identifiers, as 0007 adds it to events. The engine's queries
-- take the earliest stored_at of a row's copies, so a redelivered batch is never new again for
-- a watermark. This table has no ingested_at, so rows written before this migration read their
-- observed_at, which the first incremental pass after the upgrade covers. One statement per file.
ALTER TABLE event_identifiers ADD COLUMN IF NOT EXISTS stored_at DateTime64(3, 'UTC') DEFAULT observed_at;
