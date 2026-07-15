"""Migration file generator for PPBase.

Generates Python migration files following PocketBase's naming convention:
``{unix_timestamp}_{action}_{collection_name}.py``

Each migration file contains async ``up(app)`` and ``down(app)`` functions
that use the MigrationApp helper to apply/revert schema changes.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping
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
    return copy.deepcopy(field_dict)


_AUTH_TOKEN_OPTION_KEYS = frozenset(
    {
        "authToken",
        "passwordResetToken",
        "verificationToken",
        "emailChangeToken",
        "fileToken",
    }
)
_OAUTH_CREDENTIAL_KEYS = frozenset(
    {
        "clientId",
        "clientSecret",
        "client_id",
        "client_secret",
    }
)


def _strip_oauth_credentials(value: Any) -> Any:
    """Return a deep copy of an OAuth value without provider credentials."""
    if isinstance(value, dict):
        return {
            key: _strip_oauth_credentials(item)
            for key, item in value.items()
            if key not in _OAUTH_CREDENTIAL_KEYS
        }
    if isinstance(value, list):
        return [_strip_oauth_credentials(item) for item in value]
    return copy.deepcopy(value)


def _sanitize_auth_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Strip runtime auth secrets from options embedded in migration files.

    Token signing secrets and OAuth client credentials belong to each runtime
    environment. Exporting them into a migration both leaks credentials and
    causes a restored snapshot to reuse production secrets.
    """
    sanitized = copy.deepcopy(dict(options))

    for option_name in _AUTH_TOKEN_OPTION_KEYS:
        token_options = sanitized.get(option_name)
        if isinstance(token_options, dict):
            token_options.pop("secret", None)

    oauth_options = sanitized.get("oauth2")
    if oauth_options is not None:
        sanitized["oauth2"] = _strip_oauth_credentials(oauth_options)

    return sanitized


def _sanitize_collection_definition(
    definition: Mapping[str, Any],
) -> dict[str, Any]:
    """Deep-copy a collection definition and remove runtime credentials."""
    sanitized = copy.deepcopy(dict(definition))
    options = sanitized.get("options")
    if isinstance(options, Mapping):
        sanitized["options"] = _sanitize_auth_options(options)
    return sanitized


