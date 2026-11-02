# carto simulator

Deterministic multi-system synthetic data with ground truth (spec Section 19). M0 shipped scenario A
("shop") in batch mode: native-format files per system plus `ground_truth/` for the eval harness
(Section 18.4). M1 added live mode, which replays a batch run into a directory in real time for
the Compose dev stack. Nothing here is real customer data (spec 0.1 item 7).

## Run it

```sh
make sim SCENARIO=shop DAYS=14 SEED=1            # writes sim-out/shop
uv run carto-sim generate --scenario shop --days 14 --seed 1 --daily-volume 800 \
    --start-date 2026-09-23 --out sim-out/shop [--no-faults] [--noise-rate 1.0] [--pii-density 0.3]
uv run carto-sim list
```

`carto-sim generate` prints a short summary and returns 0; an unknown scenario or an invalid
request prints a message on stderr and returns 2. The defaults of `GenerationRequest`
(`carto_simulator.api`) are the CLI defaults. From Python:

```python
from pathlib import Path
from carto_simulator.api import GenerationRequest, generate

result = generate(GenerationRequest(days=3, daily_volume=40), Path("out"))
result.manifest.counts  # per source, plus transactions, events, noise, files
```

The output directory is created when missing. An existing directory is emptied first, so stale
files never remain, but only when it is empty or holds a previous run
(`ground_truth/manifest.json`); any other populated directory, or a path that is a file, is
refused (`ValueError` from `generate`, exit 2 from the CLI) so a mistyped `--out` never wipes a
checkout.
The default run (14 days, 800 orders per weekday, about 115,000 records, 51 MB) takes 6 to 11 s
on a laptop and well under 1 GB of memory in pure Python.

## Live mode

```sh
uv run carto-sim live --scenario shop --days 2 --speed 60 --out /data/sim [--duration-seconds 3600]
```

`carto-sim live` generates the requested run once, in batch mode into a temporary directory, then
replays it into `--out`: every timestamp is shifted by one constant so that the first record lands
at the moment the command starts, and later records follow at `--speed` simulated seconds per
real second (the default, 60, plays a simulated minute per real second, so two days take about
48 minutes). Lines are appended to the same per-day file names batch mode uses and flushed after
every record, so a tailing collector (`otel/collector.dev.yaml`) sees them promptly. `SHIP_*.csv`
drops land at their arrival instant with the mtime set to it. The warehouse rows are streamed into
`warehouse/purchase_orders.ndjson`, one JSON object per row at its creation instant, because the
M1 Compose stack has no source database; `warehouse/purchase_orders.sql` is written whole up front
with the shifted timestamps and the CSV exports are not written in live mode.

`ground_truth/` is written before the replay starts and describes the complete run with the
shifted times, so an interrupted replay (Ctrl-C, SIGTERM or `--duration-seconds`) leaves a valid
directory whose manifest counts the whole run and whose `sha256` entries are those of the fully
replayed files. Locator keys, counts, links, batches and markers are those of the batch run. The
generated `README.md` gains a "Live replay" section with the shift and the exact
`carto-eval run` command for scoring the directory. Only scenario A supports live mode in M1.

`make dev` runs the `simulator` Compose service with these arguments into the `sim-data` volume
(`deploy/compose/compose.yaml`).

## Output layout

```
out/
  README.md                          generated: sources, counts, faults, determinism note
  webstore/app-YYYY-MM-DD.ndjson     one file per UTC day
  orders/order-svc-YYYY-MM-DD.log    logfmt, one file per UTC day
  payments/messages-YYYY-MM-DD.xml   one XML document per line, one file per local day
  warehouse/purchase_orders.sql      CREATE TABLE + INSERTs (+ ALTER TABLE ... RENAME COLUMN)
  warehouse/purchase_orders.csv      rows created before the rename (header uses po_num)
  warehouse/purchase_orders.renamed.csv   rows from the rename day on (po_number); faults on only
  warehouse/export-job-YYYY-MM-DD.log     unstructured text, one file per local day
  shipping/outbound/SHIP_YYYYMMDD_HHMM.csv   nightly file drops; mtime = true arrival instant (UTC)
  shipping/shipping-app-YYYY-MM-DD.log       logfmt, one file per UTC day
  ground_truth/                      see "Ground truth" below
```

Local time is America/New_York (`zoneinfo`, DST handled). Timestamps inside the data are UTC
except the two warehouse sources, which use naive local time and run 90 s fast.

## Sources

