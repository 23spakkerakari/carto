# carto eval harness

Scores engine output against simulator ground truth and reports the spec Section 18.4 metrics.
In M0 there is no engine yet, so the harness scores the empty prediction (every metric n/a or
zero) and the ground truth against itself (every metric perfect). From M2 on the harness also
drives the engine end to end (`--engine`, below) or scores a prediction directory an engine run
handed it. Nothing here touches real customer data (spec 0.1 item 7).

## Run it

```sh
make eval SCENARIO=shop                      # writes eval/reports/shop.json, shop.md, history.ndjson
uv run carto-eval run --scenario shop --days 14 --seed 1 --sim-out sim-out/shop --out eval/reports \
    [--predictions DIR] [--regenerate] [--tolerance 2] [--enforce-targets] [--enforce-regressions] \
    [--daily-volume 800] [--engine memory|stores] [--reuse-bundle] [--config FILE]
uv run carto-eval self-check --sim-out sim-out/shop [--scenario shop] [--days 14] [--seed 1] \
    [--daily-volume 800]
make eval-engine SCENARIO=shop DAYS=14 ENGINE=memory   # the M2 path, no Docker needed
make eval-engine SCENARIO=people_ops DAYS=28           # scenario C needs 28 days; the caller passes DAYS
make fit-weights                                       # eval/weights/fitted.json (ADR 0049)
make bench-linker                                      # spec 17 scale, measured not gating
```

## The engine path (M2, `--engine`)

`carto-eval run --engine memory|stores` runs the whole map-discovery path instead of reading
`--predictions` (`carto_eval/driver.py`; plan M2 package I; ADR 0006, 0049):

1. simulate when `<sim-out>/ground_truth` is missing (the usual rules about mismatched
   parameters apply);
2. run the offline analyzer in process with `simulator/analyze.<scenario>.yaml`, the eval-only
   locator map and the run's reference time (`carto-sim as-of`) into `<sim-out>.carto`, state
   directory next to it; a previous bundle is replaced unless `--reuse-bundle` is given and it
   still verifies (the state directory is kept, so a re-analysis tokenizes identically);
