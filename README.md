# carto

Alright, here we go again lol.

Carto is an integration observability product: a read-only layer over the systems a company
already runs that maps how data moves between them, traces any business record across every
system it touched, watches for stalls and silent failures, and shows where a person is still
doing the talking.

Possible one-line pitches: _"We show you how your systems talk to each other, and
where a person is still doing the talking."_

"For the first time, you can see how your integrations talk to each other, where they're
failing, and how your best humans can spend time away from manual, high-latency workflows."

I put the build spec in [`docs/SPEC.md`](docs/SPEC.md). That's our source of truth. Decisions
that deviate from or extend it are in [`docs/adr/`](docs/adr/). Milestone plans are in
[`docs/plans/`](docs/plans/).

## Status

M0 (foundations) and M1 (edge pipeline and offline analyzer) are on `main`. M2 (map discovery)
is built on `m2-map-discovery` and waits on the design-partner gate. M3 (trace) is in
progress on `m3-demo` as the demo cut described in [`docs/plans/M3-demo.md`](docs/plans/M3-demo.md).
The milestone status in [`CLAUDE.md`](CLAUDE.md) has the detail.

## Layout

```
packages/carto-schema/   canonical event and edge-to-core contract models, JSON Schema export. This is where all our contracts live
packages/carto-common/   settings, logging with redaction, ids
edge/                    carto-edge: connectors, pipeline, gateway, offline analyzer (M1)
core/                    carto-core: ingest, profiler, linker, assembler, detector, notifier, api
simulator/               scenario generators with ground truth (scenario A "shop" in M0)
eval/                    eval harness scoring engine output against ground truth
web/                     React UI (M2)
otel/                    OpenTelemetry Collector template (M1)
deploy/                  Compose (M1) and Helm (M6)
tools/carto-ctl/         operator CLI
docs/                    SPEC, ADRs, plans, threat model, runbooks, install guides
legacy/                  the previous product in this repository, frozen (ADR 0005)
```

## Quick start

Requires [`uv`](https://docs.astral.sh/uv/) and GNU make. uv downloads Python 3.12 itself.

```
make setup                       # install the locked workspace
make check                       # lint, types, tests, schema drift check
make sim SCENARIO=shop DAYS=14   # synthetic data for five systems plus ground truth in sim-out/shop
make eval SCENARIO=shop          # score engine output (M0: the empty prediction) in eval/reports
```

On Windows, run these from Git Bash. If `uv` is not on PATH: `make UV="py -3.11 -m uv" check`.

## Security

Spec Section 2.3 lists the invariants every line of code keeps. Report a vulnerability to the
owner privately; do not open a public issue.
