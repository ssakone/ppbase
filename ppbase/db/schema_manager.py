"""Dynamic DDL engine for PostgreSQL collection tables.

Translates collection schema definitions (stored in ``_collections``)
into ``CREATE TABLE``, ``ALTER TABLE``, and ``DROP TABLE`` statements.

Uses SQLAlchemy Core with raw DDL via ``text()`` for PostgreSQL-specific
operations that are not expressible through the standard DDL API.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.models.field_types import FieldDefinition, FieldType


# ---------------------------------------------------------------------------
# Field type -> PostgreSQL column DDL mapping
# ---------------------------------------------------------------------------

def _col_ddl(field: FieldDefinition) -> str:
    """Return the PostgreSQL column DDL fragment for a single field."""
    opts = field.options
    ft = field.type

    if ft in (FieldType.TEXT, FieldType.EDITOR):
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.NUMBER:
        only_int = opts.get("onlyInt", False)
        if only_int:
            return f'"{field.name}" INTEGER NOT NULL DEFAULT 0'
        return f'"{field.name}" DOUBLE PRECISION NOT NULL DEFAULT 0'

    if ft == FieldType.BOOL:
        return f'"{field.name}" BOOLEAN NOT NULL DEFAULT FALSE'

    if ft == FieldType.EMAIL:
        return f'"{field.name}" VARCHAR(255) NOT NULL DEFAULT \'\''

    if ft == FieldType.URL:
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.DATE:
        return f'"{field.name}" TIMESTAMPTZ NULL'

    if ft == FieldType.AUTODATE:
        return f'"{field.name}" TIMESTAMPTZ NOT NULL DEFAULT NOW()'

    if ft == FieldType.SELECT:
        max_select = opts.get("maxSelect", 1) or 1
        if max_select > 1:
            return f'"{field.name}" TEXT[] NOT NULL DEFAULT \'{{}}\''
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.FILE:
        max_select = opts.get("maxSelect", 1) or 1
        if max_select > 1:
            return f'"{field.name}" TEXT[] NOT NULL DEFAULT \'{{}}\''
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.RELATION:
        max_select = opts.get("maxSelect", 1) or 1
        if max_select > 1:
            return f'"{field.name}" VARCHAR(15)[] NOT NULL DEFAULT \'{{}}\''
        return f'"{field.name}" VARCHAR(15) NOT NULL DEFAULT \'\''

    if ft == FieldType.JSON:
        return f'"{field.name}" JSONB NOT NULL DEFAULT \'null\'::jsonb'

    if ft == FieldType.PASSWORD:
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.GEO_POINT:
        return (
            f'"{field.name}" JSONB NOT NULL '
            f"DEFAULT '{{\"lon\":0,\"lat\":0}}'::jsonb"
        )

    # Fallback for unknown types
    return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''


def _is_array_field(field: FieldDefinition) -> bool:
    """Check if a field maps to a PostgreSQL array column."""
    opts = field.options
    max_select = opts.get("maxSelect", 1) or 1
    return field.type in (
        FieldType.SELECT,
        FieldType.FILE,
        FieldType.RELATION,
    ) and max_select > 1


def _is_jsonb_field(field: FieldDefinition) -> bool:
    """Check if a field maps to a JSONB column."""
    return field.type in (FieldType.JSON, FieldType.GEO_POINT)


def _safe_name(name: str) -> str:
    """Ensure a name is safe for use as a SQL identifier."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


# ---------------------------------------------------------------------------
# Auth collection system columns
# ---------------------------------------------------------------------------