| Source | System | Kind | Format | Timezone | Skew | Identifiers |
| --- | --- | --- | --- | --- | --- | --- |
| `src_webstore_log` | `sys_webstore` Webstore | log file | NDJSON | UTC | 0 | `cart_id` (`c-12618424`, 8 digits) |
| `src_orders_log` | `sys_orders` Order system | log file | logfmt | UTC | 0 | `order_id` (`4828`, 4 to 5 digits), `cart_id`, `merchant_ref` (`X9-249089`, 6 digits) |
| `src_payments_xml` | `sys_payments` Payments | log file | XML lines | America/New_York (offset in data) | 0 | `merchantRef` |
| `src_wms_db` | `sys_warehouse` Warehouse | SQL table | SQL seed + CSV | America/New_York (naive) | +90 s | `id` (`642876`, 6 digits), `po_num` (`93-035`, then `100-384` past 99,999), `order_ref` (`SO-0004828`) |
| `src_wms_export_log` | `sys_warehouse` Warehouse | log file | text | America/New_York (naive) | +90 s | file names |
| `src_ship_sftp` | `sys_shipping` Shipping | file drop | files | UTC (mtime) | 0 | file name (batch key) |
| `src_ship_log` | `sys_shipping` Shipping | log file | logfmt | UTC | 0 | `shipment_no` (`SH-3971593`, 7 digits), `po_num`, `file`, `manifest_id` |

Every counter starts at its own magnitude plus a per-seed offset (`shop_model.CounterBases`:
cart 10,000,000 + U(0, 5,000,000), order 4,471 + U(0, 1,000), merchant 200,000 + U(0, 50,000),
PO 88,210 + U(0, 5,000), shipment 3,000,000 + U(0, 1,000,000), row id 600,000 + U(0, 100,000));
the examples above are seed 1. The `digits`/`alnum` forms of two different identifiers therefore
never share a value (only `order_id` and the digits of `order_ref` do, link L05) for runs below
about 80,000 orders. The offsets come from their own generator, so the rest of the draw sequence
does not depend on them.

## Formats, one example line each

```
webstore   {"ts": "2026-09-23T13:04:06.001Z", "level": "info", "msg": "cart created", "cart_id": "c-12618424", "items": 3, "region": "us-east", "channel": "web"}
webstore   {"ts": "2026-09-23T13:16:40.381Z", "level": "info", "msg": "checkout completed", "cart_id": "c-12618424", "total": 129.99, "payment_method": "card", "customer_name": "Jane Smith", "customer_email": "jane.smith+mk3f9a2c1e@example.com"}
orders     ts=2026-09-23T13:16:46.566Z level=info msg="order created from cart" order_id=4828 cart_id=c-12618424 total=129.99 channel=web
orders     ts=2026-09-23T13:16:56.709Z level=info msg="payment requested" order_id=4828 merchant_ref=X9-249089 amount=129.99 currency=USD
orders     ts=2026-09-28T14:20:19.579Z level=error msg="payment request failed" order_id=8173 merchant_ref=X9-252434 http_status=503
orders     ts=2026-09-23T13:17:04.213Z level=info msg="order released" order_id=4828 status=RELEASED
payments   <paymentMessage><timestamp>2026-09-23T09:17:01.292-04:00</timestamp><merchantRef>X9-249089</merchantRef><amount>129.99</amount><currency>USD</currency><cardholderName>Jane Smith</cardholderName><status>AUTHORIZED</status><httpStatus>200</httpStatus><processor>cardnet</processor></paymentMessage>
payments   <paymentMessage><timestamp>2026-09-28T10:20:19.186-04:00</timestamp><merchantRef>X9-252434</merchantRef><amount>596.99</amount><currency>USD</currency><status>ERROR</status><httpStatus>503</httpStatus><error>Service Unavailable</error><processor>cardnet</processor></paymentMessage>
payments   <heartbeat><timestamp>2026-09-23T00:23:06.570-04:00</timestamp><status>OK</status></heartbeat>
warehouse  INSERT INTO purchase_orders (id, po_num, order_ref, status, warehouse_code, order_total, order_date, customer_name, ship_to_address, created_by, created_at, updated_at) VALUES (642876, '93-035', 'SO-0004828', 'SHIPPED', 'DC-01', 129.99, '2026-09-23', 'Jane Smith', '282 mk5349da48 Street, Omaha, NE 52733', 'svc_wms_integration', '2026-09-23 09:21:04', '2026-09-23 21:41:31');
warehouse  642876,93-035,SO-0004828,SHIPPED,DC-01,129.99,2026-09-23,Jane Smith,"282 mk5349da48 Street, Omaha, NE 52733",svc_wms_integration,2026-09-23 09:21:04,2026-09-23 21:41:31
export log 2026-09-23 21:12:41 INFO PO export finished: 412 POs written to SHIP_20260923_2112.csv
export log 2026-09-23 21:13:36 INFO SFTP upload complete: SHIP_20260923_2112.csv (23891 bytes)
export log 2026-10-01 21:14:30 ERROR SFTP upload failed: Permission denied (/outbound/shipping/SHIP_20261001_2113.csv)
export log 2026-09-23 10:00:12 INFO export scheduler idle, next run 21:10
SHIP file  po_num,order_ref,warehouse_code,carrier,service_level,weight_kg  /  93-035,SO-0004828,DC-01,UPS,GROUND,15.38
shipping   ts=2026-09-24T01:20:39.439Z level=info msg="shipment created" shipment_no=SH-3971593 po_num=93-035 file=SHIP_20260923_2112.csv carrier=UPS service=GROUND
shipping   ts=2026-09-24T02:13:52.000Z level=info msg="carrier pickup" shipment_no=SH-3971593 manifest_id=MAN-20260923-01 carrier=UPS
shipping   ts=2026-09-24T00:05:53.606Z level=info msg="carrier rate table refreshed" carrier=USPS rows=412
```

