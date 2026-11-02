-- events: one row per canonical event (spec 7.2), replacing duplicates (ADR 0034). A row is a
-- duplicate when its whole sort key matches, and the key ends with event_id, so a batch or
-- record delivered twice merges into one row, the one with the latest ingested_at. Queries
-- that need exact counts read with FINAL. One statement per file. {events_days} is the
-- configured retention (spec 14.10), rendered by carto_core.migrations, and apply_retention
-- rewrites it on change.
CREATE TABLE IF NOT EXISTS events (
  tenant_id LowCardinality(String),
  event_id String,
  source_id LowCardinality(String),
  system_id LowCardinality(String),
  kind LowCardinality(String),
  observed_at DateTime64(3, 'UTC'),
  ingested_at DateTime64(3, 'UTC'),
  observed_at_quality LowCardinality(String),
  template_id LowCardinality(String),
  severity LowCardinality(Nullable(String)),
  attributes Map(LowCardinality(String), String),
  actor_token Nullable(String),
  actor_kind LowCardinality(Nullable(String))
) ENGINE = ReplacingMergeTree(ingested_at)
PARTITION BY toYYYYMMDD(observed_at)
ORDER BY (tenant_id, system_id, template_id, observed_at, event_id)
TTL toDateTime(observed_at) + INTERVAL {events_days} DAY;
