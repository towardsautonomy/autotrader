from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import inspect, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.models import Base

logger = logging.getLogger(__name__)


def make_engine(database_url: str | None = None) -> AsyncEngine:
    url = database_url or get_settings().database_url
    return create_async_engine(url, echo=False, future=True)


_engine: AsyncEngine | None = None
_SessionLocal: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine()
        _SessionLocal = async_sessionmaker(
            _engine, expire_on_commit=False, class_=AsyncSession
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


async def init_db() -> None:
    """Create all tables and add any missing columns to existing tables.

    Dev/test convenience — no Alembic. Handles the common case where a
    pre-existing SQLite DB is missing columns we've added to the models
    since its last run. Only ADD COLUMN (never drop/rename), so it's safe
    to run on every boot.
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(sync_conn) -> None:
    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl = _column_add_ddl(table.name, column)
            if ddl is None:
                continue
            logger.warning(
                "adding missing column %s.%s via ALTER TABLE",
                table.name,
                column.name,
            )
            sync_conn.execute(text(ddl))


def _column_add_ddl(table_name: str, column) -> str | None:
    """Build an `ALTER TABLE ... ADD COLUMN` DDL string for SQLite.

    Skips columns SQLite refuses to add online (PK, unique, non-constant
    defaults). For the shapes we actually use (nullable floats/ints/strings
    with literal defaults, JSON columns) this is enough.
    """
    if column.primary_key:
        return None
    try:
        col_type = column.type.compile(dialect=sqlite.dialect())
    except Exception:
        col_type = str(column.type)
    parts = [f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}']
    default = column.default
    if default is not None and getattr(default, "is_scalar", False):
        value = default.arg
        if isinstance(value, bool):
            parts.append(f"DEFAULT {1 if value else 0}")
        elif isinstance(value, (int, float)):
            parts.append(f"DEFAULT {value}")
        elif isinstance(value, str):
            escaped = value.replace("'", "''")
            parts.append(f"DEFAULT '{escaped}'")
    if not column.nullable and "DEFAULT" not in " ".join(parts):
        # SQLite requires a default when adding a NOT NULL column to an
        # existing table. Skip such columns — operator must recreate the DB.
        logger.warning(
            "cannot add NOT NULL column %s.%s without default — skipping",
            table_name,
            column.name,
        )
        return None
    return " ".join(parts)
