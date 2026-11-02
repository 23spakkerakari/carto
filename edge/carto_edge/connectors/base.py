"""The read-only connector interface (spec 8.1) and what every connector gets from the edge.

Spec 8.1 fixes the shape::

    class ReadConnector(Protocol):
        type: ClassVar[str]

        def validate_config(self, cfg: dict) -> ConnectorConfig: ...
        async def test(self) -> TestResult: ...
        async def read(self, cursor: Cursor | None) -> AsyncIterator[RawRecord]: ...
        async def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]: ...

``read`` may also yield :class:`~carto_edge.pipeline.model.CursorCheckpoint` items between
records (ADR 0035): a cursor with no record, committed once everything before it is durable, so
a cursor moves across stretches of the source that held nothing.

There is no write method anywhere in this package, and the Semgrep rule in ``tools/semgrep``
fails CI on calls that could write (spec 2.3 invariant 1). A connector gets credentials only
through :class:`SecretResolver` (spec 14.3: fetched at use, never stored or logged) and may
open a connection only to a host that :class:`NetworkPolicy` accepted (spec 14.7 SSRF rules).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from carto_edge.config import SourceConfig
from carto_edge.pipeline.model import RawRecord, ReadItem

__all__ = [
    "ConnectorConfig",
    "ConnectorContext",
    "ConnectorError",
    "ConnectorFactory",
    "Cursor",
    "InvalidConfigError",
    "NetworkPolicy",
    "ReadConnector",
    "ReadOnlyStatus",
    "ReadOnlyViolationError",
    "ResolvedHost",
    "SecretResolver",
    "TestCheck",
    "TestResult",
]

Cursor = Mapping[str, Any]
"""A connector's checkpoint: JSON-serializable, opaque to everything but the connector that made
it, persisted by the scheduler only after the records before it are durably buffered."""


class ConnectorError(Exception):
    """A connector failed. Messages never carry credentials or record values."""


class InvalidConfigError(ConnectorError):
    """The source's ``config`` block is not valid for this connector type."""


class ReadOnlyViolationError(ConnectorError):
    """The source credential can write, so the source must not be enabled (spec 8.1.4)."""


class ConnectorConfig(BaseModel):
    """Base for a connector's typed ``config`` block: unknown keys rejected, frozen."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ReadOnlyStatus(StrEnum):
    """What ``test()`` could establish about the credential (spec 8.1, 4.2 step 2)."""

    VERIFIED = "verified"
    """Grants or capabilities were inspected and allow reading only."""
    NOT_VERIFIABLE = "not_verifiable"
    """The system exposes no way to check (the source may still be enabled; the install guide
    documents the manual check)."""
    WRITE_CAPABLE = "write_capable"
    """The credential can write: the source cannot be enabled without an audited override."""


@dataclass(frozen=True, slots=True)
class TestCheck:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class TestResult:
    """Outcome of ``test()``: connectivity plus read-only verification (spec 8.1)."""

    ok: bool
    read_only: ReadOnlyStatus
    checks: tuple[TestCheck, ...] = ()
    problems: tuple[str, ...] = ()
    visible: tuple[str, ...] = field(default=())
    """What the credential can see (Splunk indexes, tables, directories), for the admin."""

    @property
    def can_enable(self) -> bool:
        return self.ok and self.read_only is not ReadOnlyStatus.WRITE_CAPABLE


@dataclass(frozen=True, slots=True)
class ResolvedHost:
    """A host name the network policy accepted, with the address pinned for the connection."""

    host: str
    address: str
    port: int


@runtime_checkable
class SecretResolver(Protocol):
    """Resolves a ``secret_ref`` to its value at use time (spec 14.3)."""

    def resolve(self, secret_ref: str) -> str: ...


@runtime_checkable
class NetworkPolicy(Protocol):
    """Spec 14.7: resolve, reject loopback, link-local, metadata and unlisted private ranges,
    and pin the address."""

    def resolve(self, host: str, port: int) -> ResolvedHost: ...


@dataclass(frozen=True, slots=True)
class ConnectorContext:
    """What the edge hands a connector at construction."""

    secrets: SecretResolver
    network: NetworkPolicy
    tenant_id: str = "default"


@runtime_checkable
class ReadConnector(Protocol):
    """Spec 8.1. ``read`` is an async generator of :class:`RawRecord` and, between records,
    :class:`~carto_edge.pipeline.model.CursorCheckpoint` (ADR 0035); ``backfill`` yields records
    only (it has no cursor)."""

    type: ClassVar[str]
    source: SourceConfig

    def validate_config(self, cfg: Mapping[str, Any]) -> ConnectorConfig: ...

    async def test(self) -> TestResult: ...

    def read(self, cursor: Cursor | None) -> AsyncIterator[ReadItem]: ...

    def backfill(self, start: datetime, end: datetime) -> AsyncIterator[RawRecord]: ...

    async def close(self) -> None: ...


class ConnectorFactory(Protocol):
    def __call__(self, source: SourceConfig, context: ConnectorContext) -> ReadConnector: ...
