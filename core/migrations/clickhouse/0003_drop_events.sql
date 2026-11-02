-- ADR 0034: events becomes a ReplacingMergeTree. M1 has no installation to preserve, and an
-- applied migration is never edited, so the MergeTree table of 0001 is dropped here and
-- recreated by 0004. One statement per file.
DROP TABLE IF EXISTS events SYNC;
