"""Resolve ``@request.auth.*`` and ``@request.body.*`` macros that need database lookups.

PocketBase resolves ``@request.auth.<field>`` against the full authenticated record (not only
the token claims), including relation traversal such as ``@request.auth.role.permissions``.
Body macros may also traverse relations (``@request.body.pdv.agent.id``).

The filter parser is synchronous, so the values are resolved beforehand and stored in
``request_context["resolved_macros"]`` keyed by the macro name without ``@``. Unknown fields
resolve to ``None`` (SQL ``NULL``), which keeps comparisons well-typed and falsy.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

_MACRO_RE = re.compile(
    r"@request\.(auth|body|data)\.([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*)(?![A-Za-z0-9_.:])"
)
# Values carried by the token itself; the parser already resolves them.
_TOKEN_AUTH_FIELDS = {"id", "collectionId", "type"}


def _to_param(value: Any) -> Any:
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(list(value) if isinstance(value, tuple) else value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
    return value


class _Loader:
    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self._collections: dict[str, Any] = {}
        self._records: dict[tuple[str, str], dict[str, Any] | None] = {}

    async def collection(self, id_or_name: str) -> Any | None:
        if id_or_name not in self._collections:
            from ppbase.services.record_service import resolve_collection

            try:
                self._collections[id_or_name] = await resolve_collection(self._engine, id_or_name)
            except Exception:
                self._collections[id_or_name] = None
        return self._collections[id_or_name]

    async def record(self, collection: Any, record_id: str) -> dict[str, Any] | None:
        key = (collection.name, record_id)
        if key not in self._records:
            async with self._engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(f'SELECT * FROM "{collection.name}" WHERE "id" = :id LIMIT 1'),
                        {"id": record_id},
                    )
                ).mappings().first()
            self._records[key] = dict(row) if row else None
        return self._records[key]


def _relation_target(collection: Any, field_name: str) -> str | None:
    for field in collection.schema or []:
        definition = field if isinstance(field, dict) else getattr(field, "__dict__", {})
        if definition.get("name") != field_name or definition.get("type") != "relation":
            continue
        options = definition.get("options") or {}
        return options.get("collectionId") or definition.get("collectionId")
    return None


async def _walk(loader: _Loader, collection: Any, value: Any, segments: list[str]) -> Any:
    """Follow ``segments`` starting from ``value``, the content of a field of ``collection``."""
    field_name = None
    for segment in segments:
        if field_name is None:
            record = value
        else:
            target = _relation_target(collection, field_name)
            target_id = value[0] if isinstance(value, list) and value else value
            if not target or not target_id:
                return None
            if segment == "id":
                value, field_name = target_id, None
                continue
            collection = await loader.collection(target)
            if collection is None:
                return None
            record = await loader.record(collection, str(target_id))
        if not isinstance(record, dict) or segment not in record:
            return None
        value, field_name = record[segment], segment
    return value


async def resolve_request_macros(
    engine: Any,
    filter_str: str | None,
    request_context: dict[str, Any] | None,
    collection: Any | None = None,
) -> None:
    if not filter_str or not isinstance(request_context, dict) or "@request." not in filter_str:
        return
    matches = set(_MACRO_RE.findall(filter_str))
    if not matches:
        return
    resolved: dict[str, Any] = request_context.setdefault("resolved_macros", {})
    loader = _Loader(engine)
    auth = request_context.get("auth") or {}

    for source, path in matches:
        key = f"request.{source}.{path}"
        if key in resolved:
            continue
        segments = path.split(".")
        if source == "auth":
            if segments[0] in _TOKEN_AUTH_FIELDS and len(segments) == 1:
                continue
            if not auth.get("id") or not auth.get("collectionId"):
                # Anonymous (or revoked token): every record field is NULL, never ''
                # (a '' bound against a boolean column fails); collectionName stays ''.
                resolved[key] = "" if path == "collectionName" else None
                continue
            auth_collection = await loader.collection(str(auth["collectionId"]))
            if auth_collection is None:
                continue
            if path == "collectionName":
                resolved[key] = auth_collection.name
                continue
            record = await loader.record(auth_collection, str(auth["id"]))
            resolved[key] = _to_param(await _walk(loader, auth_collection, record, segments))
        else:
            data = request_context.get("data") or {}
            if len(segments) == 1 or collection is None or not isinstance(data, dict):
                continue
            if segments[0] not in data:
                resolved[key] = None
                continue
            # The payload acts as the starting record of the target collection.
            payload = {segments[0]: data[segments[0]]}
            resolved[key] = _to_param(await _walk(loader, collection, payload, segments))
