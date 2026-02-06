"""Business logic for collection management.

Handles CRUD operations on ``_collections`` records and coordinates with the
:mod:`ppbase.db.schema_manager` to create/alter/drop the corresponding
PostgreSQL tables.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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

# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_RESERVED_NAMES = frozenset({
    "id",
    "created",
    "updated",
    "expand",
    "collectionId",
    "collectionName",
    "_collections",
    "_admins",
    "_params",
    "_external_auths",
    "_superusers",
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
# Public API
# ---------------------------------------------------------------------------


async def list_collections(
    session: AsyncSession,
    *,
    page: int = 1,
    per_page: int = 30,
    sort: str = "",
    filter_str: str = "",
) -> CollectionListResponse:
    """List collections with pagination and sorting."""
    # Count total
    count_stmt = select(func.count(CollectionRecord.id))
    count_result = await session.execute(count_stmt)
    total_items = count_result.scalar_one()

    total_pages = max(1, math.ceil(total_items / per_page)) if per_page > 0 else 1

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
) -> CollectionResponse:
    """Create a new collection record and the corresponding table."""
    _validate_collection_name(data.name)

    # Check uniqueness
    existing_stmt = select(CollectionRecord).where(
        func.lower(CollectionRecord.name) == data.name.lower()
    )
    existing = (await session.execute(existing_stmt)).scalars().first()
    if existing is not None:
        raise ValueError(f"Collection with name '{data.name}' already exists.")

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
        options=data.options,
        created=now,
        updated=now,
    )

    session.add(record)
    await session.flush()

    # Create the physical table
    await create_collection_table(engine, record)

    await session.commit()

    return CollectionResponse.from_record(record)


async def update_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    id_or_name: str,
    data: CollectionUpdate,
) -> CollectionResponse:
    """Update a collection record and alter the underlying table if needed."""
    record = await _find_collection(session, id_or_name)

    if record.system:
        raise ValueError("Cannot modify a system collection.")

    # Snapshot old state for schema diff
    class _Snapshot:
        def __init__(self, rec: CollectionRecord) -> None:
            self.name = rec.name
            self.type = rec.type
            self.schema = list(rec.schema) if isinstance(rec.schema, list) else []

    old_snapshot = _Snapshot(record)

    # Apply updates
    if data.name is not None:
        _validate_collection_name(data.name)
        if data.name.lower() != record.name.lower():
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
        record.schema = data.schema
    if data.indexes is not None:
        record.indexes = data.indexes

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
        record.options = data.options

    record.updated = datetime.now(timezone.utc)

    await session.flush()

    # Alter the physical table
    schema_changed = (
        old_snapshot.name != record.name
        or old_snapshot.schema != (record.schema if isinstance(record.schema, list) else [])
    )
    if schema_changed:
        await update_collection_table(engine, old_snapshot, record)

    await session.commit()

    return CollectionResponse.from_record(record)


async def delete_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    id_or_name: str,
) -> None:
    """Delete a collection record and drop the underlying table."""
    record = await _find_collection(session, id_or_name)

    if record.system:
        raise ValueError("Cannot delete a system collection.")

    table_name = record.name
    col_type = record.type or "base"
    await session.delete(record)
    await session.flush()

    await delete_collection_table(engine, table_name, col_type)
    await session.commit()


async def truncate_collection(
    session: AsyncSession,
    engine: AsyncEngine,
    id_or_name: str,
) -> None:
    """Truncate all records from a collection table."""
    record = await _find_collection(session, id_or_name)
    await truncate_collection_table(engine, record.name)


async def import_collections(
    session: AsyncSession,
    engine: AsyncEngine,
    collections_data: list[dict[str, Any]],
    *,
    delete_missing: bool = False,
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
                    self.name = r.name
                    self.type = r.type
                    self.schema = list(r.schema) if isinstance(r.schema, list) else []

            old = _OldSnap(record)

            record.type = coll_data.get("type", record.type)
            record.schema = coll_data.get("schema", record.schema)
            record.indexes = coll_data.get("indexes", record.indexes)
            record.list_rule = coll_data.get("listRule", record.list_rule)
            record.view_rule = coll_data.get("viewRule", record.view_rule)
            record.create_rule = coll_data.get("createRule", record.create_rule)
            record.update_rule = coll_data.get("updateRule", record.update_rule)
            record.delete_rule = coll_data.get("deleteRule", record.delete_rule)
            record.options = coll_data.get("options", record.options)
            record.updated = datetime.now(timezone.utc)

            await session.flush()

            schema_changed = (
                old.name != record.name
                or old.schema != (record.schema if isinstance(record.schema, list) else [])
            )
            if schema_changed:
                await update_collection_table(engine, old, record)
        else:
            # Create new
            create_data = CollectionCreate(
                id=coll_data.get("id"),
                name=name,
                type=coll_data.get("type", "base"),
                system=coll_data.get("system", False),
                schema=coll_data.get("schema", []),
                indexes=coll_data.get("indexes", []),
                listRule=coll_data.get("listRule"),
                viewRule=coll_data.get("viewRule"),
                createRule=coll_data.get("createRule"),
                updateRule=coll_data.get("updateRule"),
                deleteRule=coll_data.get("deleteRule"),
                options=coll_data.get("options", {}),
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
            await create_collection_table(engine, record)

    # Delete missing collections if requested
    if delete_missing:
        for lower_name, record in existing_records.items():
            if lower_name not in imported_names and not record.system:
                table_name = record.name
                col_type = record.type or "base"
                await session.delete(record)
                await session.flush()
                await delete_collection_table(engine, table_name, col_type)

    await session.commit()
