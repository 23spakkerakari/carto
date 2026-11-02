"""The per-call audit of the LLM gateway (spec 9.9 "Every call is audited"; plan M2 package G).

One :class:`CallRecord` per provider call: task, SHA-256 of the payload JSON, provider and model
names, token counts, latency, result status and the error class when there was one. Never the
payload, the prompt or the output: the hash lets an operator tie a call to a payload they can
rebuild from the metadata tables without the gateway keeping a copy.

:class:`CallAuditSink` is the seam; :class:`InMemoryCallAuditSink` serves tests and the offline
path and :class:`PostgresCallAuditSink` writes ``llm_calls`` (migration 0004, package A). A sink
failure is reported as :class:`AuditSinkError` and the gateway counts it; the suggestion is not
withheld because the audit row failed, the operator sees the counter.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine

    from carto_core.llm_gateway.schemas import TaskKind

__all__ = [
    "AuditSinkError",
    "CallAuditSink",
    "CallRecord",
    "CallStatus",
    "InMemoryCallAuditSink",
    "PostgresCallAuditSink",
]


class CallStatus(StrEnum):
    OK = "ok"
    INVALID_OUTPUT = "invalid_output"
    PROVIDER_ERROR = "provider_error"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One row of ``llm_calls``. ``payload_sha256`` is hex; ``latency_ms`` is wall time."""

    id: str
    tenant_id: str
    at: datetime
    task: TaskKind
    payload_sha256: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    status: CallStatus
    error_class: str | None = None


class AuditSinkError(Exception):
    """The audit row could not be written; the message names the error class only."""


@runtime_checkable
class CallAuditSink(Protocol):
    def record(self, record: CallRecord) -> None: ...


class InMemoryCallAuditSink:
    """Keeps the records in order; ``records`` is a snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[CallRecord] = []

    def record(self, record: CallRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> tuple[CallRecord, ...]:
        with self._lock:
            return tuple(self._records)


_INSERT = sqlalchemy.text(
    "INSERT INTO llm_calls "
    "(id, tenant_id, at, task, payload_sha256, provider, model, input_tokens, output_tokens, "
    "latency_ms, status, error_class) "
    "VALUES (:id, :tenant_id, :at, :task, :payload_sha256, :provider, :model, :input_tokens, "
    ":output_tokens, :latency_ms, :status, :error_class)"
).bindparams(sqlalchemy.bindparam("at", type_=sqlalchemy.DateTime(timezone=True)))


class PostgresCallAuditSink:
    """``llm_calls`` through SQLAlchemy Core; ``created_at``/``updated_at`` take their defaults."""

    __slots__ = ("_engine",)

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, record: CallRecord) -> None:
        parameters = {
            "id": record.id,
            "tenant_id": record.tenant_id,
            "at": record.at,
            "task": record.task.value,
            "payload_sha256": record.payload_sha256,
            "provider": record.provider,
            "model": record.model,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "latency_ms": record.latency_ms,
            "status": record.status.value,
            "error_class": record.error_class,
        }
        try:
            with self._engine.begin() as connection:
                connection.execute(_INSERT, parameters)
        except SQLAlchemyError as exc:
            msg = f"llm_calls insert failed: {type(exc).__name__}"
            raise AuditSinkError(msg) from exc
