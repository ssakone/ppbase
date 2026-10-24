"""Relation expansion service (PocketBase 0.22.34 semantics).

Resolves the ``expand`` query parameter by batch-loading related records:
one query per expand path and nesting level, never one per record.

PocketBase 0.22.34 references (``git show v0.22.34:<file>``):

- ``daos/record_expand.go``: ``ExpandRecords`` l.37-55 (one independent pass
  per normalized path; a failing path is collected and skipped),
  ``expandRecords`` l.66-267 (direct relations and ``<collection>_via_<field>``
  back relations, nested levels expanded *before* the assignment so a failing
  nested level drops the whole path, field order kept, missing or refused
  targets skipped, single vs list) and ``normalizeExpands`` l.269-302.
- ``apis/record_helpers.go``: ``EnrichRecords`` l.149-174 and ``expandFetch``
  l.176-210, the fetch function of every HTTP/realtime expand: an admin gets
  every related record; a ``viewRule`` of ``null`` fails the path ("only
  admins can view collection ... records"); ``""`` is public; any other rule
  filters the related records with the request info of the current request
  (``@request.auth``, ``@request.data``, ``@request.query``, headers, method,
  context), hidden fields allowed. ``autoIgnoreAuthRecordsEmailVisibility``
  l.212-279 shows the email of an expanded auth record to an admin, to its
  owner and to a client matching the collection ``manageRule``.
- ``models/record.go``: ``MergeExpand`` l.176-249 and ``PublicExport``
  l.557-559 (no ``expand`` key when nothing was expanded).

``expand_records`` called *with* a ``request_context`` applies those request
semantics; *without* one it is the server-side ``Dao.ExpandRecords(records,
expands, nil)`` of hooks, which loads every related record (record_expand.go
l.67-72).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ppbase.db.system_tables import CollectionRecord
from ppbase.models.field_multiplicity import (
    field_options,
    prepare_relation_value,
    relation_is_multiple,
    to_unique_string_list,
)
from ppbase.models.record import build_record_response

logger = logging.getLogger(__name__)

# Maximum nesting depth for relation expansion (daos.MaxExpandDepth).
MAX_EXPAND_DEPTH = 6
# Back-relation ids loaded per parent record (record_expand.go l.122-124).
BACK_RELATION_LIMIT = 1000

# Go's \w is ASCII-only.
_INDIRECT_EXPAND_RE = re.compile(r"^(\w+)_via_(\w+)$", re.ASCII)
# Deprecated ``collection(field)`` syntax, still accepted by 0.22.34.
_INDIRECT_EXPAND_OLD_RE = re.compile(r"^(\w+)\((\w+)\)$", re.ASCII)
_RECORD_AUTH_TYPES = frozenset({"auth", "authRecord"})
_IDS_PARAM = "_ppb_expand_ids"


class ExpandError(Exception):
    """One failed expand path (an entry of PocketBase's ``failed`` map)."""


def normalize_expands(paths: Iterable[str]) -> list[str]:
    """PocketBase ``normalizeExpands`` (daos/record_expand.go l.269-302).

    Spaces are removed, leading/trailing dots trimmed, empty paths dropped,
    a path contained in a longer one is merged into it and duplicates are
    removed (first occurrence kept): ``["a.b.c", "a.b", " test ", "",
    "test"]`` becomes ``["a.b.c", "test"]``.
    """
    normalized = []
    for raw in paths:
        path = str(raw).replace(" ", "").strip(".")
        if path:
            normalized.append(path)

    result = []
    for i, first in enumerate(normalized):
        contained = any(
            i != j and second.startswith(first + ".")
            for j, second in enumerate(normalized)
        )
        if not contained:
            result.append(first)
    return list(dict.fromkeys(result))


def is_admin_auth(auth: Any) -> bool:
    """Whether a request ``auth`` payload is a PocketBase 0.22 admin.

    Same test as the records API: an admin token, or a record token of the
    ``_superusers`` collection.
    """
    if not isinstance(auth, dict):
        return False
    auth_type = auth.get("type")
    if auth_type == "admin":
        return True
    return (
        auth_type in _RECORD_AUTH_TYPES and auth.get("collectionName") == "_superusers"
    )


def _collection_type(collection: CollectionRecord) -> str:
    return str(getattr(collection, "type", "base") or "base").strip().lower()


def _get_relation_field(
    collection: CollectionRecord,
    field_name: str,
) -> dict[str, Any] | None:
    """Find a relation field definition by name in the collection schema."""
    schema: list[dict[str, Any]] = collection.schema or []
    for field in schema:
        if field.get("name") == field_name and field.get("type") == "relation":
            return field
    return None


def _relation_ids(value: Any, *, multiple: bool) -> list[str]:
    # Record.GetStringSlice(PrepareValue(value)): a single relation keeps
    # only its last id even if a list was stored.
    return to_unique_string_list(prepare_relation_value(value, multiple=multiple))


def _merge_expand(record: dict[str, Any], expand: Any) -> None:
    """PocketBase ``Record.MergeExpand`` (models/record.go l.176-249)."""
    if not isinstance(expand, dict) or not expand:
        return
    current = record.get("expand")
    if not isinstance(current, dict):
        record["expand"] = dict(expand)
        return

    for key, new in expand.items():
        if key not in current:
            current[key] = new
            continue

        old = current[key]
        if isinstance(old, dict):
            old_list, was_old_list = [old], False
        elif isinstance(old, list):
            old_list, was_old_list = list(old), True
        else:
            current[key] = new
            continue

        if isinstance(new, dict):
            new_list, was_new_list = [new], False
        elif isinstance(new, list):
            new_list, was_new_list = new, True
        else:
            continue

        indexed = {item.get("id"): item for item in old_list if isinstance(item, dict)}
        for new_item in new_list:
            if not isinstance(new_item, dict):
                continue
            old_item = indexed.get(new_item.get("id"))
            if old_item is not None:
                _merge_expand(old_item, new_item.get("expand"))
            else:
                old_list.append(new_item)

        if was_old_list or was_new_list or not old_list:
            current[key] = old_list
        else:
            current[key] = old_list[0]


class _Expander:
    """One ``expand`` evaluation: the request semantics and its caches."""

    def __init__(
        self,
        engine: AsyncEngine,
        all_collections: list[CollectionRecord],
        request_context: dict[str, Any] | None,
    ) -> None:
        self.engine = engine
        self.collections = list(all_collections or [])
        self.request_context = (
            request_context if isinstance(request_context, dict) else None
        )
        raw_auth = self.request_context.get("auth") if self.request_context else None
        self.auth: dict[str, Any] = raw_auth if isinstance(raw_auth, dict) else {}
        # No request context: server-side Dao.ExpandRecords(..., nil).
        self.server_side = self.request_context is None
        self.is_admin = not self.server_side and is_admin_auth(self.auth)
        self._full_relation_index: Any = None

    # -- collections -------------------------------------------------------

    def find_collection(self, name_or_id: str) -> CollectionRecord | None:
        """``Dao.FindCollectionByNameOrId``: id, or case-insensitive name."""
        for collection in self.collections:
            if collection.id == name_or_id:
                return collection
        lowered = str(name_or_id).lower()
        for collection in self.collections:
            if str(collection.name).lower() == lowered:
                return collection
        return None

    # -- SQL ---------------------------------------------------------------

    def _filter_resolvers(
        self,
        collection: CollectionRecord,
        expression: str,
    ) -> tuple[Any, Any]:
        """Same metadata as ``record_service._build_filter_resolvers``.

        Built from the collections already loaded for this expand, so that a
        rule does not cost an extra ``_collections`` query.
        """
        from ppbase.services.record_service import (
            _build_relation_index_from_collections,
            _filter_needs_relation_index,
        )

        relation_resolver = _build_relation_index_from_collections([collection])
        collection_resolver = None
        if _filter_needs_relation_index(expression):
            if self._full_relation_index is None:
                self._full_relation_index = _build_relation_index_from_collections(
                    self.collections
                )
            relation_resolver = self._full_relation_index
        if "@collection." in expression:
            collection_resolver = relation_resolver
        return relation_resolver, collection_resolver

    def rule_sql(
        self,
        collection: CollectionRecord,
        rule: str,
    ) -> tuple[str, dict[str, Any]]:
        """A collection rule as SQL, evaluated like PocketBase's resolver.

        ``resolvers.NewRecordFieldResolver(dao, collection, requestInfo,
        true)``: the request context of the current request and hidden
        fields allowed (record_helpers.go l.192, l.258).
        """
        from ppbase.services.filter_parser import parse_filter

        relation_resolver, collection_resolver = self._filter_resolvers(
            collection, rule
        )
        return parse_filter(
            rule,
            self.request_context,
            relation_resolver,
            collection_resolver,
            collection.name,
            allow_hidden=True,
        )

    async def query(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Run a read; a failure never poisons an enclosing transaction.

        Create and update expand on the connection of the write transaction:
        a failing expand query (for example a rule PostgreSQL rejects) must
        only fail its path, like in PocketBase, so it runs in a savepoint.
        """
        async with self.engine.connect() as conn:
            if conn.in_transaction():
                async with conn.begin_nested():
                    result = await conn.execute(text(sql), params)
                    return [dict(row) for row in result.mappings().all()]
            result = await conn.execute(text(sql), params)
            return [dict(row) for row in result.mappings().all()]

    # -- fetch (expandFetch) -------------------------------------------------

    async def fetch(
        self,
        collection: CollectionRecord,
        record_ids: list[str],
    ) -> list[dict[str, Any]]:
        """``expandFetch`` (apis/record_helpers.go l.176-210).

        The rule check precedes the query even for an empty id list, like
        ``Dao.FindRecordsByIds`` applying its filters before running.
        """
        where_sql = ""
        params: dict[str, Any] = {}
        if not self.server_side and not self.is_admin:
            rule = collection.view_rule
            if rule is None:
                raise ExpandError(
                    f"only admins can view collection {collection.name!r} records"
                )
            if rule != "":
                where_sql, params = self.rule_sql(collection, rule)

        unique_ids = list(dict.fromkeys(str(rid) for rid in record_ids if rid))
        if not unique_ids:
            return []

        table = collection.name
        sql = f'SELECT * FROM "{table}" WHERE "{table}"."id" = ANY(CAST(:{_IDS_PARAM} AS text[]))'
        if where_sql:
            sql += f" AND ({where_sql})"
        params[_IDS_PARAM] = unique_ids
        return await self.query(sql, params)

    async def email_visible_ids(
        self,
        collection: CollectionRecord,
        record_ids: list[str],
    ) -> set[str]:
        """``autoIgnoreAuthRecordsEmailVisibility`` (record_helpers.go l.212-279).

        Ids of the auth records whose email is shown regardless of
        ``emailVisibility``: every record for an admin, the requester's own
        record, and the records the requester manages (``manageRule``).
        """
        if not record_ids or _collection_type(collection) != "auth":
            return set()
        if self.is_admin:
            return set(record_ids)

        visible: set[str] = set()
        auth_id = str(self.auth.get("id") or "")
        if auth_id and auth_id in record_ids:
            visible.add(auth_id)

        options = collection.options if isinstance(collection.options, dict) else {}
        manage_rule = options.get("manageRule")
        if manage_rule is None or manage_rule == "":
            return visible

        table = collection.name
        try:
            where_sql, params = self.rule_sql(collection, str(manage_rule))
            params[_IDS_PARAM] = list(record_ids)
            rows = await self.query(
                f'SELECT "{table}"."id" AS "id" FROM "{table}" '
                f'WHERE "{table}"."id" = ANY(CAST(:{_IDS_PARAM} AS text[])) '
                f"AND ({where_sql})",
                params,
            )
        except Exception as exc:  # noqa: BLE001 - PocketBase ignores it (l.205)
            logger.debug(
                "expand: manageRule of %s not applied: %s", collection.name, exc
            )
            return visible
        visible.update(str(row["id"]) for row in rows)
        return visible

    async def build(
        self,
        collection: CollectionRecord,
        rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Response dicts of the fetched rows, keyed by record id."""
        from ppbase.services.record_service import _SUPERUSERS_HIDDEN_COLUMNS

        if collection.name == "_superusers":
            rows = [
                {k: v for k, v in row.items() if k not in _SUPERUSERS_HIDDEN_COLUMNS}
                for row in rows
            ]

        schema: list[dict[str, Any]] = collection.schema or []
        hidden_fields = {
            field.get("name", "")
            for field in schema
            if field.get("hidden", False) or field.get("type") == "password"
        }
        col_type = _collection_type(collection)
        visible_emails: set[str] = set()
        if not self.server_side:
            visible_emails = await self.email_visible_ids(
                collection, [str(row.get("id", "")) for row in rows]
            )

        responses: dict[str, dict[str, Any]] = {}
        for row in rows:
            record_id = str(row.get("id", ""))
            responses[record_id] = build_record_response(
                row,
                collection.id,
                collection.name,
                schema,
                hidden_fields=hidden_fields,
                is_auth_collection=col_type == "auth",
                is_view_collection=col_type == "view",
                request_auth=self.auth or None,
                apply_email_visibility=(
                    not self.server_side and record_id not in visible_emails
                ),
            )
        return responses

    # -- back relations ------------------------------------------------------

    async def back_relation_ids(
        self,
        collection: CollectionRecord,
        field_name: str,
        *,
        multiple: bool,
        parent_ids: list[str],
    ) -> dict[str, list[str]]:
        """Ids of ``collection`` records whose ``field_name`` targets each parent.

        PocketBase runs ``SELECT id FROM <collection> WHERE <field> = :id``
        (single) or ``EXISTS (json_each(<field>) = :id)`` (multiple) with
        ``LIMIT 1000`` per parent (record_expand.go l.121-150), without a
        rule and in SQLite scan order. PPBase runs one query for all parents
        and orders each parent's ids by ``ctid`` (its ``@rowid`` equivalent);
        a view has no ``ctid`` and keeps PostgreSQL's order.
        """
        unique_parents = list(dict.fromkeys(pid for pid in parent_ids if pid))
        if not unique_parents:
            return {}

        table = collection.name
        order = "" if _collection_type(collection) == "view" else " ORDER BY t.ctid"
        if multiple:
            match = f'p._parent = ANY(t."{field_name}")'
        else:
            match = f't."{field_name}" = p._parent'
        sql = (
            "SELECT s._parent AS parent, s.id AS id FROM ("
            'SELECT p._parent, t."id" AS id, '
            f"ROW_NUMBER() OVER (PARTITION BY p._parent{order}) AS _rn "
            f'FROM "{table}" AS t '
            f"JOIN UNNEST(CAST(:{_IDS_PARAM} AS text[])) AS p(_parent) ON {match}"
            f") AS s WHERE s._rn <= {BACK_RELATION_LIMIT} ORDER BY s._parent, s._rn"
        )
        rows = await self.query(sql, {_IDS_PARAM: unique_parents})
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row["parent"]), []).append(str(row["id"]))
        return result

    # -- one path (expandRecords) -------------------------------------------

    async def expand_path(
        self,
        records: list[dict[str, Any]],
        collection: CollectionRecord,
        path: str,
        level: int,
    ) -> None:
        """PocketBase ``expandRecords`` (daos/record_expand.go l.66-267)."""
        if not path or level > MAX_EXPAND_DEPTH or not records:
            return

        head, separator, rest = path.partition(".")

        if "(" in head:
            indirect_match = _INDIRECT_EXPAND_OLD_RE.match(head)
        else:
            indirect_match = _INDIRECT_EXPAND_RE.match(head)

        if indirect_match is not None:
            rel_collection = self.find_collection(indirect_match.group(1))
            if rel_collection is None:
                raise ExpandError(
                    f"couldn't find back-related collection {indirect_match.group(1)!r}"
                )
            back_field = _get_relation_field(rel_collection, indirect_match.group(2))
            if back_field is None:
                raise ExpandError(
                    f"couldn't find back-relation field {indirect_match.group(2)!r} "
                    f"in collection {rel_collection.name!r}"
                )
            back_options = field_options(back_field)
            if back_options.get("collectionId") != collection.id:
                raise ExpandError(f"invalid back-relation field path {head!r}")

            from ppbase.services.record_service import _has_single_column_unique_index

            mapping = await self.back_relation_ids(
                rel_collection,
                str(back_field.get("name")),
                multiple=relation_is_multiple(back_options),
                parent_ids=[str(record.get("id") or "") for record in records],
            )
            # maxSelect 1 only with a single-column unique index (l.156-162).
            single = _has_single_column_unique_index(
                str(back_field.get("name")), rel_collection.indexes
            )

            def ids_of(record: dict[str, Any]) -> list[str]:
                return mapping.get(str(record.get("id") or ""), [])

        else:
            rel_field = _get_relation_field(collection, head)
            if rel_field is None:
                raise ExpandError(
                    f"Couldn't find relation field {head!r} in collection "
                    f"{collection.name!r}."
                )
            options = field_options(rel_field)
            rel_collection = self.find_collection(str(options.get("collectionId", "")))
            if rel_collection is None:
                raise ExpandError(
                    f"Couldn't find related collection {options.get('collectionId')!r}."
                )
            # A list unless maxSelect is a number <= 1 (l.257-261), so
            # maxSelect: null expands to a list (D-0019).
            multiple = relation_is_multiple(options)
            single = not multiple

            def ids_of(record: dict[str, Any]) -> list[str]:
                return _relation_ids(record.get(head), multiple=multiple)

        all_ids = [rid for record in records for rid in ids_of(record)]
        rows = await self.fetch(rel_collection, all_ids)
        related = await self.build(rel_collection, rows)

        # The nested levels run first: their failure drops this level too.
        if separator:
            await self.expand_path(
                list(related.values()), rel_collection, rest, level + 1
            )

        for record in records:
            valid = [related[rid] for rid in ids_of(record) if rid in related]
            if not valid:
                continue  # no valid relations: no key

            expand = record.get("expand")
            if not isinstance(expand, dict):
                expand = {}
                record["expand"] = expand

            previous = expand.get(head)
            if isinstance(previous, dict):
                previous_list = [previous]
            elif isinstance(previous, list):
                previous_list = previous
            else:
                previous_list = []
            for old in previous_list:
                if not isinstance(old, dict):
                    continue
                for rel in valid:
                    if rel.get("id") == old.get("id"):
                        _merge_expand(rel, old.get("expand"))

            expand[head] = valid[0] if single else valid