3. discover: `memory` loads the bundle into the in-memory event store with the in-memory
   repositories and runs `carto_core.discover.discover` (one full profile pass, one full link
   pass with the composite matcher, at the run's reference time); `stores` runs
   `carto_core.discover.run_discover` against the ClickHouse and PostgreSQL named by the
   `CARTO_` settings. Either writes `engine_links.json`, `engine_entities.json` and
   `discover_summary.json` under `<out>/<scenario>.engine`, and the driver reads nothing else
   from the engine;
4. map engine fields (`system_id/template_id/path`) to truth fields
   (`system_id/source_id/field`) through the source that carries each template (the bundle's
   events joined to the locator map) and the flattened path (`carto_eval/mapping.py`; the pure
   path rules live in `carto_core.fieldpaths`, shared with `carto-core gate-report`): a dotted
   path flattens to the leaf name the truth uses unless the truth field itself is dotted, a
   map entry `parent.0.key` or a masked tail `parent.*` names `parent` (ADR 0030), CSV column
   names stand as emitted, case is preserved, a masked name (`*`, `*~1`) cannot be mapped;
5. apply the acceptance policy (ADR 0049 item 2) and write `<out>/<scenario>.predictions` with
   the reference writer, score with the usual `score()`, write `<out>/<scenario>.json` and
   `.md` (the Markdown gains the policy statement, a "Field mapping" section and an "Engine
   run" section), `<out>/<scenario>.mapping.json` (the mapping report and the discover summary
   with the settings in effect) and the history line.

The policy, stated in every report: the product keeps auto-accept off; in an eval run every
engine proposal at or above `LinkerSettings.proposal_threshold` whose role is `transaction`,
exact or bridge, counts as accepted for family building, so entity purity measures the engine
and not a simulated reviewer. Families are connected components over fields
(system, source, field), whatever forms the links use, as the engine's are
(`KeyLink.is_usable_for_families`); each member is reported once, as `raw` when a link reaches
it as `raw` and else in the form most of its links use; a bridge of role `batch` or
`association` (a file to its shipments) joins no family (spec 9.6, 9.7). Composite links count
only toward the composite metrics; association role links are reported but join no family; an
exact link whose role is `batch` is reported as a batch key link (truth type `batch`). Two more
rules come from the truth's vocabulary: the truth knows no templates, so an engine link between
two templates' copies of one source field is counted (`same_field_pairs`) and not scored, and a
second link of the same field pair from another template is a `template_duplicate`; and the
truth names one form pair per link, so per field pair the engine link with the most `raw` ends
(then the highest score) stands as the prediction and the rest are counted as `form_variants`
(zero since the linker itself proposes one link per field pair). The field-mapping section
lists every truth link field
no engine field reached (a naming mismatch), every truth link field reached but touched by no
prediction (a miss), every tokenized engine field the truth does not name, and every engine
field that could not be mapped, so nothing is silently scored as a miss.

`carto-eval fit-weights` (plan M2 decision 4) runs the analyze, load, profile, candidate and
verify steps on the named scenarios, labels every verified candidate by truth after mapping
(exact and bridge field pairs are positives; a pair of one field with itself is left out), fits
`sklearn.linear_model.LogisticRegression(class_weight="balanced", C=1.0, max_iter=1000)` on
`carto_core.linker.score.feature_vector` and writes a `Weights` file
(`fitted-<date>-<sha8 of the samples>`) that `CARTO_LINKER__WEIGHTS_FILE` can point at; it
prints precision and recall at the proposal threshold on the training samples. scikit-learn is
an eval-only dependency; a test greps `core/` for it.

`carto-eval bench-linker` builds 50 systems x 20 candidate fields with real sketches, times LSH
candidate generation and a capped verification against the in-memory store, and prints both
times with the per-pair cost extrapolated to the candidate cap (spec 17; ADR 0048). It is
informative, not a gate.

`carto-core discover --out DIR [--bundle PATH] [--as-of TIME]` and `carto-core gate-report
--predictions DIR --confirmed links.csv` are the engine-side pieces: the first runs the same
discovery over the deployment stores (loading the bundle first when given; the reference time
defaults to the bundle's last observation plus one second, else the store's latest write) and
writes the same three files; the second lists, for each link the partner confirmed by hand
(`system_a,field_a,system_b,field_b`), the engine's top 3 proposals by score x affected volume
for either field and whether the confirmed partner is among them, ending with the share found
(the M2 gate asks for at least half).

`run` generates the scenario with `carto_simulator.api.generate` when `--sim-out` holds no
`ground_truth/` or when `--regenerate` is given, and never otherwise, because generating empties
`--sim-out`. An existing ground truth whose manifest differs from the request on a parameter the
command can set (scenario, days, seed, daily volume) is refused with exit code 2 and a message
naming the difference: pass `--regenerate` to replace it or the matching parameters to score it.
One generated with other simulator-only options (`--start-date`, `--no-faults`, `--noise-rate`,
`--pii-density`) is scored as it is, with a note on stderr, so a fault-free run can be scored for
the false-alert row. An incomplete ground truth (an event stream shorter than the manifest's
per-source counts, what an interrupted generation leaves) or an unreadable one is refused with
exit code 2 as well. The command then loads the ground truth, loads `--predictions` (or the empty
prediction when the option is absent or the directory does not exist), computes every metric,
writes `<out>/<scenario>.json` and `<out>/<scenario>.md`, appends one line to
`<out>/history.ndjson`, and prints the Markdown report. Exit code 0; 1 when `--enforce-targets`
is given and a metric fails its target, or `--enforce-regressions` is given and a metric regressed
beyond the tolerance; 2 for a usage error, an invalid request, a ground truth that cannot be
scored, a prediction file that cannot be read, an output directory the simulator refuses to empty,
or a report directory that cannot be written.

`self-check` scores `Predictions.from_truth` (the perfect prediction) against the truth and returns
0 only when no metric fails and transaction F1, entity purity, link precision and recall (exact
and bridge, and composite), batch precision and recall and fault recall are exactly 1.0. One of
those may read n/a only when the truth holds nothing of that kind: a two-day scenario has no
injected fault, so fault recall cannot be measured there. On the default 14-day run every one of
the fifteen metrics is defined and passes.

`eval/reports/` is gitignored output. CI runs `make eval SCENARIO=shop DAYS=14 SEED=1`, appends
`eval/reports/shop.md` to the job summary and keeps the directory as an artifact.

From Python:

```python
from carto_eval.predictions import Predictions
from carto_eval.scoring import load_truth_events, score
from carto_simulator.ground_truth import read_ground_truth

truth = read_ground_truth(sim_out)
events = load_truth_events(sim_out)
card = score(truth, events.membership, Predictions.load(prediction_dir))
card.values()          # metric id -> value or None
card.faults.faults     # per-fault results: matched alert, time to detect
card.false_alerts      # false alert counts, fault-free days and flows behind the rate
```

## What the engine must emit

A prediction is one directory with up to six files, all UTF-8. Every file is optional: a missing
file leaves that part empty, so an M2 engine that only discovers links is scored on links alone.
Models live in `carto_eval/predictions.py`; they reject unknown keys, require timezone-aware
timestamps (normalized to UTC) and scores between 0 and 1. A malformed file is an error naming
the file and the line or item, never a partial load. `Predictions.write(dir)` is the reference
writer.

`links.json`, one object per key link the engine accepted or proposes (spec 9.3 to 9.5).
`link_type` is `exact`, `bridge`, `composite`, `batch` or `association`; `role` is `transaction`,
`association` or `batch`; `rank` (optional, any integer) is the position in the engine's review
queue and is not scored. A composite link is reported on its primary field pair.

```json
[
  {
    "a": {"system_id": "sys_webstore", "source_id": "src_webstore_log", "field": "cart_id"},
    "form_a": "raw",
    "b": {"system_id": "sys_orders", "source_id": "src_orders_log", "field": "cart_id"},
    "form_b": "raw",
    "link_type": "exact",
    "role": "transaction",
    "score": 0.97,
    "rank": 1
  }
]
```

`entities.json`, one object per entity family (spec 9.6): the (field, form) members of one
connected component over accepted transaction links.

```json
[
  {
    "entity_id": "fam_01J9W2K4M8ZQ1R5T7V9X0B2D4F",
    "fields": [
      {"ref": {"system_id": "sys_webstore", "source_id": "src_webstore_log", "field": "cart_id"}, "form": "raw"},
      {"ref": {"system_id": "sys_warehouse", "source_id": "src_wms_db", "field": "order_ref"}, "form": "digits.0"}
    ]
  }
]
```

`txn_membership.ndjson`, one JSON object per line: a ground-truth locator key and the engine's
transaction id for that record, or `null` when the engine left it alone (noise, or an event it
never placed). Keys must be unique within the file.

```
{"key": "src_webstore_log:app-2026-09-23.ndjson:line:3", "txn_id": "01J9W2K4M8ZQ1R5T7V9X0B2D4F"}
{"key": "src_wms_db:purchase_orders:row:1", "txn_id": "01J9W2K4M8ZQ1R5T7V9X0B2D4F"}
{"key": "src_webstore_log:app-2026-09-23.ndjson:line:1", "txn_id": null}
```

`batches.json`, one object per batch key the assembler detected (spec 9.7): the key value as it
appears in the data and the engine's transaction ids linked to it (only the key value is scored).

```json
[{"key_value": "SHIP_20260923_2112.csv", "txn_ids": ["01J9W2K4M8ZQ1R5T7V9X0B2D4F"]}]
```

`alerts.json`, one object per alert opened during the run (spec 10). `expectation_kind` is the
kind of the expectation that fired (`hop_deadline`, `schedule`, `volume`, `error_rate`,
`freshness`, `schema_drift`); `target` the expectation target in the engine's own words;
`is_visibility_gap` is true when the alert reports the product's own blind spot (spec 10.2);
`likely_causes` lists cause kinds per spec 10.4, best first (`visibility_gap`, `auth_failure`,
`upstream_miss`, `schema_drift`, `error_spike`, `partial_attribute_lift`, `new_templates`);
`affected_txn_ids` are the engine's transaction ids. `alert_id` must be unique within the file:
the fault metrics are keyed by it.

```json
[
  {
    "alert_id": "alr_01J9W2M0C3Q7X1T5V9Z2B4D6F8",
    "expectation_kind": "schedule",
    "target": "src_ship_sftp:SHIP_*_*.csv",
    "opened_at": "2026-10-02T01:30:00Z",
    "resolved_at": "2026-10-03T01:27:12Z",
    "affected_txn_ids": ["01J9W2K4M8ZQ1R5T7V9X0B2D4F"],
    "is_visibility_gap": false,
    "likely_causes": ["error_spike", "upstream_miss"],
    "system_id": "sys_shipping"
  }
]
```

`manual_hops.json`, one object per hop with its manual score (spec 11.1); `confirmed` is the
reviewer's decision when there is one (`true`, `false` or `null`) and, when there is one, it
decides whether the hop is scored as manual, whatever the score.

```json
[{"from_node": "payments:authorized", "to_node": "warehouse:po_created", "score": 0.82, "confirmed": null}]
```

Node names are `<system>:<event_type>` as the ground truth spells them (`webstore:cart_created`,
`orders:order_created`, `payments:authorized`, `warehouse:po_created`, `shipping:file_arrived`,
and so on; the simulator README lists them all). An engine names nodes `(system, template)` by
default (spec 9.8); the run driver maps them to these labels before scoring.

## The join contract (ADR 0006)

Native simulator files carry nothing beyond what the real system would log. Every emitted record
has a locator key `<source_id>:<locator>`:

- `<source_id>:<file name>:line:<n>` for a line in a log file (1-based within that file),
- `<source_id>:<table>:row:<primary key>` for a database row (`src_wms_db:purchase_orders:row:1`),
- `<source_id>:file:<file name>` for a file arrival (`src_ship_sftp:file:SHIP_20260923_2112.csv`).

`ground_truth/event_txn.ndjson` maps each key to the true transaction (`null` for noise), the true
node, the true observed time and the batch. The engine knows events by the `event_id` the edge
assigns; in eval mode (M1) the edge writes `locator_map.ndjson` (locator key to `event_id`) next to
its output, and whoever drives the engine run translates its memberships back to locator keys
before writing `txn_membership.ndjson`. No locator reaches core, and nothing in the native data is
correlatable with the truth.

Transaction ids are the engine's own everywhere in the prediction files. The harness resolves the
ids in `affected_txn_ids` through `txn_membership.ndjson` (an engine transaction maps to the true
transactions of its member records) before matching alerts to injected faults; an id no membership
record carries is compared literally, which is how `Predictions.from_truth` scores itself.

## Metrics and targets (spec 18.4)

| Id | Row | Definition (`carto_eval/metrics.py`) | Target |
| --- | --- | --- | --- |
| `link_precision_exact_bridge`, `link_recall_exact_bridge` | Exact and bridge link precision / recall | Set overlap of unordered (field, form) pairs with `link_type` in {exact, bridge} on both sides; direction and role are ignored | >= 0.95 / 0.90 |
| `link_precision_composite`, `link_recall_composite` | Composite link precision / recall | Same over `link_type` composite, compared on the primary pair | >= 0.85 / 0.70 |
| `entity_purity` | Entity family purity | Size-weighted mean over predicted families of the largest share of distinct members that belong to one true entity; a member of no true entity counts against purity; n/a without families | >= 0.95 |
| `transaction_pairwise_f1` | Transaction pairwise F1 | Over keys with a true transaction: pairs together on both sides versus pairs together on either; an unplaced key is its own singleton; computed from the contingency table, n choose 2 per cell | >= 0.95 |
| `batch_key_precision`, `batch_key_recall` | Batch key detection | Set overlap of batch key values | >= 0.95 / 0.90 |
| `fault_detection_recall` | Injected fault detection recall | A fault is detected by an alert with its `expected_alert_kind` (when set), opened within [start - 2 min, (end or start + 1 day) + 2 min], sharing an affected transaction when the fault lists any; the earliest match wins; over faults with `expected_alert` true | >= 1.00 |
| `time_to_detect_p95_seconds` | Time to detect after deadline | Nearest-rank p95 of `opened_at - start` (floored at zero) over detected faults | <= 120 s |
| `false_alerts_per_flow_day` | False alerts on fault-free days | Alerts matching no expected fault (alerts raised for the clock skew included) whose `opened_at` falls on a fault-free day, per entity per fault-free day. The run is `days` UTC calendar days from the manifest's `start_date`; a day is faulty when the window [start, end or start + 1 day] of a fault with `expected_alert` true touches it, so the clock skew (`expected_alert` false) never makes one faulty; n/a without fault-free days or entities | <= 1.00 |
| `visibility_gap_attribution` | Visibility gap correctly attributed | Share of visibility-gap faults whose matched alert has `is_visibility_gap` and that no non-gap alert overlaps while sharing affected transactions | >= 1.00 |
| `manual_hop_precision`, `manual_hop_recall` | Manual hop precision / recall | Over (from_node, to_node): a predicted hop is manual when `confirmed` is true, or when `confirmed` is null and `score >= 0.7`; a dismissal (`confirmed` false) never counts, whatever the score | >= 0.80 / 0.80 |
| `likely_cause_top1` | Likely cause top-1 accuracy | Over detected faults with an `expected_cause_kind`: share whose matched alert ranks it first | >= 0.70 |

A metric with nothing to score is `null` in JSON and `n/a` in Markdown, never 0 or 1. The table
lives in `carto_eval/targets.py`; `MetricTarget.evaluate` gives `pass`, `fail` or `n/a`.

## Report, history and regressions

`<out>/<scenario>.json` holds `scenario`, `seed`, `days`, `generated_at` (UTC, from the CLI's
clock), `predictions_present`, `counts` (events, transactions, links, entities, batches, faults,
alerts), `metrics` (id to `{label, value, target, comparison, unit, status}`) and `regressions`;
sorted keys, two-space indent, LF. `<out>/<scenario>.md` is a heading, the run facts, one table
(label, value, target, status) and the regressions.

Every `run` appends `{scenario, seed, days, generated_at, predictions_present, metrics}` to
`<out>/history.ndjson` and compares each metric with the previous record of the same scenario,
seed and day count (a run over other data is not comparable). A ratio regresses when it moves in
the bad direction by more than the tolerance in points
(`--tolerance 2` is 0.02); seconds and alerts per flow-day by more than one unit; a metric that
was measurable and is now n/a regresses. Regressions are listed in both reports;
`--enforce-regressions` turns them into exit code 1, as `--enforce-targets` does for failed
targets. CI keeps the history so PRs that touch the engine can report deltas.

## Security notes

The harness reads local files only (`--sim-out`, `--predictions`, `<out>/history.ndjson`) and
writes only under `--out`, plus under `--sim-out` through the simulator when it generates. It makes
no network calls, keeps no log and executes nothing from its inputs: prediction files and ground
truth go through `json` and pydantic with unknown keys rejected and UTF-8 required. Reports, the
history and stdout carry metric values, counts and scenario parameters only, never identifier
values, PII or ground-truth transaction ids; an error message names the offending file and item.
`markers.json` (the spec 18.3 leak-test input holding every PII and identifier value) is loaded as
part of the ground truth but never used, copied or written by the harness.

## Tests

`uv run pytest eval/tests` (file prefix `test_eval_`). `test_eval_driver.py` runs the engine
path in memory on shop (3 days, 200 orders a day) and people_ops (3 days) through the real
analyzer with the regex PII detector; `test_eval_mapping.py` covers the path rules and the
policy on hand-built parts; `test_eval_fit_weights.py` fits on a small shop run, checks the
file with `load_weights` and greps `core/` for scikit-learn. Metric tests use tiny hand-built inputs
with the expected numbers derived in comments; the CLI tests generate a two-day scenario with 30
orders a day in a temporary directory, score the empty prediction (n/a or zero everywhere), run the
self-check (perfect), score a written-out perfect prediction, and check the history, the regression
to the empty prediction, the tolerance, refused parameter changes, incomplete ground truth,
malformed prediction files and usage errors. One test (marked `slow`, a few seconds) runs the
self-check on fourteen days, where all six injected faults are present and every metric is
measured.
