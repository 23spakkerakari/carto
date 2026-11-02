# ADR 0038: Field name gate decisions from the M1 review (work package A1)

Status: proposed, 2026-10-15 (plan `docs/plans/M1-review-fixes.md`, package A1; spec 2.3
invariants 2, 3 and 8, 8.2 item 1, 8.3 rules 1 and 2). Amends ADR 0030; this is the "ADR 0030
amendment of 2026-10-12" that the code and `edge/tests/test_edge_names_review.py` cite. Records
the choices made while fixing the field name findings. The founder reviews it with the package.

## 1. Paths are tuples of segments; the dotted string is only a label

**Context.** `flatten()` joined keys with an unescaped dot and the gate split them again, and
`_is_index` took any one- or two-digit part for an array index. A map key `"dclfre.zebulon"` was
read as two levels and leaked as `balances.0.value.zebulon`; `{"stock": {"42": 1}}` was taken
for an array and never counted towards map detection.

**Decision.** A path is a tuple of `Segment = tuple[str, bool]` (the text and whether it is an
array index) until the gate is done (`SegmentPath`, `parse/common.py`). `Flattened.structure`
and the XML parser's `XmlRecord.structure` carry the segments of every path with more than one
segment; a path absent from it is one non-index segment, which covers logfmt, CSV and access
logs. `FieldNameStore.apply(..., *, structure=)` walks the segments and caches by `SegmentPath`;
`_is_index` is gone. Only the output name is joined with dots.

**Alternatives.** Escaping dots in keys: every consumer would have to unescape, and a missed
one would split a value again.

## 2. The gate checks the raw name for secrets and PII before masking it

**Context.** A masked name or a map entry reached the classifier as `*`, so spec 8.3 rules 1
(secret-named fields are dropped) and 2 (PII-named fields take their class) no longer applied:
`{"user_password": "Hunter1Hunter"}` under a masked name could be tokenized with its `raw`
form.

**Decision.** `_gate_path` tracks whether a path is hidden: masked, a map entry, or a trusted
name that is value-like. For a hidden path:

- `is_secret_name(raw_path)` true: the path is dropped in the gate.
- otherwise `pii_class_by_name(raw_path)` becomes a hint, carried in
  `GatedRecord.name_classes` and `ParsedRecord.name_classes` to
  `Classifier.decide(ref, path, *, name_class=)`. A hint of `FieldClass.SECRET_LIKE` acts as
  rule 1 and any other class replaces the name lookup of rule 2; pins are still checked between
  the two rules. A decision with a hint is neither read from nor written to the classifier
  cache, so a hint never leaks into another record's decision.

Names in clear are unchanged: the classifier still checks their path itself. `names.py` now
imports from `pipeline/classify.py` and `pipeline/model.py`.

## 3. Distinct hidden names never share a field within a record

**Context.** Under a masked prefix every hidden name became `*` and the first value won, so
unrelated values of one record shared one field and its statistics.

**Decision.** `_Record.mask` labels the first distinct raw name under a gated prefix `*`, the
next ones `*~1`, `*~2` and so on. Labels hold within one record only; there is no mapping across
records, so a label never identifies a name. A trusted but value-like segment is labelled the
same way and is not cached. `top_keys` still gets a plain `*` for every hidden key.

**Known limit.** The schema's digit-run rule (ADR 0043 section 6) and `safe_path` collapse
`*~100` and later labels back to `*`, so the 101st distinct hidden name under one prefix in one
record shares the first one's field again. Raising the limit needs a label that has no run of
three digits.

## 4. A record counts once towards trust, keyed by source and locator

**Context.** Spec 8.2 item 1 trusts a name after `MIN_NAME_RECORDS` (3) records. Each delivery
of the same record counted again, so one record read three times (a redelivered line, a
re-polled page) made its names trusted.

**Decision.** The parser passes `record_id=f"{raw.source_id}:{raw.locator}"` to
`FieldNameStore.apply`. Each pending name keeps the 16-byte BLAKE2b digests of the record ids
that counted for it (at most `MIN_NAME_RECORDS - 1`); an id already counted is skipped. The set
is dropped when the name becomes trusted or its parent a map. `record_id=None` counts every
call, as before.

**Alternatives.** The plan names `event_id`. `event_id` is derived from `observed_at`, source and
locator, and the gate runs before `observed_at` is final, so the gate uses the two parts it
has. A rotated file that reuses a locator counts more slowly, which fails safe.

## 5. Name register state version 2

**Decision.** `STATE_VERSION = 2` adds `records` (name digest to a sorted list of record-id
digests). Version 1 files load with no records. A register whose `records` names a digest that
is not counted, holds something that is not a digest, or holds more ids than the count is
ignored whole, like any other unreadable register.

## 6. Text is cleaned of lone surrogates where it is decoded

**Context.** `json.loads` keeps an escaped `\ud800`; every later UTF-8 encode raised (the gate
digest, the register flush, the PII detector), so one line could break every later flush.

**Decision.** `clean_text()` (`parse/common.py`) replaces `[\ud800-\udfff]` with U+FFFD and
returns ASCII text unchanged. It runs in `stringify`, on the keys and `top_keys` in `flatten`,
and in `RecordParser` on text lines, messages and template hints. The register also encodes its
digests with `surrogatepass` and writes ASCII JSON, in case a surrogate arrives some other way.

## Consequences

- ADR 0030's consequence that the first records' fields "collapse into `*`" now reads: they
  are labelled `*`, `*~1`, ... within each record (section 3). Its "State" section gains
  version 2 (section 5).
- Not built in package A1, still open in the plan: map detection that forgets over a window,
  is scoped per source and never runs at the root; configured names winning over the map check;
  deleting descendants of a parent that becomes a map; least recently used eviction of parents
  (the 4096-parent cap still masks new names); a size check on writing the register; value-like
  trusted names stored as digests (they are written in clear); and the provisional CSV header
  (`parse/csv.py` is unchanged).

## Tests

`edge/tests/test_edge_names_review.py` and `edge/tests/test_edge_names.py`.
