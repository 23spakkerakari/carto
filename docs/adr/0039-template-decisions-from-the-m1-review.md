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
- Not built in package A2, still open in the plan: access-log routes that keep a segment only
  when it repeats, and methods limited to the RFC verbs (`parse/access_log.py` is unchanged);
  XML root names, template hints and generalized file names through the name gate; repetition
  counted once per record in templates, statistics and the classifier (names do this, ADR 0038
  section 4); one memory budget with least recently used eviction; and a per-system template cap
  beyond Drain3's 10,000 clusters.

## Tests

`edge/tests/test_edge_templates_review.py`, `edge/tests/test_edge_pii_review.py`,
`edge/tests/test_edge_parser_review.py`, `edge/tests/test_edge_runtime_review.py`.