async def expand_records(
    engine: AsyncEngine,
    collection: CollectionRecord,
    records: list[dict[str, Any]],
    expand_str: str | list[str] | tuple[str, ...],
    all_collections: list[CollectionRecord],
    request_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Expand relation fields on a list of record response dicts.

    Modifies records in-place, adding an ``expand`` key where applicable.

    Args:
        engine: The async database engine (or the pinned connection adapter
            of a write transaction).
        collection: The collection the records belong to.
        records: List of record response dicts (already built via
            ``build_record_response``).
        expand_str: The raw expand query parameter value (comma separated),
            or a list of expand paths.
        all_collections: All collections for resolving relation targets.
        request_context: The rule context of the current request (as built
            for ``parse_filter``). When given, every expanded record must
            satisfy its collection ``viewRule`` for this request, admins
            excepted, and auth emails follow PocketBase's visibility rules
            (``apis.EnrichRecords``). When ``None``, every related record is
            loaded (server-side ``Dao.ExpandRecords(..., nil)``).

    Returns:
        The same list of records, now with ``expand`` dicts attached.
        A failing path (unknown field, refused collection, rule error) is
        skipped without failing the request, like PocketBase.
    """
    if not expand_str or not records:
        return records

    if isinstance(expand_str, (list, tuple)):
        raw_paths: list[str] = [str(item) for item in expand_str]
    else:
        raw_paths = str(expand_str).split(",")
    paths = normalize_expands(raw_paths)
    if not paths:
        return records

    expander = _Expander(engine, all_collections, request_context)
    failed: dict[str, str] = {}
    for path in paths:
        try:
            await expander.expand_path(records, collection, path, 1)
        except Exception as exc:  # noqa: BLE001 - PocketBase failed map
            failed[path] = str(exc)
    if failed:
        logger.debug("expand: failed paths on %s: %s", collection.name, failed)

    # PocketBase omits the "expand" key of a record with no expanded relation
    # (models/record.go l.557-559, daos/record_expand.go l.227-229).
    for record in records:
        _strip_empty_expand(record)

    return records


def _strip_empty_expand(record: dict[str, Any]) -> None:
    """Drop empty ``expand`` dicts recursively (PocketBase omits them)."""
    expand = record.get("expand")
    if not isinstance(expand, dict):
        return
    for value in expand.values():
        children = value if isinstance(value, list) else [value]
        for child in children:
            if isinstance(child, dict):
                _strip_empty_expand(child)
    if not expand:
        del record["expand"]
