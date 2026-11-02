# ADR 0054: LLM providers are adapters behind one Protocol, with a conformance test

Status: accepted, 2026-10-17 (plan `docs/plans/M2.md` decision 12 and question 4; founder
decision 4 of 2026-10-12; spec 9.9, 2.3 invariant 5, ADR 0004)

## Context

The gateway is off by default and the customer chooses the endpoint. The founder asked that
other providers be easy to add. The controls of spec 9.9 (payload schemas, data fencing, output
schema, egress allowlist, audit) must not depend on which provider is configured.

## Decision

- `carto_core.llm_gateway.schemas.Provider` is the seam: `name` and
  `complete(task_prompt, payload_json, output_schema) -> str`. An adapter is one module with
  that class and a settings model (endpoint, model, `api_key_file`); it owns the HTTP shape of
  its provider and nothing else.
- The gateway owns fencing, payload and output validation, the egress allowlist (the `httpx`
  client refuses any host but the configured endpoint's), timeouts, retries and the audit row
  per call (task, payload hash, model, token counts, latency, status), so an adapter cannot
  weaken them.
- M2 ships `anthropic` (Messages API), `openai` (any OpenAI-compatible chat completions
  endpoint, which covers most self-hosted and gateway deployments) and `fake` (tests and the
  injection corpus; not registrable in a service). Bedrock and Vertex are one module each when a
  partner asks; `LlmProvider` already names them.
- `core/tests/test_core_llm_providers.py` runs every registered adapter against a recorded
  exchange through a mock transport: request shape, credential header, response parsing,
  error mapping, and that no payload byte reaches any other host.
- `httpx` only; no vendor SDK (fewer dependencies to audit, one TLS stack, one egress check).

## Alternatives

- Vendor SDKs: each brings its own HTTP client, retries and telemetry, outside the allowlist.
- One provider in M2: the founder asked for easy addition; the Protocol is cheaper now than a
  refactor later.

## Consequences

- `LlmSettings.provider` gains `openai`; `api_key_file` replaces any environment credential.
- Adding a provider is a module, a registry entry and a recorded exchange.
