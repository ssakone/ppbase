"""Migration runner for PPBase.

Provides the ``MigrationApp`` helper class (passed to migration up()/down()
functions) and runner functions that discover, apply, and revert migration
files stored on disk.

Migration files are plain Python modules with ``up(app)`` and ``down(app)``
async functions.  The runner dynamically imports them via ``importlib``.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ppbase.core.id_generator import generate_id
from ppbase.db.schema_manager import (
    create_collection_table,
    delete_collection_table,
    update_collection_table,
)
from ppbase.db.system_tables import CollectionRecord, MigrationRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MigrationApp — helper passed to each migration's up() / down()
# ---------------------------------------------------------------------------


class MigrationApp:
    """Context object passed to migration ``up()`` and ``down()`` functions.

    Wraps a database session and engine so that migration code can create,
    update, and delete collections (both ORM records and physical tables)
    without having to import PPBase internals directly.

    All operations share the same session / transaction.
    """

    def __init__(self, session: AsyncSession, engine: AsyncEngine) -> None:
        self.session = session
        self.engine = engine

    # -- collection helpers -------------------------------------------------

    async def create_collection(self, definition: dict) -> CollectionRecord:
        """Create a new collection record and its physical table.

        ``definition`` should contain at least ``name`` and optionally
        ``type``, ``schema``, ``options``, rule fields, etc.
        """
        now = datetime.now(timezone.utc)
        record = CollectionRecord(
            id=definition.get("id") or generate_id(),
            name=definition["name"],
            type=definition.get("type", "base"),
            system=definition.get("system", False),
            schema=definition.get("schema", []),
            indexes=definition.get("indexes", []),
            list_rule=definition.get("listRule"),
            view_rule=definition.get("viewRule"),
            create_rule=definition.get("createRule"),
            update_rule=definition.get("updateRule"),
            delete_rule=definition.get("deleteRule"),
            options=definition.get("options", {}),
            created=now,
            updated=now,
        )
        self.session.add(record)
        await self.session.flush()
        await create_collection_table(self.engine, record)
        return record

    async def update_collection(
        self, id_or_name: str, changes: dict
    ) -> CollectionRecord:
        """Update an existing collection and alter the physical table."""
        record = await self.find_collection(id_or_name)

        # Snapshot old state for schema diff
        class _Snapshot:
            def __init__(self, rec: CollectionRecord) -> None:
                self.name = rec.name
                self.type = rec.type
                self.schema = list(rec.schema) if isinstance(rec.schema, list) else []
                self.options = dict(rec.options) if isinstance(rec.options, dict) else {}

        old_snapshot = _Snapshot(record)

        # Apply changes
        if "name" in changes:
            record.name = changes["name"]
        if "type" in changes:
            record.type = changes["type"]
        if "system" in changes:
            record.system = changes["system"]
        if "schema" in changes:
            import copy
            record.schema = copy.deepcopy(changes["schema"])
            flag_modified(record, "schema")
        if "indexes" in changes:
            import copy
            record.indexes = copy.deepcopy(changes["indexes"])
            flag_modified(record, "indexes")
        if "listRule" in changes:
            record.list_rule = changes["listRule"]
        if "viewRule" in changes:
            record.view_rule = changes["viewRule"]
        if "createRule" in changes:
            record.create_rule = changes["createRule"]
        if "updateRule" in changes:
            record.update_rule = changes["updateRule"]
        if "deleteRule" in changes:
            record.delete_rule = changes["deleteRule"]
        if "options" in changes:
            import copy
            record.options = copy.deepcopy(changes["options"])
            flag_modified(record, "options")

        record.updated = datetime.now(timezone.utc)
        await self.session.flush()

        # Alter physical table if schema/name changed
        schema_changed = (
            old_snapshot.name != record.name
            or old_snapshot.schema
            != (record.schema if isinstance(record.schema, list) else [])
        )
        if schema_changed:
            await update_collection_table(self.engine, old_snapshot, record)

        return record

    async def delete_collection(self, id_or_name: str) -> None:
        """Delete a collection record and drop the physical table."""
        record = await self.find_collection(id_or_name)
        table_name = record.name
        col_type = record.type or "base"

        await self.session.delete(record)
        await self.session.flush()
        await delete_collection_table(self.engine, table_name, col_type)

    async def find_collection(self, id_or_name: str) -> CollectionRecord:
        """Find a collection by ID or name (case-insensitive)."""
        stmt = select(CollectionRecord).where(
            (CollectionRecord.id == id_or_name)
            | (func.lower(CollectionRecord.name) == id_or_name.lower())
        )
        result = await self.session.execute(stmt)
        record = result.scalars().first()
        if record is None:
            raise LookupError(f"Collection '{id_or_name}' not found.")
        return record

    async def save_collection(self, definition: dict) -> CollectionRecord:
        """Create or update a collection (upsert).

        If a collection with the given ``id`` or ``name`` already exists it is
        updated; otherwise a new one is created.
        """
        id_or_name = definition.get("id") or definition.get("name")
        if not id_or_name:
            raise ValueError("Collection definition must include 'id' or 'name'.")

        try:
            record = await self.find_collection(id_or_name)
        except LookupError:
            return await self.create_collection(definition)

        # Build changes dict from definition (exclude 'id')
        changes = {k: v for k, v in definition.items() if k != "id"}
        return await self.update_collection(record.id, changes)

    async def execute_sql(self, sql: str, params: dict | None = None) -> Any:
        """Execute raw SQL within the migration transaction."""
        result = await self.session.execute(text(sql), params or {})
        return result


# ---------------------------------------------------------------------------
# Migration file discovery helpers
# ---------------------------------------------------------------------------

_MIGRATION_FILE_PATTERN = ".py"


def _list_migration_files(migrations_dir: str | Path) -> list[str]:
    """Return sorted list of migration filenames from the directory.

    Migration files are ``*.py`` files whose names start with a digit
    (timestamp prefix).  They are sorted lexicographically so that
    timestamp-prefixed names run in chronological order.
    """
    dirpath = Path(migrations_dir)
    if not dirpath.is_dir():
        return []

    files = [
        f.name
        for f in sorted(dirpath.iterdir())
        if f.is_file()
        and f.suffix == _MIGRATION_FILE_PATTERN
        and f.name[0].isdigit()
        and f.name != "__init__.py"
    ]
    return files


def _load_migration_module(migration_file: str, migrations_dir: str | Path) -> Any:
    """Dynamically import a migration Python file and return the module."""
    filepath = Path(migrations_dir) / migration_file
    if not filepath.exists():
        raise FileNotFoundError(f"Migration file not found: {filepath}")

    module_name = f"ppbase_migration_{filepath.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(filepath))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load migration module: {filepath}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# Runner functions
# ---------------------------------------------------------------------------


async def get_applied_migrations(session: AsyncSession) -> list[MigrationRecord]:
    """Return all applied migration records ordered by filename."""
    stmt = select(MigrationRecord).order_by(MigrationRecord.file)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_pending_migrations(
    session: AsyncSession, migrations_dir: str | Path
) -> list[str]:
    """Return filenames of migrations not yet applied, sorted by timestamp."""
    all_files = _list_migration_files(migrations_dir)
    if not all_files:
        return []

    applied = await get_applied_migrations(session)
    applied_names = {m.file for m in applied}

    return [f for f in all_files if f not in applied_names]


async def apply_migration(
    session: AsyncSession,
    engine: AsyncEngine,
    migration_file: str,
    migrations_dir: str | Path,
) -> None:
    """Apply a single migration file.

    Loads the module, calls ``up(app)``, and records the migration in the
    ``_migrations`` table.  Uses a savepoint so that a failure rolls back
    only this migration without affecting the outer transaction.
    """
    module = _load_migration_module(migration_file, migrations_dir)

    up_fn = getattr(module, "up", None)
    if up_fn is None:
        raise AttributeError(
            f"Migration {migration_file} does not define an 'up' function."
        )

    logger.info("Applying migration: %s", migration_file)

    async with session.begin_nested():  # savepoint
        app = MigrationApp(session, engine)
        await up_fn(app)

        # Record the migration
        record = MigrationRecord(file=migration_file)
        session.add(record)

    logger.info("Applied migration: %s", migration_file)


async def revert_migration(
    session: AsyncSession,
    engine: AsyncEngine,
    migration_file: str,
    migrations_dir: str | Path,
) -> None:
    """Revert a single migration file.

    Loads the module, calls ``down(app)``, and removes the migration record
    from the ``_migrations`` table.  Uses a savepoint for isolation.
    """
    module = _load_migration_module(migration_file, migrations_dir)

    down_fn = getattr(module, "down", None)
    if down_fn is None:
        raise AttributeError(
            f"Migration {migration_file} does not define a 'down' function."
        )

    logger.info("Reverting migration: %s", migration_file)

    async with session.begin_nested():  # savepoint
        app = MigrationApp(session, engine)
        await down_fn(app)

        # Remove the migration record
        stmt = delete(MigrationRecord).where(MigrationRecord.file == migration_file)
        await session.execute(stmt)

    logger.info("Reverted migration: %s", migration_file)


async def apply_all_pending(
    session: AsyncSession,
    engine: AsyncEngine,
    migrations_dir: str | Path,
) -> list[str]:
    """Apply all pending migrations in order.

    Returns the list of filenames that were applied.
    """
    pending = await get_pending_migrations(session, migrations_dir)

    for migration_file in pending:
        await apply_migration(session, engine, migration_file, migrations_dir)

    return pending


async def get_migration_status(
    session: AsyncSession, migrations_dir: str | Path
) -> dict[str, Any]:
    """Return a summary dict with migration status information.

    Keys:
        applied: list of applied migration filenames
        pending: list of pending migration filenames
        total: total number of migration files
        applied_count: number of applied migrations
        pending_count: number of pending migrations
    """
    all_files = _list_migration_files(migrations_dir)
    applied_records = await get_applied_migrations(session)
    applied_names = [m.file for m in applied_records]
    pending = [f for f in all_files if f not in set(applied_names)]

    return {
        "applied": applied_names,
        "pending": pending,
        "total": len(all_files),
        "applied_count": len(applied_names),
        "pending_count": len(pending),
    }
