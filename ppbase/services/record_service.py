"""Record CRUD service using SQLAlchemy Core for dynamic queries.

Provides list, get, create, update, and delete operations against
dynamically-created collection tables.  All SQL is parameterized.
"""

from __future__ import annotations

import json as _json
import mimetypes
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import NotSupportedError
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.core.id_generator import generate_id
from ppbase.db.system_tables import CollectionRecord
from ppbase.models.field_types import (
    _EMAIL_RE,
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


_FIELD_CORE_KEYS = frozenset(
    {
        "id",
        "name",
        "type",
        "required",
        "system",
        "hidden",
        "presentable",
        "options",
    }
)


def _normalize_field(f: dict[str, Any]) -> dict[str, Any]:
    """Ensure flat-format field options are nested under 'options'."""
    extra = {k: v for k, v in f.items() if k not in _FIELD_CORE_KEYS}
    if not extra:
        return f
    core = {k: v for k, v in f.items() if k in _FIELD_CORE_KEYS}
    existing = core.pop("options", None) or {}
    core["options"] = {**extra, **existing}
    return core


def _normalize_file_options(field_def: FieldDefinition) -> dict[str, Any]:
    """Return file options, supporting both nested and legacy flat schemas."""
    opts = dict(field_def.options or {})
    for key in ("maxSelect", "maxSize", "mimeTypes", "thumbs", "protected"):
        if hasattr(field_def, key) and key not in opts:  # defensive fallback
            opts[key] = getattr(field_def, key)
    return opts


def _matches_mime_pattern(mime_type: str, pattern: str) -> bool:
    candidate = str(mime_type or "").strip().lower()
    rule = str(pattern or "").strip().lower()
    if not candidate or not rule:
        return False
    if rule.endswith("/*"):
        return candidate.startswith(rule[:-1])
    return candidate == rule


def _validate_uploaded_file_constraints(
    field_def: FieldDefinition,
    filename: str,
    content: bytes,
) -> None:
    """Validate uploaded file bytes against file field options."""
    options = _normalize_file_options(field_def)
    max_size = options.get("maxSize")
    if max_size not in (None, "", 0):
        try:
            limit = int(max_size)
        except Exception:
            limit = 0
        if limit > 0 and len(content) > limit:
            raise FieldValidationError(
                field_def.name,
                "validation_max_size_constraint",
                f"Must be at most {limit} byte(s).",
            )

    mime_patterns = options.get("mimeTypes")
    if isinstance(mime_patterns, list) and mime_patterns:
        guessed_mime, _encoding = mimetypes.guess_type(filename)
        mime_type = (guessed_mime or "application/octet-stream").lower()
        if not any(_matches_mime_pattern(mime_type, p) for p in mime_patterns):
            raise FieldValidationError(
                field_def.name,
                "validation_invalid_mime_type",
                f"Unsupported file type: {mime_type}.",
            )


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
    # _superusers is now a real table (no longer mapped to _admins)
    return collection.name


def _collection_type(collection: CollectionRecord) -> str:
    """Return a normalized collection type string."""
    return str(getattr(collection, "type", "base") or "base").strip().lower()


async def _notify_record_change(
    conn: Any,
    collection_name: str,
    record_id: str,
    action: str,
) -> None:
    """Publish a realtime notification using a bound payload parameter."""
    payload = _json.dumps({
        "collection": collection_name,
        "record_id": record_id,
        "action": action,
    }, separators=(",", ":"))
    await conn.execute(
        text("SELECT pg_notify('record_changes', :payload)"),
        {"payload": payload},
    )


# Columns from the _superusers table that must never be exposed through the API
_SUPERUSERS_HIDDEN_COLUMNS = frozenset(
    {
        "password_hash",
        "token_key",
        "last_reset_sent_at",
    }
)

RelationResolver = dict[str, dict[str, Any]]


async def _build_relation_resolver(
    engine: AsyncEngine,
    collection: CollectionRecord,
) -> RelationResolver:
    """Map relation field names to recursive resolver metadata.

    This allows the filter parser to translate dotted paths like
    ``author.company.name`` into proper nested ``EXISTS`` subqueries against
    the related collections' tables.
    """
    cache: dict[str, RelationResolver] = {}

    async def _build(
        current: CollectionRecord,
        active_path: set[str],
    ) -> RelationResolver:
        cache_key = str(current.id)
        if cache_key in cache:
            return cache[cache_key]

        schema: list[dict[str, Any]] = current.schema or []
        targets: list[tuple[str, str, int]] = []
        for raw in schema:
            f = _normalize_field(raw)
            if f.get("type") != "relation":
                continue
            opts = f.get("options") or {}
            coll_id = opts.get("collectionId")
            max_select = opts.get("maxSelect", 1) or 1
            field_name = str(f.get("name") or "")
            if coll_id and field_name:
                targets.append((field_name, coll_id, max_select))

        resolver: RelationResolver = {}
        cache[cache_key] = resolver

        for field_name, coll_id, max_select in targets:
            target = await resolve_collection(engine, coll_id)
            if target is None:
                continue

            target_key = str(target.id)
            nested_relations: RelationResolver = {}
            if target_key not in active_path:
                nested_relations = await _build(target, active_path | {target_key})

            resolver[field_name] = {
                "table": _table_name(target),
                "max_select": max_select,
                "relations": nested_relations,
            }

        return resolver

    return await _build(collection, {str(collection.id)})


async def _build_collection_resolver(engine: AsyncEngine) -> dict[str, dict[str, Any]]:
    """Build relation metadata for ``@collection.collection.relation.field``.

    The filter parser is intentionally DB-agnostic, so record_service supplies
    the collection schema graph it needs to resolve relation paths inside
    ``@collection`` references.
    """
    collections = await get_all_collections(engine)
    by_id = {str(coll.id): coll for coll in collections}
    by_name = {str(coll.name): coll for coll in collections}
    cache: dict[str, RelationResolver] = {}

    def _target_collection(collection_ref: Any) -> CollectionRecord | None:
        if collection_ref is None:
            return None
        key = str(collection_ref)
        return by_id.get(key) or by_name.get(key)

    def _build(
        current: CollectionRecord,
        active_path: set[str],
    ) -> RelationResolver:
        cache_key = str(current.id)
        if cache_key in cache:
            return cache[cache_key]

        resolver: RelationResolver = {}
        cache[cache_key] = resolver

        schema: list[dict[str, Any]] = current.schema or []
        for raw in schema:
            field = _normalize_field(raw)
            if field.get("type") != "relation":
                continue
            field_name = str(field.get("name") or "")
            opts = field.get("options") or {}
            target = _target_collection(opts.get("collectionId"))
            if not field_name or target is None:
                continue

            nested: RelationResolver = {}
            target_key = str(target.id)
            if target_key not in active_path:
                nested = _build(target, active_path | {target_key})

            resolver[field_name] = {
                "table": _table_name(target),
                "max_select": int(opts.get("maxSelect", 1) or 1),
                "relations": nested,
            }

        return resolver

    result: dict[str, dict[str, Any]] = {}
    for coll in collections:
        result[_table_name(coll)] = {
            "table": _table_name(coll),
            "relations": _build(coll, {str(coll.id)}),
        }
    return result


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
        # Only resolve relations when the filter contains a dotted path
        relation_resolver = None
        collection_resolver = None
        if "." in filter_str:
            relation_resolver = await _build_relation_resolver(engine, collection)
        if "@collection." in filter_str:
            collection_resolver = await _build_collection_resolver(engine)
        where_sql, params = parse_filter(
            filter_str,
            request_context,
            relation_resolver,
            collection_resolver,
            table,
        )

    # Build ORDER BY clause
    # View collections may lack "created" — fall back to no explicit order
    col_type = _collection_type(collection)
    if col_type == "view":
        # Check if the view has a "created" column
        view_field_names = {f.name for f in schema_fields}
        if "created" in view_field_names:
            order_sql = '"created" DESC'
        else:
            order_sql = "1"
    else:
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
                if (
                    col_name not in valid_fields
                    and not col_expr.endswith("()")
                    and col_name != "ctid"
                ):
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
        for _attempt in range(2):
            try:
                async with engine.connect() as conn:
                    result = await conn.execute(text(count_sql), params)
                    row = result.mappings().first()
                    total_items = row["cnt"] if row else 0
                break
            except NotSupportedError:
                if _attempt > 0:
                    raise

    # Fetch page
    select_sql = (
        f'SELECT * FROM "{table}" WHERE {where_sql} '
        f"ORDER BY {order_sql} LIMIT :_limit OFFSET :_offset"
    )
    params["_limit"] = per_page
    params["_offset"] = offset

    for _attempt in range(2):
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(select_sql), params)
                rows = [dict(r) for r in result.mappings().all()]
            break
        except NotSupportedError:
            if _attempt > 0:
                raise

    # Strip sensitive columns for _superusers (backed by _admins table)
    if collection.name == "_superusers":
        rows = [
            {k: v for k, v in row.items() if k not in _SUPERUSERS_HIDDEN_COLUMNS}
            for row in rows
        ]

    _type = _collection_type(collection)
    _is_auth = _type == "auth"
    _is_view = _type == "view"
    auth_payload = None
    apply_email_visibility = False
    if isinstance(request_context, dict):
        apply_email_visibility = True
        raw_auth = request_context.get("auth")
        if isinstance(raw_auth, dict):
            auth_payload = raw_auth
    items = [
        build_record_response(
            row,
            collection.id,
            collection.name,
            collection.schema or [],
            fields_filter=ff,
            hidden_fields=hidden,
            is_auth_collection=_is_auth,
            is_view_collection=_is_view,
            request_auth=auth_payload,
            apply_email_visibility=apply_email_visibility,
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
    request_context: dict[str, Any] | None = None,
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

    row_dict = dict(row)

    # Strip sensitive columns for _superusers (backed by _admins table)
    if collection.name == "_superusers":
        row_dict = {
            k: v for k, v in row_dict.items() if k not in _SUPERUSERS_HIDDEN_COLUMNS
        }

    _type = _collection_type(collection)
    _is_auth = _type == "auth"
    _is_view = _type == "view"
    auth_payload = None
    apply_email_visibility = False
    if isinstance(request_context, dict):
        apply_email_visibility = True
        raw_auth = request_context.get("auth")
        if isinstance(raw_auth, dict):
            auth_payload = raw_auth
    return build_record_response(
        row_dict,
        collection.id,
        collection.name,
        collection.schema or [],
        fields_filter=ff,
        hidden_fields=hidden,
        is_auth_collection=_is_auth,
        is_view_collection=_is_view,
        request_auth=auth_payload,
        apply_email_visibility=apply_email_visibility,
    )


# ---------------------------------------------------------------------------
# Create record
# ---------------------------------------------------------------------------


async def create_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    data: dict[str, Any],
    files: dict[str, list[tuple[str, bytes]]] | None = None,
) -> dict[str, Any]:
    """Create a new record in the collection.

    Validates all fields, generates an ID, and sets timestamps.
    Handles file uploads if files dict is provided.

    For auth collections, handles password hashing, token_key generation,
    and auth system column defaults.

    Raises:
        FieldValidationError: If any field fails validation.
    """
    from ppbase.services.file_storage import save_files

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

    # --- Auth collection: password & system columns ---
    col_type = _collection_type(collection)
    if col_type == "auth":
        from ppbase.services.auth_service import generate_token_key, hash_password

        # Reject client-supplied internal columns
        data.pop("password_hash", None)
        data.pop("token_key", None)

        password = data.pop("password", None)
        password_confirm = data.pop("passwordConfirm", None)

        if not password:
            errors["password"] = {
                "code": "validation_required",
                "message": "Cannot be blank.",
            }
        elif len(password) < 8:
            errors["password"] = {
                "code": "validation_length_out_of_range",
                "message": "The length must be between 8 and 72.",
            }
        elif password_confirm != password:
            errors["passwordConfirm"] = {
                "code": "validation_values_mismatch",
                "message": "Values don't match.",
            }

        if not errors:
            columns["password_hash"] = hash_password(password)
            columns["token_key"] = generate_token_key()

        # Auth system column: email (required for auth, from data)
        email_val = data.pop("email", None)
        if email_val is None or (isinstance(email_val, str) and not email_val.strip()):
            errors["email"] = {
                "code": "validation_required",
                "message": "Cannot be blank.",
            }
        else:
            email_str = str(email_val).strip()
            if not _EMAIL_RE.match(email_str):
                errors["email"] = {
                    "code": "validation_invalid_email",
                    "message": "Must be a valid email address.",
                }
            else:
                columns["email"] = email_str

        # Auth system column defaults
        if "email_visibility" not in data:
            columns["email_visibility"] = False
        else:
            columns["email_visibility"] = bool(data.pop("email_visibility", False))
        if "verified" not in data:
            columns["verified"] = False
        else:
            columns["verified"] = bool(data.pop("verified", False))

    # Build field map for file handling
    field_map = {f.name: f for f in schema_fields}

    # Process uploaded files
    if files:
        for field_name, file_list in files.items():
            field_def = field_map.get(field_name)
            if field_def is None or field_def.type != FieldType.FILE:
                continue
            upload_has_error = False
            for uploaded_name, uploaded_content in file_list:
                try:
                    _validate_uploaded_file_constraints(
                        field_def,
                        uploaded_name,
                        uploaded_content,
                    )
                except FieldValidationError as exc:
                    errors[exc.field_name] = {
                        "code": exc.code,
                        "message": exc.message,
                    }
                    upload_has_error = True
            if upload_has_error:
                continue
            max_select = field_def.options.get("maxSelect", 1) or 1
            saved_names = save_files(
                collection.id, record_id, field_name, file_list, max_select
            )
            if max_select == 1:
                data[field_name] = saved_names[0] if saved_names else ""
            else:
                data[field_name] = saved_names

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

    # Email uniqueness check for auth collections
    if col_type == "auth" and "email" in columns:
        dup_sql = f'SELECT 1 FROM "{table}" WHERE "email" = :email LIMIT 1'
        async with engine.connect() as conn:
            dup = (
                await conn.execute(text(dup_sql), {"email": columns["email"]})
            ).first()
        if dup:
            raise _ValidationErrors(
                {
                    "email": {
                        "code": "validation_not_unique",
                        "message": "The email is already in use.",
                    }
                }
            )

    # Build INSERT
    col_names = ", ".join(f'"{c}"' for c in columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    insert_sql = f'INSERT INTO "{table}" ({col_names}) VALUES ({placeholders})'

    async with engine.begin() as conn:
        await conn.execute(text(insert_sql), columns)

        # Send PostgreSQL NOTIFY for realtime updates
        await _notify_record_change(conn, collection.name, record_id, "create")

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
    files: dict[str, list[tuple[str, bytes]]] | None = None,
) -> dict[str, Any] | None:
    """Update an existing record.

    Supports ``field+`` (append) and ``field-`` (remove) modifiers for
    multi-value fields (select, relation, file).
    Handles file uploads if files dict is provided.

    Returns the updated record dict, or None if not found.
    """
    from ppbase.services.file_storage import delete_files, save_files

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

    # --- Auth collection: password update handling ---
    col_type = _collection_type(collection)
    if col_type == "auth":
        from ppbase.services.auth_service import generate_token_key, hash_password

        # Strip internal columns from client data
        data.pop("password_hash", None)
        data.pop("token_key", None)

        password = data.pop("password", None)
        password_confirm = data.pop("passwordConfirm", None)

        if password is not None:
            if len(password) < 8:
                errors["password"] = {
                    "code": "validation_length_out_of_range",
                    "message": "The length must be between 8 and 72.",
                }
            elif password_confirm != password:
                errors["passwordConfirm"] = {
                    "code": "validation_values_mismatch",
                    "message": "Values don't match.",
                }
            else:
                updates["password_hash"] = hash_password(password)
                updates["token_key"] = generate_token_key()

        # Handle email update with format validation
        if "email" in data:
            email_val = data.pop("email")
            if email_val is None or (
                isinstance(email_val, str) and not email_val.strip()
            ):
                errors["email"] = {
                    "code": "validation_required",
                    "message": "Cannot be blank.",
                }
            else:
                email_str = str(email_val).strip()
                if not _EMAIL_RE.match(email_str):
                    errors["email"] = {
                        "code": "validation_invalid_email",
                        "message": "Must be a valid email address.",
                    }
                else:
                    updates["email"] = email_str

        # Handle camelCase-to-snake_case for auth system columns
        if "email_visibility" in data:
            updates["email_visibility"] = bool(data.pop("email_visibility"))
        elif "emailVisibility" in data:
            updates["email_visibility"] = bool(data.pop("emailVisibility"))
        if "verified" in data:
            updates["verified"] = bool(data.pop("verified"))

    field_map = {f.name: f for f in schema_fields}

    # Track file fields that need cleanup after update
    files_to_delete: list[str] = []

    def _parse_modifier_key(key: str) -> tuple[str, str | None]:
        field_name = key
        modifier: str | None = None
        if key.startswith("+"):
            modifier = "prepend"
            field_name = key[1:]
        elif key.endswith("+"):
            modifier = "+"
            field_name = key[:-1]
        elif key.endswith("-"):
            modifier = "-"
            field_name = key[:-1]
        return field_name, modifier

    def _raw_file_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value if v]
        if value:
            return [str(value)]
        return []

    def _coerce_file_value(
        field_def: FieldDefinition, values: list[str]
    ) -> str | list[str]:
        max_select = field_def.options.get("maxSelect", 1) or 1
        if max_select > 1:
            return values
        return values[-1] if values else ""

    # Build a mutable field state from the current DB row so mixed multipart
    # operations like images + images- are applied against a single source of
    # truth and DB/storage stay in sync.
    field_state: dict[str, Any] = {}
    for field_name, field_def in field_map.items():
        if field_def.type == FieldType.AUTODATE:
            continue
        field_state[field_name] = raw_row.get(field_name)

    # Apply plain/modifier body values first.
    for key, value in data.items():
        if key == "id":
            continue

        field_name, modifier = _parse_modifier_key(key)
        field_def = field_map.get(field_name)
        if field_def is None:
            continue
        if field_def.type == FieldType.AUTODATE:
            continue

        current = field_state.get(field_name)

        if modifier == "+":
            field_state[field_name] = _apply_append(current, value, field_def)
        elif modifier == "prepend":
            field_state[field_name] = _apply_prepend(current, value, field_def)
        elif modifier == "-":
            field_state[field_name] = _apply_remove(current, value, field_def)
        else:
            field_state[field_name] = value

    # Apply uploaded files using the same modifier semantics as body fields.
    if files:
        for raw_key, file_list in files.items():
            field_name, modifier = _parse_modifier_key(raw_key)
            field_def = field_map.get(field_name)
            if field_def is None or field_def.type != FieldType.FILE:
                continue

            upload_has_error = False
            for uploaded_name, uploaded_content in file_list:
                try:
                    _validate_uploaded_file_constraints(
                        field_def,
                        uploaded_name,
                        uploaded_content,
                    )
                except FieldValidationError as exc:
                    errors[exc.field_name] = {
                        "code": exc.code,
                        "message": exc.message,
                    }
                    upload_has_error = True
            if upload_has_error:
                continue

            max_select = field_def.options.get("maxSelect", 1) or 1
            saved_names = save_files(
                collection.id, record_id, field_name, file_list, max_select
            )

            current = field_state.get(field_name)
            if modifier == "+":
                field_state[field_name] = _apply_append(current, saved_names, field_def)
            elif modifier == "prepend":
                field_state[field_name] = _apply_prepend(
                    current, saved_names, field_def
                )
            else:
                field_state[field_name] = _coerce_file_value(field_def, saved_names)

    # Validate final field state and derive removed physical files from the
    # final DB value instead of from intermediate mutations.
    processed_fields: set[str] = set()
    for field_name, value in field_state.items():
        field_def = field_map.get(field_name)
        if field_def is None:
            continue
        if field_def.type == FieldType.AUTODATE:
            continue

        raw_current = raw_row.get(field_name)
        if value == raw_current:
            continue

        processed_fields.add(field_name)

        if field_def.type == FieldType.FILE:
            old_files = set(_raw_file_list(raw_current))
            new_files = set(_raw_file_list(value))
            files_to_delete.extend(old_files - new_files)

        try:
            validated = validate_field_value(field_def, value)
            updates[field_name] = _serialize_for_pg(validated, field_def)
        except FieldValidationError as exc:
            errors[exc.field_name] = {"code": exc.code, "message": exc.message}

    if errors:
        raise _ValidationErrors(errors)

    # Email uniqueness check for auth collections on update
    if col_type == "auth" and "email" in updates and updates["email"]:
        dup_sql = (
            f'SELECT 1 FROM "{table}" WHERE "email" = :email AND "id" != :rid LIMIT 1'
        )
        async with engine.connect() as conn:
            dup = (
                await conn.execute(
                    text(dup_sql), {"email": updates["email"], "rid": record_id}
                )
            ).first()
        if dup:
            raise _ValidationErrors(
                {
                    "email": {
                        "code": "validation_not_unique",
                        "message": "The email is already in use.",
                    }
                }
            )

    if len(updates) <= 1:
        # Only "updated" timestamp -- nothing else changed
        return existing

    # Build UPDATE
    set_clauses = ", ".join(f'"{c}" = :{c}' for c in updates)
    update_sql = f'UPDATE "{table}" SET {set_clauses} WHERE "id" = :_rec_id'
    params = {**updates, "_rec_id": record_id}

    async with engine.begin() as conn:
        await conn.execute(text(update_sql), params)

        # Send PostgreSQL NOTIFY for realtime updates
        await _notify_record_change(conn, collection.name, record_id, "update")

    # Delete removed files from disk
    if files_to_delete:
        delete_files(collection.id, record_id, files_to_delete)

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


