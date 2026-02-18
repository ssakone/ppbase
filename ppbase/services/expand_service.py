"""Relation expansion service.

Resolves the ``expand`` query parameter by batch-loading related records
from relation fields.  Supports up to 6 levels of nested dot-notation
expansion (e.g., ``author.company.address``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord
from ppbase.models.record import build_record_response, format_datetime

# Maximum nesting depth for relation expansion (PocketBase limit).
MAX_EXPAND_DEPTH = 6


def _collection_type(collection: CollectionRecord) -> str:
    return str(getattr(collection, "type", "base") or "base").strip().lower()


def _parse_expand_string(expand_str: str) -> list[list[str]]:
    """Parse an expand string into a list of field-path lists.

    ``"author,tags,author.company"`` becomes
    ``[["author"], ["tags"], ["author", "company"]]``.
    """
    if not expand_str or not expand_str.strip():
        return []
    paths: list[list[str]] = []
    for part in expand_str.split(","):
        part = part.strip()
        if not part:
            continue
        segments = part.split(".")
        if len(segments) > MAX_EXPAND_DEPTH:
            segments = segments[:MAX_EXPAND_DEPTH]
        paths.append(segments)
    return paths


def _find_collection_by_id(
    coll_id: str,
    all_collections: list[CollectionRecord],
) -> CollectionRecord | None:
    for c in all_collections:
        if c.id == coll_id:
            return c
    return None


def _find_collection_by_name(
    name: str,
    all_collections: list[CollectionRecord],
) -> CollectionRecord | None:
    for c in all_collections:
        if c.name == name:
            return c
    return None


def _get_relation_field(
    collection: CollectionRecord,
    field_name: str,
) -> dict[str, Any] | None:
    """Find a relation field definition by name in the collection schema."""
    schema: list[dict[str, Any]] = collection.schema or []
    for f in schema:
        if f.get("name") == field_name and f.get("type") == "relation":
            return f
    return None


async def _batch_fetch_records(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Fetch multiple records from a collection table by IDs.

    Returns a dict mapping record ID -> raw row dict.
    """
    if not record_ids:
        return {}

    table = collection.name
    # Use parameterized IN clause
    placeholders = ", ".join(f":_eid{i}" for i in range(len(record_ids)))
    sql = f'SELECT * FROM "{table}" WHERE "id" IN ({placeholders})'
    params = {f"_eid{i}": rid for i, rid in enumerate(record_ids)}

    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        rows = result.mappings().all()

    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_dict = dict(row)
        records[row_dict["id"]] = row_dict
    return records


async def expand_records(
    engine: AsyncEngine,
    collection: CollectionRecord,
    records: list[dict[str, Any]],
    expand_str: str,
    all_collections: list[CollectionRecord],
    request_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand relation fields on a list of record response dicts.

    Modifies records in-place, adding an ``expand`` key where applicable.

    Args:
        engine: The async database engine.
        collection: The collection the records belong to.
        records: List of record response dicts (already built via
            ``build_record_response``).
        expand_str: The raw expand query parameter value.
        all_collections: All collections for resolving relation targets.

    Returns:
        The same list of records, now with ``expand`` dicts attached.
    """
    if not expand_str or not records:
        return records

    paths = _parse_expand_string(expand_str)
    if not paths:
        return records

    # Group paths by first segment for efficient batch loading
    for path in paths:
        await _expand_path(
            engine,
            collection,
            records,
            path,
            all_collections,
            depth=0,
            request_context=request_context,
        )

    return records


async def _expand_path(
    engine: AsyncEngine,
    collection: CollectionRecord,
    records: list[dict[str, Any]],
    path: list[str],
    all_collections: list[CollectionRecord],
    depth: int,
    request_context: dict[str, Any] | None = None,
) -> None:
    """Recursively expand a single dot-notation path on a set of records."""
    if depth >= MAX_EXPAND_DEPTH or not path:
        return

    field_name = path[0]
    remaining = path[1:]

    # Find the relation field definition
    rel_field = _get_relation_field(collection, field_name)
    if rel_field is None:
        return

    opts = rel_field.get("options", {})
    target_coll_id = opts.get("collectionId", "")
    max_select = opts.get("maxSelect", 1) or 1
    is_multi = max_select > 1

    target_coll = _find_collection_by_id(target_coll_id, all_collections)
    if target_coll is None:
        return

    # Collect all relation IDs from the records
    all_ids: set[str] = set()
    for record in records:
        value = record.get(field_name)
        if value is None:
            continue
        if isinstance(value, list):
            all_ids.update(str(v) for v in value if v)
        elif value:
            all_ids.add(str(value))

    if not all_ids:
        return

    # Batch fetch related records
    related_rows = await _batch_fetch_records(engine, target_coll, list(all_ids))

    # Build response dicts for related records
    target_schema: list[dict[str, Any]] = target_coll.schema or []
    hidden_fields = {
        f.get("name", "")
        for f in target_schema
        if f.get("hidden", False) or f.get("type") == "password"
    }

    _type = _collection_type(target_coll)
    _is_auth = _type == "auth"
    _is_view = _type == "view"
    auth_payload = None
    apply_email_visibility = False
    if isinstance(request_context, dict):
        apply_email_visibility = True
        raw_auth = request_context.get("auth")
        if isinstance(raw_auth, dict):
            auth_payload = raw_auth
    related_responses: dict[str, dict[str, Any]] = {}
    for rid, row in related_rows.items():
        related_responses[rid] = build_record_response(
            row,
            target_coll.id,
            target_coll.name,
            target_schema,
            hidden_fields=hidden_fields,
            is_auth_collection=_is_auth,
            is_view_collection=_is_view,
            request_auth=auth_payload,
            apply_email_visibility=apply_email_visibility,
        )

    # Attach expanded records to each parent record
    for record in records:
        if "expand" not in record:
            record["expand"] = {}

        value = record.get(field_name)
        if value is None:
            continue

        if is_multi and isinstance(value, list):
            expanded_list = [
                related_responses[str(v)]
                for v in value
                if str(v) in related_responses
            ]
            if expanded_list:
                record["expand"][field_name] = expanded_list
        elif not is_multi:
            rid = str(value) if value else ""
            if rid in related_responses:
                record["expand"][field_name] = related_responses[rid]

    # If there are remaining segments, recurse into the expanded records
    if remaining:
        # Collect all expanded records at this level to expand further
        nested_records: list[dict[str, Any]] = []
        for record in records:
            expand_data = record.get("expand", {})
            expanded = expand_data.get(field_name)
            if expanded is None:
                continue
            if isinstance(expanded, list):
                nested_records.extend(expanded)
            elif isinstance(expanded, dict):
                nested_records.append(expanded)

        if nested_records:
            await _expand_path(
                engine,
                target_coll,
                nested_records,
                remaining,
                all_collections,
                depth + 1,
                request_context=request_context,
            )
