"""Async SQLAlchemy session factory used by application processes."""
from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def async_database_url() -> str:
    """Read DATABASE_URL and normalize the PostgreSQL psycopg driver name."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("Set DATABASE_URL for the application database")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


def create_async_database_engine() -> AsyncEngine:
    return create_async_engine(async_database_url(), pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
