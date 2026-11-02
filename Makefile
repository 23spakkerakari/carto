# carto v1 build targets (spec Section 20). Works with GNU make on Linux, macOS and Git Bash on
# Windows. Recipes use ">" as the prefix instead of a tab so the file survives any editor.
#
#   make UV="py -3.11 -m uv" test      # when uv is not on PATH (ADR 0007)

.RECIPEPREFIX := >
.DEFAULT_GOAL := help
SHELL := /bin/bash

UV ?= uv
SCENARIO ?= shop
DAYS ?= 14
SEED ?= 1
SIM_OUT ?= sim-out
EVAL_OUT ?= eval/reports
ENGINE ?= memory
WEIGHTS_OUT ?= eval/weights/fitted.json

.PHONY: help setup lint fmt type test test-unit test-integration cov check schema schema-check sim eval eval-engine fit-weights bench-linker leak bench bench-gateway analyze sec sbom images dev dev-down release clean web-setup web-check web-e2e

help: ## Show targets
> @grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-13s %s\n", $$1, $$2}'

setup: ## Install the locked workspace (uv sync)
> $(UV) sync --locked --all-packages

lint: ## ruff lint and format check
> $(UV) run ruff check .
> $(UV) run ruff format --check .

fmt: ## Apply ruff formatting and safe fixes
> $(UV) run ruff check --fix .
> $(UV) run ruff format .

type: ## mypy --strict over every member
> $(UV) run mypy

test: ## Unit, property and (when Docker is reachable) integration tests
> $(UV) run pytest

test-unit: ## Everything except the Docker-backed integration tests
> $(UV) run pytest -m "not integration"

test-integration: ## Only the Docker-backed integration tests (testcontainers)
> $(UV) run pytest -m integration