The SQL seed is standard SQL that both sqlite3 and PostgreSQL execute: strings with doubled single
quotes, no backslash escapes, timestamps as `'YYYY-MM-DD HH:MM:SS'`, `order_total` as a decimal
literal. XML text is escaped with `xml.sax.saxutils.escape`. logfmt values with spaces, quotes,
`=` or backslashes are double-quoted with `\"` and `\\` escapes.

## Lifecycle and timings

One transaction is one order, `txn_000001` ascending by cart creation time. Volumes:
`daily_volume` orders per weekday and `ceil(daily_volume / 2)` on Saturday and Sunday (the
expectation is used exactly, not drawn); hour-of-day weights in local time are low overnight, rise
from 07:00 and are highest from 11:00 to 21:00; the time within the hour is uniform.

| Step | Node | When | Line |
| --- | --- | --- | --- |
| 1 | `webstore:cart_created` | t0 | `cart created`, `cart_id` = `c-` + 8-digit counter (`CounterBases.cart`) |
| 2 | `webstore:checkout_completed` | t0 + U(2, 20) min | `checkout completed`; `customer_name` and `customer_email` with probability `pii_density` |
| 3 | `orders:order_created` | + U(5, 60) s | `order created from cart`, `order_id` from `CounterBases.order` (4,471 to 5,470; bridge `cart_id` ~ `order_id`) |
| 4 | `orders:payment_requested` | + U(5, 30) s | `payment requested`, `merchant_ref` = `X9-` + 6-digit counter from `CounterBases.merchant` (bridge) |
| 5 | `payments:authorized` | + U(1, 15) s | `AUTHORIZED` message; `cardholderName` (same name as the checkout) with probability `pii_density` |
| 6 | `orders:order_released` | + U(1, 10) s | `order released` |
| 7 | `warehouse:po_created` | automated (70%): authorized + U(1, 5) min, `created_by=svc_wms_integration`, actor service. Clerk (30%): the first instant at or after authorized + 20 min inside Mon to Fri 08:00 to 18:00, plus U(0, 240) min; past 18:00 it moves to the next business day 08:00 + U(0, 60) min; `created_by` one of 8 clerks, actor human | one `purchase_orders` row; `order_ref` = `SO-` + order id zero-padded to 7 digits; 2% of clerk rows get a typo in `order_ref` (one digit replaced or two adjacent differing digits swapped), so the `digits.0` form no longer equals the order id |
| 8 | picking | created + U(30, 240) min inside 06:00 to 22:00; otherwise the next opening at 06:00 + U(0, 60) min (same day when before opening) | status `PICKED`; POs picked before 21:00 ride that day's export, later ones the next day's |
| 8 | `warehouse:export_finished`, `warehouse:upload_complete` | 21:10 + U(0, 5) min local on every day with at least one due PO; upload 30 to 60 s later | `PO export finished: N POs written to SHIP_YYYYMMDD_HHMM.csv`; HHMM is the warehouse's own clock (true + 90 s) |
| 9 | `shipping:file_arrived` | export + U(3, 8) min | the SHIP file lands; mtime = arrival; `batch_<file name without .csv>` |
| 10 | `shipping:shipment_created` | arrival + U(5, 70) min per PO | `shipment_no` = `SH-` + 7-digit counter from `CounterBases.shipment` in creation order; row status `SHIPPED` |
| 11 | `shipping:carrier_pickup` | 22:00 + U(0, 30) min local per day | one line per shipment created before the pickup time; later shipments ride the next day's manifest `MAN-YYYYMMDD-01` |

Row ids (6 digits from `CounterBases.row`) and PO numbers (`93-035` from `CounterBases.po`, 88,210
to 93,209; three-digit prefix past 99,999, so a default run reaches `100-384`) follow `created_at`
order. `order_date` is the local date of `orders:order_created`. Rows carry
the final status (`CREATED`, `PICKED`, `SHIPPED`) and `updated_at` of the last change inside the
window. Steps falling after the window (last day 23:59:59 local) are not emitted and ground truth
records only what was emitted; a row whose later steps fall outside the window keeps the status it
had at the window end.

Noise (node `<system>:noise`, no transaction, no identifiers) has a Poisson count per hour with
mean `rate * noise_rate`, spread uniformly within the hour: webstore 40/h (`health check ok`,
`cache refresh`), orders 30/h (`scheduler tick`, `db pool stats`), payments 6/h (heartbeat),
warehouse export log 1/h (`export scheduler idle, next run 21:10`), shipping log 10/h
(`carrier rate table refreshed`). Lifecycle and noise lines are interleaved in each file in
rendered-time order (ties keep generation order).

