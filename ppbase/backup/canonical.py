"""Canonical PPBase table models for backup schema reconciliation.

Builds the authoritative :class:`~ppbase.backup.schema_contract.TableSpec` set
that the live ``public`` schema must match, so a backup fails closed on any
table PPBase did not author — including one that is merely *registered* in
``_collections`` but whose physical shape PPBase never emitted.

Two authoritative sources feed the canonical set:

* the fixed system infrastructure tables, translated from the SQLAlchemy ORM
  models in :mod:`ppbase.db.system_tables`; and
* one table per non-view ``_collections`` row, rebuilt from the stored field
  schema using the exact column rules of :mod:`ppbase.db.schema_manager`.

Every emitted type/default string is the value PostgreSQL's ``format_type`` /
``pg_get_expr`` renders for the DDL PPBase emits (verified empirically), so a
canonical spec compares equal to a faithfully introspected live table.  An ORM
type or default this translator cannot reproduce raises rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

from ppbase.backup.schema_contract import (
    ColumnSpec,
    SchemaContractError,
    TableSpec,
)
from ppbase.models.field_types import FieldDefinition, FieldType


class CanonicalModelError(SchemaContractError):
    """Raised when a canonical PPBase table model cannot be built."""


# ---------------------------------------------------------------------------
# System tables (translated from the ORM declarative models)
# ---------------------------------------------------------------------------

# The exact ``pg_get_expr`` rendering of every literal ``server_default`` PPBase
# declares on a system table, keyed by ``(raw_default_value, contract_type)``.
# The ORM stores these as the bare string value passed to ``server_default``
# (``"false"``, ``"[]"``, ``"0"`` …), not a quoted SQL literal.  A value/type
# pair outside this closed map aborts the backup.
_LITERAL_DEFAULTS: dict[tuple[str, str], str] = {
    ("false", "boolean"): "false",
    ("[]", "jsonb"): "'[]'::jsonb",
    ("{}", "jsonb"): "'{}'::jsonb",
    ("0", "integer"): "0",
    ("0", "double precision"): "'0'::double precision",
}


def _sa_column_type(sa_type: Any, *, column: str) -> str:
    """Translate a SQLAlchemy column type to its ``format_type`` rendering."""
    from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
    from sqlalchemy.dialects.postgresql import JSONB

    # ``Text`` is a subclass of ``String``; test it first.
    if isinstance(sa_type, Text):
        return "text"
    if isinstance(sa_type, String):
        length = sa_type.length
        if length is None:
            raise CanonicalModelError(f"{column}: variable-length String")
        return f"character varying({length})"
    if isinstance(sa_type, Boolean):
        return "boolean"
    if isinstance(sa_type, Integer):
        return "integer"
    if isinstance(sa_type, Float):
        return "double precision"
    if isinstance(sa_type, DateTime):
        return "timestamp with time zone"
    if isinstance(sa_type, JSONB):
        return "jsonb"
    raise CanonicalModelError(f"{column}: unmapped column type {sa_type!r}")


def _sa_column_default(
    sa_column: Any, contract_type: str, *, column: str
) -> str | None:
    """Translate a SQLAlchemy ``server_default`` to its ``pg_get_expr`` form."""
    from sqlalchemy.sql.functions import now as _sa_now

    server_default = sa_column.server_default
    if server_default is None:
        return None
    arg = getattr(server_default, "arg", None)
    if isinstance(arg, _sa_now):
        return "now()"
    # A literal ``server_default`` is stored as the bare string value passed to
    # the ORM (``"false"``, ``"base"`` …); a ``text()`` clause exposes it via
    # ``.text``.  Accept both, reject anything else.
    if isinstance(arg, str):
        literal = arg
    else:
        literal = getattr(arg, "text", None)
    if not isinstance(literal, str):
        raise CanonicalModelError(f"{column}: unmappable server_default")
    if literal == "base" and contract_type.startswith("character varying"):
        return "'base'::character varying"
    resolved = _LITERAL_DEFAULTS.get((literal, contract_type))
    if resolved is None:
        raise CanonicalModelError(
            f"{column}: server_default {literal!r} on {contract_type!r} is not "
            "a known PPBase default"
        )
    return resolved


def build_system_table_specs() -> dict[str, TableSpec]:
    """Return the canonical :class:`TableSpec` for every ORM system table."""
    from sqlalchemy import UniqueConstraint

    from ppbase.db.system_tables import Base

    specs: dict[str, TableSpec] = {}
    for name, table in Base.metadata.tables.items():
        columns: list[ColumnSpec] = []
        for col in table.columns:
            label = f"{name}.{col.name}"
            col_type = _sa_column_type(col.type, column=label)
            default = _sa_column_default(col, col_type, column=label)
            columns.append(
                ColumnSpec(
                    name=col.name,
                    type=col_type,
                    nullable=bool(col.nullable),
                    default=default,
                )
            )
        primary_key = tuple(c.name for c in table.primary_key.columns)
        uniques: list[tuple[str, ...]] = [
            tuple(c.name for c in constraint.columns)
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        ]
        specs[name] = TableSpec(
            name=name,
            kind="system",
            columns=tuple(columns),
            primary_key=primary_key,
            unique_constraints=tuple(uniques),
        )
    return specs


# ---------------------------------------------------------------------------
# Collection tables (rebuilt from _collections.schema via schema_manager rules)
# ---------------------------------------------------------------------------

# The three columns every base/auth collection table starts with, exactly as
# ``schema_manager.create_collection_table`` emits them.
_BASE_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(name="id", type="character varying(15)", nullable=False, default=None),
    ColumnSpec(
        name="created",
        type="timestamp with time zone",
        nullable=False,
        default="now()",
    ),
    ColumnSpec(
        name="updated",
        type="timestamp with time zone",
        nullable=False,
        default="now()",
    ),
)

# The auth system columns appended (after the base columns) for auth tables,
# mirroring ``schema_manager._AUTH_SYSTEM_COLUMN_DEFINITIONS``.
_AUTH_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec(
        name="email",
        type="character varying(255)",
        nullable=False,
        default="''::character varying",
    ),
    ColumnSpec(name="email_visibility", type="boolean", nullable=False, default="false"),
    ColumnSpec(name="verified", type="boolean", nullable=False, default="false"),
    ColumnSpec(name="password_hash", type="text", nullable=False, default="''::text"),
    ColumnSpec(
        name="token_key",
        type="character varying(50)",
        nullable=False,
        default="''::character varying",
    ),
)


def _field_column_spec(field: FieldDefinition) -> ColumnSpec:
    """Return the canonical :class:`ColumnSpec` for one collection field.

    Mirrors ``schema_manager._col_ddl`` exactly, emitting the ``format_type`` /
    ``pg_get_expr`` rendering rather than a DDL fragment.
    """
    name = field.name
    field_type = field.type
    options = field.options

    if field_type in (
        FieldType.TEXT,
        FieldType.EDITOR,
        FieldType.URL,
        FieldType.PASSWORD,
    ):
        return ColumnSpec(name=name, type="text", nullable=False, default="''::text")
    if field_type == FieldType.NUMBER:
        if options.get("onlyInt", False):
            return ColumnSpec(name=name, type="integer", nullable=False, default="0")
        return ColumnSpec(
            name=name,
            type="double precision",
            nullable=False,
            default="'0'::double precision",
        )
    if field_type == FieldType.BOOL:
        return ColumnSpec(name=name, type="boolean", nullable=False, default="false")
    if field_type == FieldType.EMAIL:
        return ColumnSpec(
            name=name,
            type="character varying(255)",
            nullable=False,
            default="''::character varying",
        )
    if field_type == FieldType.DATE:
        return ColumnSpec(
            name=name, type="timestamp with time zone", nullable=True, default=None
        )
    if field_type == FieldType.AUTODATE:
        return ColumnSpec(
            name=name,
            type="timestamp with time zone",
            nullable=False,
            default="now()",
        )
    if field_type in (FieldType.SELECT, FieldType.FILE):
        max_select = options.get("maxSelect", 1) or 1
        if max_select > 1:
            return ColumnSpec(
                name=name, type="text[]", nullable=False, default="'{}'::text[]"
            )
        return ColumnSpec(name=name, type="text", nullable=False, default="''::text")
    if field_type == FieldType.RELATION:
        max_select = options.get("maxSelect", 1) or 1
        if max_select > 1:
            return ColumnSpec(
                name=name,
                type="character varying(15)[]",
                nullable=False,
                default="'{}'::character varying[]",
            )
        return ColumnSpec(
            name=name,
            type="character varying(15)",
            nullable=False,
            default="''::character varying",
        )
    if field_type == FieldType.JSON:
        return ColumnSpec(
            name=name, type="jsonb", nullable=False, default="'null'::jsonb"
        )
    if field_type == FieldType.GEO_POINT:
        return ColumnSpec(
            name=name,
            type="jsonb",
            nullable=False,
            default='\'{"lat": 0, "lon": 0}\'::jsonb',
        )
    # Matches the ``_col_ddl`` fallback for an unknown type.
    return ColumnSpec(name=name, type="text", nullable=False, default="''::text")


def _parse_schema_fields(schema_raw: Any) -> list[FieldDefinition]:
    """Normalise a stored ``_collections.schema`` value to field definitions."""
    if isinstance(schema_raw, str):
        try:
            schema_raw = json.loads(schema_raw)
        except (TypeError, ValueError) as exc:
            raise CanonicalModelError("_collections.schema is not valid JSON") from exc
    if schema_raw is None:
        return []
    if not isinstance(schema_raw, list):
        raise CanonicalModelError("_collections.schema must be a JSON array")
    fields: list[FieldDefinition] = []
    for definition in schema_raw:
        if isinstance(definition, FieldDefinition):
            fields.append(definition)
        elif isinstance(definition, dict):
            try:
                fields.append(FieldDefinition(**definition))
            except Exception as exc:  # pydantic ValidationError et al.
                raise CanonicalModelError(
                    "_collections.schema has an invalid field definition"
                ) from exc
        else:
            raise CanonicalModelError(
                "_collections.schema field must be an object"
            )
    return fields


def build_collection_table_spec(
    *, name: str, kind: str, schema_raw: Any
) -> TableSpec:
    """Return the canonical :class:`TableSpec` for a base/auth collection."""
    if kind not in ("base", "auth"):
        raise CanonicalModelError(f"collection {name!r} has non-table kind {kind!r}")
    fields = _parse_schema_fields(schema_raw)
    columns: list[ColumnSpec] = list(_BASE_COLUMNS)
    if kind == "auth":
        columns.extend(_AUTH_COLUMNS)
    columns.extend(_field_column_spec(field) for field in fields)
    return TableSpec(
        name=name,
        kind=kind,
        columns=tuple(columns),
        primary_key=("id",),
        unique_constraints=(),
    )


async def build_canonical_tables(pg_conn: Any) -> dict[str, TableSpec]:
    """Return the canonical table model for the live PPBase schema.

    Combines the fixed system-table specs with one spec per non-view
    ``_collections`` row.  A name that is both a system table and a collection
    row keeps its system classification (mirroring
    :func:`ppbase.backup.native.build_public_inventory`).
    """
    canonical = build_system_table_specs()
    rows = await pg_conn.fetch(
        'SELECT name, type, schema FROM public."_collections"'
    )
    for row in rows:
        name = row["name"]
        if not isinstance(name, str) or not name:
            raise CanonicalModelError("_collections row has an empty name")
        kind = row["type"]
        if kind not in {"base", "auth", "view"}:
            raise CanonicalModelError(
                f"collection {name!r} has unsupported type {kind!r}"
            )
        if kind == "view":
            continue
        if name in canonical:
            # A system infrastructure table always wins its classification.
            continue
        table_kind = "auth" if kind == "auth" else "base"
        canonical[name] = build_collection_table_spec(
            name=name, kind=table_kind, schema_raw=row["schema"]
        )
    return canonical


async def validate_canonical_collection_objects(connection: Any) -> None:
    """Reconcile live collection views/indexes with ``_collections`` metadata.

    Table columns are reconciled by :func:`build_canonical_tables` on the raw
    asyncpg connection.  This complementary pass uses the existing
    ``schema_manager`` canonicalization machinery on the caller's SQLAlchemy
    transaction to validate the two PostgreSQL objects whose semantics are not
    captured by :class:`TableSpec`: collection views and secondary indexes.
    """
    from sqlalchemy import text

    from ppbase.db.schema_manager import (
        validate_collection_indexes,
        validate_collection_view,
    )
    from ppbase.db.system_tables import Base

    rows = (
        await connection.execute(
            text(
                'SELECT name, type, schema, indexes, options '
                'FROM public."_collections" ORDER BY name'
            )
        )
    ).mappings().all()
    system_tables = set(Base.metadata.tables)

    for row in rows:
        name = row["name"]
        kind = row["type"]
        if not isinstance(name, str) or not name:
            raise CanonicalModelError("_collections row has an empty name")
        if kind not in {"base", "auth", "view"}:
            raise CanonicalModelError(
                f"collection {name!r} has unsupported type {kind!r}"
            )
        # PocketBase represents some fixed infrastructure tables as collection
        # rows too. Their table shape is governed by the ORM and they do not use
        # dynamic collection indexes/views.
        if name in system_tables:
            continue

        raw_indexes = row["indexes"]
        raw_options = row["options"]
        if not isinstance(raw_indexes, list):
            raise CanonicalModelError(
                f"collection {name!r} indexes must be a JSON array"
            )
        # A non-textual or empty index entry is silently dropped by the
        # collection machinery; refuse it here so malformed index metadata can
        # never pass the backup contract unnoticed.
        for entry in raw_indexes:
            if not isinstance(entry, str) or not entry.strip():
                raise CanonicalModelError(
                    f"collection {name!r} has a non-textual or empty index entry"
                )
        if not isinstance(raw_options, dict):
            raise CanonicalModelError(
                f"collection {name!r} options must be a JSON object"
            )

        collection = SimpleNamespace(
            name=name,
            type=kind,
            schema=row["schema"],
            indexes=raw_indexes,
            options=raw_options,
        )
        try:
            if kind == "view":
                await validate_collection_view(connection, collection)
            else:
                await validate_collection_indexes(connection, collection)
        except CanonicalModelError:
            raise
        except Exception as exc:
            raise CanonicalModelError(
                f"collection {name!r} diverges from the PPBase canonical model: "
                f"{exc}"
            ) from exc


def _canonical_json(value: Any) -> str:
    """Return a deterministic JSON string for a stored JSONB/text value."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


