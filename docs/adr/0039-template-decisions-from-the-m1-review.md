# ADR 0039: Template and detector decisions from the M1 review (work package A2)

Status: proposed, 2026-10-15 (plan `docs/plans/M1-review-fixes.md`, package A2; spec 2.3
invariants 2, 7 and 8, 7.1, 8.2, 8.3). Refines ADR 0016 (templates) and ADR 0020 (detector).
Records the choices made while fixing the template and repetition findings. The founder reviews
it with the package; section 3 holds a trade-off that needs the founder's answer.

## 1. Assigned values are masked before Drain3 mines a line

**Context.** Drain3 keeps a token as a template constant once it repeats. A line that logged
`password=CorrectHorseBatteryStaple` four times turned the password into part of the template
text, which leaves the edge as a name (invariant 2, ADR 0027).

**Decision.** `mask_tokens()` (`pipeline/templates.py`) rewrites the tokens Drain3 sees:

- A token `key=value` or `key:value` (`_KEY_VALUE`, key 1 to 64 characters) whose key holds a
  letter and no digit or `@` is mined as `key=<*>`. The value half stays a parameter, read back
  from its offset, so `user=svc_sync` still yields a `param_N` field. `10:30` has no letter in
  its key and is masked whole as before; `https://x` becomes `https:<*>`.
- A key that names a secret (`_SECRET_WORDS`, else `is_secret_name`, which defers to the product
  log rule of ADR 0010) is masked and is not a parameter: the value never reaches the
  classifier, which could not tell it from any other value. This covers `key=` and
  `"api_key":"..."`.
- A bare secret word, or a token ending in `=` or `:` whose key is a secret, masks the next
  token the same way; an HTTP auth scheme (`basic`, `bearer`, `digest`, `negotiate`, `ntlm`,
  `token`) after it keeps the masking armed for the credential that follows. Bare `token` and
  `session` are not trigger words; they count only as keys.
- A token that looks like a secret by shape (`looks_like_secret`: JWT, PEM, AWS key, a
  high-entropy run) is mined as `<*>` and stays a parameter, so classifier rule 1 drops the
  field by its shape.

**Consequence.** Parameter positions change for lines with `key=value` tokens, so their
`param_N` field refs change once. Template state keeps `STATE_VERSION = 1`; templates mined
before the change are not migrated and their ids differ from those mined after it (ADR 0016's
"ids survive restarts" holds again from the first restart on the new build).

## 2. Template redaction also masks secret-shaped tokens

**Decision.** After `truncate_text` and before the detector, `redact.py` replaces every
whitespace-delimited token that `looks_like_secret` with `SECRET_MASK = "<SECRET>"` and counts it
in `entities_masked`. For a mined template this is a second check; for `template_hint`, an XML
root name and an access-log template it is the only secret check, since those do not pass
through `mask_tokens`.

## 3. Open: secret words also mask the word after them in prose

**Context.** Section 1's trigger rule masks the token after a bare secret word, whether or not
it is a credential. Checked on the current code:

| Line | Mined as |
| --- | --- |
| `authorization failed for user` | `authorization <*> for user` |
| `password reset sent` | `password <*> sent` |
| `Bearer token expired` | `Bearer token <*>` |

The error lexicon of spec 8.2 (`failed`) and the credential-failure rule of spec 10.4
(`token (expired|invalid)`) lose the word they match on.

**Question for the founder.** Keep the rule (no credential ever becomes a constant, at the cost
of these words), or mask the next token only when it is not a lexicon word or looks like a value.
Until answered the rule stays as built, which is the side that cannot leak.

## 4. Persistence and identifiers survive lone surrogates

**Context.** A lone surrogate in one line made every later template flush fail, and `mine()`
with it.

**Decision.**

- `TemplateStore._write` writes JSON with ASCII escapes. A failed write (`OSError`,
  `ValueError`) is logged as `template_state_write_failed` with the system id and error type,
  its temporary file is removed, and the pipeline goes on. Files written before the change still
  load. The state is written again at the next flush that has new input.
- `compute_template_id` hashes with `surrogatepass`, so a template holding a surrogate has an id;
  valid text gives the same id as before.
