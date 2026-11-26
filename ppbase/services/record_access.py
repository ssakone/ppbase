"""Rule-aware record resolution.

Single place where a caller's API rule access to records is resolved, shared
by the records API, relation expansion, file serving and realtime:

- :func:`build_rule_auth_context` turns a decoded token payload into the auth
  context understood by :func:`ppbase.services.rule_engine.check_rule`.
- :func:`fetch_records_with_rule` loads records by id, keeping only the rows
  the caller may access under a given collection rule.
- :func:`can_access_record` checks a single record against a rule.
- :func:`build_collection_filter_access` decides how ``@collection.<name>``
  references in caller supplied filters resolve (the referenced collection's
  ``listRule``).

Superusers always have full access.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord
from ppbase.services.filter_parser import CollectionAccess
from ppbase.services.record_service import (
    _table_name,
    compile_record_filters,
    get_all_collections,
)
from ppbase.services.rule_engine import check_rule

SUPERUSERS_COLLECTION = "_superusers"


def is_superuser_payload(auth_payload: dict[str, Any] | None) -> bool:
    """Return whether a decoded token payload belongs to a superuser."""
    if not isinstance(auth_payload, dict):
        return False
    token_type = auth_payload.get("type")
    if token_type == "admin":
        return True
    return (
        token_type == "authRecord"
        and auth_payload.get("collectionName") == SUPERUSERS_COLLECTION
    )


def build_rule_auth_context(
    auth_payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the :func:`check_rule` auth context for a token payload."""
    if not isinstance(auth_payload, dict) or not auth_payload.get("id"):
        return None
    if is_superuser_payload(auth_payload):
        return {
            "is_admin": True,
            "@request.auth.id": auth_payload.get("id", ""),
            "@request.auth.email": auth_payload.get("email", ""),
        }
    return {
        "is_admin": False,
        "@request.auth.id": auth_payload.get("id", ""),
        "@request.auth.collectionId": auth_payload.get("collectionId", ""),
        "@request.auth.collectionName": auth_payload.get("collectionName", ""),
        "@request.auth.type": auth_payload.get("type", ""),
    }


def rule_auth_context_from_request(
    request_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Derive the rule auth context from a filter ``request_context``."""
    if not isinstance(request_context, dict):
        return None
    auth = request_context.get("auth")
    return build_rule_auth_context(auth if isinstance(auth, dict) else None)


async def fetch_records_with_rule(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_ids: list[str],
    rule: str | None,
    *,
    auth_ctx: dict[str, Any] | None,
    request_context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Load raw rows by id, keeping only those the caller may access.

    ``rule`` is the collection rule to enforce (usually ``view_rule``).  A
    ``None`` rule allows superusers only, ``""`` everyone, and an expression
    is applied as an additional SQL condition.

    Returns a mapping of record id -> raw row dict.
    """
    ids = list(dict.fromkeys(str(rid) for rid in record_ids if rid))
    if not ids:
        return {}

    rule_result = check_rule(rule, auth_ctx)
    if rule_result is False:
        return {}

    placeholders = ", ".join(f":_eid{i}" for i in range(len(ids)))
    where_sql = f'"id" IN ({placeholders})'
    params: dict[str, Any] = {f"_eid{i}": rid for i, rid in enumerate(ids)}

    if isinstance(rule_result, str):
        rule_sql, rule_params = await compile_record_filters(
            engine,
            collection,
            rule_filter=rule_result,
            request_context=request_context,
        )
        where_sql = f"{where_sql} AND ({rule_sql})"
        params.update(rule_params)

    table = _table_name(collection)
    sql = f'SELECT * FROM "{table}" WHERE {where_sql}'
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params)
        rows = result.mappings().all()

    return {str(row["id"]): dict(row) for row in rows}


async def can_access_record(
    engine: AsyncEngine,
    collection: CollectionRecord,
    record_id: str,
    rule: str | None,
    *,
    auth_ctx: dict[str, Any] | None,
    request_context: dict[str, Any] | None = None,
) -> bool:
    """Return whether an existing record passes ``rule`` for the caller."""
    rows = await fetch_records_with_rule(
        engine,
        collection,
        [record_id],
        rule,
        auth_ctx=auth_ctx,
        request_context=request_context,
    )
    return str(record_id) in rows


def _filter_mentions_collection(filter_str: str | None) -> bool:
    return bool(filter_str) and "@collection." in str(filter_str)


async def build_collection_filter_access(
    engine: AsyncEngine,
    auth_ctx: dict[str, Any] | None,
    filter_str: str | None,
) -> CollectionAccess | None:
    """Return the ``@collection`` access callback for a caller filter.

    A referenced collection resolves only when the caller may list it: a
    ``None`` list rule (superusers only) or an unknown collection fails to
    resolve, and an expression list rule is applied inside the subquery.
    Returns ``None`` (no restriction) for superusers or filters without
    ``@collection`` references.
    """
    if auth_ctx and auth_ctx.get("is_admin"):
        return None
    if not _filter_mentions_collection(filter_str):
        return None

    collections = await get_all_collections(engine)
    by_name = {str(coll.name): coll for coll in collections}

    def _access(name: str) -> bool | str:
        coll = by_name.get(name)
        if coll is None:
            return False
        return check_rule(coll.list_rule, auth_ctx)

    return _access
