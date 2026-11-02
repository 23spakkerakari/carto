# ADR 0069: Core refuses value shapes at the contract and masks them before the provider

Status: accepted, 2026-11-02 (plan `docs/plans/security-fixes-2026-11.md`, package C, findings
M3 and M4; spec 2.3 invariants 2, 5 and 7; spec 7.1, 9.9, 14.3)

## Context

The edge tokenizes or drops every high-cardinality value before it forwards an event (spec 2.3
invariant 2), and the LLM gateway receives allowlisted metadata only (invariant 5). Core's side
of that contract was thin. `AttributeValue` had a length cap only, `TemplateText` refused a run
of six or more digits and nothing else, so an email address, a UUID, a long hex id, a JWT, a
cloud key, a PEM block or `password=hunter2` in an attribute value or a template would have
been written to ClickHouse `attributes` and Postgres `templates.template_text`, shown in the
console and, for template text, sent to the LLM provider. The bundle loader's
`BundleField.path` and `BundleTemplate.template_text` were plain strings. The LLM payload
builder's `clean_text` masked carto token shapes only. The inventory's `secret_ref` was a plain
string, so a literal secret in that slot would have been stored in `sources.secret_ref`, and a
connector configuration value with a password in a DSN would have reached `sources.config_json`.

All of these are backstops: the edge already does the right thing. A backstop exists for the
edge that misclassifies a field, the bundle a customer edits by hand, and the reader that
returns a value by mistake.

## Decision

1. **Refuse at the contract.** `carto_schema.event.value_shape` names the first value shape in a
   text, and the `AttributeValue` and `TemplateText` types carry an `AfterValidator` that refuses
   a text with one. `BundleField.path` is a `FieldName` and `BundleTemplate.template_text` a
   `TemplateText`, so the bundle loader (`read_bundle_metadata`) refuses `templates.json` and
   `fields.json` under the same rules as `/internal/ingest`. The JSON Schemas are unchanged: the
   rule is a validator, not a pattern.
2. **Mask before the provider.** `carto_schema.event.mask_value_shapes` writes the template
   placeholder `<*>` over every shape; `carto_core.llm_gateway.payload.clean_text` applies it
   to every text of a payload (template text, system name, field path) after the control and
   format characters are removed, so a zero-width character cannot split a shape past the mask.
   An attribute value that carries a shape is dropped from the payload rather than masked (a
   bare placeholder tells the model nothing); a top shape that is not its own shape and carries
   one is dropped too.
3. **Inventory.** `InventorySource.secret_ref` is a `SecretRef` (`<scheme>://<path>` with the
   spec 14.3 schemes, so `hunter2` is a 422); a config key that names a secret under the
   logging processor's rule (`config_key_is_secret`, the twin of
   `carto_common.logging.is_secret_key`) is refused; a config string value, alone or in a list,
   is under the value-shape rule. Hostnames, ports, schedules, `local:///` paths, key file
   paths, DSNs without a password and Splunk searches still pass. Refusal rather than masking
   keeps the inventory contract consistent with the event contract and leaves
   `carto_core.metadata` untouched.

### The shapes

In the order they are tried; the words are what the error message says.

| Shape | Rule |
| --- | --- |
| a PEM header | `-----BEGIN` (masked through `-----END ...-----` or to the end) |
| a JWT | `eyJ` + base64url, dot, base64url, dot, base64url |
| a carto token | `t<1-4 digits>.<22 base64url>` (`carto_schema.forms.TOKEN_PATTERN`, unanchored) |
| an AWS access key id | `AKIA` or `ASIA` + 16 upper-case alphanumerics |
| a URL with a password | `://user:password@`, the user name possibly empty (`redis://:pw@`); the password is the shape, and `://carto:<*>@` is already masked and passes |
| a secret-named key with a value | a word of `carto_common.logging.SECRET_KEY_WORDS` or pair of `SECRET_KEY_PAIRS`, `=` or `:`, a value that is not `<*>`, a `[MASK]` or empty; a quoted or braced value runs to its closing character however long (the review of 2026-11-02 found a 512-character bound that let a longer quoted value through) |
| an email address | local part, `@`, domain with a dotted TLD (a DSN user name does not match) |
| a UUID | 8-4-4-4-12 hex |
| a run of 16 or more hex characters | `[0-9A-Fa-f]{16,}` |
| a run of 6 or more digits | `[0-9]{6,}` (the earlier template rule, now shared) |

