# ADR 0068: Template constants need distinct messages; the classifier ratchets

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, package E; spec 2.3
invariants 2, 3 and 7, 8.2 item 6, 8.3 rules 5 and 6, 18.3)

The security review of 2026-11-01 found three ways a value could leave the edge in clear
without any rule being broken on paper, and a gap in what the leak test could see. This ADR
records the decisions package E took to close them. Every item is small; they share one
invariant and are recorded together.

## 1. A template token is a constant only once three distinct messages share it

Drain3 keeps a one-member cluster's message verbatim as its template, which ADR 0016 and the
template store's `MIN_CLUSTER_SIZE` guarded by using a cluster's template only from its third
member. Members were counted, not messages: a retry loop logging `Refund approved for Alice
Smith by manager` three times made a three-member cluster, and `mask_tokens` masks only
tokens with a digit or an `@`, `key=value` values and secrets, so the name travelled to core as
template text (invariant 2).

The store now keeps, per cluster, a bounded list of short digests (BLAKE2b, 6 bytes, at most
`MAX_DISTINCT_DIGESTS` = 8) of the distinct whitespace-normalised messages it has seen, and
uses the mined template only once the list holds `MIN_CLUSTER_SIZE` entries. Identical repeats
count once; until then every token is a parameter and the template is all `<*>`. The digests
are persisted with the cluster in the plain-JSON state (`"d"`, state versions 2 and 3); a
version 1 file loads with every list empty, so its clusters earn their constants again from
distinct messages rather than carrying a possibly verbatim template over. The frozen replay of
ADR 0017 reads the same lists. The original message is never in the state.

The digest is of the original message, not of the masked one: `order 7 created` and `order 8
created` are two distinct messages whose shared words are legitimately constants. The review of
2026-11-02 asked for the masked form instead, so that `Refund approved for Alice Smith attempt
1`, `... attempt 2` and `... attempt 3`, which differ only in a token that is a parameter from
the first sighting, would count once; measured on the shop corpus that rule left every
warehouse export line all-parameters for good (`SFTP upload complete <*> <*> bytes)` varies
only in masked tokens, as the commonest log lines do) and gave every all-parameter line of one
length one template id, which breaks the map and error-spike detection. It was not adopted.

**The constant filter** closes the review's case instead. When a cluster first reaches
`MIN_CLUSTER_SIZE` distinct messages the store hands the template's constant tokens (every
token that is not `<*>`, the key of a `key=<*>` token included), joined with spaces, to the
`constant_filter` it was built with (`TemplateStore(constant_filter=...)`). The runtime builds
the store with `pii_constant_filter(detector)` over the edge's PII detector: the constants may
travel unless the detector reports any entity (PERSON, LOCATION, e-mail, phone, government id,
card, IBAN, medical licence, every class the classifier treats as PII) at or above
`pii.score_threshold` (0.5 when the detector carries no settings, the classifier's rule). On a
hit the cluster is marked `pii` and stays all parameters for good; a filter that raises is a
hit (the store fails closed and logs the cluster id, never the words); without a hit the
template is used as before. The verdict is taken once, at the cluster's first promotion (or at
the first use of a cluster a version 2 file promoted), never per message, and is persisted with
the cluster (`"p"`, state version 3). A bare `TemplateStore()` without a filter allows every
template, which keeps the unit tests and the behaviour before this rule.

Two shapes join the secrets that `mask_tokens` never turns into parameters, because the
classifier could not tell them apart from an ordinary value once they were a field: every
token of a PEM armour block (from a token holding `-----BEGIN` to the closing dashes after
`-----END`, or the end of the message) and a URL with userinfo (`postgres://svc:hunter2@db`).

**Trade-off.** A message whose every sighting is identical never gets constants: its words are
parameters, become `param_k` fields and reach core only when the classifier's keep rules let
them (quarantine, then rule 6 with the detector pass and the three-sightings rule of
`Classifier.keeps`). Such a template reads as `<*> <*> <*>` in the console and its words as kept
low-cardinality attributes, which costs some readability on fully static lines (health checks
and the like) and nothing on lines that vary. The detector is the control for a name inside an
otherwise constant line: a retry loop whose only change is a counter promotes the line, and
only the detector's PERSON or LOCATION hit keeps the name out of the constants. With
`pii.enabled=false` the regex detector of ADR 0020 knows no names, so such a loop can still
promote a name (an e-mail address, a phone number or a card number in the same position is
still caught); pure identical repeats never promote anything, whatever the detector. The eval of
both scenarios records the effect (plan, package E; "Engine counts" below).

## 2. Cardinality and detector hits ratchet

Rule 5 computed `high_cardinality` from the live distinct estimate and the live non-null count
at every recheck, and rule 6's detector pass looked at the reservoir. A field that was an
identifier could therefore fall to `keep` when a flood of one constant value grew the non-null
count past the ratio and filled the reservoir with the flood value, and a field that had
carried PII could do the same once the reservoir had forgotten it.

