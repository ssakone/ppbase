"""Transactional coordination for collection snapshot generation."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.db.system_tables import CollectionRecord, MigrationRecord
from ppbase.services.migration_generator import generate_snapshot_migration
from ppbase.services.migration_runner import (
    MigrationCommitOutcomeError,
    commit_migration_transaction,
    migration_lock,
)


async def _load_view_dependencies(
    session: AsyncSession,
) -> dict[str, set[str]]:
    """Return public view-to-view dependencies from PostgreSQL catalogs."""
    result = await session.execute(text("""
        SELECT DISTINCT
            dependent.relname AS view_name,
            referenced.relname AS dependency_name
        FROM pg_rewrite AS rewrite
        JOIN pg_class AS dependent
            ON dependent.oid = rewrite.ev_class
        JOIN pg_namespace AS dependent_namespace
            ON dependent_namespace.oid = dependent.relnamespace
        JOIN pg_depend AS dependency
            ON dependency.classid = 'pg_rewrite'::regclass
            AND dependency.objid = rewrite.oid
            AND dependency.refclassid = 'pg_class'::regclass
        JOIN pg_class AS referenced
            ON referenced.oid = dependency.refobjid
        JOIN pg_namespace AS referenced_namespace
            ON referenced_namespace.oid = referenced.relnamespace
        WHERE dependent_namespace.nspname = 'public'
            AND referenced_namespace.nspname = 'public'
            AND dependent.relkind IN ('v', 'm')
            AND referenced.relkind IN ('v', 'm')
            AND dependent.oid <> referenced.oid
        ORDER BY dependent.relname, referenced.relname
    """))

    dependencies: dict[str, set[str]] = {}
    for view_name, dependency_name in result.all():
        dependencies.setdefault(str(view_name), set()).add(str(dependency_name))
    return dependencies


async def create_migration_snapshot(
    engine: AsyncEngine,
    migrations_dir: str | Path,
    *,
    lock_timeout_seconds: float = 30.0,
) -> str:
    """Create one snapshot file and register it as already applied.

    The advisory lock prevents a migration runner from racing the database
    read/history insert.  Filesystem writes cannot be part of a PostgreSQL
    transaction. A known rollback removes the new file; an ambiguous COMMIT
    preserves it so a later runner can reconcile the durable snapshot intent.
    """
    async with migration_lock(
        engine,
        timeout_seconds=lock_timeout_seconds,
    ) as connection:
        generated_path: Path | None = None
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            await session.begin()
            try:
                result = await session.execute(
                    select(CollectionRecord).order_by(
                        CollectionRecord.type,
                        CollectionRecord.name,
                        CollectionRecord.id,
                    )
                )
                collections = list(result.scalars().all())
                view_dependencies = await _load_view_dependencies(session)
                generated_path = Path(
                    generate_snapshot_migration(
                        collections,
                        migrations_dir,
                        view_dependencies=view_dependencies,
                    )
                )
                session.add(MigrationRecord(file=generated_path.name))
                await session.flush()
            except BaseException:
                await session.rollback()
                if generated_path is not None:
                    generated_path.unlink(missing_ok=True)
                raise

            try:
                await commit_migration_transaction(
                    session,
                    connection,
                    generated_path.name,
                    expected_applied=True,
                )
            except MigrationCommitOutcomeError:
                # The exact outcome or lock lease is uncertain. Keep the file:
                # it is the only durable recovery intent outside PostgreSQL.
                raise
            except BaseException:
                generated_path.unlink(missing_ok=True)
                raise

        if generated_path is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("Snapshot generation did not produce a file.")
        return str(generated_path)
