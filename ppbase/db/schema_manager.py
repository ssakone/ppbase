"""Dynamic DDL engine for PostgreSQL collection tables.

Translates collection schema definitions (stored in ``_collections``)
into ``CREATE TABLE``, ``ALTER TABLE``, and ``DROP TABLE`` statements.

Uses SQLAlchemy Core with raw DDL via ``text()`` for PostgreSQL-specific
operations that are not expressible through the standard DDL API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import re
from typing import Any
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from ppbase.models.field_types import FieldDefinition, FieldType


SchemaExecutor = AsyncEngine | AsyncConnection | AsyncSession


@asynccontextmanager
async def _ddl_connection(executor: SchemaExecutor) -> AsyncIterator[AsyncConnection]:
    """Yield the connection on which a DDL operation must run.

    Engines retain the historical convenience behavior: this helper opens and
    commits a transaction for the operation. Connections and sessions are
    caller-owned, so their transaction is reused and is never committed or
    rolled back here.
    """
    if isinstance(executor, AsyncEngine):
        async with executor.begin() as connection:
            yield connection
        return

    if isinstance(executor, AsyncConnection):
        yield executor
        return

    if isinstance(executor, AsyncSession):
        # The returned connection participates in the session transaction.
        # The session owns it, so don't context-manage or close it here.
        yield await executor.connection()
        return

    raise TypeError(
        "Schema executor must be an AsyncEngine, AsyncConnection, or AsyncSession."
    )


@asynccontextmanager
async def _read_connection(executor: SchemaExecutor) -> AsyncIterator[AsyncConnection]:
    """Yield a connection for a read without taking transaction ownership."""
    if isinstance(executor, AsyncEngine):
        async with executor.connect() as connection:
            yield connection
        return

    async with _ddl_connection(executor) as connection:
        yield connection


# ---------------------------------------------------------------------------
# Field type -> PostgreSQL column DDL mapping
# ---------------------------------------------------------------------------

def _col_ddl(field: FieldDefinition) -> str:
    """Return the PostgreSQL column DDL fragment for a single field."""
    opts = field.options
    ft = field.type

    if ft in (FieldType.TEXT, FieldType.EDITOR):
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.NUMBER:
        only_int = opts.get("onlyInt", False)
        if only_int:
            return f'"{field.name}" INTEGER NOT NULL DEFAULT 0'
        return f'"{field.name}" DOUBLE PRECISION NOT NULL DEFAULT 0'

    if ft == FieldType.BOOL:
        return f'"{field.name}" BOOLEAN NOT NULL DEFAULT FALSE'

    if ft == FieldType.EMAIL:
        return f'"{field.name}" VARCHAR(255) NOT NULL DEFAULT \'\''

    if ft == FieldType.URL:
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.DATE:
        return f'"{field.name}" TIMESTAMPTZ NULL'

    if ft == FieldType.AUTODATE:
        return f'"{field.name}" TIMESTAMPTZ NOT NULL DEFAULT NOW()'

    if ft == FieldType.SELECT:
        max_select = opts.get("maxSelect", 1) or 1
        if max_select > 1:
            return f'"{field.name}" TEXT[] NOT NULL DEFAULT \'{{}}\''
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.FILE:
        max_select = opts.get("maxSelect", 1) or 1
        if max_select > 1:
            return f'"{field.name}" TEXT[] NOT NULL DEFAULT \'{{}}\''
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.RELATION:
        max_select = opts.get("maxSelect", 1) or 1
        if max_select > 1:
            return f'"{field.name}" VARCHAR(15)[] NOT NULL DEFAULT \'{{}}\''
        return f'"{field.name}" VARCHAR(15) NOT NULL DEFAULT \'\''

    if ft == FieldType.JSON:
        return f'"{field.name}" JSONB NOT NULL DEFAULT \'null\'::jsonb'

    if ft == FieldType.PASSWORD:
        return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''

    if ft == FieldType.GEO_POINT:
        return (
            f'"{field.name}" JSONB NOT NULL '
            f"DEFAULT '{{\"lon\":0,\"lat\":0}}'::jsonb"
        )

    # Fallback for unknown types
    return f'"{field.name}" TEXT NOT NULL DEFAULT \'\''


def _is_array_field(field: FieldDefinition) -> bool:
    """Check if a field maps to a PostgreSQL array column."""
    opts = field.options
    max_select = opts.get("maxSelect", 1) or 1
    return field.type in (
        FieldType.SELECT,
        FieldType.FILE,
        FieldType.RELATION,
    ) and max_select > 1


def _is_jsonb_field(field: FieldDefinition) -> bool:
    """Check if a field maps to a JSONB column."""
    return field.type in (FieldType.JSON, FieldType.GEO_POINT)


def _safe_name(name: str) -> str:
    """Ensure a name is safe for use as a SQL identifier."""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    _validate_postgres_identifier_length(name, label="SQL identifier")
    return name


_POSTGRES_IDENTIFIER_MAX_BYTES = 63
_INDEX_NAME_HASH_LENGTH = 16


def _validate_postgres_identifier_length(name: str, *, label: str) -> None:
    """Reject physical identifiers PostgreSQL would silently truncate."""
    length = len(name.encode("utf-8"))
    if length > _POSTGRES_IDENTIFIER_MAX_BYTES:
        raise ValueError(
            f"{label} {name!r} is {length} bytes; PostgreSQL identifiers "
            f"must not exceed {_POSTGRES_IDENTIFIER_MAX_BYTES} bytes."
        )


def _truncate_postgres_identifier(name: str) -> str:
    """Return the identifier PostgreSQL stores after its 63-byte truncation."""
    encoded = name.encode("utf-8")
    if len(encoded) <= _POSTGRES_IDENTIFIER_MAX_BYTES:
        return name
    return encoded[:_POSTGRES_IDENTIFIER_MAX_BYTES].decode(
        "utf-8",
        errors="ignore",
    )


def _bounded_index_name(name: str) -> str:
    """Keep a deterministic index name within PostgreSQL's byte limit."""
    encoded = name.encode("utf-8")
    if len(encoded) <= _POSTGRES_IDENTIFIER_MAX_BYTES:
        return name

    digest = hashlib.sha256(encoded).hexdigest()[:_INDEX_NAME_HASH_LENGTH]
    suffix = f"_{digest}"
    prefix_bytes = encoded[
        :_POSTGRES_IDENTIFIER_MAX_BYTES - len(suffix.encode("ascii"))
    ]
    prefix = prefix_bytes.decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}"


