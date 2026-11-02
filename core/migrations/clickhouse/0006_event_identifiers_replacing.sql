-- event_identifiers: exploded identifier forms, the join workhorse (spec 7.2), replacing
-- duplicates (ADR 0034). The sort key carries event_id, field_ref and form: without them two
-- events sharing a token in the same millisecond would merge into one row. There is no
-- version column. Duplicates are identical. One statement per file. {events_days} is the
-- configured retention (spec 14.10: identifiers follow events).
CREATE TABLE IF NOT EXISTS event_identifiers (
  tenant_id LowCardinality(String),
  token String,
  field_ref LowCardinality(String),   -- system_id/template_id/field
  form LowCardinality(String),
  shape LowCardinality(String),
  event_id String,
  system_id LowCardinality(String),
  observed_at DateTime64(3, 'UTC')
) ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMMDD(observed_at)
ORDER BY (tenant_id, token, observed_at, event_id, field_ref, form)
TTL toDateTime(observed_at) + INTERVAL {events_days} DAY;
