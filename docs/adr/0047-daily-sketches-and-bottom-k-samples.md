# ADR 0047: Field sketches are kept per day and merged into the window

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 2; spec 7.3 `field_sketches`,
9.1)

## Context

Spec 9.1 keeps a MinHash signature, a HyperLogLog, a bottom-k sample and the counts per
`(field_ref, form)` over a rolling 7-day window "rebuilt nightly, updated incrementally every
5 min". A sketch over a sliding window cannot forget the day that dropped out; keeping one
sketch per day and merging the last seven makes the nightly rebuild and the incremental update
the same code path.

## Decision

- `field_sketches` is keyed by `(tenant_id, field_id, form, day)` and holds `count`, the shape
  and length histograms, the MinHash (128 permutations, `datasketch`), the HyperLogLog, the
  bottom-k sample and the most frequent token hashes with their counts. The incremental pass
  reads rows with `stored_at` past the watermark (ADR 0046), groups them by day and merges them
  into the day's sketch; the nightly rebuild recomputes the days of the window from scratch.
- The window sketch is the merge of the day sketches in the window: MinHash and HyperLogLog
  merge natively; histograms add; the bottom-k sample is the `k` (default 2,048) smallest 64-bit
  hashes (`xxhash`-free: the first 8 bytes of SHA-256 over the token, so every process agrees)
  and merges by taking the smallest `k` of the union.
- Two bottom-k samples give an exact containment estimate of their union's smallest `k`, which
  the candidate generator uses to rank LSH candidates before the store join (ADR 0048).
- A bottom-k sample that holds fewer than `k` hashes is the whole token set, so the distinct
  count is exact there and the HyperLogLog estimate is used only once the sample is saturated
  (added 2026-10-18 after the first eval: a source with exactly 50 distinct values in the
  window must not fall under the `min_distinct` floor on an estimate's error).
- `q(field)` combines the distinct ratio, the top-shape share, the length stability and the
  low value concentration exactly as spec 9.1 lists them; the weights of the combination and
  the floors (`0.4`, 50 distinct) are `ProfilerSettings`.

## Alternatives

- One sketch per field, rebuilt every five minutes: the full 7-day read every pass; at the spec
  17 scale (50 systems, 1,000 candidate fields) that is the whole table every five minutes.
- Hourly sketches: eight times the rows for no precision the engine uses.

## Consequences

- A field's first day in the window is partial until the day ends; `q` is computed over the
  merged window, so a new field needs the `min_distinct` floor before it is a candidate anyway.
- Changing `minhash_permutations` or `bottom_k` invalidates stored sketches; `sketch_version`
  on the row makes the rebuild drop the old ones.
