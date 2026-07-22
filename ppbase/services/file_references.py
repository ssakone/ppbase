"""Canonical read-only extraction of local record file references."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ppbase.core.storage_safety import (
    is_remote_file_reference,
    validate_collection_id,
    validate_file_reference,
    validate_record_id,
)


LocalFileReference = tuple[str, str, str]


def _value(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _quote_identifier(value: Any, *, label: str) -> str:
    identifier = str(value or "")
    if not identifier or "\x00" in identifier:
        raise ValueError(f"invalid {label}")
    return '"' + identifier.replace('"', '""') + '"'


def _raw_file_values(value: Any) -> Iterable[Any]:
    if isinstance(value, (list, tuple, set)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if stripped.startswith("["):
            try:
                decoded = json.loads(stripped)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, list):
                return decoded
        return (value,)
    if value:
        return (value,)
    return ()


def canonical_local_file_references_for_row(
    collection: Any,
    row: Mapping[str, Any],
) -> tuple[LocalFileReference, ...]:
    """Return validated, deduplicated local references owned by one row.

    View collections do not own storage. Legacy HTTP(S) values remain in the
    database for compatibility but are external resources, not local backup
    inputs.
    """
    if str(_value(collection, "type", "base") or "base") not in {"base", "auth"}:
        return ()

    collection_id = validate_collection_id(_value(collection, "id"))
    record_id = validate_record_id(row.get("id"))
    references: set[LocalFileReference] = set()
    for raw_field in _value(collection, "schema", ()) or ():
        if not isinstance(raw_field, Mapping) or raw_field.get("type") != "file":
            continue
        field_name = str(raw_field.get("name", "") or "")
        if not field_name:
            continue
        for raw_filename in _raw_file_values(row.get(field_name)):
            filename = str(raw_filename)
            if not filename or is_remote_file_reference(filename):
                continue
            references.add(
                (
                    collection_id,
                    record_id,
                    validate_file_reference(filename),
                )
            )
    return tuple(sorted(references))


async def _load_storage_collections(
    connection: AsyncConnection,
) -> list[dict[str, Any]]:
    rows = (
        await connection.execute(
            text(
                """
                SELECT id, name, type, "schema"
                FROM public."_collections"
                WHERE type IN ('base', 'auth')
                ORDER BY id
                """
            )
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def read_canonical_local_file_references(
    connection: AsyncConnection,
    collections: Iterable[Any] | None = None,
    *,
    targets: set[tuple[str, str]] | None = None,
) -> tuple[LocalFileReference, ...]:
    """Read the canonical local-file reference set from PostgreSQL.

    ``targets=None`` scans every owning base/auth collection for backup. An
    explicit target set limits reconciliation to the affected records.
    """
    selected_collections = list(
        collections
        if collections is not None
        else await _load_storage_collections(connection)
    )

    target_ids: dict[str, set[str]] | None = None
    if targets is not None:
        target_ids = {}
        for raw_collection_id, raw_record_id in targets:
            collection_id = validate_collection_id(raw_collection_id)
            record_id = validate_record_id(raw_record_id)
            target_ids.setdefault(collection_id, set()).add(record_id)
        if not target_ids:
            return ()

    references: set[LocalFileReference] = set()
    visited_target_collections: set[str] = set()
    for collection in selected_collections:
        collection_type = str(_value(collection, "type", "base") or "base")
        if collection_type not in {"base", "auth"}:
            continue
        collection_id = validate_collection_id(_value(collection, "id"))
        if target_ids is not None:
            record_ids = target_ids.get(collection_id)
            if not record_ids:
                continue
            visited_target_collections.add(collection_id)
        else:
            record_ids = None

        fields = sorted(
            {
                str(raw_field.get("name", "") or "")
                for raw_field in (_value(collection, "schema", ()) or ())
                if isinstance(raw_field, Mapping)
                and raw_field.get("type") == "file"
                and raw_field.get("name")
            }
        )
        if not fields:
            continue

        quoted_table = _quote_identifier(
            _value(collection, "name"),
            label="collection table name",
        )
        quoted_fields = ", ".join(
            _quote_identifier(field, label="file field name") for field in fields
        )
        statement = f'SELECT "id", {quoted_fields} FROM public.{quoted_table}'
        parameters: dict[str, Any] = {}
        if record_ids is not None:
            statement += (
                ' WHERE "id"::pg_catalog.text = '
                "ANY(CAST(:record_ids AS pg_catalog.text[]))"
            )
            parameters["record_ids"] = sorted(record_ids)
        statement += ' ORDER BY "id"'

        async with connection.stream(text(statement), parameters) as result:
            async for row in result.mappings():
                references.update(
                    canonical_local_file_references_for_row(collection, row)
                )

    if target_ids is not None:
        missing = sorted(set(target_ids) - visited_target_collections)
        if missing:
            raise RuntimeError(
                "Missing collection during storage file reconciliation: "
                + ", ".join(missing)
            )
    return tuple(sorted(references))
