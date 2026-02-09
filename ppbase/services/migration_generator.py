"""Migration file generator for PPBase.

Generates Python migration files following PocketBase's naming convention:
``{unix_timestamp}_{action}_{collection_name}.py``

Each migration file contains async ``up(app)`` and ``down(app)`` functions
that use the MigrationApp helper to apply/revert schema changes.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ppbase.db.system_tables import CollectionRecord


# ---------------------------------------------------------------------------
# Collection serialization helpers
# ---------------------------------------------------------------------------


def _serialize_field(field_dict: dict[str, Any]) -> dict[str, Any]:
    """Serialize a single field definition for embedding in migration code.

    Ensures all values are plain Python types suitable for ``repr()``.
    """
    result: dict[str, Any] = {}
    for key, value in field_dict.items():
        if isinstance(value, dict):
            result[key] = dict(value)
        elif isinstance(value, list):
            result[key] = list(value)
        else:
            result[key] = value
    return result


def _serialize_collection(record: CollectionRecord) -> dict[str, Any]:
    """Serialize a CollectionRecord ORM instance to a plain dict.

    The resulting dict contains all the information needed to fully
    recreate the collection via ``app.create_collection()``.
    """
    raw_schema = record.schema if isinstance(record.schema, list) else []
    raw_indexes = record.indexes if isinstance(record.indexes, list) else []
    raw_options = record.options if isinstance(record.options, dict) else {}

    fields = [_serialize_field(f) for f in raw_schema]

    return {
        "id": record.id,
        "name": record.name,
        "type": record.type,
        "system": record.system,
        "schema": fields,
        "indexes": list(raw_indexes),
        "listRule": record.list_rule,
        "viewRule": record.view_rule,
        "createRule": record.create_rule,
        "updateRule": record.update_rule,
        "deleteRule": record.delete_rule,
        "options": dict(raw_options),
    }


# ---------------------------------------------------------------------------
# Safe name for file paths
# ---------------------------------------------------------------------------

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _safe_filename(name: str) -> str:
    """Convert a collection name to a safe filename component."""
    return _SAFE_NAME_RE.sub("_", name).lower()


# ---------------------------------------------------------------------------
# Code formatting helpers
# ---------------------------------------------------------------------------


def _format_dict(d: dict[str, Any], indent: int = 4) -> str:
    """Format a Python dict as a readable, indented string.

    Produces valid Python code that can be embedded in migration files.
    """
    return _format_value(d, indent=indent, current_indent=indent)


def _format_value(value: Any, indent: int = 4, current_indent: int = 0) -> str:
    """Recursively format a Python value as readable code."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = []
        for item in value:
            formatted = _format_value(item, indent=indent, current_indent=current_indent + indent)
            items.append(f"{' ' * (current_indent + indent)}{formatted},")
        inner = "\n".join(items)
        return f"[\n{inner}\n{' ' * current_indent}]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = []
        for k, v in value.items():
            formatted_v = _format_value(v, indent=indent, current_indent=current_indent + indent)
            items.append(f"{' ' * (current_indent + indent)}{repr(k)}: {formatted_v},")
        inner = "\n".join(items)
        return f"{{\n{inner}\n{' ' * current_indent}}}"
    return repr(value)


# ---------------------------------------------------------------------------
# Schema diff helpers (for update migrations)
# ---------------------------------------------------------------------------


