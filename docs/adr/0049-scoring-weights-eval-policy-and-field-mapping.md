# ADR 0049: Scoring weights are versioned configuration; how the eval harness counts links and maps fields

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decisions 4, 5 and 6; spec 9.3, 18.4)

## Context

Spec 9.3 ships hand-set logistic weights in v1 and fits them with logistic regression on
simulator ground truth once the harness exists; core must never depend on scikit-learn. The
eval harness scores entity families, but the product keeps auto-accept off, so without a
policy no link is ever "accepted" in an eval run and purity is undefined. The engine names a
field `system_id/template_id/path`; the truth names it `(system_id, source_id, field)`.

## Decision

1. **Weights.** `core/carto_core/linker/weights.py` holds the shipped weights as a versioned
   `Weights` model (features, coefficients, intercept, `version`, `fitted_on`).
   `LinkerSettings.weights_file` points at another set when the customer or the eval harness
   has one. `carto-eval fit-weights --scenario shop,people_ops` fits a logistic regression with
   scikit-learn on the `PairFeatures` the engine computed over simulator truth and writes a
   weights file; the eval report names the weights version it ran with. Core never imports
   scikit-learn.
2. **Eval acceptance policy.** In an eval run, proposed links at or above
   `LinkerSettings.proposal_threshold` (0.5) whose role is `transaction`, and bridges, count as
   accepted for family building; composite links count only toward the composite link metrics,
   never toward families (spec 9.5: never auto-accepted). The report states the policy so purity
   measures the engine, not a simulated reviewer.
3. **Field mapping.** The eval driver maps engine fields to truth fields through the source
   that carries the template (the bundle's `fields.json` and `templates.json` name the source
   per template in eval mode) and the flattened path, after the edge's name masking. The report
   lists every engine field that matched no truth field and every truth field that no engine
   field matched, so a naming mismatch is visible, never silently scored as a miss.

## Alternatives

- Fit weights inside core at install: scikit-learn in the service image and training data
  from customer links the engine would then depend on; declined.
- Count every proposed link as accepted: association links would merge families that the
  product never merges (spec 9.6).

## Consequences

- An engine change that moves a metric must run `make eval` and report the deltas (CLAUDE.md).
- The partner gate uses the same mapping report to explain a miss.