## Faults

Days are 1-based from `start_date`; with the default start (Wednesday 2026-09-23) the fault days
are the weekdays the spec names. A fault whose day is beyond `days` is skipped. `--no-faults`
turns all of them off except the clock skew, which is a property of the warehouse source.

| Id | Kind | When (local) | What happens | Expected detection |
| --- | --- | --- | --- | --- |
| `f1_payments_503_spike` | `error_spike` | day 6, 10:20 to 11:00 | every payment attempt in the window gets `<status>ERROR</status><httpStatus>503</httpStatus><error>Service Unavailable</error>` (node `payments:error_503`, `is_error`) and the order log writes `level=error msg="payment request failed" ... http_status=503` (`orders:payment_failed`); the order retries every U(5, 10) min with the same `merchant_ref` until the first attempt after 11:00 | alert kind `error_rate`, cause `error_spike`, evidence `Service Unavailable` |
| `f2_missing_nightly_file` | `missing_file` | day 9, expected by 21:30 | the export runs (the fault needs a due PO that night; a day 9 without an export produces no FaultTruth), then exactly 12 `ERROR SFTP upload failed: Permission denied (/outbound/shipping/SHIP_...csv)` lines between 21:13 and 21:19 true time (`warehouse:upload_failed`); no SHIP file lands; day 10's file lists both days' POs and their shipments follow its arrival | alert kind `schedule`, cause `error_spike`, evidence `SFTP upload failed: Permission denied`; `end` = day 10's arrival |
| `f3_webstore_outage` | `visibility_gap` | day 10, 14:00 to 16:00 | webstore lines in the window are not written (no EventTruth either); orders and everything downstream continue | alert kind `freshness`, cause `visibility_gap`; must never be reported as a stall (spec 10.2) |
| `f4_schema_rename` | `schema_drift` | from day 12 00:00 | rows created from then on use column `po_number`: `ALTER TABLE ... RENAME COLUMN` between the inserts in the SQL seed, and the CSV export is split into `purchase_orders.csv` / `purchase_orders.renamed.csv` | alert kind `schema_drift`, attributes `{"field": "po_num", "renamed_to": "po_number"}` |
| `f5_partial_stall_dc03` | `partial_stall` | from day 13 10:00 | POs with `warehouse_code=DC-03` created from then on are never picked, exported or shipped (status stays `CREATED`) | alert kind `hop_deadline`, cause `partial_attribute_lift` ("what's different: warehouse_code=DC-03") |
| `f6_warehouse_clock_skew` | `clock_skew` | whole run | both warehouse sources render true time + 90 s (always on; the FaultTruth exists only with faults on) | no alert (`expected_alert` false) |

## Ground truth

`ground_truth/` holds the files described in `carto_simulator/ground_truth.py` (ADR 0006):

- `event_txn.ndjson`: one `EventTruth` per emitted record, streamed in `(observed_at, key)`
  order. `observed_at` is true UTC time before skew. Locator keys:
  `<source_id>:<file name>:line:<n>` (1-based within that file),
  `src_wms_db:purchase_orders:row:<id>`, `src_ship_sftp:file:<file name>`. `txn_id` is null for
  noise, file arrivals and export-log lines; `batch_id` is set on records that carry the batch key
  (file arrivals, `export_finished`/`upload_complete`, shipment and pickup lines). Every non-null
  `batch_id` has a `BatchTruth`; day 9's failed export lines carry no batch because that file never
  lands. `actor_kind` is `human` for clerk rows, `service` for automated lifecycle records, null
  for noise and file arrivals. `is_error` is true only for `payments:error_503`,
  `orders:payment_failed` and `warehouse:upload_failed`.
- Nodes: `webstore:cart_created`, `webstore:checkout_completed`, `orders:order_created`,
  `orders:payment_requested`, `orders:payment_failed`, `orders:order_released`,
  `payments:authorized`, `payments:error_503`, `warehouse:po_created`,
  `warehouse:export_finished`, `warehouse:upload_complete`, `warehouse:upload_failed`,
  `shipping:file_arrived`, `shipping:shipment_created`, `shipping:carrier_pickup`, and
  `<system>:noise` for webstore, orders, payments, warehouse (export log) and shipping.
