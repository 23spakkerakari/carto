"""The LLM gateway (spec 9.9, 2.3 invariant 5; ADR 0004, 0054; plan M2 package G).

Every LLM call of the product goes through :class:`LlmGateway`, which owns the controls an
adapter cannot weaken:

- **Off by default.** ``llm.enabled`` false raises :class:`GatewayDisabledError` before anything is
  built or recorded; callers fall back to the heuristic dictionary (spec 9.9).
- **Fixed payloads.** A task accepts exactly its request model (``LabelFieldRequest`` for
  ``label_field`` and so on), already validated by pydantic and validated again here from its
  dump so a subclass with a weaker configuration is refused. Anything else is refused, counted
  and audited as ``refused``.
- **Fencing.** The payload JSON travels between nonce-bearing delimiter lines inside the user
  message; the system prompt states the task, the closed label set and the output schema
  (:mod:`.prompts`).
- **Retries.** At most two retries, on :class:`~.providers.TransientProviderError` only
  (408, 429, 5xx, timeouts, transport errors), with a short back-off. Each attempt is a call and
  gets its own audit row.
- **Strict output.** The answer must be one JSON object (a surrounding markdown code fence is
  removed, nothing else is tolerated) that validates as :class:`~.schemas.SuggestionList`
  (unknown keys refused by the model). A suggestion for a target that is not in the request is
  dropped; one target keeps its first suggestion; the list is capped at the request size. Any
  token-shaped string in a label or rationale discards the whole answer. Invalid output is
  discarded, counted and audited as ``invalid_output``, and the caller receives an empty list.
- **Audit and metrics.** One :class:`~.audit.CallRecord` per provider call (task, SHA-256 of
  the payload JSON, provider, model, token counts, latency, status, error class); counters for
  calls, invalid outputs, refusals, provider errors, retries, dropped suggestions and audit
  failures. The log sees task, status and latency; never a payload, a prompt or an output
  (spec 2.3 invariant 7).

Output is a suggestion a person confirms (spec 2.3 invariant 5); the gateway triggers nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ValidationError

from carto_common.ids import new_ulid
from carto_common.logging import get_logger
from carto_core.llm_gateway.audit import AuditSinkError, CallRecord, CallStatus
from carto_core.llm_gateway.payload import TOKEN_SEARCH
from carto_core.llm_gateway.prompts import fence_payload, new_nonce, output_schema, task_prompt
from carto_core.llm_gateway.providers import Completion, TransientProviderError, UsageProvider
from carto_core.llm_gateway.schemas import (
    LabelFieldRequest,
    NameEntityRequest,
    NameNodeRequest,
    Provider,
    ProviderError,
    Suggestion,
    SuggestionList,
    TaskKind,
)

if TYPE_CHECKING:
    from carto_common.settings import LlmSettings
    from carto_core.llm_gateway.audit import CallAuditSink

__all__ = [
    "MAX_ATTEMPTS",
    "MAX_OUTPUT_CHARS",
    "METRIC_NAMES",
    "GatewayDisabledError",
    "GatewayMetrics",
    "InvalidOutputError",
    "LlmGateway",
    "PayloadRefusedError",
    "parse_suggestions",
]

log = get_logger(component="llm_gateway")

MAX_ATTEMPTS: Final = 3
"""One call plus at most two retries on transient provider errors."""
MAX_OUTPUT_CHARS: Final = 64 * 1024
"""Longest answer parsed; a full suggestion list is under 20 KiB."""
RETRY_BACKOFF_SECONDS: Final = (0.5, 2.0)

METRIC_NAMES: Final[tuple[tuple[str, str], ...]] = (
    ("carto_llm_calls_total", "Provider calls made (every attempt counts)."),
    ("carto_llm_invalid_output_total", "Answers discarded because they failed the schema."),
    ("carto_llm_refused_total", "Requests refused before any call (payload not a task model)."),
    ("carto_llm_provider_errors_total", "Calls that ended in a provider error."),
    ("carto_llm_retries_total", "Calls repeated after a transient provider error."),
    (
        "carto_llm_suggestions_dropped_total",
        "Suggestions dropped (unknown target, duplicate, cap).",
    ),
    ("carto_llm_audit_failures_total", "Audit rows that could not be written."),
)

_CODE_FENCE: Final = re.compile(
    r"\A\s*```[A-Za-z0-9_-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```\s*\Z", re.DOTALL
)

_REQUEST_MODELS: Final[dict[TaskKind, type[BaseModel]]] = {
    TaskKind.LABEL_FIELD: LabelFieldRequest,
    TaskKind.NAME_ENTITY: NameEntityRequest,
    TaskKind.NAME_NODE: NameNodeRequest,
}


class GatewayDisabledError(Exception):
    """``llm.enabled`` is false: use the heuristic dictionary (spec 9.9)."""


class PayloadRefusedError(ValueError):
    """The request is not the task's model; the message names the reason, never the content."""


