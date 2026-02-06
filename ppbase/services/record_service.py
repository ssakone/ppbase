"""Record CRUD service using SQLAlchemy Core for dynamic queries.

Provides list, get, create, update, and delete operations against
dynamically-created collection tables.  All SQL is parameterized.
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import CollectionRecord
from ppbase.models.field_types import (
    FieldDefinition,
    FieldType,
    FieldValidationError,
    validate_field_value,
)
from ppbase.models.record import (
    build_list_response,
    build_record_response,
    format_datetime,
)
from ppbase.services.filter_parser import parse_filter, parse_sort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIELD_CORE_KEYS = frozenset({
    "id", "name", "type", "required", "system", "hidden", "presentable", "options",
})


def _normalize_field(f: dict[str, Any]) -> dict[str, Any]:
    """Ensure flat-format field options are nested under 'options'."""
    extra = {k: v for k, v in f.items() if k not in _FIELD_CORE_KEYS}
    if not extra:
        return f
    core = {k: v for k, v in f.items() if k in _FIELD_CORE_KEYS}
    existing = core.pop("options", None) or {}
    core["options"] = {**extra, **existing}
    return core


def _get_schema_fields(collection: CollectionRecord) -> list[FieldDefinition]:
    """Parse the collection's schema JSONB into FieldDefinition objects.

    Handles both nested-options and flat-format schemas gracefully.
    """
    raw: list[dict[str, Any]] = collection.schema or []
    return [FieldDefinition(**_normalize_field(f)) for f in raw]


def _get_hidden_fields(fields: list[FieldDefinition]) -> set[str]:
    return {f.name for f in fields if f.hidden}


def _serialize_for_pg(value: Any, field_def: FieldDefinition) -> Any:
    """Serialize a validated value for PostgreSQL insertion.

    JSON and GeoPoint fields need their Python dicts/lists serialized to
    JSON strings for asyncpg's JSONB columns.
    """
    if field_def.type in (FieldType.JSON, FieldType.GEO_POINT):
        if value is None:
            return _json.dumps(None)
        return _json.dumps(value, separators=(",", ":"))
    return value


def _fields_filter(fields_param: str | None) -> list[str] | None:
    """Parse the ``fields`` query parameter."""
    if not fields_param:
        return None
    return [f.strip() for f in fields_param.split(",") if f.strip()]


def _table_name(collection: CollectionRecord) -> str:
    return collection.name


# ---------------------------------------------------------------------------
# List records
# ---------------------------------------------------------------------------


async def list_records(
    engine: AsyncEngine,
    collection: CollectionRecord,
    *,
    page: int = 1,
    per_page: int = 30,
    sort: str | None = None,
    filter_str: str | None = None,
    fields: str | None = None,
    skip_total: bool = False,
    request_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """List records with filtering, sorting, and pagination.

    Returns a paginated response dict.
    """
    table = _table_name(collection)
    schema_fields = _get_schema_fields(collection)
    hidden = _get_hidden_fields(schema_fields)
    ff = _fields_filter(fields)

    # Build WHERE clause
    where_sql = "1=1"
    params: dict[str, Any] = {}
    if filter_str:
        where_sql, params = parse_filter(filter_str, request_context)

    # Build ORDER BY clause
    order_sql = '"created" DESC'
    if sort:
        sort_parts = parse_sort(sort)
        if sort_parts:
            # Validate sort fields exist in the collection schema or are system fields
            system_fields = {"id", "created", "updated"}
            schema_field_names = {f.name for f in schema_fields}
            valid_fields = system_fields | schema_field_names
            for col_expr, _dir in sort_parts:
                # col_expr is quoted like '"fieldname"' or a function like 'RANDOM()'
                col_name = col_expr.strip('"')
                if col_name not in valid_fields and not col_expr.endswith("()") and col_name != "ctid":
                    raise ValueError(
                        f"Invalid sort field: {col_name}. "
                        f"Available fields: {', '.join(sorted(valid_fields))}."
                    )
            order_sql = ", ".join(f"{col} {direction}" for col, direction in sort_parts)

    # Clamp pagination (PocketBase normalizes invalid values instead of rejecting)
    if per_page <= 0:
        per_page = 30
    per_page = min(500, per_page)
    page = max(1, page)
    offset = (page - 1) * per_page

    # Total count (unless skipped)
    total_items = -1
    if not skip_total:
        count_sql = f'SELECT COUNT(*) AS cnt FROM "{table}" WHERE {where_sql}'
        async with engine.connect() as conn:
            result = await conn.execute(text(count_sql), params)
            row = result.mappings().first()
            total_items = row["cnt"] if row else 0

    # Fetch page
    select_sql = (
        f'SELECT * FROM "{table}" WHERE {where_sql} '
        f"ORDER BY {order_sql} LIMIT :_limit OFFSET :_offset"
    )
    params["_limit"] = per_page
    params["_offset"] = offset

    async with engine.connect() as conn:
        result = await conn.execute(text(select_sql), params)
        rows = [dict(r) for r in result.mappings().all()]

    items = [
        build_record_response(
            row, collection.id, collection.name, collection.schema or [],
            fields_filter=ff, hidden_fields=hidden,
        )
        for row in rows
    ]

    return build_list_response(items, page, per_page, total_items)


# ---------------------------------------------------------------------------
# Get single record
# ---------------------------------------------------------------------------


async def get_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    *,
    fields: str | None = None,
) -> dict[str, Any] | None:
    """Fetch a single record by ID.  Returns None if not found."""
    table = _table_name(collection)
    schema_fields = _get_schema_fields(collection)
    hidden = _get_hidden_fields(schema_fields)
    ff = _fields_filter(fields)

    sql = f'SELECT * FROM "{table}" WHERE "id" = :id LIMIT 1'

    async with engine.connect() as conn:
        result = await conn.execute(text(sql), {"id": record_id})
        row = result.mappings().first()

    if row is None:
        return None

    return build_record_response(
        dict(row), collection.id, collection.name, collection.schema or [],
        fields_filter=ff, hidden_fields=hidden,
    )


# ---------------------------------------------------------------------------
# Create record
# ---------------------------------------------------------------------------


async def create_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Create a new record in the collection.

    Validates all fields, generates an ID, and sets timestamps.

    Raises:
        FieldValidationError: If any field fails validation.
    """
    table = _table_name(collection)
    schema_fields = _get_schema_fields(collection)
    hidden = _get_hidden_fields(schema_fields)

    now = datetime.now(timezone.utc)
    now_str = format_datetime(now)

    record_id = data.pop("id", None) or generate_id()

    # Validate and collect column values
    columns: dict[str, Any] = {
        "id": record_id,
        "created": now,
        "updated": now,
    }

    errors: dict[str, dict[str, str]] = {}
    for field_def in schema_fields:
        # Skip autodate fields -- we handle created/updated ourselves
        if field_def.type == FieldType.AUTODATE:
            continue

        value = data.get(field_def.name)

        try:
            validated = validate_field_value(field_def, value)
            columns[field_def.name] = _serialize_for_pg(validated, field_def)
        except FieldValidationError as exc:
            errors[exc.field_name] = {"code": exc.code, "message": exc.message}

    if errors:
        raise _ValidationErrors(errors)

    # Build INSERT
    col_names = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    insert_sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'

    async with engine.begin() as conn:
        await conn.execute(text(insert_sql), columns)

    # Fetch and return the created record
    return (await get_record(engine, collection, record_id)) or {
        "id": record_id,
        "collectionId": collection.id,
        "collectionName": collection.name,
        "created": now_str,
        "updated": now_str,
    }