def _apply_prepend(
    current: Any,
    new_values: Any,
    field_def: FieldDefinition,
) -> Any:
    """Prepend values for multi-value fields or increment for numbers."""
    if field_def.type == FieldType.NUMBER:
        cur = float(current) if current is not None else 0.0
        inc = float(new_values) if new_values is not None else 0.0
        return cur + inc

    cur_list = list(current) if isinstance(current, (list, tuple)) else []
    prepend_values: list[Any]
    if isinstance(new_values, list):
        prepend_values = list(new_values)
    elif new_values is not None:
        prepend_values = [new_values]
    else:
        prepend_values = []
    return prepend_values + cur_list


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
    Deletes associated files from disk.

    Returns True if a record was deleted, False if not found.
    """
    from ppbase.services.file_storage import delete_all_files

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

        # Send PostgreSQL NOTIFY for realtime updates
        await _notify_record_change(conn, collection.name, record_id, "delete")

    # Delete all associated files from disk
    delete_all_files(collection.id, record_id)

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
                    f'SELECT "id" FROM "{other_table}" WHERE :rid = ANY("{field_name}")'
                )
            else:
                # Scalar column
                find_sql = (
                    f'SELECT "id" FROM "{other_table}" WHERE "{field_name}" = :rid'
                )

            async with engine.connect() as conn:
                result = await conn.execute(text(find_sql), {"rid": record_id})
                related_ids = [dict(r)["id"] for r in result.mappings().all()]

            for related_id in related_ids:
                await delete_record(
                    engine,
                    other_coll,
                    related_id,
                    all_collections=all_collections,
                )


# ---------------------------------------------------------------------------
# Rule filter check
# ---------------------------------------------------------------------------


async def check_record_rule(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    rule_filter: str,
    request_context: dict[str, Any] | None = None,
) -> bool:
    """Check if a record matches a rule filter expression.

    Used for view/update/delete rule enforcement.  Parses the rule as a
    PocketBase filter expression and runs it as a WHERE clause against the
    record's row.

    Returns True if the record matches the rule, False otherwise.
    """
    table = _table_name(collection)
    # Resolve relation fields for dotted paths in the rule
    relation_resolver = None
    collection_resolver = None
    if "." in rule_filter:
        relation_resolver = await _build_relation_resolver(engine, collection)
    if "@collection." in rule_filter:
        collection_resolver = await _build_collection_resolver(engine)
    where_sql, params = parse_filter(
        rule_filter,
        request_context,
        relation_resolver,
        collection_resolver,
        table,
    )
    sql = (
        f'SELECT 1 FROM "{table}" WHERE "id" = :_rule_rec_id AND ({where_sql}) LIMIT 1'
    )
    params["_rule_rec_id"] = record_id
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        return result.first() is not None


# ---------------------------------------------------------------------------
# Collection resolution helper
# ---------------------------------------------------------------------------


async def resolve_collection(
    engine: AsyncEngine,
    id_or_name: str,
) -> CollectionRecord | None:
    """Resolve a collection by ID or name from the _collections table."""
    sql = text('SELECT * FROM "_collections" WHERE id = :val OR name = :val LIMIT 1')
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
