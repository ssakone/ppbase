"""Shared PostgreSQL bootstrap and application-migration preparation.

The startup server, migration CLI, and migrations API all use this module so
that a blank database is initialized in the same order everywhere:

1. fixed PPBase system tables;
2. PocketBase-compatible system collections and the stable ``users`` auth
   collection;
3. application migration files, one committed transaction per file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ppbase.db.bootstrap import bootstrap_system_collections
from ppbase.db.system_tables import Base, CollectionRecord
from ppbase.services.auth_service import generate_default_auth_options
from ppbase.services.migration_runner import (
    apply_pending_on_connection,
    migration_lock,
    revert_on_connection,
)


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` without mutating either."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


async def _backfill_auth_options(session: AsyncSession) -> None:
    """Fill missing auth runtime secrets without replacing existing values."""
    result = await session.execute(
        select(CollectionRecord).where(CollectionRecord.type == "auth")
    )
    for collection in result.scalars().all():
        existing = collection.options if isinstance(collection.options, dict) else {}
        defaults = generate_default_auth_options(
            is_superusers=collection.name == "_superusers"
        )
        merged = _deep_merge_dicts(defaults, existing)
        if merged != existing:
            collection.options = merged
            flag_modified(collection, "options")


async def bootstrap_on_connection(connection: AsyncConnection) -> None:
    """Bootstrap fixed and dynamic system schema in one root transaction."""
    async with connection.begin():
        await connection.run_sync(Base.metadata.create_all)

        # The outer connection transaction is the authoritative boundary.
        # A savepoint-backed ORM session lets bootstrap helpers use normal ORM
        # operations while all DDL remains on this same connection.
        async with AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ) as session:
            async with session.begin():
                await bootstrap_system_collections(session, session)
                await _backfill_auth_options(session)


async def prepare_database(
    engine: AsyncEngine,
    migrations_dir: str | Path,
    *,
    apply_migrations: bool = True,
    lock_timeout_seconds: float = 30.0,
) -> list[str]:
    """Prepare a database under one PostgreSQL advisory-lock lease.

    Bootstrap is committed first.  Each application migration is then
    committed independently, so if migration N fails, migrations 1..N-1 stay
    applied and migration N leaves no metadata, DDL, or history trace.
    """
    async with migration_lock(
        engine,
        timeout_seconds=lock_timeout_seconds,
    ) as connection:
        await bootstrap_on_connection(connection)
        if not apply_migrations:
            return []
        return await apply_pending_on_connection(connection, migrations_dir)


async def prepare_database_and_revert(
    engine: AsyncEngine,
    migrations_dir: str | Path,
    *,
    count: int = 1,
    lock_timeout_seconds: float = 30.0,
) -> list[str]:
    """Bootstrap and revert migrations under one uninterrupted lock lease."""
    async with migration_lock(
        engine,
        timeout_seconds=lock_timeout_seconds,
    ) as connection:
        await bootstrap_on_connection(connection)
        return await revert_on_connection(
            connection,
            migrations_dir,
            count=count,
        )
