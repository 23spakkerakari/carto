-- ADR 0034: event_identifiers becomes a ReplacingMergeTree with the event in its sort key. The
-- MergeTree table of 0002 is dropped here and recreated by 0006. One statement per file.
DROP TABLE IF EXISTS event_identifiers SYNC;