def _serialize_collection(record: CollectionRecord) -> dict[str, Any]:
    """Serialize a CollectionRecord ORM instance to a plain dict.

    The resulting dict contains all the information needed to fully
    recreate the collection via ``app.create_collection()``.
    """
    raw_schema = record.schema if isinstance(record.schema, list) else []
    raw_indexes = record.indexes if isinstance(record.indexes, list) else []
    raw_options = record.options if isinstance(record.options, dict) else {}

    fields = [_serialize_field(f) for f in raw_schema]

    definition = {
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
    return _sanitize_collection_definition(definition)


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


_TIMESTAMPED_FILENAME_RE = re.compile(r"^(?P<timestamp>\d+)(?P<rest>_.+\.py)$")
_TIMESTAMP_RESERVATION_RE = re.compile(
    r"^\.ppbase-migration-(?P<timestamp>\d+)\.reserve$"
)


def _next_collision_filename(filename: str, attempt: int) -> str:
    """Return a deterministic alternative without changing migration syntax."""
    match = _TIMESTAMPED_FILENAME_RE.fullmatch(filename)
    if match:
        timestamp = int(match.group("timestamp")) + attempt
        return f"{timestamp}{match.group('rest')}"

    path = Path(filename)
    return f"{path.stem}_{attempt}{path.suffix}"


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes after publishing a migration file."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _max_reserved_timestamp(directory: Path) -> int | None:
    """Return the largest timestamp already published or currently reserved."""
    largest: int | None = None
    for entry in directory.iterdir():
        migration_match = _TIMESTAMPED_FILENAME_RE.fullmatch(entry.name)
        reservation_match = _TIMESTAMP_RESERVATION_RE.fullmatch(entry.name)
        match = migration_match or reservation_match
        if match is None:
            continue
        timestamp = int(match.group("timestamp"))
        largest = timestamp if largest is None else max(largest, timestamp)
    return largest


def _reserve_timestamp(directory: Path, requested: int) -> tuple[int, Path]:
    """Atomically reserve a timestamp greater than every existing migration."""
    while True:
        largest = _max_reserved_timestamp(directory)
        timestamp = max(requested, (largest + 1) if largest is not None else requested)
        reservation = directory / f".ppbase-migration-{timestamp}.reserve"
        try:
            descriptor = os.open(
                reservation,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            requested = timestamp + 1
            continue
        else:
            os.close(descriptor)
            return timestamp, reservation


def _write_migration_file(migrations_dir: str | Path, filename: str, content: str) -> str:
    """Atomically create a migration file and return its full path.

    A complete temporary file is hard-linked into place, making the final
    path visible in one operation. Timestamp reservations serialize writers in
    the same directory and ensure every generated migration sorts after all
    earlier files, even when their suffixes differ or the clock is frozen.
    """
    dir_path = Path(migrations_dir)
    dir_path.mkdir(parents=True, exist_ok=True)

    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=dir_path,
        prefix=f".{filename}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        timestamp_match = _TIMESTAMPED_FILENAME_RE.fullmatch(filename)
        if timestamp_match is not None:
            requested_timestamp = int(timestamp_match.group("timestamp"))
            rest = timestamp_match.group("rest")
            while True:
                timestamp, reservation = _reserve_timestamp(
                    dir_path,
                    requested_timestamp,
                )
                file_path = dir_path / f"{timestamp}{rest}"
                linked = False
                try:
                    os.link(temporary_path, file_path)
                    linked = True
                    _fsync_directory(dir_path)
                    return str(file_path)
                except FileExistsError:
                    requested_timestamp = timestamp + 1
                except BaseException:
                    if linked:
                        file_path.unlink(missing_ok=True)
                    raise
                finally:
                    reservation.unlink(missing_ok=True)

        attempt = 0
        while True:
            candidate_name = (
                filename
                if attempt == 0
                else _next_collision_filename(filename, attempt)
            )
            file_path = dir_path / candidate_name
            linked = False
            try:
                os.link(temporary_path, file_path)
                linked = True
                _fsync_directory(dir_path)
                return str(file_path)
            except FileExistsError:
                attempt += 1
            except BaseException:
                if linked:
                    file_path.unlink(missing_ok=True)
                raise
    finally:
        temporary_path.unlink(missing_ok=True)


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
        old_dict = _sanitize_collection_definition(old_snapshot)
    else:
        old_dict = _serialize_collection(old_snapshot)

    if isinstance(new_record, dict):
        new_dict = _sanitize_collection_definition(new_record)
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


# ---------------------------------------------------------------------------
# Full collections snapshot
# ---------------------------------------------------------------------------


_DEFAULT_USERS_ID = "_pb_users_auth_"


def _normalize_snapshot_collection(
    collection: CollectionRecord | Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize a snapshot input to the migration collection shape."""
    if not isinstance(collection, Mapping):
        return _serialize_collection(collection)

    raw = copy.deepcopy(dict(collection))
    raw_schema = raw.get("schema")
    raw_indexes = raw.get("indexes")
    raw_options = raw.get("options")
    definition = {
        "id": raw.get("id"),
        "name": raw.get("name"),
        "type": raw.get("type") or "base",
        "system": bool(raw.get("system", False)),
        "schema": raw_schema if isinstance(raw_schema, list) else [],
        "indexes": raw_indexes if isinstance(raw_indexes, list) else [],
        "listRule": raw.get("listRule", raw.get("list_rule")),
        "viewRule": raw.get("viewRule", raw.get("view_rule")),
        "createRule": raw.get("createRule", raw.get("create_rule")),
        "updateRule": raw.get("updateRule", raw.get("update_rule")),
        "deleteRule": raw.get("deleteRule", raw.get("delete_rule")),
        "options": dict(raw_options) if isinstance(raw_options, Mapping) else {},
    }
    return _sanitize_collection_definition(definition)


def _is_default_users_collection(definition: Mapping[str, Any]) -> bool:
    """Return whether a definition represents PPBase's bootstrap users auth."""
    return definition.get("id") == _DEFAULT_USERS_ID or (
        definition.get("name") == "users" and definition.get("type") == "auth"
    )


def _order_snapshot_collections(
    definitions: list[dict[str, Any]],
    view_dependencies: Mapping[str, Iterable[str]] | None,
) -> list[dict[str, Any]]:
    """Order tables first and views in stable PostgreSQL dependency order."""
    def sort_key(definition: Mapping[str, Any]) -> tuple[str, str]:
        return (
            str(definition.get("name", "")).casefold(),
            str(definition.get("id", "")),
        )
    table_collections = sorted(
        (
            definition
            for definition in definitions
            if definition.get("type") != "view"
        ),
        key=sort_key,
    )
    view_collections = [
        definition
        for definition in definitions
        if definition.get("type") == "view"
    ]
    if not view_collections:
        return table_collections

    views_by_name = {
        str(definition["name"]): definition
        for definition in view_collections
    }
    dependency_map = view_dependencies or {}
    remaining = {
        name: {
            str(dependency)
            for dependency in dependency_map.get(name, ())
            if str(dependency) in views_by_name and str(dependency) != name
        }
        for name in views_by_name
    }

    ordered_views: list[dict[str, Any]] = []
    while remaining:
        ready = sorted(
            (name for name, dependencies in remaining.items() if not dependencies),
            key=lambda name: sort_key(views_by_name[name]),
        )
        if not ready:
            unresolved = ", ".join(sorted(remaining))
            raise ValueError(
                "Snapshot contains cyclic or unresolved view dependencies: "
                f"{unresolved}."
            )

        for name in ready:
            ordered_views.append(views_by_name[name])
            remaining.pop(name)
        for dependencies in remaining.values():
            dependencies.difference_update(ready)

    return [*table_collections, *ordered_views]


def generate_snapshot_migration(
    collections: Iterable[CollectionRecord | Mapping[str, Any]],
    migrations_dir: str | Path,
    *,
    include_system: bool = False,
    view_dependencies: Mapping[str, Iterable[str]] | None = None,
) -> str:
    """Generate one PocketBase-style migration for all current collections.

    By default internal system collections are excluded because PPBase owns
    and bootstraps them. The stable ``users`` auth collection is updated in
    place, regular collections are created before views, and all collection
    and field IDs are preserved.

    Runtime token secrets and OAuth client credentials are always removed,
    including when ``include_system`` is explicitly enabled.

    Args:
        collections: Collection records or serialized collection mappings.
        migrations_dir: Directory in which to create the migration file.
        include_system: Include PPBase-owned system collections when true.
        view_dependencies: Mapping of view names to other view names they use.

    Returns:
        The full path of the generated snapshot migration.

    Raises:
        ValueError: If a collection is missing an ID/name or if more than one
            collection could represent the stable users collection.
    """
    definitions: list[dict[str, Any]] = []
    for collection in collections:
        definition = _normalize_snapshot_collection(collection)
        if definition.get("system") and not include_system:
            continue
        if not definition.get("id"):
            raise ValueError("Snapshot collections must have a stable ID.")
        if not definition.get("name"):
            raise ValueError("Snapshot collections must have a name.")
        definitions.append(definition)

    users_definitions = [
        definition
        for definition in definitions
        if _is_default_users_collection(definition)
    ]
    if len(users_definitions) > 1:
        raise ValueError("Snapshot contains multiple default users collections.")

    users_changes: dict[str, Any] | None = None
    users_target: str | None = None
    source_users_id: str | None = None
    if users_definitions:
        source_users_id = str(users_definitions[0].get("id") or "")
        # Resolve by name at replay time. The target may be PPBase's stable
        # bootstrap collection or a recovered legacy database with another ID.
        users_target = "users"
        users_changes = {
            key: value
            for key, value in users_definitions[0].items()
            if key != "id"
        }

    created_collections = _order_snapshot_collections(
        [
            definition
            for definition in definitions
            if not _is_default_users_collection(definition)
        ],
        view_dependencies,
    )

    users_changes_str = _format_value(
        users_changes,
        indent=4,
        current_indent=0,
    )
    created_collections_str = _format_value(
        created_collections,
        indent=4,
        current_indent=0,
    )
    content = f'''"""Auto-generated migration: collections snapshot."""

import copy


_USERS_TARGET = {users_target!r}

_SOURCE_USERS_ID = {source_users_id!r}

_USERS_CHANGES = {users_changes_str}

_CREATED_COLLECTIONS = {created_collections_str}


def _rewrite_users_relations(definition, actual_users_id):
    """Return a copy whose users relations target the replay database."""
    rewritten = copy.deepcopy(definition)
    if _SOURCE_USERS_ID is None:
        return rewritten

    schema = rewritten.get("schema")
    if not isinstance(schema, list):
        return rewritten

    for field in schema:
        if not isinstance(field, dict) or field.get("type") != "relation":
            continue

        options = field.get("options")
        if (
            isinstance(options, dict)
            and options.get("collectionId") == _SOURCE_USERS_ID
        ):
            options["collectionId"] = actual_users_id

        if field.get("collectionId") == _SOURCE_USERS_ID:
            field["collectionId"] = actual_users_id

    return rewritten


async def _find_users_collection(app):
    """Resolve the snapshot users collection on the replay database."""
    try:
        return await app.find_collection(_SOURCE_USERS_ID)
    except LookupError:
        return await app.find_collection(_USERS_TARGET)


async def up(app):
    """Apply the complete collections snapshot."""
    actual_users_id = None
    if _USERS_TARGET is not None and _USERS_CHANGES is not None:
        users = await _find_users_collection(app)
        actual_users_id = users.id
        changes = _rewrite_users_relations(_USERS_CHANGES, actual_users_id)
        await app.update_collection(actual_users_id, changes)

    # Extend-mode replay updates existing collections and creates missing ones.
    for definition in _CREATED_COLLECTIONS:
        if actual_users_id is not None:
            definition = _rewrite_users_relations(definition, actual_users_id)
        else:
            definition = copy.deepcopy(definition)
        await app.save_collection(definition)


async def down(app):
    """Keep data intact when removing this baseline history marker."""
    # Snapshot files are registered as already applied on their source DB.
    # Their exact pre-snapshot state is unknown, so a destructive rollback
    # would delete collections and data the snapshot itself never created.
    pass
'''

    timestamp = int(time.time())
    filename = f"{timestamp}_collections_snapshot.py"
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
