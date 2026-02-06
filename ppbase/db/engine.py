"""Async SQLAlchemy engine and session management."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_engine(
    database_url: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 20,
    echo: bool = False,
) -> AsyncEngine:
    """Create the async engine and session factory.

    Args:
        database_url: SQLAlchemy-style async database URL
            (e.g. ``postgresql+asyncpg://localhost:5432/ppbase``).
        pool_size: Core connection pool size.
        max_overflow: Maximum number of connections above *pool_size*.
        echo: If ``True``, log all SQL statements.

    Returns:
        The newly created :class:`AsyncEngine`.
    """
    global _engine, _session_factory

    _engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        echo=echo,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _engine


async def close_engine() -> None:
    """Dispose of the engine and release all pooled connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


def get_engine() -> AsyncEngine:
    """Return the current engine, raising if not yet initialised."""
    if _engine is None:
        raise RuntimeError(
            "Database engine not initialised. Call init_engine() first."
        )
    return _engine


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session for FastAPI dependency injection.

    Usage::

        @app.get("/example")
        async def example(session: AsyncSession = Depends(get_async_session)):
            ...
    """
    if _session_factory is None:
        raise RuntimeError(
            "Session factory not initialised. Call init_engine() first."
        )
    async with _session_factory() as session:
        yield session