def _managed_index_name(table_name: str, suffix: str) -> str:
    """Return a deterministic, PostgreSQL-safe managed index name."""
    return _bounded_index_name(f"idx_{table_name}_{suffix}")


# ---------------------------------------------------------------------------
# Auth collection system columns
# ---------------------------------------------------------------------------

_AUTH_SYSTEM_COLUMN_DEFINITIONS = (
    '"email" VARCHAR(255) NOT NULL DEFAULT \'\'',
    '"email_visibility" BOOLEAN NOT NULL DEFAULT FALSE',
    '"verified" BOOLEAN NOT NULL DEFAULT FALSE',
    '"password_hash" TEXT NOT NULL DEFAULT \'\'',
    '"token_key" VARCHAR(50) NOT NULL DEFAULT \'\'',
)

_AUTH_SYSTEM_COLUMNS = ",\n    ".join(_AUTH_SYSTEM_COLUMN_DEFINITIONS)


def _collection_type(collection: Any) -> str:
    """Return a normalized collection type."""
    return getattr(collection, "type", None) or "base"


def _view_query(collection: Any) -> str:
    """Return the configured query for a view collection."""
    options = getattr(collection, "options", None) or {}
    if not isinstance(options, dict):
        return ""
    query = options.get("query", "")
    return query if isinstance(query, str) else ""


def _parse_fields(raw: Any) -> list[FieldDefinition]:
    """Normalize a raw collection schema to field definitions."""
    fields: list[FieldDefinition] = []
    schema = raw if isinstance(raw, list) else []
    for definition in schema:
        if isinstance(definition, FieldDefinition):
            field = definition
        elif isinstance(definition, dict):
            field = FieldDefinition(**definition)
        else:
            continue
        _validate_postgres_identifier_length(field.name, label="Field name")
        fields.append(field)
    return fields


def _field_indexes(
    table_name: str,
    fields: list[FieldDefinition],
) -> dict[str, str]:
    """Build PPBase-managed field indexes keyed by index name."""
    indexes: dict[str, str] = {}
    for field in fields:
        index_name = _managed_index_name(table_name, field.name)
        if _is_array_field(field) or _is_jsonb_field(field):
            indexes[index_name] = (
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f'ON "public"."{table_name}" USING GIN ("{field.name}")'
            )
        elif field.type == FieldType.RELATION:
            indexes[index_name] = (
                f'CREATE INDEX IF NOT EXISTS "{index_name}" '
                f'ON "public"."{table_name}" ("{field.name}") '
                f'WHERE "{field.name}" != \'\''
            )
    return indexes


def _custom_indexes(collection: Any) -> list[str]:
    """Return non-empty custom index statements from a collection."""
    raw_indexes = getattr(collection, "indexes", None) or []
    return [
        statement
        for statement in raw_indexes
        if isinstance(statement, str) and statement.strip()
    ]


def _custom_indexes_by_stored_name(
    statements: list[str],
    *,
    require_names: bool,
) -> dict[str, str]:
    """Index custom statements by the identifier PostgreSQL will store."""
    indexes: dict[str, str] = {}
    for statement in statements:
        index_name = _custom_index_name(statement)
        if index_name is None:
            if require_names:
                raise ValueError(
                    "Cannot update custom index because its name could not be "
                    f"parsed from SQL: {statement!r}"
                )
            continue
        stored_name = _truncate_postgres_identifier(index_name)
        if stored_name in indexes:
            raise ValueError(
                "Duplicate custom index name after PostgreSQL normalization: "
                f"{stored_name!r}"
            )
        indexes[stored_name] = statement
    return indexes


def _validate_managed_custom_index_names(
    managed_indexes: dict[str, str],
    custom_indexes: dict[str, str],
) -> None:
    """Reject custom names that would mask a PPBase-managed index."""
    for managed_name in managed_indexes:
        stored_name = _truncate_postgres_identifier(managed_name)
        if stored_name in custom_indexes:
            raise ValueError(
                f"Custom index name {stored_name!r} conflicts with a "
                "PPBase-managed index."
            )


def _managed_indexes(collection: Any) -> dict[str, str]:
    """Build indexes generated and owned by PPBase, keyed by name."""
    table_name = _safe_name(collection.name)
    collection_type = _collection_type(collection)
    created_name = _managed_index_name(table_name, "created")
    indexes = {
        created_name: (
            f'CREATE INDEX IF NOT EXISTS "{created_name}" '
            f'ON "public"."{table_name}" ("created")'
        )
    }

    if collection_type == "auth":
        email_name = _managed_index_name(table_name, "email")
        token_name = _managed_index_name(table_name, "token_key")
        indexes[email_name] = (
            f'CREATE UNIQUE INDEX IF NOT EXISTS "{email_name}" '
            f'ON "public"."{table_name}" ("email") WHERE "email" != \'\''
        )
        indexes[token_name] = (
            f'CREATE INDEX IF NOT EXISTS "{token_name}" '
            f'ON "public"."{table_name}" ("token_key")'
        )

    indexes.update(
        _field_indexes(table_name, _parse_fields(collection.schema))
    )
    return indexes


