-- ADR 0046: stored_at is the time core wrote the row (the writer's wall clock), the profiler's
-- incremental watermark. Rows written before this migration read their ingested_at, which only
-- makes the first incremental pass after the upgrade scan more. One statement per file.
ALTER TABLE events ADD COLUMN IF NOT EXISTS stored_at DateTime64(3, 'UTC') DEFAULT ingested_at;