- Detector input goes through `_utf8()` in `pii.py`, which replaces lone surrogates with U+FFFD.
  This holds even if a surrogate arrives some other way than the decode boundary of ADR 0038.

**Known limit.** Escapes make non-ASCII state up to six bytes per character, so a large file can
grow past `MAX_STATE_BYTES` (64 MiB); it is then ignored on load and mining starts fresh. Nothing
checks the size when writing.

## 5. Shutdown runs every step

**Decision.** `EdgePipeline.flush` saves the field names even when the template flush fails.
`EdgeRuntime.flush` saves the field statistics and, in gateway mode, runs housekeeping (ADR 0042)
even when an earlier step fails. `EdgeRuntime.close` runs flush, the last audit anchor
(ADR 0033), then closes templates, vault, audit file and secret resolver through an
`ExitStack`. Every step runs; the last error propagates with the earlier ones chained as its
context.

## 6. Smaller decisions

- **IBAN scan in linear time.** The fallback pattern with nested quantifiers backtracked
  (about 290 ms per 4 KB call). `_IBAN_RUN` now finds a candidate run once and `_iban_end()`
  picks the longest valid end from the group lengths, followed by mod-97. Matches are identical
  to the old pattern; a property test compares the two.
- **Parser error logs name the record kind.** `parse_internal_error` logged the locator prefix,
  which for an upload is the customer's file name (invariant 7). It now logs
  `record_kind=raw.kind.value`, a fixed set.

## Consequences

- `template_id` is computed before redaction (ADR 0016), so an emitted `template_text` holding
  U+FFFD or `<SECRET>` no longer hashes to its id. Only tests recompute ids today, from mined text.
- Not built in package A2, still open in the plan: repetition counted once per record in
  templates, statistics and the classifier (names do this, ADR 0038 section 4); one memory
  budget with least recently used eviction; and a per-system template cap beyond Drain3's
  10,000 clusters. Access-log routes that keep a segment only when it repeats, methods limited
  to the registered verbs, and XML root names, template hints and generalized file names
  through the name gate were built on 2026-11-02 (amendment below).

## Tests

`edge/tests/test_edge_templates_review.py`, `edge/tests/test_edge_pii_review.py`,
`edge/tests/test_edge_parser_review.py`, `edge/tests/test_edge_runtime_review.py`.

## Amendment 2026-11-02: the three unmined template channels get the mined template's rules

**What was open.** Section 2 and the Consequences above left three ways for customer text to
reach `template_text`, and so core, without the name gate or the digit masking a mined template
gets: the XML root element name (`<ORDER4471/>` or `<customer_alice/>` was the template
constant of the record), the access-log request line (the method was attacker text and
`generalize_path` masked digits, UUIDs and hex only, so `GET /customers/alice 2xx` travelled
with the name), and a connector's `template_hint` (`generalize_name` masks digit runs only, so a
file named after a person travelled in `file_arrived <name>`; a webhook event type travelled
as typed). The security review of 2026-11-01 listed them as known and deferred; the core
backstop of ADR 0069 refuses the worst shapes (an e-mail address, a run of six digits, a
secret assignment) but a plain name passes it.

**What the channels do now.** Every word of the three channels goes through
`RecordParser._constants` before it is a template constant, and that method applies, in
order, what a mined template gets:

1. `mask_tokens` (section 1): a token with a digit or an `@`, the value of a `key=value`
   token, a secret by key, word or shape, a PEM block and a URL with userinfo become `<*>`.
2. The value-shape rule of field names, as the stricter of ADR 0027's `safe_segment` and the
   miner's token rule: any digit, any `@` or more than 64 characters masks the word whole
   (`parse/common.py`, `is_value_like` and `template_word`). `parse/xml.py` returns the root
   name as `XmlRecord.template` with this rule already applied; `parse/access_log.py` applies
   it (with the UUID and 16-hex-character rules) per route segment in `generalize_path`, so
   `/orders/4471/items` reads `/orders/<*>/items` and `/api/v2/carts` reads `/api/<*>/carts`.
   The mask is the template placeholder `<*>` on every channel now, not `*` (ADR 0069's
   contract passes `<*>`; `generalize_name` of file names still writes `*` inside a token,
   `SHIP_*_*.csv`, which is then one word of the hint).
