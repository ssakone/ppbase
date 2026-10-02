"""Validate client-supplied ``filter`` and ``sort`` expressions before they reach SQL.

PocketBase never lets the API filter or sort by auth secrets, only lets superusers filter or sort by
hidden fields, and answers 400 for unknown fields. Without this guard, a user allowed to list an auth
collection could recover password hashes or token keys character by character with ``~`` filters,
and an unknown field produced a 500. Collection rules are trusted and are not checked here.
"""

from __future__ import annotations

from typing import Any

from lark import Token, Tree

# Never filterable/sortable through the API, whatever the auth (stored secrets of auth collections).
PROTECTED_FIELDS = frozenset({"password", "password_hash", "passwordHash", "tokenKey", "token_key"})
_BASE_FIELDS = frozenset({"id", "created", "updated"})
_AUTH_FIELDS = frozenset({"email", "emailVisibility", "email_visibility", "verified"})


def _schema_fields(collection: Any) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for field in collection.schema or []:
        definition = field if isinstance(field, dict) else getattr(field, "__dict__", {})
        if definition.get("name"):
            fields[definition["name"]] = definition
    return fields


def _paths(filter_str: str) -> list[tuple[str | None, list[str]]]:
    """``(collection_name_or_None, segments)`` for every field reference of the expression."""
    from ppbase.services.filter_parser import _parser

    tree = _parser.parse(filter_str)
    found: list[tuple[str | None, list[str]]] = []
    for node in tree.iter_subtrees():
        if node.data == "field_path":
            found.append((None, [str(t).split(":", 1)[0] for t in node.children if isinstance(t, Token)]))
        elif node.data == "macro":
            name = str(node.children[0]).lstrip("@")
            if name.startswith("collection."):
                parts = name.split(".")
                if len(parts) >= 3:
                    found.append((parts[1].split(":", 1)[0], [p.split(":", 1)[0] for p in parts[2:] if p]))
    return found


def _sort_paths(sort_str: str) -> list[list[str]]:
    result = []
    for part in sort_str.split(","):
        part = part.strip().lstrip("+-")
        if part and not part.startswith("@"):
            result.append(part.split("."))
    return result


async def _check_path(loader, collection: Any, segments: list[str]) -> None:
    current = collection
    for index, segment in enumerate(segments):
        if segment in PROTECTED_FIELDS:
            raise ValueError(f"Invalid filter field: {segment}.")
        if "_via_" in segment:
            target_name, _, via_field = segment.partition("_via_")
            target = await loader.collection(target_name)
            if target is None or via_field not in _schema_fields(target):
                raise ValueError(f"Invalid filter field: {segment}.")
            current = target
            continue
        fields = _schema_fields(current)
        allowed_system = _BASE_FIELDS | (_AUTH_FIELDS if current.type == "auth" else frozenset())
        if segment in allowed_system:
            return
        definition = fields.get(segment)
        if definition is None:
            raise ValueError(f"Invalid filter field: {segment}.")
        if definition.get("hidden"):
            raise ValueError(f"Invalid filter field: {segment}.")
        if index == len(segments) - 1:
            return
        if definition.get("type") != "relation":
            return  # json/select paths: deeper segments are values, not fields
        options = definition.get("options") or {}
        target = await loader.collection(options.get("collectionId") or definition.get("collectionId") or "")
        if target is None:
            raise ValueError(f"Invalid filter field: {segment}.")
        current = target


async def assert_client_query_allowed(
    engine: Any,
    collection: Any,
    filter_str: str | None,
    sort_str: str | None,
    *,
    is_superuser: bool,
) -> None:
    """Raise ``ValueError`` (mapped to 400) if the client expression references a forbidden field."""
    if not filter_str and not sort_str:
        return
    from ppbase.services.request_macros import _Loader

    loader = _Loader(engine)
    references: list[tuple[str | None, list[str]]] = []
    if filter_str:
        try:
            references.extend(_paths(filter_str))
        except Exception as exc:  # syntax errors are reported by the parser itself
            raise ValueError(f"Invalid filter syntax: {exc}") from exc
    if sort_str:
        references.extend((None, path) for path in _sort_paths(sort_str))
    for collection_name, segments in references:
        if not segments:
            continue
        if is_superuser:
            # Superusers may use hidden fields, never stored auth secrets.
            if any(s in PROTECTED_FIELDS for s in segments):
                raise ValueError(f"Invalid filter field: {next(s for s in segments if s in PROTECTED_FIELDS)}.")
            continue
        start = collection if collection_name is None else await loader.collection(collection_name)
        if start is None:
            raise ValueError(f"Invalid filter collection: {collection_name}.")
        await _check_path(loader, start, segments)
