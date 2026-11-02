"""The fake provider: canned answers and deliberate misbehaviour for tests (ADR 0054).

Never registered: :func:`carto_core.llm_gateway.registry.provider_for` cannot return it, and
``LlmProvider`` has no ``fake`` member, so a service cannot be configured with it. Tests build
it directly. It captures every call (task prompt, payload text, output schema) so a test can
inspect what a real provider would have received, and it answers per :class:`FakeBehaviour`:

- ``BEHAVE``: a valid suggestion for every target in the payload;
- ``OBEY_INJECTION``: the attacker's label text for every target, as a model that followed an
  instruction in the data would;
- ``EXTRA_FIELDS``: valid suggestions plus keys the schema does not have;
- ``INVALID_JSON``: prose instead of JSON;
- ``TOKEN_LABEL``: a label that carries a token-shaped string (which the schema pattern alone
  would accept);
- ``OVERSIZE_LABEL``: labels longer than the limit;
- ``UNKNOWN_TARGET``: suggestions for targets that are not in the request;
- ``FENCED_JSON``: a valid object inside a markdown code fence (accepted by the gateway);
- ``JSON_ARRAY``: a JSON array instead of an object;
- ``ERROR``: a :class:`~carto_core.llm_gateway.schemas.ProviderError` every time.

``script`` overrides the behaviour call by call: each entry is a text to return or an
exception to raise, consumed in order; when it runs out the behaviour applies again.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from carto_core.llm_gateway.providers import Completion
from carto_core.llm_gateway.schemas import MAX_LABEL_LEN, LabelType, ProviderError

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["CapturedCall", "FakeBehaviour", "FakeProvider"]


class FakeBehaviour(StrEnum):
    BEHAVE = "behave"
    OBEY_INJECTION = "obey_injection"
    EXTRA_FIELDS = "extra_fields"
    INVALID_JSON = "invalid_json"
    TOKEN_LABEL = "token_label"  # noqa: S105  # nosec B105 - test double, not a credential
    OVERSIZE_LABEL = "oversize_label"
    UNKNOWN_TARGET = "unknown_target"
    FENCED_JSON = "fenced_json"
    JSON_ARRAY = "json_array"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CapturedCall:
    task_prompt: str
    payload_json: str
    output_schema: dict[str, object]


_FAKE_TOKEN = "t1.AAAAAAAAAAAAAAAAAAAAAA"  # noqa: S105  # nosec B105 - test double, not a credential


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        behaviour: FakeBehaviour = FakeBehaviour.BEHAVE,
        *,
        attacker_label: str = "PWNED",
        script: Iterable[str | Exception] = (),
        input_tokens: int | None = 120,
        output_tokens: int | None = 40,
    ) -> None:
        self.behaviour = behaviour
        self.attacker_label = attacker_label
        self.calls: list[CapturedCall] = []
        self._script: deque[str | Exception] = deque(script)
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens

    @property
    def model(self) -> str:
        return "fake-1"

    def complete(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> str:
        return self.complete_with_usage(task_prompt, payload_json, output_schema).text

    def complete_with_usage(
        self, task_prompt: str, payload_json: str, output_schema: dict[str, object]
    ) -> Completion:
        self.calls.append(CapturedCall(task_prompt, payload_json, output_schema))
        if self._script:
            step = self._script.popleft()
            if isinstance(step, Exception):
                raise step
            return Completion(step, self._input_tokens, self._output_tokens)
        return Completion(
            self._answer(payload_json, self.behaviour), self._input_tokens, self._output_tokens
        )

    def _answer(self, payload_json: str, behaviour: FakeBehaviour) -> str:
        targets = _targets(payload_json)
        if behaviour is FakeBehaviour.ERROR:
            msg = "fake: http 5xx"
            raise ProviderError(msg)
        if behaviour is FakeBehaviour.INVALID_JSON:
            return "Sure! Here are the labels you asked for: order number, PO reference."
        if behaviour is FakeBehaviour.JSON_ARRAY:
            return json.dumps([_suggestion(target, "Order number") for target in targets])
        if behaviour is FakeBehaviour.OBEY_INJECTION:
            return _dump(
                [
                    _suggestion(target, self.attacker_label, rationale=self.attacker_label)
                    for target in targets
                ]
            )
        if behaviour is FakeBehaviour.EXTRA_FIELDS:
            suggestions = [_suggestion(target, "Order number") for target in targets]
            for suggestion in suggestions:
                suggestion["action"] = "reveal"
            return json.dumps({"suggestions": suggestions, "tool_calls": [{"name": "reveal"}]})
        if behaviour is FakeBehaviour.TOKEN_LABEL:
            return _dump([_suggestion(target, _FAKE_TOKEN) for target in targets])
        if behaviour is FakeBehaviour.OVERSIZE_LABEL:
            return _dump([_suggestion(target, "O" * (MAX_LABEL_LEN + 1)) for target in targets])
        if behaviour is FakeBehaviour.UNKNOWN_TARGET:
            return _dump([_suggestion(f"not-{target}", "Order number") for target in targets])
        text = _dump([_suggestion(target, "Order number") for target in targets])
        if behaviour is FakeBehaviour.FENCED_JSON:
            return f"```json\n{text}\n```"
        return text


def _targets(payload_json: str) -> list[str]:
    """The target ids a real model would copy from the data; empty when the payload is odd."""
    payload = _loads(payload_json)
    if payload is None:
        # The gateway fences the payload: the JSON document sits between the fence lines.
        lines = payload_json.splitlines()
        payload = _loads("\n".join(lines[1:-1])) if len(lines) >= 3 else None
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("entity_id"), str):
        return [str(payload["entity_id"])]
    if isinstance(payload.get("node_id"), str):
        return [str(payload["node_id"])]
    fields = payload.get("fields")
    if not isinstance(fields, list):
        return []
    return [
        str(entry["field_id"])
        for entry in fields
        if isinstance(entry, dict) and isinstance(entry.get("field_id"), str)
    ]


def _loads(text: str) -> object | None:
    try:
        parsed: object = json.loads(text)
    except ValueError:
        return None
    return parsed


def _suggestion(target: str, label: str, *, rationale: str = "looks like one") -> dict[str, object]:
    return {
        "target_id": target,
        "label_type": LabelType.ORDER.value,
        "label": label,
        "confidence": 0.8,
        "rationale": rationale,
    }


def _dump(suggestions: list[dict[str, object]]) -> str:
    return json.dumps({"suggestions": suggestions})