`FieldStats` carries two sticky flags, persisted in the snapshot (version 2; a version 1
snapshot loads with both false) and set through `FieldStatsStore.note`, which nothing unsets:

- `high_cardinality_seen`, set the first time rule 5's test is true after quarantine. Rule 5
  treats the field as high cardinality when the live test or the flag says so; a decision that
  only the flag carries has the reason `identifier:ratchet`.
- `detector_hits_seen`, set whenever the detector hit a sample. Rule 6 drops the field
  (`low_card:pii_hits`) while it is set.

Admin pins are unchanged: a pin to `keep` on a non-PII class still wins (pins are read before
rules 2 to 8), and PII classes still cannot be pinned to keep. A genuine enum that was never
high cardinality and never carried a hit becomes `keep` as before.

## 3. The inventory masks every configuration string

`strip_config` dropped secret-bearing keys and nested structures but passed other strings
through, so a `url` with userinfo, an owner e-mail or a token under a benign key reached core
in `sources.config_json`. Every string value and list item now goes through the log redactor's
`mask_string` (URL userinfo, e-mail, JWT, AWS key id, PEM, high-entropy runs, secret
assignments, identifier shapes), the same masks the product logs use (invariant 7), so the
inventory and the logs can never disagree about what a value looks like. A string is masked
whole and cut to `MAX_CONFIG_VALUE_LEN` afterwards: the first cut masked the cut string, so a
password whose `@` sat past the cut left in clear up to it (review of 2026-11-02). Core applies
the same mask on its side (ADR 0069).

## 4. The simulator plants secrets and the leak test runs both scenarios

`markers.json` gains `secret_values`: an AWS access key id, a bearer token, a `password=`
assignment in prose, a PEM header with a one-line key body and a DSN with userinfo (and its
password), drawn from the scenario RNG at the first secret noise line (after every
transaction, so nothing before phase D changes) and planted verbatim into 5% of the noise lines
of every log source of both scenarios; the payments XML source of `shop` carries its line as a
`<note>` element of the heartbeat document, the others in their message field. The same line
repeats, as a retry loop would log it, which is also item 1's case. The leak test scans the new
category like the others, plants a
thrice-repeated probe message of its own, and runs the small end-to-end run over `shop` and
`people_ops` (two days each) instead of `shop` alone.

## Consequences

- `test_edge_templates_review.py` held two tests written against the old rule
  (`test_a_template_with_a_lone_surrogate_has_an_id`, which mined one message three times and
  expected constants, and `test_a_repeated_password_never_becomes_a_template_constant`, whose
  four lines differed only in the timestamp the parser consumes); the same commit varied their
  messages by a word. The rest of the file passes unchanged under the constant filter.
- Template state files written before this change load: version 1 with constants to be earned
  again, version 2 with its digests and every promoted cluster judged by the filter at its next
  use. Field statistics snapshots of version 1 load with both flags false.
- A persisted template state grows by at most eight 12-character digests and one boolean per
  cluster.
- The constant filter costs one detector call per cluster promotion (Presidio loads its model
  at the first call, as it does for the classifier), nothing per message. The gateway's state
  file keeps a `pii` cluster's Drain3 tokens as before (the state is edge-local and always held
  the words of every cluster); only what leaves the edge changed.
- Measured on 2026-11-02 (`make eval-engine`, memory engine, seed 1): every spec 18.4 metric
  is unchanged on both scenarios (shop all targets; people_ops exact and bridge recall 0.80 as
  before). The bundles show item 1's trade-off: shop 28 kept, 103 tokenized, 21 dropped fields
  before against 99 kept, 73 tokenized, 24 dropped after; people_ops 16/73/6 against 76/41/12.
  The new kept fields are the words of static noise lines (`health check ok`), which travelled
  as template constants before and as kept low-cardinality attributes now; the fields that
  existed only in pass 1 templates (59 and 29) are gone.
- `simulator/README.md` lists `secret_values` next to the other `markers.json` categories (the
  same commit).
- Engine counts. The spec 18.4 table is unchanged on both scenarios while the engine internals
  moved: shop tokenized fields 103 to 73, engine links 25 to 19, pass-1-only fields 59 to 0;
  people_ops tokenized fields 73 to 41. The before and after corpora differ because the
  simulator changed in the same commit (shop 114,326 against 114,515 events), so the deltas mix
  the engine change with the corpus change. Old engine on the new corpus: (filled in by the
  integration run).
- Not built: a per-template console hint that a template is all-parameters because its message
  never varied (readability item for M3), and a `secret_values` scan in `carto-eval` (the eval
  does not read `markers.json`, so the new category changes nothing there).