def _fields_by_id(schema: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index fields by their id (or name if id is missing)."""
    result: dict[str, dict[str, Any]] = {}
    for field in schema:
        key = field.get("id") or field.get("name", "")
        if key:
            result[key] = field
    return result


def _compute_schema_diff(
    old_schema: list[dict[str, Any]],
    new_schema: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute the diff between two field schemas.

    Returns a dict with:
        - added: list of new field definitions
        - removed: list of removed field definitions
        - changed: list of (old_field, new_field) tuples for modified fields
    """
    old_by_id = _fields_by_id(old_schema)
    new_by_id = _fields_by_id(new_schema)

    old_keys = set(old_by_id.keys())
    new_keys = set(new_by_id.keys())

    added = [new_by_id[k] for k in sorted(new_keys - old_keys)]
    removed = [old_by_id[k] for k in sorted(old_keys - new_keys)]

    changed = []
    for key in sorted(old_keys & new_keys):
        old_field = old_by_id[key]
        new_field = new_by_id[key]
        if old_field != new_field:
            changed.append((old_field, new_field))

    return {"added": added, "removed": removed, "changed": changed}


def _compute_collection_diff(
    old_snapshot: dict[str, Any],
    new_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Compute the diff between two collection snapshots.

    Returns a dict of changed top-level keys (excluding schema, which is
    diffed separately).
    """
    changes: dict[str, Any] = {}

    # Keys to compare at the collection level (not schema)
    compare_keys = [
        "name", "type", "system", "indexes", "options",
        "listRule", "viewRule", "createRule", "updateRule", "deleteRule",
    ]

    for key in compare_keys:
        old_val = old_snapshot.get(key)
        new_val = new_snapshot.get(key)
        if old_val != new_val:
            changes[key] = {"old": old_val, "new": new_val}

    return changes


# ---------------------------------------------------------------------------
# Migration file generators
# ---------------------------------------------------------------------------


def _write_migration_file(migrations_dir: str | Path, filename: str, content: str) -> str:
    """Write a migration file and return its full path."""
    dir_path = Path(migrations_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    file_path = dir_path / filename
    file_path.write_text(content, encoding="utf-8")
    return str(file_path)


def generate_create_migration(
    collection_record: CollectionRecord,
    migrations_dir: str | Path,
) -> str:
    """Generate a 'created' migration file for a new collection.

    The up() function creates the collection from a full definition.
    The down() function deletes the collection.

    Args:
        collection_record: The CollectionRecord ORM instance.
        migrations_dir: Directory to write the migration file to.

    Returns:
        The full path of the generated migration file.
    """
    timestamp = int(time.time())
    safe_name = _safe_filename(collection_record.name)
    filename = f"{timestamp}_created_{safe_name}.py"

    definition = _serialize_collection(collection_record)
    definition_str = _format_value(definition, indent=4, current_indent=4)

    content = f'''"""Auto-generated migration: create collection '{collection_record.name}'."""


async def up(app):
    """Apply migration: create collection."""
    definition = {definition_str}
    await app.create_collection(definition)


async def down(app):
    """Revert migration: delete collection."""
    await app.delete_collection({repr(collection_record.id)})
'''

    return _write_migration_file(migrations_dir, filename, content)


def generate_update_migration(
    old_snapshot: CollectionRecord | dict[str, Any],
    new_record: CollectionRecord | dict[str, Any],
    migrations_dir: str | Path,
) -> str:
    """Generate an 'updated' migration file for a modified collection.

    The up() function applies the forward changes.
    The down() function reverses each change.

    Args:
        old_snapshot: The previous collection state (CollectionRecord or dict).
        new_record: The updated collection state (CollectionRecord or dict).
        migrations_dir: Directory to write the migration file to.

    Returns:
        The full path of the generated migration file.
    """
    # Normalize to dicts
    if isinstance(old_snapshot, dict):
        old_dict = dict(old_snapshot)
    else:
        old_dict = _serialize_collection(old_snapshot)

    if isinstance(new_record, dict):
        new_dict = dict(new_record)
    else:
        new_dict = _serialize_collection(new_record)

    collection_name = new_dict.get("name", old_dict.get("name", "unknown"))
    collection_id = new_dict.get("id", old_dict.get("id", ""))

    timestamp = int(time.time())
    safe_name = _safe_filename(collection_name)
    filename = f"{timestamp}_updated_{safe_name}.py"

    # Compute diffs
    collection_changes = _compute_collection_diff(old_dict, new_dict)

    old_schema = old_dict.get("schema", [])
    new_schema = new_dict.get("schema", [])
    schema_diff = _compute_schema_diff(old_schema, new_schema)

    # Build up() body
    up_lines: list[str] = []
    down_lines: list[str] = []

    up_lines.append(f"    collection = await app.find_collection({repr(collection_id)})")
    down_lines.append(f"    collection = await app.find_collection({repr(collection_id)})")
    up_lines.append("")
    down_lines.append("")

    # Build the forward changes dict
    forward_changes: dict[str, Any] = {}
    reverse_changes: dict[str, Any] = {}

    for key, change in collection_changes.items():
        forward_changes[key] = change["new"]
        reverse_changes[key] = change["old"]

    # Handle schema changes
    if schema_diff["added"] or schema_diff["removed"] or schema_diff["changed"]:
        forward_changes["schema"] = new_schema
        reverse_changes["schema"] = old_schema

    # Generate the update calls
    if forward_changes:
        forward_str = _format_value(forward_changes, indent=4, current_indent=4)
        up_lines.append(f"    changes = {forward_str}")
        up_lines.append(f"    await app.update_collection({repr(collection_id)}, changes)")
    else:
        up_lines.append("    # No changes detected")

    if reverse_changes:
        reverse_str = _format_value(reverse_changes, indent=4, current_indent=4)
        down_lines.append(f"    changes = {reverse_str}")
        down_lines.append(f"    await app.update_collection({repr(collection_id)}, changes)")
    else:
        down_lines.append("    # No changes to revert")

    up_body = "\n".join(up_lines)
    down_body = "\n".join(down_lines)

    content = f'''"""Auto-generated migration: update collection '{collection_name}'."""


async def up(app):
    """Apply migration: update collection."""
{up_body}


async def down(app):
    """Revert migration: reverse collection update."""
{down_body}
'''

    return _write_migration_file(migrations_dir, filename, content)


def generate_delete_migration(
    collection_record: CollectionRecord,
    migrations_dir: str | Path,
) -> str:
    """Generate a 'deleted' migration file for a removed collection.

    The up() function deletes the collection.
    The down() function recreates it from the full snapshot for rollback.

    Args:
        collection_record: The CollectionRecord ORM instance being deleted.
        migrations_dir: Directory to write the migration file to.

    Returns:
        The full path of the generated migration file.
    """
    timestamp = int(time.time())
    safe_name = _safe_filename(collection_record.name)
    filename = f"{timestamp}_deleted_{safe_name}.py"

    definition = _serialize_collection(collection_record)
    definition_str = _format_value(definition, indent=4, current_indent=4)

    content = f'''"""Auto-generated migration: delete collection '{collection_record.name}'."""


async def up(app):
    """Apply migration: delete collection."""
    await app.delete_collection({repr(collection_record.id)})


async def down(app):
    """Revert migration: recreate collection."""
    definition = {definition_str}
    await app.create_collection(definition)
'''

    return _write_migration_file(migrations_dir, filename, content)


def generate_sql_migration(
    name: str,
    up_sql: str,
    down_sql: str,
    migrations_dir: str | Path,
    *,
    up_params: dict[str, Any] | None = None,
    down_params: dict[str, Any] | None = None,
) -> str:
    """Generate a migration file with raw SQL statements.

    Useful for custom data migrations or schema changes not covered by
    the collection CRUD operations.

    Args:
        name: A descriptive name for the migration (used in filename).
        up_sql: SQL to execute in the forward direction.
        down_sql: SQL to execute in the reverse direction.
        migrations_dir: Directory to write the migration file to.
        up_params: Optional parameters for the up SQL statement.
        down_params: Optional parameters for the down SQL statement.

    Returns:
        The full path of the generated migration file.
    """
    timestamp = int(time.time())
    safe_name = _safe_filename(name)
    filename = f"{timestamp}_updated_{safe_name}.py"

    up_params_str = _format_value(up_params, indent=4, current_indent=4) if up_params else "None"
    down_params_str = _format_value(down_params, indent=4, current_indent=4) if down_params else "None"

    content = f'''"""Auto-generated migration: {name}."""


async def up(app):
    """Apply migration."""
    await app.execute_sql(
        {repr(up_sql)},
        {up_params_str},
    )


async def down(app):
    """Revert migration."""
    await app.execute_sql(
        {repr(down_sql)},
        {down_params_str},
    )
'''

    return _write_migration_file(migrations_dir, filename, content)