class InvalidOutputError(ValueError):
    """The provider's answer failed the output contract; the message names the reason only."""


class GatewayMetrics:
    """Hand-rolled counters rendered in the Prometheus text format (spec 16)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = {name: 0 for name, _help in METRIC_NAMES}

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._values[name] += amount

    def get(self, name: str) -> int:
        with self._lock:
            return self._values[name]

    def render(self) -> str:
        with self._lock:
            values = dict(self._values)
        lines: list[str] = []
        for name, help_text in METRIC_NAMES:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {values[name]}")
        return "\n".join(lines) + "\n"


def _reject_constant(name: str) -> object:
    msg = f"JSON constant {name} is not allowed"
    raise ValueError(msg)


def _strip_code_fence(text: str) -> str:
    match = _CODE_FENCE.match(text)
    return match.group(1) if match else text


def parse_suggestions(
    text: str, allowed_targets: frozenset[str], cap: int
) -> tuple[SuggestionList, int]:
    """Parse and filter the provider's answer (module docstring, "Strict output").

    Returns the list and the number of suggestions dropped; raises :class:`InvalidOutputError`.
    """
    if len(text) > MAX_OUTPUT_CHARS:
        msg = "output longer than the limit"
        raise InvalidOutputError(msg)
    body = _strip_code_fence(text).strip()
    try:
        parsed: object = json.loads(body, parse_constant=_reject_constant)
    except ValueError:
        msg = "output is not JSON"
        raise InvalidOutputError(msg) from None
    if not isinstance(parsed, dict):
        msg = "output is not a JSON object"
        raise InvalidOutputError(msg)
    try:
        candidate = SuggestionList.model_validate(parsed)
    except ValidationError as exc:
        msg = f"output failed the schema ({exc.error_count()} errors)"
        raise InvalidOutputError(msg) from None
    kept: list[Suggestion] = []
    seen: set[str] = set()
    dropped = 0
    for suggestion in candidate.suggestions:
        if TOKEN_SEARCH.search(suggestion.label) or TOKEN_SEARCH.search(suggestion.rationale):
            msg = "output carries a token-shaped string"
            raise InvalidOutputError(msg)
        if (
            suggestion.target_id not in allowed_targets
            or suggestion.target_id in seen
            or len(kept) >= cap
        ):
            dropped += 1
            continue
        seen.add(suggestion.target_id)
        kept.append(suggestion)
    return SuggestionList(suggestions=kept), dropped


def _targets(request: BaseModel) -> tuple[frozenset[str], int]:
    if isinstance(request, LabelFieldRequest):
        ids = frozenset(summary.field_id for summary in request.fields)
        return ids, len(ids)
    if isinstance(request, NameEntityRequest):
        return frozenset({request.entity_id}), 1
    if isinstance(request, NameNodeRequest):
        return frozenset({request.node_id}), 1
    msg = "request is not a task model"  # pragma: no cover - _validated refuses it first
    raise PayloadRefusedError(msg)


class LlmGateway:
    """The one door to the provider (module docstring)."""

    def __init__(
        self,
        settings: LlmSettings,
        provider: Provider,
        audit_sink: CallAuditSink,
        clock: Callable[[], datetime] | None = None,
        *,
        metrics: GatewayMetrics | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        tenant_id_max_len: int = 64,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._audit = audit_sink
        self._clock = clock if clock is not None else _utc_now
        self.metrics = metrics if metrics is not None else GatewayMetrics()
        self._monotonic = monotonic
        self._sleep = sleep
        self._tenant_id_max_len = tenant_id_max_len
        self._model = str(getattr(provider, "model", settings.model or ""))[:128]

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def suggest_labels(self, tenant_id: str, request: LabelFieldRequest) -> SuggestionList:
        """Field labels for ``request.fields``; at most one suggestion per field."""
        return self._run(tenant_id, TaskKind.LABEL_FIELD, request)

    def suggest_entity_name(self, tenant_id: str, request: NameEntityRequest) -> SuggestionList:
        """One name for ``request.entity_id`` from its member fields."""
        return self._run(tenant_id, TaskKind.NAME_ENTITY, request)

    def suggest_node_name(self, tenant_id: str, request: NameNodeRequest) -> SuggestionList:
        """One business-language name for ``request.node_id`` (M3 uses it)."""
        return self._run(tenant_id, TaskKind.NAME_NODE, request)

    # -----------------------------------------------------------------------------------------

    def _run(self, tenant_id: str, task: TaskKind, request: object) -> SuggestionList:
        if not self._settings.enabled:
            msg = "llm.enabled is false"
            raise GatewayDisabledError(msg)
        tenant = tenant_id[: self._tenant_id_max_len]
        try:
            validated = self._validated(task, request)
        except PayloadRefusedError as exc:
            self.metrics.inc("carto_llm_refused_total")
            self._record(tenant, task, "", None, 0, CallStatus.REFUSED, type(exc).__name__)
            log.warning("llm request refused", task=task.value, reason=str(exc))
            raise
        payload_json = validated.model_dump_json()
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        allowed, cap = _targets(validated)
        nonce = new_nonce()
        prompt = task_prompt(task, nonce)
        fenced = fence_payload(payload_json, nonce)
        schema = output_schema()

        completion: Completion | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self.metrics.inc("carto_llm_retries_total")
                self._sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])
            started = self._monotonic()
            try:
                completion = self._complete(prompt, fenced, schema)
            except ProviderError as exc:
                latency = self._latency_ms(started)
                self.metrics.inc("carto_llm_calls_total")
                self.metrics.inc("carto_llm_provider_errors_total")
                self._record(
                    tenant,
                    task,
                    digest,
                    None,
                    latency,
                    CallStatus.PROVIDER_ERROR,
                    type(exc).__name__,
                )
                log.warning(
                    "llm call failed",
                    task=task.value,
                    status=CallStatus.PROVIDER_ERROR.value,
                    latency_ms=latency,
                    attempt=attempt + 1,
                    error=type(exc).__name__,
                )
                transient = isinstance(exc, TransientProviderError)
                if not transient or attempt == MAX_ATTEMPTS - 1:
                    raise
                continue
            latency = self._latency_ms(started)
            self.metrics.inc("carto_llm_calls_total")
            break
        if completion is None:  # pragma: no cover - the loop raises or breaks
            msg = "no completion"
            raise ProviderError(msg)

        try:
            suggestions, dropped = parse_suggestions(completion.text, allowed, cap)
        except InvalidOutputError as exc:
            self.metrics.inc("carto_llm_invalid_output_total")
            self._record(
                tenant,
                task,
                digest,
                completion,
                latency,
                CallStatus.INVALID_OUTPUT,
                type(exc).__name__,
            )
            log.warning(
                "llm output discarded",
                task=task.value,
                status=CallStatus.INVALID_OUTPUT.value,
                latency_ms=latency,
                reason=str(exc),
            )
            return SuggestionList(suggestions=[])
        if dropped:
            self.metrics.inc("carto_llm_suggestions_dropped_total", dropped)
        self._record(tenant, task, digest, completion, latency, CallStatus.OK, None)
        log.info(
            "llm call ok",
            task=task.value,
            status=CallStatus.OK.value,
            latency_ms=latency,
            suggestions=len(suggestions.suggestions),
            dropped=dropped,
        )
        return suggestions

    def _validated(self, task: TaskKind, request: object) -> BaseModel:
        model = _REQUEST_MODELS[task]
        if not isinstance(request, BaseModel) or type(request) is not model:
            msg = f"{task.value} takes a {model.__name__}, not {type(request).__name__}"
            raise PayloadRefusedError(msg)
        try:
            validated = model.model_validate(request.model_dump())
        except ValidationError as exc:
            msg = f"{task.value} payload failed the schema ({exc.error_count()} errors)"
            raise PayloadRefusedError(msg) from None
        if getattr(validated, "task", None) is not task:
            msg = f"{task.value} payload names another task"
            raise PayloadRefusedError(msg)
        return validated

    def _complete(self, prompt: str, fenced: str, schema: dict[str, object]) -> Completion:
        if isinstance(self._provider, UsageProvider):
            return self._provider.complete_with_usage(prompt, fenced, schema)
        return Completion(self._provider.complete(prompt, fenced, schema))

    def _latency_ms(self, started: float) -> int:
        return max(0, round((self._monotonic() - started) * 1000))

    def _record(
        self,
        tenant_id: str,
        task: TaskKind,
        digest: str,
        completion: Completion | None,
        latency_ms: int,
        status: CallStatus,
        error_class: str | None,
    ) -> None:
        record = CallRecord(
            id=new_ulid(),
            tenant_id=tenant_id,
            at=self._clock(),
            task=task,
            payload_sha256=digest,
            provider=self._provider.name,
            model=self._model,
            input_tokens=completion.input_tokens if completion else None,
            output_tokens=completion.output_tokens if completion else None,
            latency_ms=latency_ms,
            status=status,
            error_class=error_class,
        )
        try:
            self._audit.record(record)
        except AuditSinkError as exc:
            self.metrics.inc("carto_llm_audit_failures_total")
            log.error("llm audit row not written", task=task.value, error=str(exc))


def _utc_now() -> datetime:
    return datetime.now(UTC)
