"""Per-tenant pass locks (plan M3 decision 2; ADR 0060).

The job lease is not a lock: the queue's dedupe matches unleased rows only, ``ensure_recurring``
enqueues every loop and an expired lease is retaken, so two workers can run the same pass at
once, and the M2 passes' concurrent ``replace_all`` calls would delete each other's entities.
:func:`postgres_pass_lock` gives the worker a try-lock per ``(tenant, pass)``: a PostgreSQL
session advisory lock on ``hashtext(key)`` held on its own autocommit connection for the
duration of the pass and released explicitly (the connection is discarded when the release
fails, so a pooled connection never keeps the lock). The key differs from the queue's dedupe
key (``tenant/kind``), which would otherwise block every ``ensure_recurring`` behind a running
pass. A worker that does not get the lock skips the round; the job re-queues as usual.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING

import sqlalchemy
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = ["PassLock", "pass_lock_key", "postgres_pass_lock"]

PassLock = Callable[[str], AbstractContextManager[bool]]
"""``with lock(key) as taken:`` runs the block with the lock when ``taken`` is true."""

_TRY_LOCK = sqlalchemy.text("SELECT pg_try_advisory_lock(hashtext(:key))")
_UNLOCK = sqlalchemy.text("SELECT pg_advisory_unlock(hashtext(:key))")


def pass_lock_key(tenant_id: str, kind: str) -> str:
    """``<tenant>/<kind>-pass``: distinct from the queue's dedupe key ``<tenant>/<kind>``."""
    return f"{tenant_id}/{kind}-pass"


def postgres_pass_lock(engine: Engine) -> PassLock:
    """A :data:`PassLock` over ``engine`` (module docstring)."""

    @contextmanager
    def take(key: str) -> Iterator[bool]:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            taken = bool(connection.execute(_TRY_LOCK, {"key": key}).scalar_one())
            try:
                yield taken
            finally:
                if taken:
                    try:
                        connection.execute(_UNLOCK, {"key": key})
                    except SQLAlchemyError:
                        connection.invalidate()
                        raise

    return take