3. The access-log method is a constant only when it is one of `HTTP_METHODS` (RFC 9110, PATCH,
   the WebDAV, versioning, ACL, CalDAV, binding, search, link and QUERY methods, as the IANA
   registry spells them, case-sensitive); anything else is `<*>`; an absent method stays `-`.
4. The name gate's trust rule (ADR 0038): a word is a constant once three distinct records
   carried it under the same prefix and the detector does not flag it. The register is reused
   as it is: the parser gates the words as the field path
   `<channel>.<word 1>.<word 2>...` with one prefix per word, under the parents `$xml`,
   `$route` and `$hint` (`$hint.file_arrived`, `$hint.file_removed` and `$hint.row_change`
   are trusted from the first record: those verbs are the product's, what follows them is the
   customer's), through `FieldNameStore.apply` with the record's id, so a record counts once
   however often it is delivered and the analyzer's frozen second pass counts nothing. A word
   is in clear only when the register returns its prefix with the word itself at the end; a
   masked label, a map entry (a prefix with 256 distinct words under it) and a dropped
   secret-named path all read as `<*>`. Counting under the channel's own parent keeps a route
   vocabulary and a record's field names from ever making each other a map, and the prefix
   tree keeps `alice` under `/customers` apart from `alice` under `/users` and lets
   `/customers/<*>/orders` keep its last word once it repeats. The register is per system,
   so the sources of one system share a route vocabulary, as they share field names.

Without a name store (parser unit tests only; the pipeline always passes one) rules 1 to 3
apply alone, as for field names.

**Trade-off.** Root names, route segments and hint words are parameters until trusted: the
first two records of every XML root, route and generalized file name travel as `<*>`,
`GET /<*>/<*> 2xx` or `file_arrived <*>`, and a route whose words never repeat in three
records never has any. A word three distinct records carry is trusted, so `/customers/alice`
reads `/customers/alice` from the third request on unless the detector flags `alice` (Presidio
may, for a first name; the regex detector of `pii.enabled=false` knows no names, ADR 0020); that is the name gate's rule for a JSON key, applied here on purpose, and
the leak test plants each probe in two records for that reason. Each distinct route prefix
adds a parent to the system's register (bounded by `MAX_PARENTS`, 4096; past it new words are
masked), and a record puts at most 32 words of a channel through it. Access-log template ids
change once (`*` to `<*>`); XML and hint ids change for the first two records of each word.
Section 2's remark that the redactor is the only secret check for these channels no longer
holds; it is the second check now, as for a mined template.

**Evidence.** `edge/tests/test_edge_parse_xml.py` (the root's template word),
`edge/tests/test_edge_parse_access_log.py` (the allowlist, the route rules),
`edge/tests/test_edge_parser.py` (trust after three records, re-delivery, the detector's
flag, the prefix tree, the hint verbs, the no-store mode) and `edge/tests/test_edge_leak.py`,
whose `_plant_probes` adds an XML source, an access-log source and a file source to each
scenario's analyzer configuration and plants a marker in a root name, a route segment, a
request method and a file name, two records each, and asserts none crosses. The pipeline
test `test_template_text_is_redacted_and_counted` now expects `row_change <*> <*>` for a hint
carrying an e-mail address and no redaction count, since the address is masked before the
redactor. Measured on 2026-11-02 (`make eval-engine`, memory engine, shop, 14 days, seed 1,
114,468 events, the corpus of ADR 0068's after-run): the spec 18.4 table is identical (the
five link and family rows pass, the rest are M3 and later as before), 19 engine links stored
with 17 scored, 13 usable for families and 1 composite as before; 205 engine fields with 66
tokenized and 16 in pass-1-only templates against 196, 59 and 0 before, the difference
being the fields of the `<*>`, `row_change <*>` and `file_arrived <*>` templates that the
first two records of each root, table and file name get in pass 1 (pass 2 runs on the
frozen register, where every word trusted in pass 1 is in clear from the first record).
