-- txn_events: transaction membership (spec 7.2), append-only with a version per assignment
-- (ADR 0060, plan M3 decision 2). The sort key includes txn_id, so a moved event keeps its
-- old row under the merged-away transaction: a reader that starts from an event takes
-- argMax(txn_id, version), a reader that starts from a transaction reads survivors only.
-- stored_at is core's wall clock at the insert (ADR 0046). Partitioned by month: the
-- assembler's micro-batches span days, never a hundred partitions. One statement per file.
-- {txn_membership_days} is the configured membership retention (spec 14.10), at least the
-- event retention.
CREATE TABLE IF NOT EXISTS txn_events (
  tenant_id LowCardinality(String),
  txn_id String,
  event_id String,
  node_id LowCardinality(String),
  observed_at DateTime64(3, 'UTC'),
  version UInt64,
  stored_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(observed_at)
ORDER BY (tenant_id, txn_id, observed_at, event_id)
TTL toDateTime(observed_at) + INTERVAL {txn_membership_days} DAY;