- `links.json`: L01 exact webstore `cart_id` ~ orders `cart_id`; L02 bridge `cart_id` ~
  `order_id`; L03 bridge `order_id` ~ `merchant_ref`; L04 exact `merchant_ref` ~ payments
  `merchantRef`; L05 exact `order_id` raw ~ warehouse `order_ref` `digits.0`; L06 composite
  (manual) payments `amount` ~ warehouse `order_total` with components amount, `timestamp` date ~
  `order_date`, `cardholderName` `phonetic.0` ~ `customer_name` `phonetic.0`, each with its
  measured agreement rate over clerk rows; L07 exact warehouse `po_num` ~ shipping `po_num`;
  L07b the same for `po_number` (only when the rename is active); L08 bridge `shipment_no` ~
  `po_num`; L09 batch `file_name` ~ shipping `file` (entity `ent_shipping_file`); L10 bridge
  `shipment_no` ~ `manifest_id` (role batch, entity `ent_manifest`); the bridges the data holds
  because several identifiers sit in one record (added after the first M2 engine eval found them):
  L11 bridge warehouse `id` ~ `po_num`, L12 bridge `id` ~ `order_ref` `digits.0`, L13 bridge
  `order_ref` `digits.0` ~ `po_num` (role transaction, `ent_order`), L11b and L13b the same with
  `po_number` (only when the rename is active), L14 bridge shipping `file` ~ `shipment_no` and
  L15 bridge `file` ~ `po_num` (role batch, `ent_shipping_file`).
- `entities.json`: `ent_order` (every identifier field with its form, the warehouse row `id`
  included, `po_number` only when the rename is active), `ent_shipping_file`, `ent_manifest`.
- `batches.json`: `batch_SHIP_YYYYMMDD_HHMM` keyed by `src_ship_sftp/file_name` and
  `batch_MAN-YYYYMMDD-01` keyed by `src_ship_log/manifest_id`, each with sorted `txn_ids`.
- `faults.json`: the table above (`start`/`end` in UTC).
- `manual_hops.json`: `hop_payments_to_warehouse` (manual, share 0.3, the 8 clerks, typo rate
  0.02) and the automated hops `hop_webstore_cart_to_checkout`, `hop_webstore_to_orders`,
  `hop_orders_created_to_payment_requested`, `hop_orders_to_payments`, `hop_payments_to_orders`,
  `hop_warehouse_to_shipping`, `hop_shipping_file_to_shipment`, `hop_shipping_shipment_to_pickup`.
- `markers.json`: `marker_tokens` (every `mk` + 8 lowercase hex token embedded in an emitted email
  or address, embedded unchanged so `token in value` holds for each one),
  `pii_values` (every emitted `customer_name`, `customer_email`, `cardholderName` and
  `ship_to_address`), `identifier_values` keyed by `str(FieldRef)` such as
  `sys_orders/src_orders_log/order_id` (only fields that appear in the data have a key).
- `manifest.json`: counts per source plus `transactions`, `events` (records with a transaction),
  `noise`, `files`; sha256 of every native file by relative path; the request parameters;
  `generator_version` from `carto_simulator.api.GENERATOR_VERSION`.

`ground_truth/` sits inside the output directory next to the native data. Point connectors and
offline bundles only at the per-system subdirectories (`webstore/`, `orders/`, `payments/`,
`warehouse/`, `shipping/`), never at the whole tree: `markers.json` holds the synthetic PII in
clear and `event_txn.ndjson` the transaction mapping the eval must not see through the engine.

## Determinism

Every random draw comes from one `random.Random(seed)` consumed in a fixed order: cart times per
day, then each transaction's lifecycle in cart order, then per-day export parameters, then the
nightly exports day by day (per-PO carrier, service, weight and shipment delay), then noise source
by source. Nothing reads the wall clock, uses `uuid`, `os.urandom` or iterates a set. The same
request produces byte-identical native files and ground truth on every platform; the generated
`README.md` quotes the seed and the sha256 of `event_txn.ndjson`, and `manifest.json` carries
the digest of every native file. The generator opens no network connection and, apart from the
tz database, reads nothing outside its own output directory.

Local time comes from `zoneinfo`. The IANA database is the system's where one exists and the
`tzdata` package otherwise (a declared dependency of this package, ADR 0003), so Windows and
minimal containers compute the same instants as Linux. `carto_simulator.clock` raises a clear
error naming `tzdata` if the zone cannot be loaded.

## Choices where the spec is silent

- Daily volume is the expectation exactly; weekends use `ceil(daily_volume / 2)`.
- Addresses embed the marker token as drawn (`282 mk5349da48 Street`), not capitalised, so every
  recorded token is an exact substring of the value it marks (the `MarkerSet` contract).
- `order_date` is the local date of `orders:order_created`.
- Picking outside warehouse hours resumes at the next 06:00 opening (the same day when the pick
  would fall before opening), plus U(0, 60) min.
- SHIP file names use the warehouse's own clock (true time + 90 s); the arrival mtime is true time.
- The rename split (F4) is by true `created_at`; rendered `created_at` therefore reads from
  00:01:30 on the rename day for the first renamed rows.
- F2's twelve error lines are evenly spaced between max(export + 30 s, 21:13:00) and 21:19:00 true
  time, so they always follow the export line; rendered they read 90 s later.
- Payment failure lines are logged 50 to 500 ms after the 503 response; retries wait U(5, 10) min
  after that line.