_INDEX_NAME_RE = re.compile(
    r'^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+'
    r'(?:CONCURRENTLY\s+)?'
    r'(?:IF\s+NOT\s+EXISTS\s+)?'
    r'(?:(?:"(?P<quoted>(?:[^"]|"")+)"|`(?P<backtick>[^`]+)`|'
    r'(?P<plain>[a-zA-Z_][a-zA-Z0-9_$]*)))'
    r'(?=\s+ON\b)',
    re.IGNORECASE,
)

_INDEX_TARGET_RE = re.compile(
    r'''\s+ON\s+(?:ONLY\s+)?
    (?P<target>
        (?:(?:"(?P<quoted_schema>(?:[^"]|"")+)"|(?P<plain_schema>[a-zA-Z_][a-zA-Z0-9_$]*))\s*\.\s*)?
        (?:"(?P<quoted_table>(?:[^"]|"")+)"|(?P<plain_table>[a-zA-Z_][a-zA-Z0-9_$]*))
    )
    (?=\s*(?:USING\b|\())''',
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class _IndexDefinition:
    """PostgreSQL properties that determine an index's behavior."""

    owner: str
    unique: bool
    valid: bool
    exclusion: bool
    nulls_not_distinct: bool
    method: str
    keys: tuple[str, ...]
    included: tuple[str, ...]
    predicate: str | None

    def signature(self) -> tuple[Any, ...]:
        """Return the semantic properties compared for an existing index."""
        return (
            self.unique,
            self.valid,
            self.exclusion,
            self.nulls_not_distinct,
            self.method,
            self.keys,
            self.included,
            self.predicate,
        )


def _custom_index_name(statement: str) -> str | None:
    """Extract the index identifier from a conventional CREATE INDEX."""
    match = _INDEX_NAME_RE.match(statement)
    if not match:
        return None
    quoted = match.group("quoted")
    if quoted is not None:
        return quoted.replace('""', '"')
    backtick = match.group("backtick")
    if backtick is not None:
        return backtick
    plain = match.group("plain")
    return plain.lower() if plain is not None else None


def _parsed_identifier(
    match: re.Match[str],
    quoted_group: str,
    plain_group: str,
) -> str | None:
    """Return an identifier with PostgreSQL quoting/case-folding semantics."""
    quoted = match.group(quoted_group)
    if quoted is not None:
        return quoted.replace('""', '"')
    plain = match.group(plain_group)
    return plain.lower() if plain is not None else None


def _index_statement_parts(
    statement: str,
) -> tuple[re.Match[str], re.Match[str]]:
    """Parse the name and target of a conventional PostgreSQL index DDL."""
    name_match = _INDEX_NAME_RE.match(statement)
    if name_match is None:
        raise ValueError(
            "Cannot safely validate custom index SQL because its index name "
            f"could not be parsed: {statement!r}"
        )
    if re.search(r"\bCONCURRENTLY\b", name_match.group(0), re.IGNORECASE):
        raise ValueError(
            "CREATE INDEX CONCURRENTLY is incompatible with PPBase's "
            "transactional schema synchronization."
        )

    target_match = _INDEX_TARGET_RE.match(statement, name_match.end())
    if target_match is None:
        raise ValueError(
            "Cannot safely validate custom index SQL because its target table "
            f"could not be parsed: {statement!r}"
        )
    return name_match, target_match


def _normalize_index_statement_target(
    statement: str,
    table_name: str,
) -> str:
    """Validate and schema-qualify a collection index's target table."""
    _, target_match = _index_statement_parts(statement)
    declared_schema = _parsed_identifier(
        target_match,
        "quoted_schema",
        "plain_schema",
    )
    declared_table = _parsed_identifier(
        target_match,
        "quoted_table",
        "plain_table",
    )
    if declared_schema not in {None, "public"}:
        raise ValueError(
            f"Collection index targets schema {declared_schema!r}; expected "
            "the public schema."
        )
    if declared_table != table_name:
        raise ValueError(
            f"Collection index targets table {declared_table!r}; expected "
            f"{table_name!r}."
        )

    start, end = target_match.span("target")
    qualified_target = f'"public".{_quote_identifier(table_name)}'
    return f"{statement[:start]}{qualified_target}{statement[end:]}"


def _rewrite_index_name(statement: str, index_name: str) -> str:
    """Replace a parsed index name with a safely quoted identifier."""
    name_match, _ = _index_statement_parts(statement)
    start, end = name_match.span("quoted")
    if start >= 0:
        escaped_name = index_name.replace('"', '""')
        return f"{statement[:start]}{escaped_name}{statement[end:]}"
    if name_match.group("backtick") is not None:
        raise ValueError(
            "Backtick-quoted index names are not valid PostgreSQL syntax."
        )
    start, end = name_match.span("plain")
    if start >= 0:
        return f"{statement[:start]}{_quote_identifier(index_name)}{statement[end:]}"
    raise ValueError(f"Cannot parse index name from SQL: {statement!r}")


def _rewrite_index_target(statement: str, qualified_target: str) -> str:
    """Replace a parsed index target with a trusted qualified identifier."""
    _, target_match = _index_statement_parts(statement)
    start, end = target_match.span("target")
    return f"{statement[:start]}{qualified_target}{statement[end:]}"


def _quote_identifier(name: str) -> str:
    """Quote a PostgreSQL identifier that was parsed from trusted DDL."""
    return '"' + name.replace('"', '""') + '"'


async def _read_index_definition(
    connection: AsyncConnection,
    index_name: str,
    *,
    temporary: bool = False,
) -> _IndexDefinition | None:
    """Load the behaviorally relevant definition of a PostgreSQL index."""
    namespace_filter = (
        "index_namespace.oid = pg_my_temp_schema()"
        if temporary
        else "index_namespace.nspname = 'public'"
    )
    result = await connection.execute(
        text(
            "SELECT index_relation.relkind::text, table_relation.relname, "
            "table_namespace.nspname, index_metadata.indisunique, "
            "index_metadata.indisvalid, index_metadata.indisexclusion, "
            "index_metadata.indnullsnotdistinct, access_method.amname, "
            "ARRAY(SELECT pg_get_indexdef(index_relation.oid, key_no, true) "
            "FROM generate_series(1, index_metadata.indnkeyatts) "
            "AS key_position(key_no) ORDER BY key_no), "
            "ARRAY(SELECT pg_get_indexdef(index_relation.oid, include_no, true) "
            "FROM generate_series(index_metadata.indnkeyatts + 1, "
            "index_metadata.indnatts) AS include_position(include_no) "
            "ORDER BY include_no), "
            "pg_get_expr(index_metadata.indpred, index_metadata.indrelid, true) "
            "FROM pg_catalog.pg_class AS index_relation "
            "JOIN pg_catalog.pg_namespace AS index_namespace "
            "ON index_namespace.oid = index_relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_index AS index_metadata "
            "ON index_metadata.indexrelid = index_relation.oid "
            "LEFT JOIN pg_catalog.pg_class AS table_relation "
            "ON table_relation.oid = index_metadata.indrelid "
            "LEFT JOIN pg_catalog.pg_namespace AS table_namespace "
            "ON table_namespace.oid = table_relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_am AS access_method "
            "ON access_method.oid = index_relation.relam "
            f"WHERE {namespace_filter} "
            "AND index_relation.relname = :index_name"
        ),
        {"index_name": _truncate_postgres_identifier(index_name)},
    )
    row = result.one_or_none()
    if row is None:
        return None

    (
        relation_kind,
        owner,
        table_schema,
        unique,
        valid,
        exclusion,
        nulls_not_distinct,
        method,
        keys,
        included,
        predicate,
    ) = row
    if relation_kind not in {"i", "I"} or owner is None:
        namespace = "temporary" if temporary else "public"
        raise ValueError(
            f"Cannot use index name {index_name!r}: {namespace}."
            f"{_truncate_postgres_identifier(index_name)!r} already exists "
            f"with relation kind {relation_kind!r} and is not an index."
        )
    if not temporary and table_schema != "public":
        raise ValueError(
            f"Cannot use index name {index_name!r}: its owning table is not "
            "in the public schema."
        )
    return _IndexDefinition(
        owner=str(owner),
        unique=bool(unique),
        valid=bool(valid),
        exclusion=bool(exclusion),
        nulls_not_distinct=bool(nulls_not_distinct),
        method=str(method),
        keys=tuple(str(key) for key in (keys or ())),
        included=tuple(str(column) for column in (included or ())),
        predicate=str(predicate) if predicate is not None else None,
    )


async def _expected_index_definition(
    connection: AsyncConnection,
    statement: str,
    table_name: str,
) -> _IndexDefinition:
    """Ask PostgreSQL to canonicalize expected index DDL on an empty clone."""
    probe_suffix = uuid.uuid4().hex
    probe_table = _bounded_index_name(f"_ppbase_index_table_{probe_suffix}")
    probe_index = _bounded_index_name(f"_ppbase_index_probe_{probe_suffix}")
    savepoint = await connection.begin_nested()
    try:
        await connection.execute(text(
            f"CREATE TEMP TABLE {_quote_identifier(probe_table)} "
            f"(LIKE \"public\".{_quote_identifier(table_name)}) ON COMMIT DROP"
        ))
        probe_statement = _rewrite_index_target(
            statement,
            f'"pg_temp".{_quote_identifier(probe_table)}',
        )
        probe_statement = _rewrite_index_name(probe_statement, probe_index)
        await connection.execute(text(probe_statement))
        definition = await _read_index_definition(
            connection,
            probe_index,
            temporary=True,
        )
        if definition is None:  # pragma: no cover - PostgreSQL invariant
            raise RuntimeError("PostgreSQL did not create the validation index.")
        return definition
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


async def _index_owner(
    connection: AsyncConnection,
    index_name: str,
) -> str | None:
    """Return the public table owning an index and reject other relation kinds."""
    result = await connection.execute(
        text(
            "SELECT index_relation.relkind::text, table_relation.relname, "
            "table_namespace.nspname "
            "FROM pg_catalog.pg_class AS index_relation "
            "JOIN pg_catalog.pg_namespace AS index_namespace "
            "ON index_namespace.oid = index_relation.relnamespace "
            "LEFT JOIN pg_catalog.pg_index AS index_metadata "
            "ON index_metadata.indexrelid = index_relation.oid "
            "LEFT JOIN pg_catalog.pg_class AS table_relation "
            "ON table_relation.oid = index_metadata.indrelid "
            "LEFT JOIN pg_catalog.pg_namespace AS table_namespace "
            "ON table_namespace.oid = table_relation.relnamespace "
            "WHERE index_namespace.nspname = 'public' "
            "AND index_relation.relname = :index_name"
        ),
        {"index_name": _truncate_postgres_identifier(index_name)},
    )
    row = result.one_or_none()
    if row is None:
        return None

    relation_kind, owner, table_schema = row
    if relation_kind not in {"i", "I"} or owner is None:
        raise ValueError(
            f"Cannot use index name {index_name!r}: public."
            f"{_truncate_postgres_identifier(index_name)!r} already exists "
            f"with relation kind {relation_kind!r} and is not an index."
        )
    if table_schema != "public":
        raise ValueError(
            f"Cannot use index name {index_name!r}: its owning table is not "
            "in the public schema."
        )
    return owner


def _expected_table_names(*table_names: str) -> set[str]:
    """Normalize expected table identifiers as PostgreSQL stores them."""
    return {_truncate_postgres_identifier(name) for name in table_names}


def _raise_wrong_index_owner(
    *,
    operation: str,
    index_name: str,
    owner: str,
    expected_tables: set[str],
) -> None:
    expected = ", ".join(sorted(repr(name) for name in expected_tables))
    raise ValueError(
        f"Cannot {operation} index {index_name!r}: public."
        f"{_truncate_postgres_identifier(index_name)!r} belongs to table "
        f"{owner!r}; expected {expected}."
    )


async def _drop_index_if_owned(
    connection: AsyncConnection,
    index_name: str,
    *,
    expected_tables: set[str],
) -> None:
    """Drop a public index only after verifying its owning table."""
    owner = await _index_owner(connection, index_name)
    if owner is None:
        return
    if owner not in expected_tables:
        _raise_wrong_index_owner(
            operation="drop",
            index_name=index_name,
            owner=owner,
            expected_tables=expected_tables,
        )

    stored_name = _truncate_postgres_identifier(index_name)
    await connection.execute(
        text(
            f'DROP INDEX IF EXISTS "public".{_quote_identifier(stored_name)}'
        )
    )


async def _create_index_if_owned(
    connection: AsyncConnection,
    *,
    index_name: str,
    statement: str,
    table_name: str,
) -> None:
    """Create an index only when its name is free or exactly compatible."""
    table_name = _safe_name(table_name)
    statement = _normalize_index_statement_target(statement, table_name)
    expected_tables = _expected_table_names(table_name)
    actual = await _read_index_definition(connection, index_name)
    if actual is not None and actual.owner not in expected_tables:
        _raise_wrong_index_owner(
            operation="create",
            index_name=index_name,
            owner=actual.owner,
            expected_tables=expected_tables,
        )

    if actual is not None:
        expected = await _expected_index_definition(
            connection,
            statement,
            table_name,
        )
        if actual.signature() != expected.signature():
            raise ValueError(
                f"Cannot create index {index_name!r}: an index with the same "
                f"name exists on table {actual.owner!r} with a different "
                "definition (table keys/expression, uniqueness, predicate, "
                "included columns, or access method). Drop or rename the "
                "conflicting index before retrying."
            )
        return

    await connection.execute(text(statement))


async def _dependent_view_names(
    connection: AsyncConnection,
    relation_name: str,
) -> list[str]:
    """Return public views that directly depend on a public relation."""
    result = await connection.execute(
        text(
            "SELECT DISTINCT dependent.relname "
            "FROM pg_catalog.pg_rewrite AS rewrite "
            "JOIN pg_catalog.pg_class AS dependent "
            "ON dependent.oid = rewrite.ev_class "
            "JOIN pg_catalog.pg_namespace AS dependent_namespace "
            "ON dependent_namespace.oid = dependent.relnamespace "
            "JOIN pg_catalog.pg_depend AS dependency "
            "ON dependency.classid = 'pg_rewrite'::regclass "
            "AND dependency.objid = rewrite.oid "
            "AND dependency.refclassid = 'pg_class'::regclass "
            "JOIN pg_catalog.pg_class AS referenced "
            "ON referenced.oid = dependency.refobjid "
            "JOIN pg_catalog.pg_namespace AS referenced_namespace "
            "ON referenced_namespace.oid = referenced.relnamespace "
            "WHERE dependent_namespace.nspname = 'public' "
            "AND referenced_namespace.nspname = 'public' "
            "AND dependent.relkind IN ('v', 'm') "
            "AND dependent.oid <> referenced.oid "
            "AND referenced.relname = :relation_name "
            "ORDER BY dependent.relname"
        ),
        {"relation_name": relation_name},
    )
    return [str(name) for name in result.scalars().all()]


def _postgres_sqlstate(exc: BaseException) -> str | None:
    """Extract a PostgreSQL SQLSTATE from SQLAlchemy/asyncpg wrappers."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        state = getattr(current, "sqlstate", None) or getattr(
            current,
            "pgcode",
            None,
        )
        if state:
            return str(state)
        current = getattr(current, "orig", None) or getattr(
            current,
            "__cause__",
            None,
        )
    return None


async def _drop_collection_object(
    connection: AsyncConnection,
    collection_name: str,
) -> None:
    """Drop a table/view without silently cascading into dependent views."""
    table_name = _safe_name(collection_name)
    dependent_views = await _dependent_view_names(connection, table_name)
    if dependent_views:
        raise ValueError(
            f"Cannot replace or delete collection {collection_name!r}: "
            "dependent views must be updated or deleted first: "
            f"{', '.join(dependent_views)}."
        )

    row = (await connection.execute(text(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = :name"
    ), {"name": table_name})).first()
    if row and row[0] == "VIEW":
        await connection.execute(
            text(f'DROP VIEW IF EXISTS "{table_name}"')
        )
    else:
        await connection.execute(
            text(f'DROP TABLE IF EXISTS "{table_name}"')
        )


async def _replace_collection_object(
    connection: AsyncConnection,
    old_collection: Any,
    new_collection: Any,
) -> None:
    """Atomically replace an object when entering/leaving the view type."""
    old_name = _safe_name(old_collection.name)
    old_type = _collection_type(old_collection)
    new_type = _collection_type(new_collection)
    query = _view_query(new_collection) if new_type == "view" else ""
    if new_type == "view":
        if not query:
            raise ValueError("View collection requires a SQL query in options.query.")
        # Validate before dropping the old object. The savepoint also keeps the
        # root transaction usable when PostgreSQL rejects the query.
        await validate_view_query(connection, query)

    # PostgreSQL preserves dependent views only when CREATE OR REPLACE can keep
    # every existing output column's name, order, and data type compatible.
    if old_type == "view" and new_type == "view" and old_name == new_collection.name:
        replace_savepoint = await connection.begin_nested()
        try:
            await connection.execute(
                text(f'CREATE OR REPLACE VIEW "{old_name}" AS {query}')
            )
        except Exception as replace_error:
            await replace_savepoint.rollback()
            if _postgres_sqlstate(replace_error) != "42P16":
                # Only PostgreSQL's invalid_table_definition means CREATE OR
                # REPLACE was rejected for an incompatible output schema. Do
                # not reclassify deadlocks, timeouts, permissions, or outages.
                raise
            dependent_views = await _dependent_view_names(connection, old_name)
            if dependent_views:
                raise ValueError(
                    f"Cannot replace view {old_collection.name!r} because "
                    "PostgreSQL cannot preserve its existing output schema and "
                    "the following views depend on it: "
                    f"{', '.join(dependent_views)}. Update or delete the "
                    "dependent views first."
                ) from replace_error
            # With no dependents, retain PocketBase's ability to change the
            # complete view output by replacing the object transactionally.
        else:
            await replace_savepoint.commit()
            return

    await _drop_collection_object(connection, old_collection.name)

    if new_type == "view":
        table_name = _safe_name(new_collection.name)
        await connection.execute(
            text(f'CREATE VIEW "{table_name}" AS {query}')
        )
    else:
        await create_collection_table(connection, new_collection)


async def _sync_collection_indexes(
    connection: AsyncConnection,
    old_collection: Any,
    new_collection: Any,
) -> None:
    """Synchronize managed and custom indexes in the current transaction."""
    old_managed = _managed_indexes(old_collection)
    new_managed = _managed_indexes(new_collection)
    drop_names = {
        name
        for name, statement in old_managed.items()
        if new_managed.get(name) != statement
    }

    old_custom = _custom_indexes(old_collection)
    new_custom = _custom_indexes(new_collection)
    custom_changed = old_custom != new_custom
    new_custom_by_name = _custom_indexes_by_stored_name(
        new_custom,
        require_names=True,
    )
    _validate_managed_custom_index_names(new_managed, new_custom_by_name)
    if custom_changed:
        old_custom_by_name = _custom_indexes_by_stored_name(
            old_custom,
            require_names=True,
        )
        drop_names.update({
            name
            for name, statement in old_custom_by_name.items()
            if new_custom_by_name.get(name) != statement
        })

    expected_drop_tables = _expected_table_names(
        _safe_name(old_collection.name),
        _safe_name(new_collection.name),
    )
    for index_name in sorted(drop_names):
        await _drop_index_if_owned(
            connection,
            index_name,
            expected_tables=expected_drop_tables,
        )

    # Recheck every desired managed index, not only changed definitions. This
    # safely upgrades long-named collections created before names were bounded.
    for index_name, statement in new_managed.items():
        await _create_index_if_owned(
            connection,
            index_name=index_name,
            statement=statement,
            table_name=_safe_name(new_collection.name),
        )

    # Recheck every desired custom index too. Unchanged metadata must not let
    # database drift hide behind CREATE INDEX IF NOT EXISTS.
    for stored_name, statement in new_custom_by_name.items():
        await _create_index_if_owned(
            connection,
            index_name=stored_name,
            statement=statement,
            table_name=_safe_name(new_collection.name),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_collection_table(
    engine: SchemaExecutor,
    collection: Any,
) -> None:
    """Create a PostgreSQL table (or view) for a collection record.

    Args:
        engine: An async SQLAlchemy engine, connection, or session. When a
            connection/session is provided, its transaction remains owned by
            the caller.
        collection: A ``CollectionRecord`` (ORM) or dict-like object with
            ``name``, ``type``, ``schema``, and ``options`` attributes.
    """
    table_name = _safe_name(collection.name)
    col_type = _collection_type(collection)

    # View collections: create a PostgreSQL VIEW from the query
    if col_type == "view":
        query = _view_query(collection)
        if not query:
            raise ValueError("View collection requires a SQL query in options.query.")
        view_sql = f'CREATE OR REPLACE VIEW "{table_name}" AS {query}'
        async with _ddl_connection(engine) as conn:
            # Keep validation and creation in the same root transaction.
            await validate_view_query(conn, query)
            await conn.execute(text(view_sql))
        return

    # Parse schema field definitions
    fields = _parse_fields(collection.schema)

    # Build column definitions
    parts: list[str] = [
        '"id" VARCHAR(15) PRIMARY KEY',
        '"created" TIMESTAMPTZ NOT NULL DEFAULT NOW()',
        '"updated" TIMESTAMPTZ NOT NULL DEFAULT NOW()',
    ]

    # Auth collection system columns
    if col_type == "auth":
        parts.append(_AUTH_SYSTEM_COLUMNS)

    # Dynamic user-defined columns
    for f in fields:
        parts.append(_col_ddl(f))

    columns_sql = ",\n    ".join(parts)
    create_sql = f'CREATE TABLE IF NOT EXISTS "{table_name}" (\n    {columns_sql}\n)'

    managed_indexes = _managed_indexes(collection)
    custom_indexes = _custom_indexes(collection)
    parsed_custom_indexes = _custom_indexes_by_stored_name(
        custom_indexes,
        require_names=True,
    )
    _validate_managed_custom_index_names(
        managed_indexes,
        parsed_custom_indexes,
    )

    async with _ddl_connection(engine) as conn:
        await conn.execute(text(create_sql))
        for index_name, statement in managed_indexes.items():
            await _create_index_if_owned(
                conn,
                index_name=_truncate_postgres_identifier(index_name),
                statement=statement,
                table_name=table_name,
            )
        for statement in custom_indexes:
            index_name = _custom_index_name(statement)
            if index_name is None:  # pragma: no cover - validated above
                raise ValueError(f"Cannot parse custom index SQL: {statement!r}")
            await _create_index_if_owned(
                conn,
                index_name=index_name,
                statement=statement,
                table_name=table_name,
            )


async def update_collection_table(
    engine: SchemaExecutor,
    old_collection: Any,
    new_collection: Any,
) -> None:
    """Update a collection's PostgreSQL table or view atomically.

    Compares old and new collection definitions and issues:
    - ADD COLUMN for new fields
    - DROP COLUMN for removed fields
    - RENAME TABLE if the collection name changed
    - Type changes are handled via DROP + ADD (simple approach)
    - View/query changes are handled via DROP + CREATE
    - Managed and custom indexes are synchronized

    Args:
        engine: An async SQLAlchemy engine, connection, or session. When a
            connection/session is provided, its transaction remains owned by
            the caller.
        old_collection: The previous collection record.
        new_collection: The updated collection record.
    """
    old_name = _safe_name(old_collection.name)
    new_name = _safe_name(new_collection.name)
    old_type = _collection_type(old_collection)
    new_type = _collection_type(new_collection)

    # PostgreSQL cannot ALTER a table into a view (or the reverse). Replacing
    # the object inside one transaction is both simpler and rollback-safe.
    if old_type == "view" or new_type == "view":
        view_changed = (
            old_name != new_name
            or old_type != new_type
            or _view_query(old_collection) != _view_query(new_collection)
        )
        if view_changed:
            async with _ddl_connection(engine) as conn:
                await _replace_collection_object(
                    conn,
                    old_collection,
                    new_collection,
                )
        return

    # Parse old and new field definitions
    def _fields_by_identity(raw: Any) -> dict[str, FieldDefinition]:
        result: dict[str, FieldDefinition] = {}
        for fdef in _parse_fields(raw):
            # Key by field id if available, else by name
            key = fdef.id if fdef.id else fdef.name
            result[key] = fdef
        return result

    old_fields = _fields_by_identity(old_collection.schema)
    new_fields = _fields_by_identity(new_collection.schema)

    old_keys = set(old_fields.keys())
    new_keys = set(new_fields.keys())

    removed_keys = old_keys - new_keys
    added_keys = new_keys - old_keys
    common_keys = old_keys & new_keys

    stmts: list[str] = []

    # Rename table if name changed
    current_table = old_name
    if old_name != new_name:
        stmts.append(
            f'ALTER TABLE "{old_name}" RENAME TO "{new_name}"'
        )
        current_table = new_name

    # Preserve table data when switching between base and auth collections.
    if old_type != "auth" and new_type == "auth":
        for definition in _AUTH_SYSTEM_COLUMN_DEFINITIONS:
            stmts.append(
                f'ALTER TABLE "{current_table}" ADD COLUMN {definition}'
            )
    elif old_type == "auth" and new_type != "auth":
        for column_name in (
            "email",
            "email_visibility",
            "verified",
            "password_hash",
            "token_key",
        ):
            stmts.append(
                f'ALTER TABLE "{current_table}" '
                f'DROP COLUMN IF EXISTS "{column_name}"'
            )

    # Drop removed columns
    for key in sorted(removed_keys):
        col_name = old_fields[key].name
        stmts.append(
            f'ALTER TABLE "{current_table}" DROP COLUMN IF EXISTS "{col_name}"'
        )

    # Add new columns
    for key in sorted(added_keys):
        fdef = new_fields[key]
        col_ddl = _col_ddl(fdef)
        stmts.append(
            f'ALTER TABLE "{current_table}" ADD COLUMN {col_ddl}'
        )

    # Handle changed fields (rename or type change)
    for key in sorted(common_keys):
        old_f = old_fields[key]
        new_f = new_fields[key]

        # Rename column if name changed
        if old_f.name != new_f.name:
            stmts.append(
                f'ALTER TABLE "{current_table}" '
                f'RENAME COLUMN "{old_f.name}" TO "{new_f.name}"'
            )

        # If the type or relevant options changed, drop and re-add
        if old_f.type != new_f.type or _type_options_changed(old_f, new_f):
            col_ddl = _col_ddl(new_f)
            # Use the new name (after rename)
            stmts.append(
                f'ALTER TABLE "{current_table}" DROP COLUMN IF EXISTS "{new_f.name}"'
            )
            stmts.append(
                f'ALTER TABLE "{current_table}" ADD COLUMN {col_ddl}'
            )

    async with _ddl_connection(engine) as conn:
        for stmt in stmts:
            await conn.execute(text(stmt))

        # ALTER statements and index replacement are one atomic schema change.
        await _sync_collection_indexes(conn, old_collection, new_collection)


def _type_options_changed(old_f: FieldDefinition, new_f: FieldDefinition) -> bool:
    """Check if type-relevant options changed (e.g., maxSelect, onlyInt)."""
    if old_f.type != new_f.type:
        return True

    if old_f.type == FieldType.NUMBER:
        return old_f.options.get("onlyInt") != new_f.options.get("onlyInt")

    if old_f.type in (FieldType.SELECT, FieldType.FILE, FieldType.RELATION):
        old_max = old_f.options.get("maxSelect", 1) or 1
        new_max = new_f.options.get("maxSelect", 1) or 1
        return (old_max > 1) != (new_max > 1)

    return False


async def _recreate_field_indexes(
    engine: SchemaExecutor,
    collection: Any,
) -> None:
    """Drop and recreate field-level indexes for a collection."""
    table_name = _safe_name(collection.name)

    fields = _parse_fields(collection.schema)

    index_stmts: list[tuple[str, str]] = []
    for f in fields:
        idx_name = _managed_index_name(table_name, f.name)

        if _is_array_field(f):
            index_stmts.append(
                (
                    idx_name,
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                    f'ON "public"."{table_name}" USING GIN ("{f.name}")',
                )
            )
        elif _is_jsonb_field(f):
            index_stmts.append(
                (
                    idx_name,
                    f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                    f'ON "public"."{table_name}" USING GIN ("{f.name}")',
                )
            )
        elif f.type == FieldType.RELATION:
            max_select = f.options.get("maxSelect", 1) or 1
            if max_select <= 1:
                index_stmts.append(
                    (
                        idx_name,
                        f'CREATE INDEX IF NOT EXISTS "{idx_name}" '
                        f'ON "public"."{table_name}" ("{f.name}") '
                        f'WHERE "{f.name}" != \'\'',
                    )
                )

    async with _ddl_connection(engine) as conn:
        expected_tables = _expected_table_names(table_name)
        for index_name, statement in index_stmts:
            await _drop_index_if_owned(
                conn,
                index_name,
                expected_tables=expected_tables,
            )
            await _create_index_if_owned(
                conn,
                index_name=index_name,
                statement=statement,
                table_name=table_name,
            )


async def delete_collection_table(
    engine: SchemaExecutor,
    collection_name: str,
    collection_type: str = "base",
) -> None:
    """Drop the PostgreSQL table or view for a collection.

    Args:
        engine: An async SQLAlchemy engine, connection, or session. When a
            connection/session is provided, its transaction remains owned by
            the caller.
        collection_name: Name of the table/view to drop.
        collection_type: Collection type ("base", "auth", or "view").
    """
    # ``collection_type`` remains in the signature for backward compatibility;
    # the database is authoritative so legacy table-backed views are handled.
    del collection_type
    async with _ddl_connection(engine) as conn:
        await _drop_collection_object(conn, collection_name)


async def truncate_collection_table(
    engine: SchemaExecutor,
    collection_name: str,
) -> None:
    """Truncate all rows from the collection table.

    Args:
        engine: An async SQLAlchemy engine, connection, or session. When a
            connection/session is provided, its transaction remains owned by
            the caller.
        collection_name: Name of the table to truncate.
    """
    table_name = _safe_name(collection_name)
    async with _ddl_connection(engine) as conn:
        await conn.execute(text(f'TRUNCATE TABLE "{table_name}"'))


async def validate_view_query(engine: SchemaExecutor, query: str) -> None:
    """Validate a SQL SELECT query by creating a temp view and rolling back.

    The temporary view is isolated in a savepoint on the supplied connection,
    allowing a caller-owned root transaction to continue after invalid SQL.

    Raises ``ValueError`` with a clear message if the query is invalid.
    """
    import secrets

    tmp_name = f"_ppbase_validate_{secrets.token_hex(4)}"
    async with _ddl_connection(engine) as conn:
        trans = await conn.begin_nested()
        try:
            await conn.execute(
                text(f'CREATE TEMP VIEW "{tmp_name}" AS {query}')
            )
            await conn.execute(text(f'DROP VIEW IF EXISTS "{tmp_name}"'))
        except Exception as exc:
            msg = str(exc)
            # asyncpg/SQLAlchemy wraps errors like:
            # (sqlalchemy...ProgrammingError) <class 'asyncpg...'> : actual msg\n[SQL:...
            # Extract just the human-readable PostgreSQL error
            import re as _re
            # Try to get text after the last ">:" pattern
            m = _re.search(r">:\s*(.+?)(?:\n\[SQL:|$)", msg, _re.DOTALL)
            if m:
                msg = m.group(1).strip()
            else:
                # Fallback: strip [SQL:...] and (Background...) noise
                msg = _re.sub(r"\[SQL:.*", "", msg, flags=_re.DOTALL).strip()
                msg = _re.sub(r"\(Background.*", "", msg, flags=_re.DOTALL).strip()
            raise ValueError(f"Invalid view query: {msg}") from None
        finally:
            await trans.rollback()


async def get_database_tables(engine: SchemaExecutor) -> list[dict]:
    """Return all user tables/views with their column names and types.

    Used by the admin UI SQL editor for autocompletion.
    """
    sql = text("""
        SELECT
            t.table_name,
            c.column_name,
            c.data_type
        FROM information_schema.tables t
        JOIN information_schema.columns c
            ON c.table_name = t.table_name AND c.table_schema = t.table_schema
        WHERE t.table_schema = 'public'
            AND t.table_type IN ('BASE TABLE', 'VIEW')
        ORDER BY t.table_name, c.ordinal_position
    """)
    async with _read_connection(engine) as conn:
        result = await conn.execute(sql)
        rows = result.fetchall()

    tables: dict[str, list[dict]] = {}
    for table_name, col_name, data_type in rows:
        if table_name not in tables:
            tables[table_name] = []
        tables[table_name].append({"name": col_name, "type": data_type})

    return [{"name": t, "columns": cols} for t, cols in tables.items()]
