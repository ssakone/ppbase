"""Business logic for collection management.

Handles CRUD operations on ``_collections`` records and coordinates with the
:mod:`ppbase.db.schema_manager` to create/alter/drop the corresponding
PostgreSQL tables.
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from ppbase.core.id_generator import generate_id
from ppbase.db.schema_manager import (
    create_collection_table,
    delete_collection_table,
    truncate_collection_table,
    update_collection_table,
)
from ppbase.db.system_tables import CollectionRecord
from ppbase.models.collection import (
    CollectionCreate,
    CollectionListResponse,
    CollectionResponse,
    CollectionUpdate,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RESERVED_NAMES = frozenset({
    "id",
    "created",
    "updated",
    "expand",
    "collectionid",
    "collectionname",
    "_collections",
    "_params",
    "_external_auths",
    "_externalauths",
    "_superusers",
    "_mfas",
    "_otps",
    "_authorigins",
    "_migrations",
    "users",
    "import",
})


def _validate_collection_name(name: str) -> None:
    if not name:
        raise ValueError("Collection name is required.")
    if not _NAME_RE.match(name):
        raise ValueError(
            "Collection name must start with a letter or underscore "
            "and contain only alphanumeric characters or underscores."
        )
    if name.lower() in _RESERVED_NAMES:
        raise ValueError(f"Collection name '{name}' is reserved.")


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return a new dict."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _find_collection(
    session: AsyncSession,
    id_or_name: str,
) -> CollectionRecord:
    """Look up a collection by ID or by name (case-insensitive)."""
    stmt = select(CollectionRecord).where(
        (CollectionRecord.id == id_or_name)
        | (func.lower(CollectionRecord.name) == id_or_name.lower())
    )
    result = await session.execute(stmt)
    record = result.scalars().first()
    if record is None:
        raise LookupError(f"Collection '{id_or_name}' not found.")
    return record


def _apply_sort(stmt: Any, sort_str: str) -> Any:
    """Apply simple sort expression to a SELECT statement."""
    if not sort_str:
        return stmt
    for part in sort_str.split(","):
        part = part.strip()
        if not part:
            continue
        desc = part.startswith("-")
        field_name = part.lstrip("+-").strip()
        col = getattr(CollectionRecord, field_name, None)
        if col is not None:
            stmt = stmt.order_by(col.desc() if desc else col.asc())
    return stmt


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


async def _record_migration(
    session: AsyncSession,
    migration_filename: str,
) -> None:
    """Record a migration file as applied in the _migrations table."""
    from ppbase.db.system_tables import MigrationRecord

    record = MigrationRecord(
        file=migration_filename,
        applied=datetime.now(timezone.utc),
    )
    session.add(record)
    await session.flush()


async def _maybe_generate_migration(
    session: AsyncSession,
    *,
    auto_migrate: bool,
    migrations_dir: str | None,
    kind: str,
    record: CollectionRecord | None = None,
    old_snapshot: Any | None = None,
) -> str | None:
    """Generate a migration file and record it as applied if auto_migrate is on.

    Args:
        session: The active database session (for recording the migration).
        auto_migrate: Whether auto-migration is enabled.
        migrations_dir: Directory to write migration files to.
        kind: One of "create", "update", "delete".
        record: The CollectionRecord (current state).
        old_snapshot: For updates, the snapshot of the old state.
    """
    if not auto_migrate or not migrations_dir:
        return None

    from ppbase.services.migration_generator import (
        generate_create_migration,
        generate_delete_migration,
        generate_update_migration,
    )

    os.makedirs(migrations_dir, exist_ok=True)

    generated_path: str | None = None
    try:
        if kind == "create" and record is not None:
            generated_path = generate_create_migration(record, migrations_dir)
        elif kind == "update" and old_snapshot is not None and record is not None:
            generated_path = generate_update_migration(
                old_snapshot,
                record,
                migrations_dir,
            )
        elif kind == "delete" and record is not None:
            generated_path = generate_delete_migration(record, migrations_dir)
        else:
            return None

        if generated_path:
            # Store just the basename, not the full path
            basename = os.path.basename(generated_path)
            await _record_migration(session, basename)
        return generated_path
    except BaseException:
        if generated_path:
            Path(generated_path).unlink(missing_ok=True)
        raise


def _cleanup_generated_migrations(paths: list[str]) -> None:
    """Remove only migration files created by the current failed mutation."""
    for path in paths:
        Path(path).unlink(missing_ok=True)


async def _recorded_generated_migrations(
    session: AsyncSession,
    filenames: list[str],
) -> set[str]:
    """Read matching migration history through the verifier transaction."""
    from ppbase.db.system_tables import MigrationRecord

    result = await session.execute(
        select(MigrationRecord.file).where(MigrationRecord.file.in_(filenames))
    )
    return set(result.scalars().all())


async def _reconcile_generated_migrations(
    engine: AsyncEngine,
    generated_paths: list[str],
    *,
    lock_timeout_seconds: float,
) -> set[str]:
    """Fence runners while verifying history and deleting rolled-back intent.

    The producer's transaction-level advisory lock necessarily ends with its
    uncertain COMMIT. A fresh verifier transaction therefore reacquires the
    same lock before consulting ``_migrations``. If a runner won the race, the
    verifier waits for it and observes its committed history. If the verifier
    wins, a known rollback is cleaned up before any runner can consume the
    generated file.
    """
    from ppbase.services.migration_runner import (
        acquire_migration_transaction_lock,
    )

    filenames = [Path(path).name for path in generated_paths]
    async with AsyncSession(bind=engine, expire_on_commit=False) as verifier:
        await acquire_migration_transaction_lock(
            verifier,
            timeout_seconds=lock_timeout_seconds,
        )
        committed = await _recorded_generated_migrations(verifier, filenames)
        if not committed:
            # Keep the verifier's transaction lock until every current intent
            # file is gone, preventing a runner from discovering it midway.
            _cleanup_generated_migrations(generated_paths)
        await verifier.rollback()
        return committed


async def _commit_with_generated_migrations(
    session: AsyncSession,
    engine: AsyncEngine,
    generated_paths: list[str],
    *,
    migration_lock_timeout: float = 30.0,
) -> None:
    """Commit schema/history and reconcile a potentially lost COMMIT ACK.

    The filesystem cannot participate in the PostgreSQL transaction. Migration
    files are therefore durable intent records published before COMMIT. On a
    commit error a fresh verifier reacquires the runner lock before reading
    ``_migrations``. A complete history match proves that the generated intent
    is now applied (either by the original COMMIT or by a runner that won the
    lock race); no matches while fenced allows cleanup. Any unverifiable or
    partial outcome preserves the files for operator recovery.
    """
    try:
        await session.commit()
    except BaseException as commit_error:
        try:
            await session.rollback()
        except Exception:
            logger.exception("Rollback failed after collection commit error")

        # Release the request connection before reconciliation so deployments
        # with pool_size=1 can still query the authoritative history row.
        try:
            await session.close()
        except Exception:
            logger.exception("Session close failed after collection commit error")

        if not generated_paths:
            raise

        from ppbase.services.migration_runner import MigrationCommitOutcomeError

        filenames = [Path(path).name for path in generated_paths]
        try:
            committed = await _reconcile_generated_migrations(
                engine,
                generated_paths,
                lock_timeout_seconds=migration_lock_timeout,
            )
        except BaseException as verify_error:
            raise MigrationCommitOutcomeError(
                "PostgreSQL commit outcome is unknown for generated migrations "
                f"{filenames!r}. Their files were preserved; inspect migration "
                "status before retrying the collection mutation."
            ) from verify_error

        expected = set(filenames)
        if committed == expected:
            logger.warning(
                "Collection COMMIT acknowledgement was lost, but migration "
                "history now confirms the generated intent for %s",
                filenames,
            )
            return
        if not committed:
            raise commit_error

        raise MigrationCommitOutcomeError(
            "PostgreSQL returned a partial migration-history match after a "
            f"commit error (expected {sorted(expected)!r}, found "
            f"{sorted(committed)!r}). Files were preserved; manual inspection "
            "is required before retrying."
        ) from commit_error


async def _lock_migration_producer(
    session: AsyncSession,
    *,
    migration_lock_timeout: float,
) -> None:
    """Serialize every collection mutation with migration runners."""

    from ppbase.services.migration_runner import (
        acquire_migration_transaction_lock,
    )

    await acquire_migration_transaction_lock(
        session,
        timeout_seconds=migration_lock_timeout,
    )


async def _generate_migration_or_rollback(
    session: AsyncSession,
    **kwargs: Any,
) -> str | None:
    """Generate/history-sync a file or roll back the collection mutation."""
    try:
        return await _maybe_generate_migration(session, **kwargs)
    except BaseException:
        try:
            await session.rollback()
        except Exception:
            logger.exception("Rollback failed after migration generation error")
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def list_collections(
    session: AsyncSession,
    *,
    page: int = 1,
    per_page: int = 30,
    sort: str = "",
    filter_str: str = "",
    skip_total: bool = False,
) -> CollectionListResponse:
    """List collections with pagination and sorting."""
    # Count total unless explicitly skipped
    total_items = -1
    if not skip_total:
        count_stmt = select(func.count(CollectionRecord.id))
        count_result = await session.execute(count_stmt)
        total_items = count_result.scalar_one()

    if total_items < 0:
        total_pages = -1
    else:
        total_pages = max(1, math.ceil(total_items / per_page)) if per_page > 0 else 0

    # Query items
    stmt = select(CollectionRecord)
    stmt = _apply_sort(stmt, sort)
    offset = (page - 1) * per_page
    stmt = stmt.offset(offset).limit(per_page)

    result = await session.execute(stmt)
    records = result.scalars().all()

    items = [CollectionResponse.from_record(r) for r in records]

    return CollectionListResponse(
        page=page,
        perPage=per_page,
        totalItems=total_items,
        totalPages=total_pages,
        items=items,
    )


async def get_collection(
    session: AsyncSession,
    id_or_name: str,
) -> CollectionRecord:
    """Look up a single collection by ID or name.

    Raises ``LookupError`` if not found.
    """
    return await _find_collection(session, id_or_name)


async def create_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    data: CollectionCreate,
    *,
    auto_migrate: bool = False,
    migrations_dir: str | None = None,
    migration_lock_timeout: float = 30.0,
) -> CollectionResponse:
    """Create a new collection record and the corresponding table."""
    await _lock_migration_producer(
        session,
        migration_lock_timeout=migration_lock_timeout,
    )
    _validate_collection_name(data.name)

    # Check uniqueness
    existing_stmt = select(CollectionRecord).where(
        func.lower(CollectionRecord.name) == data.name.lower()
    )
    existing = (await session.execute(existing_stmt)).scalars().first()
    if existing is not None:
        raise ValueError(f"Collection with name '{data.name}' already exists.")

    # Auth collections need default auth options (per-collection token secrets)
    # if the client didn't provide them.
    options = data.options if isinstance(data.options, dict) else {}
    if data.type == "auth":
        from ppbase.services.auth_service import generate_default_auth_options

        options = _deep_merge_dicts(
            generate_default_auth_options(is_superusers=False),
            options,
        )

    now = datetime.now(timezone.utc)
    record = CollectionRecord(
        id=data.id or generate_id(),
        name=data.name,
        type=data.type,
        system=data.system,
        schema=data.schema,
        indexes=data.indexes,
        list_rule=data.list_rule,
        view_rule=data.view_rule,
        create_rule=data.create_rule,
        update_rule=data.update_rule,
        delete_rule=data.delete_rule,
        options=options,
        created=now,
        updated=now,
    )

    session.add(record)
    await session.flush()

    # Create the physical table
    await create_collection_table(session, record)

    # Generate migration file if auto_migrate is enabled
    generated_path = await _generate_migration_or_rollback(
        session,
        auto_migrate=auto_migrate,
        migrations_dir=migrations_dir,
        kind="create",
        record=record,
    )

    await _commit_with_generated_migrations(
        session,
        engine,
        [generated_path] if generated_path else [],
        migration_lock_timeout=migration_lock_timeout,
    )

    return CollectionResponse.from_record(record)


async def update_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    id_or_name: str,
    data: CollectionUpdate,
    *,
    auto_migrate: bool = False,
    migrations_dir: str | None = None,
    migration_lock_timeout: float = 30.0,
) -> CollectionResponse:
    """Update a collection record and alter the underlying table if needed."""
    await _lock_migration_producer(
        session,
        migration_lock_timeout=migration_lock_timeout,
    )
    record = await _find_collection(session, id_or_name)

    if record.system:
        raise ValueError("Cannot modify a system collection.")

    # Snapshot old state for schema diff and migration generation
    class _Snapshot:
        def __init__(self, rec: CollectionRecord) -> None:
            self.id = rec.id
            self.name = rec.name
            self.type = rec.type
            self.system = rec.system
            self.schema = list(rec.schema) if isinstance(rec.schema, list) else []
            self.indexes = list(rec.indexes) if isinstance(rec.indexes, list) else []
            self.list_rule = rec.list_rule
            self.view_rule = rec.view_rule
            self.create_rule = rec.create_rule
            self.update_rule = rec.update_rule
            self.delete_rule = rec.delete_rule
            self.options = dict(rec.options) if isinstance(rec.options, dict) else {}

    old_snapshot = _Snapshot(record)

    # Apply updates
    if data.name is not None and data.name.lower() != record.name.lower():
        _validate_collection_name(data.name)
        dup_stmt = select(CollectionRecord).where(
            func.lower(CollectionRecord.name) == data.name.lower(),
            CollectionRecord.id != record.id,
        )
        dup = (await session.execute(dup_stmt)).scalars().first()
        if dup is not None:
            raise ValueError(
                f"Collection with name '{data.name}' already exists."
            )
        record.name = data.name

    if data.type is not None:
        record.type = data.type
    if data.system is not None:
        record.system = data.system
    if data.schema is not None:
        import copy
        record.schema = copy.deepcopy(data.schema)
        flag_modified(record, "schema")
    if data.indexes is not None:
        import copy
        record.indexes = copy.deepcopy(data.indexes)
        flag_modified(record, "indexes")

    # Rules: only update if explicitly provided (must use model_fields_set)
    update_payload = data.model_dump(exclude_unset=True, by_alias=False)
    if "list_rule" in update_payload:
        record.list_rule = data.list_rule
    if "view_rule" in update_payload:
        record.view_rule = data.view_rule
    if "create_rule" in update_payload:
        record.create_rule = data.create_rule
    if "update_rule" in update_payload:
        record.update_rule = data.update_rule
    if "delete_rule" in update_payload:
        record.delete_rule = data.delete_rule

    if data.options is not None:
        incoming_options = data.options if isinstance(data.options, dict) else {}
        # Dashboard updates often send partial auth options (e.g. only oauth2).
        # Preserve existing token/password settings and merge in provided keys.
        if (record.type or "base") == "auth":
            from ppbase.services.auth_service import generate_default_auth_options

            existing_options = record.options if isinstance(record.options, dict) else {}
            is_superusers = record.name == "_superusers"
            merged = _deep_merge_dicts(
                generate_default_auth_options(is_superusers=is_superusers),
                existing_options,
            )
            record.options = _deep_merge_dicts(merged, incoming_options)
        else:
            record.options = incoming_options
        flag_modified(record, "options")
    elif (record.type or "base") == "auth" and old_snapshot.type != "auth":
        from ppbase.services.auth_service import generate_default_auth_options

        existing_options = record.options if isinstance(record.options, dict) else {}
        record.options = _deep_merge_dicts(
            generate_default_auth_options(is_superusers=record.name == "_superusers"),
            existing_options,
        )
        flag_modified(record, "options")

    record.updated = datetime.now(timezone.utc)

    await session.flush()

    physical_schema_changed = (
        old_snapshot.name != record.name
        or old_snapshot.type != record.type
        or old_snapshot.schema
        != (record.schema if isinstance(record.schema, list) else [])
        or old_snapshot.indexes
        != (record.indexes if isinstance(record.indexes, list) else [])
        or (
            (old_snapshot.type == "view" or record.type == "view")
            and old_snapshot.options
            != (record.options if isinstance(record.options, dict) else {})
        )
    )
    if physical_schema_changed:
        await update_collection_table(session, old_snapshot, record)

    # Generate migration file if auto_migrate is enabled
    generated_path = await _generate_migration_or_rollback(
        session,
        auto_migrate=auto_migrate,
        migrations_dir=migrations_dir,
        kind="update",
        record=record,
        old_snapshot=old_snapshot,
    )

    await _commit_with_generated_migrations(
        session,
        engine,
        [generated_path] if generated_path else [],
        migration_lock_timeout=migration_lock_timeout,
    )

    return CollectionResponse.from_record(record)


async def delete_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    id_or_name: str,
    *,
    auto_migrate: bool = False,
    migrations_dir: str | None = None,
    migration_lock_timeout: float = 30.0,
) -> None:
    """Delete a collection record and drop the underlying table."""
    await _lock_migration_producer(
        session,
        migration_lock_timeout=migration_lock_timeout,
    )
    record = await _find_collection(session, id_or_name)

    if record.system:
        raise ValueError("Cannot delete a system collection.")

    # Capture state before deletion for migration rollback
    class _DeleteSnapshot:
        def __init__(self, r: CollectionRecord) -> None:
            self.id = r.id
            self.name = r.name
            self.type = r.type or "base"
            self.system = r.system
            self.schema = list(r.schema) if isinstance(r.schema, list) else []
            self.indexes = list(r.indexes) if isinstance(r.indexes, list) else []
            self.list_rule = r.list_rule
            self.view_rule = r.view_rule
            self.create_rule = r.create_rule
            self.update_rule = r.update_rule
            self.delete_rule = r.delete_rule
            self.options = dict(r.options) if isinstance(r.options, dict) else {}

    saved_record = _DeleteSnapshot(record)

    table_name = record.name
    col_type = record.type or "base"
    await session.delete(record)
    await session.flush()

    await delete_collection_table(session, table_name, col_type)

    # Generate migration file if auto_migrate is enabled
    generated_path = await _generate_migration_or_rollback(
        session,
        auto_migrate=auto_migrate,
        migrations_dir=migrations_dir,
        kind="delete",
        record=saved_record,
    )

    await _commit_with_generated_migrations(
        session,
        engine,
        [generated_path] if generated_path else [],
        migration_lock_timeout=migration_lock_timeout,
    )


async def truncate_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    id_or_name: str,
) -> None:
    """Truncate all records from a collection table."""
    record = await _find_collection(session, id_or_name)
    await truncate_collection_table(session, record.name)
    await session.commit()


async def _import_collections_impl(
    session: AsyncSession,
    engine: AsyncEngine,
    collections_data: list[dict[str, Any]],
    *,
    generated_paths: list[str],
    delete_missing: bool = False,
    auto_migrate: bool = False,
    migrations_dir: str | None = None,
) -> None:
    """Bulk import collections.

    Creates or updates collections based on the provided data.
    Optionally deletes collections not present in the import.
    """
    # Get existing collections
    existing_stmt = select(CollectionRecord)
    existing_result = await session.execute(existing_stmt)
    existing_records = {r.name.lower(): r for r in existing_result.scalars().all()}

    imported_names: set[str] = set()

    for coll_data in collections_data:
        name = coll_data.get("name", "")
        if not name:
            continue
        imported_names.add(name.lower())

        if name.lower() in existing_records:
            # Update existing
            record = existing_records[name.lower()]

            class _OldSnap:
                def __init__(self, r: CollectionRecord) -> None:
                    self.id = r.id
                    self.name = r.name
                    self.type = r.type
                    self.system = r.system
                    self.schema = list(r.schema) if isinstance(r.schema, list) else []
                    self.indexes = list(r.indexes) if isinstance(r.indexes, list) else []
                    self.list_rule = r.list_rule
                    self.view_rule = r.view_rule
                    self.create_rule = r.create_rule
                    self.update_rule = r.update_rule
                    self.delete_rule = r.delete_rule
                    self.options = dict(r.options) if isinstance(r.options, dict) else {}

            old = _OldSnap(record)

            record.type = coll_data.get("type", record.type)
            record.schema = coll_data.get("schema", record.schema)
            record.indexes = coll_data.get("indexes", record.indexes)
            record.list_rule = coll_data.get("listRule", record.list_rule)
            record.view_rule = coll_data.get("viewRule", record.view_rule)
            record.create_rule = coll_data.get("createRule", record.create_rule)
            record.update_rule = coll_data.get("updateRule", record.update_rule)
            record.delete_rule = coll_data.get("deleteRule", record.delete_rule)
            incoming_options = coll_data.get("options", record.options)
            if record.type == "auth" and isinstance(incoming_options, dict):
                from ppbase.services.auth_service import generate_default_auth_options

                existing_options = (
                    record.options if isinstance(record.options, dict) else {}
                )
                merged_options = _deep_merge_dicts(
                    generate_default_auth_options(
                        is_superusers=record.name == "_superusers"
                    ),
                    existing_options,
                )
                record.options = _deep_merge_dicts(merged_options, incoming_options)
            else:
                record.options = incoming_options
            record.updated = datetime.now(timezone.utc)

            await session.flush()

            schema_changed = (
                old.name != record.name
                or old.type != record.type
                or old.schema != (record.schema if isinstance(record.schema, list) else [])
                or old.indexes
                != (record.indexes if isinstance(record.indexes, list) else [])
                or (
                    (old.type == "view" or record.type == "view")
                    and old.options
                    != (record.options if isinstance(record.options, dict) else {})
                )
            )
            if schema_changed:
                await update_collection_table(session, old, record)

            # Generate update migration for imported change
            generated_path = await _maybe_generate_migration(
                session,
                auto_migrate=auto_migrate,
                migrations_dir=migrations_dir,
                kind="update",
                record=record,
                old_snapshot=old,
            )
            if generated_path:
                generated_paths.append(generated_path)
        else:
            # Create new
            import_options = coll_data.get("options", {})
            if not isinstance(import_options, dict):
                import_options = {}
            import_type = coll_data.get("type", "base")
            # Auth collections need default auth options if not provided
            if import_type == "auth":
                from ppbase.services.auth_service import generate_default_auth_options
                import_options = _deep_merge_dicts(
                    generate_default_auth_options(is_superusers=False),
                    import_options,
                )

            create_data = CollectionCreate(
                id=coll_data.get("id"),
                name=name,
                type=import_type,
                system=coll_data.get("system", False),
                schema=coll_data.get("schema", []),
                indexes=coll_data.get("indexes", []),
                listRule=coll_data.get("listRule"),
                viewRule=coll_data.get("viewRule"),
                createRule=coll_data.get("createRule"),
                updateRule=coll_data.get("updateRule"),
                deleteRule=coll_data.get("deleteRule"),
                options=import_options,
            )
            now = datetime.now(timezone.utc)
            record = CollectionRecord(
                id=create_data.id or generate_id(),
                name=create_data.name,
                type=create_data.type,
                system=create_data.system,
                schema=create_data.schema,
                indexes=create_data.indexes,
                list_rule=create_data.list_rule,
                view_rule=create_data.view_rule,
                create_rule=create_data.create_rule,
                update_rule=create_data.update_rule,
                delete_rule=create_data.delete_rule,
                options=create_data.options,
                created=now,
                updated=now,
            )
            session.add(record)
            await session.flush()
            await create_collection_table(session, record)

            # Generate create migration for imported collection
            generated_path = await _maybe_generate_migration(
                session,
                auto_migrate=auto_migrate,
                migrations_dir=migrations_dir,
                kind="create",
                record=record,
            )
            if generated_path:
                generated_paths.append(generated_path)

    # Delete missing collections if requested
    if delete_missing:
        for lower_name, record in existing_records.items():
            if lower_name not in imported_names and not record.system:
                # Capture state before deletion for migration rollback
                class _ImportDeleteSnap:
                    def __init__(self, r: CollectionRecord) -> None:
                        self.id = r.id
                        self.name = r.name
                        self.type = r.type or "base"
                        self.system = r.system
                        self.schema = list(r.schema) if isinstance(r.schema, list) else []
                        self.indexes = list(r.indexes) if isinstance(r.indexes, list) else []
                        self.list_rule = r.list_rule
                        self.view_rule = r.view_rule
                        self.create_rule = r.create_rule
                        self.update_rule = r.update_rule
                        self.delete_rule = r.delete_rule
                        self.options = dict(r.options) if isinstance(r.options, dict) else {}

                saved = _ImportDeleteSnap(record)
                table_name = record.name
                col_type = record.type or "base"
                await session.delete(record)
                await session.flush()
                await delete_collection_table(session, table_name, col_type)

                # Generate delete migration for removed collection
                generated_path = await _maybe_generate_migration(
                    session,
                    auto_migrate=auto_migrate,
                    migrations_dir=migrations_dir,
                    kind="delete",
                    record=saved,
                )
                if generated_path:
                    generated_paths.append(generated_path)


async def import_collections(
    session: AsyncSession,
    engine: AsyncEngine,
    collections_data: list[dict[str, Any]],
    *,
    delete_missing: bool = False,
    auto_migrate: bool = False,
    migrations_dir: str | None = None,
    migration_lock_timeout: float = 30.0,
) -> None:
    """Bulk import collections and their generated files atomically."""
    from ppbase.services.migration_runner import MigrationCommitOutcomeError

    generated_paths: list[str] = []
    try:
        await _lock_migration_producer(
            session,
            migration_lock_timeout=migration_lock_timeout,
        )
        await _import_collections_impl(
            session,
            engine,
            collections_data,
            generated_paths=generated_paths,
            delete_missing=delete_missing,
            auto_migrate=auto_migrate,
            migrations_dir=migrations_dir,
        )
        await _commit_with_generated_migrations(
            session,
            engine,
            generated_paths,
            migration_lock_timeout=migration_lock_timeout,
        )
    except MigrationCommitOutcomeError:
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "Rollback failed after ambiguous collection import commit"
            )
        # The commit reconciler deliberately preserves durable intent when it
        # cannot prove the database outcome. Do not erase those recovery files.
        raise
    except BaseException:
        try:
            await session.rollback()
        except Exception:
            logger.exception("Rollback failed after collection import error")
        _cleanup_generated_migrations(generated_paths)
        raise