# ---------------------------------------------------------------------------
# Update record
# ---------------------------------------------------------------------------


async def update_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Update an existing record.

    Supports ``field+`` (append) and ``field-`` (remove) modifiers for
    multi-value fields (select, relation, file).

    Returns the updated record dict, or None if not found.
    """
    table = _table_name(collection)
    schema_fields = _get_schema_fields(collection)

    # Check record exists
    existing = await get_record(engine, collection, record_id)
    if existing is None:
        return None

    # Fetch raw row for modifier operations
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f'SELECT * FROM "{table}" WHERE "id" = :id LIMIT 1'),
            {"id": record_id},
        )
        raw_row = dict(result.mappings().first() or {})

    now = datetime.now(timezone.utc)
    updates: dict[str, Any] = {"updated": now}
    errors: dict[str, dict[str, str]] = {}

    field_map = {f.name: f for f in schema_fields}

    # Process +/- modifiers and plain field updates
    processed_fields: set[str] = set()
    for key, value in data.items():
        if key == "id":
            continue

        modifier = None
        field_name = key
        if key.endswith("+"):
            modifier = "+"
            field_name = key[:-1]
        elif key.endswith("-"):
            modifier = "-"
            field_name = key[:-1]

        field_def = field_map.get(field_name)
        if field_def is None:
            continue
        if field_def.type == FieldType.AUTODATE:
            continue

        processed_fields.add(field_name)

        if modifier == "+":
            current = raw_row.get(field_name)
            merged = _apply_append(current, value, field_def)
            try:
                validated = validate_field_value(field_def, merged)
                updates[field_name] = _serialize_for_pg(validated, field_def)
            except FieldValidationError as exc:
                errors[exc.field_name] = {"code": exc.code, "message": exc.message}

        elif modifier == "-":
            current = raw_row.get(field_name)
            reduced = _apply_remove(current, value, field_def)
            try:
                validated = validate_field_value(field_def, reduced)
                updates[field_name] = _serialize_for_pg(validated, field_def)
            except FieldValidationError as exc:
                errors[exc.field_name] = {"code": exc.code, "message": exc.message}

        else:
            try:
                validated = validate_field_value(field_def, value)
                updates[field_name] = _serialize_for_pg(validated, field_def)
            except FieldValidationError as exc:
                errors[exc.field_name] = {"code": exc.code, "message": exc.message}

    if errors:
        raise _ValidationErrors(errors)

    if len(updates) <= 1:
        # Only "updated" timestamp -- nothing else changed
        return existing

    # Build UPDATE
    set_clauses = ", ".join(f'"{c}" = :{c}' for c in updates)
    update_sql = f'UPDATE "{table}" SET {set_clauses} WHERE "id" = :_rec_id'
    params = {**updates, "_rec_id": record_id}

    async with engine.begin() as conn:
        await conn.execute(text(update_sql), params)

    return await get_record(engine, collection, record_id)


def _apply_append(
    current: Any,
    new_values: Any,
    field_def: FieldDefinition,
) -> Any:
    """Append values for multi-value fields or increment for numbers."""
    if field_def.type == FieldType.NUMBER:
        cur = float(current) if current is not None else 0.0
        inc = float(new_values) if new_values is not None else 0.0
        return cur + inc

    # Multi-value fields (select, relation, file)
    cur_list = list(current) if isinstance(current, (list, tuple)) else []
    if isinstance(new_values, list):
        cur_list.extend(new_values)
    elif new_values is not None:
        cur_list.append(new_values)
    return cur_list


def _apply_remove(
    current: Any,
    remove_values: Any,
    field_def: FieldDefinition,
) -> Any:
    """Remove values for multi-value fields or decrement for numbers."""
    if field_def.type == FieldType.NUMBER:
        cur = float(current) if current is not None else 0.0
        dec = float(remove_values) if remove_values is not None else 0.0
        return cur - dec

    cur_list = list(current) if isinstance(current, (list, tuple)) else []
    if isinstance(remove_values, list):
        to_remove = set(str(v) for v in remove_values)
    elif remove_values is not None:
        to_remove = {str(remove_values)}
    else:
        to_remove = set()
    return [v for v in cur_list if str(v) not in to_remove]


# ---------------------------------------------------------------------------
# Delete record
# ---------------------------------------------------------------------------


async def delete_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    *,
    all_collections: list[CollectionRecord] | None = None,
) -> bool:
    """Delete a record by ID.

    Handles cascade deletes for relation fields when configured.

    Returns True if a record was deleted, False if not found.
    """
    table = _table_name(collection)

    # Check existence
    async with engine.connect() as conn:
        result = await conn.execute(
            text(f'SELECT "id" FROM "{table}" WHERE "id" = :id LIMIT 1'),
            {"id": record_id},
        )
        if result.first() is None:
            return False

    # Handle cascade deletes
    if all_collections:
        await _cascade_delete(engine, collection, record_id, all_collections)

    # Delete the record
    delete_sql = f'DELETE FROM "{table}" WHERE "id" = :id'
    async with engine.begin() as conn:
        await conn.execute(text(delete_sql), {"id": record_id})

    return True


async def _cascade_delete(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    all_collections: list[CollectionRecord],
) -> None:
    """Find and delete records in other collections that reference this record
    with cascadeDelete enabled."""
    for other_coll in all_collections:
        schema: list[dict[str, Any]] = other_coll.schema or []
        for field_raw in schema:
            if field_raw.get("type") != "relation":
                continue
            opts = field_raw.get("options", {})
            if opts.get("collectionId") != collection.id:
                continue
            if not opts.get("cascadeDelete", False):
                continue

            field_name = field_raw.get("name", "")
            if not field_name:
                continue

            other_table = _table_name(other_coll)
            max_select = opts.get("maxSelect", 1) or 1

            if max_select > 1:
                # Array column: find records where array contains this ID
                find_sql = (
                    f'SELECT "id" FROM "{other_table}" '
                    f'WHERE :rid = ANY("{field_name}")'
                )
            else:
                # Scalar column
                find_sql = (
                    f'SELECT "id" FROM "{other_table}" '
                    f'WHERE "{field_name}" = :rid'
                )

            async with engine.connect() as conn:
                result = await conn.execute(text(find_sql), {"rid": record_id})
                related_ids = [dict(r)["id"] for r in result.mappings().all()]

            for related_id in related_ids:
                await delete_record(
                    engine, other_coll, related_id,
                    all_collections=all_collections,
                )


# ---------------------------------------------------------------------------
# Collection resolution helper
# ---------------------------------------------------------------------------


async def resolve_collection(
    engine: AsyncEngine,
    id_or_name: str,
) -> CollectionRecord | None:
    """Resolve a collection by ID or name from the _collections table."""
    sql = text(
        'SELECT * FROM "_collections" '
        "WHERE id = :val OR name = :val LIMIT 1"
    )
    async with engine.connect() as conn:
        result = await conn.execute(sql, {"val": id_or_name})
        row = result.mappings().first()

    if row is None:
        return None

    row_dict = dict(row)
    return CollectionRecord(
        id=row_dict["id"],
        name=row_dict["name"],
        type=row_dict.get("type", "base"),
        system=row_dict.get("system", False),
        schema=row_dict.get("schema", []),
        indexes=row_dict.get("indexes", []),
        list_rule=row_dict.get("list_rule"),
        view_rule=row_dict.get("view_rule"),
        create_rule=row_dict.get("create_rule"),
        update_rule=row_dict.get("update_rule"),
        delete_rule=row_dict.get("delete_rule"),
        options=row_dict.get("options", {}),
        created=row_dict.get("created", datetime.now(timezone.utc)),
        updated=row_dict.get("updated", datetime.now(timezone.utc)),
    )


async def get_all_collections(engine: AsyncEngine) -> list[CollectionRecord]:
    """Fetch all collections from the _collections table."""
    sql = text('SELECT * FROM "_collections"')
    async with engine.connect() as conn:
        result = await conn.execute(sql)
        rows = result.mappings().all()

    collections = []
    for row_dict in rows:
        rd = dict(row_dict)
        collections.append(
            CollectionRecord(
                id=rd["id"],
                name=rd["name"],
                type=rd.get("type", "base"),
                system=rd.get("system", False),
                schema=rd.get("schema", []),
                indexes=rd.get("indexes", []),
                list_rule=rd.get("list_rule"),
                view_rule=rd.get("view_rule"),
                create_rule=rd.get("create_rule"),
                update_rule=rd.get("update_rule"),
                delete_rule=rd.get("delete_rule"),
                options=rd.get("options", {}),
                created=rd.get("created", datetime.now(timezone.utc)),
                updated=rd.get("updated", datetime.now(timezone.utc)),
            )
        )
    return collections


# ---------------------------------------------------------------------------
# Validation error wrapper
# ---------------------------------------------------------------------------


class _ValidationErrors(Exception):
    """Wraps multiple field validation errors for API responses."""

    def __init__(self, errors: dict[str, dict[str, str]]) -> None:
        self.errors = errors
        super().__init__(f"Validation failed: {errors}")