_AUTH_SYSTEM_COLUMNS = """
    "email" VARCHAR(255) NOT NULL DEFAULT '',
    "email_visibility" BOOLEAN NOT NULL DEFAULT FALSE,
    "verified" BOOLEAN NOT NULL DEFAULT FALSE,
    "password_hash" TEXT NOT NULL DEFAULT '',
    "token_key" VARCHAR(50) NOT NULL DEFAULT ''
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_collection_table(
    engine: AsyncEngine,
    collection: Any,
) -> None:
    """Create a PostgreSQL table (or view) for a collection record.

    Args:
        engine: The async SQLAlchemy engine.
        collection: A ``CollectionRecord`` (ORM) or dict-like object with
            ``name``, ``type``, ``schema``, and ``options`` attributes.
    """
    table_name = _safe_name(collection.name)
    col_type = getattr(collection, "type", "base")

    # View collections: create a PostgreSQL VIEW from the query
    if col_type == "view":
        raw_options = getattr(collection, "options", None) or {}
        query = raw_options.get("query", "")
        if not query:
            raise ValueError("View collection requires a SQL query in options.query.")
        # Validate the query first using a temp view in a savepoint
        await validate_view_query(engine, query)
        view_sql = f'CREATE OR REPLACE VIEW "{table_name}" AS {query}'
        async with engine.begin() as conn:
            await conn.execute(text(view_sql))
        return

    # Parse schema field definitions
    fields: list[FieldDefinition] = []
    raw_schema = collection.schema if isinstance(collection.schema, list) else []
    for fd in raw_schema:
        if isinstance(fd, FieldDefinition):
            fields.append(fd)
        elif isinstance(fd, dict):
            fields.append(FieldDefinition(**fd))

    # Build column definitions
    parts: list[str] = [
        '"id" VARCHAR(15) PRIMARY KEY',
        '"created" TIMESTAMPTZ NOT NULL DEFAULT NOW()',
        '"updated" TIMESTAMPTZ NOT NULL DEFAULT NOW()',
    ]

    # Auth collection system columns
    if col_type == "auth":
        parts.append(_AUTH_SYSTEM_COLUMNS.strip())

    # Dynamic user-defined columns
    for f in fields:
        parts.append(_col_ddl(f))

    columns_sql = ",\n    ".join(parts)
    create_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    {columns_sql}\n)'

    # Build index statements
    index_stmts: list[str] = [
        f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_created" ON "{table_name}" ("created")',
    ]

    # Auth-specific indexes
    if col_type == "auth":
        index_stmts.extend([
            f'CREATE UNIQUE INDEX IF NOT EXISTS "idx_{table_name}_email" '
            f'ON "{table_name}" ("email") WHERE "email" != \'\'',
            f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_token_key" '
            f'ON "{table_name}" ("token_key")',
        ])

    # Field-specific indexes
    for f in fields:
        if _is_array_field(f):
            index_stmts.append(
                f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_{f.name}" '
                f'ON "{table_name}" USING GIN ("{f.name}")'
            )
        elif _is_jsonb_field(f):
            index_stmts.append(
                f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_{f.name}" '
                f'ON "{table_name}" USING GIN ("{f.name}")'
            )
        elif f.type == FieldType.RELATION:
            # Single relation: B-tree index
            index_stmts.append(
                f'CREATE INDEX IF NOT EXISTS "idx_{table_name}_{f.name}" '
                f'ON "{table_name}" ("{f.name}") WHERE "{f.name}" != \'\''
            )

    # Custom indexes from collection definition
    raw_indexes = getattr(collection, 'indexes', None) or []
    for idx_sql in raw_indexes:
        if isinstance(idx_sql, str) and idx_sql.strip():
            index_stmts.append(idx_sql)

    async with engine.begin() as conn:
        await conn.execute(text(create_sql))
        for idx_sql in index_stmts:
            await conn.execute(text(idx_sql))


async def update_collection_table(
    engine: AsyncEngine,
    old_collection: Any,
    new_collection: Any,
) -> None:
    """Apply ALTER TABLE statements to match the updated schema.

    Compares old and new collection definitions and issues:
    - ADD COLUMN for new fields
    - DROP COLUMN for removed fields
    - RENAME TABLE if the collection name changed
    - Type changes are handled via DROP + ADD (simple approach)

    Args:
        engine: The async SQLAlchemy engine.
        old_collection: The previous collection record.
        new_collection: The updated collection record.
    """
    old_name = _safe_name(old_collection.name)
    new_name = _safe_name(new_collection.name)

    # Parse old and new field definitions
    def _parse_fields(raw: Any) -> dict[str, FieldDefinition]:
        result: dict[str, FieldDefinition] = {}
        schema = raw if isinstance(raw, list) else []
        for fd in schema:
            if isinstance(fd, FieldDefinition):
                fdef = fd
            elif isinstance(fd, dict):
                fdef = FieldDefinition(**fd)
            else:
                continue
            # Key by field id if available, else by name
            key = fdef.id if fdef.id else fdef.name
            result[key] = fdef
        return result

    old_fields = _parse_fields(old_collection.schema)
    new_fields = _parse_fields(new_collection.schema)

    old_keys = set(old_fields.keys())
    new_keys = set(new_fields.keys())

    removed_keys = old_keys - new_keys
    added_keys = new_keys - old_keys
    common_keys = old_keys & new_keys

    stmts: list[str] = []

    # Rename table if name changed
    current_table = old_name
    if old_name != new_name:
        stmts.append(
            f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"'
        )
        current_table = new_name

    # Drop removed columns
    for key in removed_keys:
        col_name = old_fields[key].name
        stmts.append(
            f'ALTER TABLE "{current_table}" DROP COLUMN IF EXISTS "{col_name}"'
        )

    # Add new columns
    for key in added_keys:
        fdef = new_fields[key]
        col_ddl = _col_ddl(fdef)
        stmts.append(
            f'ALTER TABLE "{current_table}" ADD COLUMN {col_ddl}'
        )

    # Handle changed fields (rename or type change)
    for key in common_keys:
        old_f = old_fields[key]
        new_f = new_fields[key]

        # Rename column if name changed
        if old_f.name != new_f.name:
            stmts.append(
                f'ALTER TABLE "{current_table}" '
                f'RENAME COLUMN "{old_f.name}" TO "{new_f.name}"'
            )

        # If the type or relevant options changed, drop and re-add
        if old_f.type != new_f.type or _type_options_changed(old_f, new_f):
            col_ddl = _col_ddl(new_f)
            # Use the new name (after rename)
            stmts.append(
                f'ALTER TABLE "{current_table}" DROP COLUMN IF EXISTS "{new_f.name}"'
            )
            stmts.append(
                f'ALTER TABLE "{current_table}" ADD COLUMN {col_ddl}'
            )

    async with engine.begin() as conn:
        for stmt in stmts:
            await conn.execute(text(stmt))

    # Re-create field-specific indexes for the new schema
    await _recreate_field_indexes(engine, new_collection)


def _type_options_changed(old_f: FieldDefinition, new_f: FieldDefinition) -> bool:
    """Check if type-relevant options changed (e.g., maxSelect, onlyInt)."""
    if old_f.type != new_f.type:
        return True

    if old_f.type == FieldType.NUMBER:
        return old_f.options.get("onlyInt") != new_f.options.get("onlyInt")

    if old_f.type in (FieldType.SELECT, FieldType.FILE, FieldType.RELATION):
        old_max = old_f.options.get("maxSelect", 1) or 1
        new_max = new_f.options.get("maxSelect", 1) or 1
        return (old_max > 1) != (new_max > 1)

    return False


async def _recreate_field_indexes(
    engine: AsyncEngine,
    collection: Any,
) -> None:
    """Drop and recreate field-level indexes for a collection."""
    table_name = _safe_name(collection.name)

    raw_schema = collection.schema if isinstance(collection.schema, list) else []
    fields: list[FieldDefinition] = []
    for fd in raw_schema:
        if isinstance(fd, FieldDefinition):
            fields.append(fd)
        elif isinstance(fd, dict):
            fields.append(FieldDefinition(**fd))

    index_stmts: list[str] = []
    for f in fields:
        idx_name = f"idx_{table_name}_{f.name}"
        # Drop existing index first
        drop_stmt = f'DROP INDEX IF EXISTS "{idx_name}"'

        if _is_array_field(f):
            index_stmts.append(drop_stmt)
            index_stmts.append(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                f'ON "{table_name}" USING GIN ("{f.name}")'
            )
        elif _is_jsonb_field(f):
            index_stmts.append(drop_stmt)
            index_stmts.append(
                f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                f'ON "{table_name}" USING GIN ("{f.name}")'
            )
        elif f.type == FieldType.RELATION:
            max_select = f.options.get("maxSelect", 1) or 1
            if max_select <= 1:
                index_stmts.append(drop_stmt)
                index_stmts.append(
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                    f'ON "{table_name}" ("{f.name}") WHERE "{f.name}" != \'\''
                )

    async with engine.begin() as conn:
        for stmt in index_stmts:
            await conn.execute(text(stmt))


async def delete_collection_table(
    engine: AsyncEngine,
    collection_name: str,
    collection_type: str = "base",
) -> None:
    """Drop the PostgreSQL table or view for a collection.

    Args:
        engine: The async SQLAlchemy engine.
        collection_name: Name of the table/view to drop.
        collection_type: Collection type ("base", "auth", or "view").
    """
    table_name = _safe_name(collection_name)
    async with engine.begin() as conn:
        # Check actual object type via information_schema to handle legacy
        # view collections that were mistakenly created as physical tables.
        row = (await conn.execute(text(
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = :name"
        ), {"name": table_name})).first()
        is_view = row and row[0] == "VIEW"
        if is_view:
            await conn.execute(text(f'DROP VIEW IF EXISTS "{table_name}" CASCADE'))
        else:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}" CASCADE'))


async def truncate_collection_table(
    engine: AsyncEngine,
    collection_name: str,
) -> None:
    """Truncate all rows from the collection table.

    Args:
        engine: The async SQLAlchemy engine.
        collection_name: Name of the table to truncate.
    """
    table_name = _safe_name(collection_name)
    async with engine.begin() as conn:
        await conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))


async def validate_view_query(engine: AsyncEngine, query: str) -> None:
    """Validate a SQL SELECT query by creating a temp view and rolling back.

    Raises ``ValueError`` with a clear message if the query is invalid.
    """
    import secrets

    tmp_name = f"_ppbase_validate_{secrets.token_hex(4)}"
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(f'CREATE TEMP VIEW "{tmp_name}" AS {query}')
            )
            await conn.execute(text(f'DROP VIEW IF EXISTS "{tmp_name}"'))
        except Exception as exc:
            msg = str(exc)
            # asyncpg/SQLAlchemy wraps errors like:
            # (sqlalchemy...ProgrammingError) <class 'asyncpg...'> : actual msg\n[SQL:...
            # Extract just the human-readable PostgreSQL error
            import re as _re
            # Try to get text after the last ">:" pattern
            m = _re.search(r">:\s*(.+?)(?:\n\[SQL:|$)", msg, _re.DOTALL)
            if m:
                msg = m.group(1).strip()
            else:
                # Fallback: strip [SQL:...] and (Background...) noise
                msg = _re.sub(r"\[SQL:.*", "", msg, flags=_re.DOTALL).strip()
                msg = _re.sub(r"\(Background.*", "", msg, flags=_re.DOTALL).strip()
            raise ValueError(f"Invalid view query: {msg}") from None
        finally:
            await trans.rollback()


async def get_database_tables(engine: AsyncEngine) -> list[dict]:
    """Return all user tables/views with their column names and types.

    Used by the admin UI SQL editor for autocompletion.
    """
    sql = text("""
        SELECT
            t.table_name,
            c.column_name,
            c.data_type
        FROM information_schema.tables t
        JOIN information_schema.columns c
            ON c.table_name = t.table_name AND c.table_schema = t.table_schema
        WHERE t.table_schema = 'public'
            AND t.table_type IN ('BASE TABLE', 'VIEW')
        ORDER BY t.table_name, c.ordinal_position
    """)
    async with engine.connect() as conn:
        result = await conn.execute(sql)
        rows = result.fetchall()

    tables: dict[str, list[dict]] = {}
    for table_name, col_name, data_type in rows:
        if table_name not in tables:
            tables[table_name] = []
        tables[table_name].append({"name": col_name, "type": data_type})

    return [{"name": t, "columns": cols} for t, cols in tables.items()]