`approved`, `EUR`, `GET /orders`, `<*>`, `HTTP/1.1 200`, `DC-03`, ISO timestamps,
`password=<*>`, `Authorization: Bearer <*>`, `token: [REDACTED]`, `token_count=3`,
`secret_ref=vault://x`, `bypass=1`, `oauth=1`, `tokens=512` and `postgres://carto@db.internal`
pass and are the positive tests.

## Why refuse at ingest rather than mask

A masked value is silently lost and the edge that leaked it keeps leaking. A refusal is seen.
An edge of the same version validates every event against the same contract model before it
forwards it (`carto_edge.pipeline.pipeline`, which drops the record with reason `contract` and
logs `event_contract_violation` with the field locations, never the value), so a record that
carries a shape never leaves such an edge and core's 422 is the version-skew case: an older
edge whose contract model lacks the rule, or a non-Python edge. Then the 422 parks the batch at
the edge (spec 8.5, ADR 0036), the source's health shows it and the operator sees which
template or field the edge misclassified. The same holds for a hand-edited bundle and for an
inventory. The only place that masks is the LLM payload, which cannot refuse: it is built from
rows that were accepted already, and a provider call that fails over one text would stop the
whole task.

## Why the shapes are repeated in `carto_schema`

`carto-schema` depends on pydantic only, by design (ADR 0008; `carto_common.crypto` repeats
`TOKEN_PATTERN` for the same reason), and adding `carto-common` to it would have changed the
lock file under another package's edit. The shape patterns, the secret-key rule and the
`secret_ref` rule are therefore mirrored from `carto_common.logging` and
`carto_common.settings`, with the one difference the contract needs: a secret assignment whose
value is the placeholder `<*>` is a template constant, not a value.
`core/tests/test_core_llm_payload.py::test_the_schema_mirrors_agree_with_carto_common` holds
the two sides together on a corpus of samples, keys and references.

## Evidence

Both simulator bundles of 2026-11-02 (shop: 114,326 events, 21 templates, 152 field paths;
people_ops: 28,203 events, 15 templates, 95 field paths) pass the new rules without a refusal,
and the edge forward and inventory tests pass against the stricter contract.

## Consequences

- Version skew parks batches. A core that refuses a shape an older edge still emits (or a
  newer edge emits a shape this list has not learned to mask) answers 422 for the whole batch,
  and the edge parks it (ADR 0043): one value stops a batch, by design, until the edge is
  upgraded or the value is masked at the edge. Operators see it as a parked batch with the
  shape named, never the value. Roll the edge before the core when a release tightens the list.

- An edge that classifies a secret-looking constant as low-cardinality (`session: expired`,
  `token: invalid` in a log template) drops the record at its own pipeline (reason `contract`,
  counted and logged by location) when it runs this contract version, and is refused with a
  parked batch when it runs an older one; the template builder is the place to fix that, not
  the rule. The eval bundles carry no such template today.
- A six-digit low-cardinality code (a compact date, a branch number) in an attribute is refused;
  the edge classifies those as `date` or tokenizes them, and none appears in the eval bundles.
- The JSON Schemas do not express the rule; a non-Python edge must apply the same shapes.
- `read_bundle_metadata` raises `BundleError` naming the file and never the text; the
  `TypeAdapter(list[...])` instances it validates with carry `hide_input_in_errors` themselves
  (`core/carto_core/bundle.py`, the same commit), so the chained `ValidationError` does not
  render the refused text either.

## Not built

- `InventorySource.last_test`, `InventorySystem.name`, `owner_group` and the pin `reason` are
  free text the edge redacts; they are not under the shape rule. A refusal there would block a
  whole inventory for a message, which is the wrong trade for text that no one treats as data.
- Attribute keys are under the field-name rule (ADR 0027) but not under the secret-key rule;
  the edge drops `secret_like` fields and a key named `password` with a masked value carries
  nothing. The LLM payload does not drop such keys either.
- The console renders stored values as before; it never had a masking step and the contract now
  guarantees it needs none. Rows written before this change are not rescanned.
