# ADR 0030: Field names travel only once they repeat; maps turn their keys into values

Status: accepted by the founder, 2026-10-11 (spec 8.2 item 1, 8.3, 2.3 invariant 2). Closes the
residual risk of ADR 0027 (JSON maps keyed by names). Amended by ADR 0038 (segment paths, secret
and PII checks on hidden names, per-record labels, state version 2).

## Context

Field names leave the edge in clear: attribute keys, identifier and dropped field names and the
`keys:` template (ADR 0016) all carry them, so the map in M2 can be built from them. Data that
uses values as keys (`{"stock": {"Alice Smith": 3}}`, a webhook keyed by customer) puts those
values into names. ADR 0027 masks name segments with a run of three digits, an inner `@` or more
than 64 characters. A key with none of these, such as a person's name, still travels in clear.

The founder approved map detection: a parent with many distinct child names is a map, and its
keys become values. Detection alone leaves a gap. It can only trigger after the edge has seen
the names, so the first keys under every map would leave in clear. The threshold also has to be
high, because polymorphic objects (a webhook `data` object whose fields differ by event type)
have a hundred or more distinct names and are not maps. The founder chose to close the gap with
a name gate as well (2026-10-11).

## Decision

**Name gate.** The parser keeps a per-system register of the names it has read from input, by
parent path. Array indexes count as `#`. A parent that is not trusted yet is keyed by a digest
of its name, so its children are counted from the first record, and the subtree moves under the
name once it is trusted. A name read from the input is used in clear only when both hold:

1. it has been seen in at least three records under the same parent (`MIN_NAME_RECORDS`, the
   rule ADR 0027 applies to kept values and template words);
2. the PII detector does not flag it read as words (`alice_smith` and `aliceSmith` become
   `alice smith`). The detector runs once per name, when the name reaches three records. The
   name as written is not checked: spaCy's recogniser reads a bare `order_id` as a person and
   `warehouse_code` as a location, while the spaced forms of every scenario A field name are
   clean and person names are still found.

Until then the segment is `*`, as an ADR 0027 mask would be. Some names are exempt and trusted
from the first record: those the configuration names (the source's timestamp, message, severity
and actor fields, the default message fields, and the paths of field policy pins), and the
names the parser itself generates (`param_<n>`, map entries below). The `keys:` template uses
the gated names.

**Map detection.** A parent with at least `classify.map_min_keys` distinct names (default 256,
configurable from 16) becomes a map, permanently. Each of its entries is written like an array
element: `<parent>.<i>.key` holds the key as a value, and the entry's content is under
`<parent>.<i>.value`, nested paths included. Entries are numbered in record order, and only the
first 20 are kept, as for arrays (spec 8.2 item 1). From then on the key is classified like any
value (tokenized as an identifier, dropped as a name) and never forms a field name.

**State.** Untrusted names are counted under a 16-byte BLAKE2b digest, so no raw value is
stored. Trusted names are stored in clear, since they travel anyway. A map keeps only its flag.
Each system keeps at most 4,096 parents, and past that limit every name under a new parent is
masked. The gateway persists the register per system next to the Drain3 state. The offline
analyzer builds the register in pass 1 and freezes it for pass 2, which counts nothing (as the
template store does, ADR 0027 addendum), so every record of pass 2 sees the names of the whole
input.

## Alternatives

- Map detection only: the first keys of every map, up to the threshold, leave in clear.
- Gate only: keys that repeat three times and that the detector misses (product names without
  digits) still leave, and map entries collapse into one masked field.
- A lower map threshold: polymorphic objects would lose their field names.

## Consequences

- In the gateway, the first two records of each new structure lose their field names, so their
  fields collapse into `*` and their `keys:` template differs. This is the same trade-off as
  ADR 0027's rare values. Restarts keep the register.
- A key that repeats three or more times before its parent reaches the map threshold, and that
  the detector does not flag, travels as a name until the parent becomes a map. That residual
  is accepted; the leak test plants name-keyed maps and recurring keys.
- Throughput: the gate costs about 6 microseconds per record once names are trusted (about 4%
  of pass 2 under the profiler). `make bench-gateway`, alternating between the commit before
  the gate (806b37c, its own environment) and the gate, 20 s each, on 2026-10-12:

  | Core | Before the gate (events/s) | With the gate (events/s) |
  | --- | --- | --- |
  | Performance, 4 pairs | 2,795, 3,648, 3,579, 3,203 (mean 3,306) | 3,102, 3,386, 2,550, 3,155 (mean 3,048) |
  | Efficiency, 2 pairs | 2,050, 1,136 | 2,016, 1,128 |

  Within a pair the difference is a few per cent; between pairs the laptop's load moved the
  figures far more (it had just come back to mains power from 3% charge, and another session
  was running). The efficiency-core figures are below ADR 0028's 2,398 for both builds, so the
  slow-core margin is not established under these conditions; the reference-node run decides.
  Each name's first PII check also loads the spaCy model earlier than before (once per
  process).
- Scenario A analyzer: no emitted event carries a masked name; fields and policies are
  unchanged (`fields.json` also lists fields seen only in pass 1, under `*`).
- `make eval` deltas are reported in the PR (M1: no engine output yet).
