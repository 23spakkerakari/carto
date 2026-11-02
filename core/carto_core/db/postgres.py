"""SQLAlchemy engine for the PostgreSQL metadata store (spec 7.3, 14.4, 14.7).

psycopg 3 through SQLAlchemy 2. The password is read from ``password_file`` at engine creation
and lives only inside the :class:`sqlalchemy.engine.URL`, whose ``repr`` and ``str`` mask it, so
a logged engine never shows it. ``sslmode`` is passed to libpq as configured (default
``verify-full``) together with ``sslrootcert`` when a CA file is set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import sqlalchemy
from sqlalchemy import URL, Engine
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from carto_core.settings import PostgresSettings

__all__ = ["create_engine_from_settings", "engine_url", "ping_engine"]


def engine_url(settings: PostgresSettings) -> URL:
    """The connection URL with the password from the file and TLS parameters as configured."""
    query: dict[str, str] = {
        "sslmode": settings.sslmode,
        "connect_timeout": str(settings.connect_timeout_seconds),
        "application_name": "carto-core",
    }
    if settings.ca_file is not None:
        query["sslrootcert"] = str(settings.ca_file)
    return URL.create(
        "postgresql+psycopg",
        username=settings.user,
        password=settings.read_password() or None,
        host=settings.host,
        port=settings.port,
        database=settings.database,
        query=query,
    )


def create_engine_from_settings(settings: PostgresSettings) -> Engine:
    """A pooled engine with ``pool_pre_ping`` so a dropped connection is replaced, not surfaced."""
    return sqlalchemy.create_engine(engine_url(settings), pool_pre_ping=True)


def ping_engine(engine: Engine) -> bool:
    """``SELECT 1``; True when the database answers, never raises (readiness probes)."""
    try:
        with engine.connect() as connection:
            connection.execute(sqlalchemy.text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        return False
    return True