- Noise counts per hour are Poisson draws around the expectation.
- Payment methods: card 80%, paypal 12%, apple_pay 8%; shipment weights U(0.2, 30) kg.
- Counters live at different magnitudes with a per-seed offset (see "Sources"): the spec's
  examples (`c-88213`, `4471`, `X9-0442`, `88-210`, `SH-5521`) fix the shapes, not the ranges,
  and dense counters in overlapping ranges made unrelated identifiers share thousands of
  `digits`/`alnum` values, which the engine took for exact links indistinguishable from L05.

## Scenario C: "people_ops" (M2, plan package H)

```sh
make sim SCENARIO=people_ops DAYS=28                 # writes sim-out/people_ops
uv run carto-sim generate --scenario people_ops --days 28 --seed 1 --out sim-out/people_ops
uv run carto-eval self-check --scenario people_ops --days 28 --sim-out sim-out/people_ops
```

One transaction is one hire (`txn_000001` ascending by start date, main hires before legacy ones
within a day), plus a smaller flow of SKUs (`sku_000001` ascending by the time the row was added
to the sheet). 28 days by default so composite matching sees enough pairs; 3 days work for CI.
`daily_volume` is a scale: `ceil(daily_volume / 20)` main and `ceil(daily_volume / 80)` legacy
hires start per weekday before the week-start weights Mon 1.4, Tue 1.2, Wed 1.0, Thu 0.8, Fri 0.6
(the default 800 gives 56, 48, 40, 32, 24 main and 14, 12, 10, 8, 6 legacy: 40 and 10 per weekday
on average), and `ceil(daily_volume / 160)` SKUs are added per weekday (25 per week by default).
Nothing starts at the weekend. Local time is America/New_York except the legacy onboarding system,
which logs naive Europe/London time. No source runs a skewed clock.

### Sources

| Source | System | Kind | Format | Timezone | Identifiers and PII |
| --- | --- | --- | --- | --- | --- |
| `src_onboarding_log` | `sys_onboarding` Onboarding | log file | NDJSON | UTC | `candidate_id` (`C-1000481`), `candidate_name`, `work_email`, `start_date` |
| `src_legacy_log` | `sys_onboarding_legacy` Onboarding (legacy) | log file | logfmt, key `time`, severity `lvl` | Europe/London (naive) | `cand_ref` (`L204117`), `applicant`, `mail`, `startDt` (`%d/%m/%Y`) |
| `src_hris_db` | `sys_hris` HRIS | SQL table `employees` | SQL seed + CSV | America/New_York (naive) | `employee_id` (`E-40210`), `source_ref` (the candidate id, 60% of rows), `full_name`, `start_date`, `created_by` |
| `src_payroll_csv` | `sys_payroll` Payroll | rows, one CSV per weekday | CSV | America/New_York (naive) | `worker_number` (`W-300118`), `employee_ref` (keyed from `employee_id`), `legal_name`, `start_date`, `entered_by` |
| `src_lms_log` | `sys_lms` Training LMS | log file | NDJSON | UTC | `username` (`jane.smith.mk3f9a2c1e`), `user_email`, `actor` |
| `src_sku_export_csv` | `sys_sku_sheets` Product sheets | rows, one CSV per Friday | CSV | America/New_York (naive) | `legacy_sku` (`500100`), `description`, `unit_price` |
| `src_sku_master_db` | `sys_sku_master` SKU master | SQL table `sku_master` | SQL seed + CSV | America/New_York (naive) | `sku` (`PKG-10240`), `description`, `unit_price`, `created_by` |