async def fingerprint_collection_objects(connection: Any) -> str:
    """Return a stable digest of the metadata/views/indexes backup depends on.

    The canonical view/index reconciliation in
    :func:`validate_canonical_collection_objects` runs in a transaction that
    precedes the REPEATABLE READ, READ ONLY export snapshot.  The backup write
    barrier only blocks cooperating PPBase writers, so an external PostgreSQL
    client could mutate ``_collections``, a view, or an index in the gap between
    the two transactions.  Capturing this fingerprint during validation and
    re-computing it inside the export snapshot lets the caller fail closed if any
    of those objects drifted before the first COPY.
    """
    from sqlalchemy import text

    collections = (
        await connection.execute(
            text(
                'SELECT name, type, schema, indexes, options '
                'FROM public."_collections" ORDER BY name'
            )
        )
    ).mappings().all()
    views = (
        await connection.execute(
            text(
                "SELECT c.relname AS name, "
                "pg_catalog.pg_get_viewdef(c.oid, true) AS definition "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind = 'v' "
                "ORDER BY c.relname"
            )
        )
    ).mappings().all()
    indexes = (
        await connection.execute(
            text(
                "SELECT indexname AS name, indexdef AS definition "
                "FROM pg_catalog.pg_indexes "
                "WHERE schemaname = 'public' "
                "ORDER BY indexname"
            )
        )
    ).mappings().all()

    payload = {
        "collections": [
            {
                "name": row["name"],
                "type": row["type"],
                "schema": _canonical_json(row["schema"]),
                "indexes": _canonical_json(row["indexes"]),
                "options": _canonical_json(row["options"]),
            }
            for row in collections
        ],
        "views": [
            {"name": row["name"], "definition": row["definition"]}
            for row in views
        ],
        "indexes": [
            {"name": row["name"], "definition": row["definition"]}
            for row in indexes
        ],
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "CanonicalModelError",
    "build_canonical_tables",
    "build_collection_table_spec",
    "build_system_table_specs",
    "fingerprint_collection_objects",
    "validate_canonical_collection_objects",
]