cov: ## Tests with coverage (terminal and XML)
> $(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

check: lint type test schema-check ## Everything CI's python job runs, locally

schema: ## Regenerate the committed JSON Schemas from the Pydantic models
> $(UV) run carto-schema export --out packages/carto-schema/schemas

schema-check: ## Fail if the committed JSON Schemas drift from the models
> $(UV) run carto-schema check --dir packages/carto-schema/schemas

sim: ## Generate simulator data: make sim SCENARIO=shop DAYS=14 SEED=1
> $(UV) run carto-sim generate --scenario $(SCENARIO) --days $(DAYS) --seed $(SEED) --out $(SIM_OUT)/$(SCENARIO)

eval: ## Score engine output (or the empty prediction) against ground truth: make eval SCENARIO=shop
> $(UV) run carto-eval run --scenario $(SCENARIO) --days $(DAYS) --seed $(SEED) --sim-out $(SIM_OUT)/$(SCENARIO) --out $(EVAL_OUT)

eval-engine: ## Analyze, load, discover, map and score in one run (M2): make eval-engine SCENARIO=shop DAYS=14 ENGINE=memory (people_ops: pass DAYS=28)
> $(UV) run carto-eval run --engine $(ENGINE) --scenario $(SCENARIO) --days $(DAYS) --seed $(SEED) --sim-out $(SIM_OUT)/$(SCENARIO) --out $(EVAL_OUT)

fit-weights: ## Fit the linker's logistic weights on scenario truth (ADR 0049): writes $(WEIGHTS_OUT)
> $(UV) run carto-eval fit-weights --scenario shop --scenario people_ops --seed $(SEED) --sim-out $(SIM_OUT) --out $(WEIGHTS_OUT) --reuse-bundle

bench-linker: ## Linker at the spec 17 scale (50 systems x 1,000 candidate fields), measured not gating
> $(UV) run carto-eval bench-linker

analyze: ## Offline analyzer over the simulator output: make analyze SCENARIO=shop (writes $(SIM_OUT)/$(SCENARIO).carto)
> AS_OF="$$($(UV) run carto-sim as-of --out $(SIM_OUT)/$(SCENARIO))" && $(UV) run carto-edge analyze --config simulator/analyze.$(SCENARIO).yaml --input $(SIM_OUT)/$(SCENARIO) --out $(SIM_OUT)/$(SCENARIO).carto --state-dir $(SIM_OUT)/$(SCENARIO).edge-state --locator-map --as-of "$$AS_OF"

leak: ## Spec 18.3 leak test over scenario $(SCENARIO): bundle, forwarded batches, logs, ClickHouse rows
> $(UV) run pytest edge/tests/test_edge_leak.py -q -m "not integration" --scenario $(SCENARIO)

bench: ## Edge pipeline throughput benchmark (spec 17, M1 acceptance: 2,000 events/s sustained)
> AS_OF="$$($(UV) run carto-sim as-of --out $(SIM_OUT)/$(SCENARIO))" && $(UV) run carto-edge bench --input $(SIM_OUT)/$(SCENARIO) --config simulator/analyze.$(SCENARIO).yaml --seconds 30 --as-of "$$AS_OF"

bench-gateway: ## Full gateway ingest path benchmark: buffer, cursors, forwarder (ADR 0028)
> AS_OF="$$($(UV) run carto-sim as-of --out $(SIM_OUT)/$(SCENARIO))" && $(UV) run carto-edge bench --path gateway --input $(SIM_OUT)/$(SCENARIO) --config simulator/analyze.$(SCENARIO).yaml --seconds 30 --as-of "$$AS_OF"

web-setup: ## Install the console's locked npm dependencies (web/, ADR 0052)
> cd web && npm ci

web-check: ## Console lint, types, format, unit tests and production build (CI's web job minus e2e)
> cd web && npm run lint && npm run typecheck && npm run format:check && npm run test && npm run build

web-e2e: ## Console end-to-end tests with axe and the XSS corpus against the mock API (Playwright)
> cd web && npm run e2e

sec: ## Security scanners available locally; CI runs the full set (ADR 0007)
> $(UV) run bandit -c pyproject.toml -r packages edge core simulator eval tools -q
> $(UV) export --all-packages --no-emit-workspace --no-emit-package en-core-web-sm --no-hashes --format requirements-txt -o requirements-export.txt
> $(UV) run pip-audit -r requirements-export.txt --strict --desc on
> @command -v semgrep >/dev/null 2>&1 && semgrep --test --metrics=off tools/semgrep && semgrep scan --config tools/semgrep --error --metrics=off edge/carto_edge/connectors && semgrep scan --config p/python --config p/secrets --error --metrics=off --exclude legacy --exclude tools/semgrep . || echo "semgrep: not installed locally, runs in CI"
> @command -v gitleaks >/dev/null 2>&1 && gitleaks git --no-banner --redact . || echo "gitleaks: not installed locally, runs in CI"
> @command -v trivy >/dev/null 2>&1 && trivy fs --scanners vuln,secret,misconfig --severity HIGH,CRITICAL --exit-code 1 --skip-dirs legacy . || echo "trivy: not installed locally, runs in CI"
> @command -v osv-scanner >/dev/null 2>&1 && osv-scanner scan source --lockfile uv.lock || echo "osv-scanner: not installed locally, runs in CI"

sbom: ## CycloneDX SBOM of the workspace (syft)
> @command -v syft >/dev/null 2>&1 && syft scan dir:. --exclude ./legacy -o cyclonedx-json=sbom.cdx.json || echo "syft: not installed locally, runs in CI"

images: ## Build the service images (edge, core, ctl, simulator) from deploy/docker/base.Dockerfile
> docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=edge --build-arg ENTRY=carto-edge -t carto-edge:dev .
> docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=core --build-arg ENTRY=carto-core -t carto-core:dev .
> docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=ctl --build-arg ENTRY=carto-ctl -t carto-ctl:dev .
> docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=simulator --build-arg ENTRY=carto-sim -t carto-simulator:dev .
> docker build -f web/deploy/Dockerfile -t carto-web:dev web

dev: images ## Compose stack + simulator live mode (spec Section 20)
> @for s in pg_password ch_password pg_ingest_password pg_app_password pg_worker_password oidc_client_secret; do test -s deploy/compose/secrets/$$s || openssl rand -hex 24 | tr -d '\r\n' > deploy/compose/secrets/$$s; done
> @test -s deploy/compose/proxy-tls/tls.crt || openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 365 -subj /CN=localhost -addext subjectAltName=DNS:localhost -keyout deploy/compose/proxy-tls/tls.key -out deploy/compose/proxy-tls/tls.crt 2>/dev/null && chmod 644 deploy/compose/proxy-tls/tls.key
> docker compose -f deploy/compose/compose.yaml --profile dev up --remove-orphans

dev-down: ## Stop the Compose stack and remove its volumes
> docker compose -f deploy/compose/compose.yaml --profile dev down -v --remove-orphans

release: ## Build, sign and publish images (cosign signing of images and provenance wire up in M6; release.yml signs the SBOM on main)
> @echo "make release: build images with 'make images'; registry push, cosign image signatures and SLSA provenance arrive in M6 (docs/plans/M1.md, Deferred)."

clean: ## Remove generated data and caches
> rm -rf $(SIM_OUT) $(EVAL_OUT) *.carto .pytest_cache .mypy_cache .ruff_cache .hypothesis coverage.xml .coverage requirements-export.txt sbom.cdx.json