Layout: `onboarding/onboarding-YYYY-MM-DD.ndjson` (UTC day), `onboarding_legacy/legacy-hires-YYYY-MM-DD.log`
(London day), `hris/employees.sql` + `hris/employees.csv`, `payroll/payroll_YYYYMMDD.csv` (local
day of `entered_at`), `lms/lms-YYYY-MM-DD.ndjson` (UTC day), `sku_sheets/sku_sheet_YYYYMMDD.csv`
(Friday's date), `sku_master/sku_master.sql` + `sku_master/sku_master.csv`. One example each:

```
onboarding  {"ts": "2026-10-05T12:41:07.000Z", "level": "info", "msg": "onboarding started", "candidate_id": "C-1000481", "candidate_name": "Jane Smith", "work_email": "jane.smith+mk3f9a2c1e@example.com", "start_date": "2026-10-05", "department": "Operations"}
legacy      time="2026-10-05 09:02:00" lvl=INFO msg="first day" cand_ref=L204546 applicant="Amara Patel" mail=amara.patel+mkb756241f@example.com startDt=05/10/2026 site=MAN
hris        1,E-40210,Mei Walsh,2026-09-23,C-1000515,Finance,hr_dlamini,2026-09-24 08:04:31
payroll     W-300418,E-40528,Nora Ivanova,2026-09-30,monthly,pay_farrow,2026-10-09 08:02:12
lms         {"ts": "2026-10-05T08:11:18.000Z", "level": "info", "msg": "enrollment created", "username": "chloe.brown.mk0ac45d41", "user_email": "chloe.brown+mk0ac45d41@example.com", "course": "safety-induction", "actor": "svc_lms_provisioner"}
sheet       500100,Green corner guard 811 mm,160.39,Labels,2026-09-23 08:56:14
master      1,LBL-10240,Green corner guard 811 mm,160.39,prd_haines,2026-09-28 10:04:50
```

### Lifecycle and timings

| Step | Node | When | Line or row |
| --- | --- | --- | --- |
| 1 | `onboarding:application_received` (legacy `onboarding_legacy:application_logged`) | offer day minus U(2, 10) days, at U(07:00, 22:59) local; not emitted when before the run | `candidate_id` and `channel`; `candidate_name` with probability `pii_density` |
| 2 | `onboarding:offer_accepted` (`onboarding_legacy:offer_signed`) | start date minus U(3, 10) days, moved back to a business day, 08:00 + U(0, 10 h) local | `candidate_name` and `start_date` (legacy: `applicant`, `startDt`) |
| 3 | `onboarding:onboarding_started` (`onboarding_legacy:first_day`) | the start date, 08:00 + U(0, 90) min New York (legacy: 09:00 + U(0, 60) min London), actor service | name, work email and start date together: the export HR works from |
| 4 | `lms:enrolled` | start + U(2, 30) min, actor `svc_lms_provisioner` | `username` = the email's local part with `+` as `.`, `user_email`, `course` |
| 5 | `hris:employee_created` | 1 or 2 business days after the start, 08:00 + U(0, 10 h) local, one of 4 HR clerks, actor human | `employee_id` from `E-40210` in creation order; `full_name` keyed with 3% typos (two letters swapped or one doubled); `source_ref` = the candidate id for 60% of rows, blank otherwise |
| 6 | `payroll:worker_entered` | 1 or 2 business days after the HRIS row, 08:00 + U(0, 10 h) local, one of 3 payroll clerks, actor human | `worker_number` from `W-300118` in entry order; `employee_ref` = `employee_id` with 2% digit typos; `legal_name` = the HRIS name with 3% typos; `start_date` copied exactly |
| 7 | `sku_sheets:sku_added` | every weekday 08:00 + U(0, 8 h) local; visible in the export of that week's Friday at 17:00 (rows of a week whose Friday is past the window are never exported) | `legacy_sku` from `500100`, `description` (colour, noun, size), `unit_price` U(0.50, 250.00), `category` |
| 8 | `sku_master:sku_created` | 1 or 2 business days after the Friday export, business hours, one of 2 product clerks, actor human | `sku` = category code + counter from 10240 (`PKG-10240`); `description` re-keyed with 3% typos; `unit_price` copied exactly; the master never carries `legacy_sku` |

Hires start on weekdays only; steps falling after the window are not emitted and ground truth
records only what was emitted. Noise (`onboarding:noise` 20/h, `onboarding_legacy:noise` 4/h,
`lms:noise` 10/h, scaled by `noise_rate`) carries no transaction and no identifiers; the row
sources have no noise.

### Faults

Days are 1-based from `start_date`; with the default start (Wednesday 2026-09-23) week 3 is days
13 to 19 (Monday 2026-10-05 onwards). `--no-faults` turns both off.

| Id | Kind | When (local) | What happens | Expected detection |
| --- | --- | --- | --- | --- |
| `c1_payroll_backlog_week3` | `partial_stall` | day 13 08:00 to the first backlog entry on day 17 | every payroll entry due on days 13, 14 and 15 is keyed on day 17 (the next business day when day 17 is not one): no `payroll_*.csv` exists for those three days and day 17's file carries their rows with its own; the FaultTruth exists only when a row was affected (short runs) | alert kind `hop_deadline` on the HRIS to payroll hop (H03), cause `systemic_stall` (hop volume zero, spec 10.4 item 6); the contract has no whole-hop stall kind, so `partial_stall` is the closest |
| `c2_payroll_never_reached` | `partial_stall` | whole run | 1% of hires with an HRIS row never get a payroll row (spec 11.2 drop rate) | none in M2 (`expected_alert` false): truth only, the affected transaction ids are listed for M4's detector |

### Ground truth

- Locator keys: `<source_id>:<file name>:line:<n>` for the three logs,
  `src_hris_db:employees:row:<id>`, `src_payroll_csv:workers:row:<worker_number>`,
  `src_sku_export_csv:sku_sheet:row:<legacy_sku>`, `src_sku_master_db:sku_master:row:<id>`.
  `actor_kind` is `human` for HRIS, payroll, sheet and master rows, `service` for the onboarding
  and LMS lines, null for noise. No record is an error and there are no batches.
- `links.json` (`C01` onwards; `manual` true where a person moves the data): C01 exact onboarding
  `candidate_id` ~ HRIS `source_ref` and C02 exact legacy `cand_ref` ~ `source_ref` (a_to_b, 60% of
  rows); C03 bridge `employee_id` ~ `source_ref`; C04 composite onboarding `candidate_name`
  `phonetic.0` ~ HRIS `full_name` `phonetic.0` with components name and `start_date` `date`, C05
  the same from the legacy `applicant` and `startDt`; C06 exact HRIS `employee_id` ~ payroll
  `employee_ref` (keyed by hand); C07 bridge `worker_number` ~ `employee_ref`; C08 composite
  `full_name` ~ `legal_name` phonetic with components name, start date and `employee_id` ~
  `employee_ref` `digits.0`; C09 exact `work_email` raw ~ LMS `user_email` raw and C10 the same
  from legacy `mail`; C11 bridge `username` ~ `user_email`; C12 bridge `candidate_id` ~
  `work_email`; C13 bridge `cand_ref` ~ `mail`; C14 composite sheet `description` `alnum` ~ master
  `description` `alnum` (entity `ent_sku`) with components `alnum`, `phonetic.0` and `unit_price`
  `amount`. Agreement rates are measured over the generated pairs: names and descriptions by exact
  equality after keying (about 0.97; a lower bound of the phonetic agreement and the exact `alnum`
  agreement), `employee_ref` about 0.98, dates and prices 1.0.
- `entities.json`: `ent_employee` (every identifier field of the five employee sources plus the
  four name fields in their `phonetic.0` form) and `ent_sku` (`legacy_sku`, `sku`, both
  descriptions in `alnum`).
- `manual_hops.json`: H01 onboarding started to HRIS, H02 legacy first day to HRIS, H03 HRIS to
  payroll, H04 sheet to master (manual, share 1.0, the clerks, typo rates 0.03, 0.03, 0.02,
  0.03); H05 to H10 the automated hops inside onboarding and into the LMS.
- `markers.json`: every name as emitted (typos included), every email and username, every
  identifier value per field (`sys_hris/src_hris_db/employee_id`, ... including both
  `description` fields), the nine clerk logins, and every unit price.

### Choices where the spec is silent

- Hires are anchored on the start date so that a 3-day run exercises every source but the SKU
  master (keyed the week after); the application and offer lines of the first days fall before
  the window and are not emitted.
- The legacy volume (10 hires per weekday, 50 per week) is the founder-confirmed figure and is
  kept on purpose although it puts the legacy `cand_ref` and `mail` fields at exactly 50 distinct
  values in a 7-day profiler window, the engine's `min_distinct` floor: the profiler counts
  distinct values exactly while the sample is not saturated (ADR 0047), so the fields qualify, and
  a small acquired system sitting at the floor is a realistic case the eval should cover.
- `employee_id` is `E-40210`, not `E40210`: a letter followed by digits also occurs inside ULID
  event ids by chance, which the leak test's substring rule counts as a leak; every identifier
  therefore carries punctuation, as the shop's do.
- The legacy `startDt` is `%d/%m/%Y` as the plan's table says. The edge reads `a/b/yyyy` month
  first unless `a` exceeds 12 (`carto_edge.pipeline.forms`), so for days 1 to 12 its `date` token
  differs from the HRIS `start_date` token; the truth records the true agreement (1.0). This is a
  real-world hazard the scenario keeps on purpose.
- The name and description typos are a swap of two adjacent letters (60%) or a doubled letter.
- The LMS username keeps the marker (`jane.smith.mk3f9a2c1e`) so a username in clear is caught
  by the marker scan like the email it came from.
- `simulator/analyze.people_ops.yaml` pins the four name fields to `phonetic`, the three email
  fields and the username to `contact` tokenized with the `raw` form only, and both descriptions
  to `alnum` + `phonetic`; nothing is pinned to `keep`. Scenario C has no batch keys, so no
  ADR 0026 pin is needed.

## Tests

`uv run pytest simulator/tests` (file prefix `test_sim_`). The suite checks formats with
independent parsers (json, a logfmt regex, `xml.etree`, sqlite3), the faults day by day, that every
locator key resolves, the leak-test markers, determinism, the CLI and the request contract. Live
mode is checked with a replay that never sleeps (`test_sim_live.py`): shifted timestamps in every
format, file mtimes, the row stream, the SQL seed, the shifted ground truth and the early-stop
paths; `test_sim_analyze_config.py` checks that `simulator/analyze.shop.yaml`,
`deploy/compose/sources.dev.yaml` and `otel/collector.dev.yaml` name exactly the sources and
systems the ground truth declares and match the generated file layout. The statistical test (14
days by 200 orders, clerk share 0.30 +/- 0.08, typo share 0.02 +/- 0.02) is marked `slow`.
`test_sim_people_ops.py` covers scenario C the same way (formats, the backlog and the dropped
hires, keys, links and entities, agreement rates, markers, typo shares, determinism, the 3-day
run and `simulator/analyze.people_ops.yaml`).
