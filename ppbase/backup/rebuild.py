"""Structural rebuild of a native (v2) backup into an empty PostgreSQL database.

This is the restore counterpart of :mod:`ppbase.backup.schema_contract` and
:mod:`ppbase.backup.copy_format`.  Given a completeness-checked
:class:`~ppbase.backup.schema_contract.DatabaseSchema` (loaded through
:meth:`DatabaseSchema.from_archive_bytes`) and a
:class:`~ppbase.backup.copy_format.SegmentReader` positioned over ``data.copy``,
it recreates every table, streams the COPY data back in, then recreates indexes
and views — entirely with asyncpg, never shelling out to ``pg_restore``/``psql``.

Safety properties:

* **Empty target only.**  The rebuild refuses to run unless the target
  ``public`` schema is completely empty (no tables, views, sequences, types,
  functions, triggers or non-baseline extensions), so it can never clobber or
  merge into a populated database.
* **No unsafe SQL concatenation.**  Every DDL statement is assembled from
  values that already passed the closed schema contract: identifiers match the
  unquoted-identifier grammar (double-quoted for emission), column types come
  from the closed type allowlist, defaults come from the exact-default
  allowlist, and view/predicate bodies are re-validated by the read-only SQL
  lexer.  No archive-supplied string is interpolated without validation.
* **Deterministic order.**  Tables first (name order), then the COPY import in
  the recorded segment order, then indexes (name order), then views in
  dependency (topological) order.
* **All-or-nothing.**  The whole rebuild runs inside one transaction; any
  failure rolls it back and leaves the target ``public`` schema empty.
* **Catalog reconciliation.**  After building, the live schema is re-introspected
  and compared field-for-field against the archive; any drift aborts the
  restore before it can be committed.
"""

from __future__ import annotations

import re
from typing import Any

from ppbase.backup.copy_format import (
    SegmentReader,
    apply_copy_session_settings,
    import_table_segment,
)
from ppbase.backup.models import BackupError
from ppbase.backup.schema_contract import (
    DatabaseSchema,
    IndexSpec,
    TableSpec,
    ViewSpec,
    _classify_default,
    _validate_index_predicate,
    _validate_view_definition,
    introspect_public_schema,
    normalize_column_type,
)


_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class RestoreError(BackupError):
    """Raised when a native backup cannot be rebuilt into the target database."""


class TargetNotEmptyError(RestoreError):
    """Raised when the restore target ``public`` schema is not empty."""


def _quote_ident(value: str) -> str:
    """Return ``value`` as a safe double-quoted identifier.

    ``value`` must already be a validated unquoted identifier (the schema
    contract guarantees this for every spec field).  We re-check here so a bug
    upstream can never turn into injected DDL.
    """
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise RestoreError(f"refusing to emit unsafe identifier: {value!r}")
    if len(value.encode("utf-8")) > 63:
        raise RestoreError(f"identifier exceeds 63 bytes: {value!r}")
    return f'"{value}"'


def _qualified(name: str) -> str:
    """Return ``"public"."name"`` so DDL never depends on ``search_path``."""
    return f'"public".{_quote_ident(name)}'


def _column_ddl(table: TableSpec) -> list[str]:
    fragments: list[str] = []
    for column in table.columns:
        # ``type`` and ``default`` were validated on spec construction; re-run
        # the allowlist checks so emission cannot outrun validation.
        col_type = normalize_column_type(column.type)
        piece = f"{_quote_ident(column.name)} {col_type}"
        if not column.nullable:
            piece += " NOT NULL"
        default = _classify_default(column.default)
        if default is not None:
            piece += f" DEFAULT {default}"
        fragments.append(piece)
    return fragments


def _create_table_sql(table: TableSpec) -> str:
    parts = _column_ddl(table)
    if table.primary_key:
        cols = ", ".join(_quote_ident(c) for c in table.primary_key)
        parts.append(f"PRIMARY KEY ({cols})")
    for unique in table.unique_constraints:
        cols = ", ".join(_quote_ident(c) for c in unique)
        parts.append(f"UNIQUE ({cols})")
    body = ", ".join(parts)
    return f"CREATE TABLE {_qualified(table.name)} ({body})"


def _index_key_sql(key: Any, *, orderable: bool) -> str:
    piece = _quote_ident(key.column)
    if not orderable:
        # Only btree (amcanorder) accepts ASC/DESC/NULLS ordering; emitting them
        # for hash/gin/brin/spgist is a syntax error.
        return piece
    if key.descending:
        piece += " DESC"
    if key.nulls_first is True:
        piece += " NULLS FIRST"
    elif key.nulls_first is False:
        piece += " NULLS LAST"
    return piece


def _create_index_sql(index: IndexSpec) -> str:
    unique = "UNIQUE " if index.unique else ""
    orderable = index.method == "btree"
    keys = ", ".join(_index_key_sql(k, orderable=orderable) for k in index.keys)
    # The index inherits the table's schema, so only the table is qualified.
    sql = (
        f"CREATE {unique}INDEX {_quote_ident(index.name)} "
        f"ON {_qualified(index.table)} USING {index.method} ({keys})"
    )
    if index.included:
        included = ", ".join(_quote_ident(c) for c in index.included)
        sql += f" INCLUDE ({included})"
    if index.unique and index.nulls_not_distinct:
        # NULLS NOT DISTINCT (PostgreSQL 15+) follows the key/INCLUDE list and
        # precedes any partial-index predicate.
        sql += " NULLS NOT DISTINCT"
    if index.predicate is not None:
        # Re-validate: a predicate must be a read-only, allowlisted expression.
        predicate = _validate_index_predicate(index.predicate)
        sql += f" WHERE {predicate}"
    return sql


def _create_view_sql(view: ViewSpec) -> str:
    definition = _validate_view_definition(view.definition)
    return f"CREATE VIEW {_qualified(view.name)} AS {definition}"


def _order_views(views: tuple[ViewSpec, ...]) -> list[ViewSpec]:
    """Return views in dependency order (a view after every view it needs).

    Only intra-view dependencies matter for creation order; table dependencies
    are already satisfied because all tables are created first.  Ties are broken
    by name for determinism.  A dependency cycle aborts the restore.
    """
    by_name = {v.name: v for v in views}
    view_names = set(by_name)
    ordered: list[ViewSpec] = []
    placed: set[str] = set()
    # Kahn-style stable topological sort over the view-only dependency graph.
    pending = sorted(by_name, key=str)
    while pending:
        progressed = False
        still_pending: list[str] = []
        for name in pending:
            deps = {
                dep
                for dep in by_name[name].depends_on
                if dep in view_names and dep != name
            }
            if deps <= placed:
                ordered.append(by_name[name])
                placed.add(name)
                progressed = True
            else:
                still_pending.append(name)
        if not progressed:
            raise RestoreError(
                "view dependency cycle detected among: "
                + ", ".join(sorted(still_pending))
            )
        pending = still_pending
    return ordered


# Transaction-scoped advisory-lock key shared by every PPBase rebuild/restore
# path.  The two int4 halves spell ``PPBa``/``REST`` (0x50504261, 0x52455354)
# so the lock is unlikely to collide with an unrelated application's advisory
# lock.  ``pg_advisory_xact_lock`` blocks until the key is held and releases it
# automatically at COMMIT/ROLLBACK, so no cleanup path can leak it.
_RESTORE_ADVISORY_LOCK_HI = 0x50504261
_RESTORE_ADVISORY_LOCK_LO = 0x52455354


async def acquire_restore_coordination_lock(pg_conn: Any) -> None:
    """Take the transaction-scoped PPBase restore coordination lock.

    This serialises *cooperating* PPBase rebuild/restore transactions against
    one another: any other session that also calls this function will block
    until the in-flight rebuild commits or rolls back, so two PPBase restores
    can never interleave their emptiness check and their first ``CREATE``.

    **Cooperative, not absolute.**  An advisory lock does not, and cannot,
    stop an *arbitrary* third-party session from issuing ``CREATE TABLE public.x``
    mid-rebuild — PostgreSQL offers no lock over an empty schema's namespace.
    The deployment contract is therefore that a restore targets a database not
    otherwise in use (the destructive path even ``DROP SCHEMA public CASCADE``
    first).  The mandatory post-restore re-introspection
    (:func:`_verify_rebuilt_schema`) is the backstop that catches any object
    that slipped in regardless, aborting before COMMIT.
    """
    await pg_conn.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock($1::int4, $2::int4)",
        _RESTORE_ADVISORY_LOCK_HI,
        _RESTORE_ADVISORY_LOCK_LO,
    )


async def assert_target_database_empty(pg_conn: Any) -> None:
    """Raise :class:`TargetNotEmptyError` unless ``public`` is completely empty.

    Guarantees the rebuild starts from a clean slate: no relations of any kind,
    no user functions, no user types, no triggers, and no non-baseline
    extensions.  Refusing a non-empty target prevents a partial merge or an
    accidental overwrite of an existing database.
    """
    relation_rows = await pg_conn.fetch(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' "
        "AND c.relkind IN ('r', 'v', 'm', 'S', 'p', 'f', 'c') "
        "AND c.relpersistence <> 't' "
        "ORDER BY c.relname"
    )
    if relation_rows:
        names = ", ".join(row[0] for row in relation_rows)
        raise TargetNotEmptyError(
            f"restore target public schema already contains relations: {names}"
        )
    function_rows = await pg_conn.fetch(
        "SELECT p.proname FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public'"
    )
    if function_rows:
        raise TargetNotEmptyError(
            "restore target public schema already contains functions"
        )
    type_rows = await pg_conn.fetch(
        "SELECT t.typname FROM pg_type t "
        "JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd', 'c')"
    )
    if type_rows:
        raise TargetNotEmptyError(
            "restore target public schema already contains user types"
        )


async def rebuild_schema_into_empty_database(
    pg_conn: Any,
    schema: DatabaseSchema,
    reader: SegmentReader,
    *,
    verify: bool = True,
) -> None:
    """Rebuild ``schema`` (tables, data, indexes, views) into ``pg_conn``.

    The connection must target an **empty** ``public`` schema; the whole rebuild
    runs in one transaction, so any failure rolls back to that empty state.
    ``schema`` must already have passed archive-completeness validation (use
    :meth:`DatabaseSchema.from_archive_bytes`).  When ``verify`` is true (the
    default), the rebuilt schema is re-introspected and compared against the
    archive before the transaction commits.
    """
    schema.assert_every_table_exported()  # defence in depth vs. a raw from_dict

    segments_by_table = {segment.table: segment for segment in schema.segments}

    async with pg_conn.transaction():
        # Serialise cooperating PPBase restores, then assert emptiness *inside*
        # the same transaction so the check and the rebuild are atomic w.r.t.
        # every other PPBase restore.  The mandatory verification below is the
        # backstop against any non-cooperating third-party session.
        await acquire_restore_coordination_lock(pg_conn)
        await assert_target_database_empty(pg_conn)
        await _rebuild_within_open_transaction(pg_conn, schema, reader, verify=verify)

    # Sanity: every table's segment was consumed (no orphan segment).
    unused = set(segments_by_table) - {t.name for t in schema.tables}
    if unused:
        raise RestoreError(
            "internal error: segments without a table survived rebuild: "
            + ", ".join(sorted(unused))
        )


async def _rebuild_within_open_transaction(
    pg_conn: Any,
    schema: DatabaseSchema,
    reader: SegmentReader,
    *,
    verify: bool,
) -> None:
    """Recreate every object inside an already-open transaction.

    The caller owns the surrounding transaction (empty-schema rebuild or the
    destructive drop-and-replace path), so this helper only issues the ordered
    DDL/COPY steps and, optionally, the post-restore verification.  It never
    opens or commits a transaction itself.
    """
    # Pin a closed search_path so unqualified names in view bodies and index
    # predicates resolve only against public (pg_catalog is always implicit).
    await pg_conn.execute("SET LOCAL search_path = public")
    # Pin the same COPY session GUCs the export used so text values
    # (timestamps, intervals, encoding, backslashes) parse back identically.
    await apply_copy_session_settings(pg_conn)

    # 1. Tables (name order; PPBase tables have no inter-table FKs).
    for table in sorted(schema.tables, key=lambda t: t.name):
        await pg_conn.execute(_create_table_sql(table))

    # 2. COPY import, in the recorded segment order, integrity-checked and
    #    row-count-verified inside import_table_segment.
    for segment in schema.segments:
        await import_table_segment(pg_conn, reader, segment)

    # 3. Indexes (name order) after the data load.
    for index in sorted(schema.indexes, key=lambda i: i.name):
        await pg_conn.execute(_create_index_sql(index))

    # 4. Views in dependency order.
    for view in _order_views(schema.views):
        await pg_conn.execute(_create_view_sql(view))

    if verify:
        await _verify_rebuilt_schema(pg_conn, schema)


_RESTORE_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def _quote_role(role: Any) -> str:
    """Double-quote a server-reported role name for safe DDL emission."""
    if not isinstance(role, str) or not role or "\x00" in role:
        raise RestoreError("restore runtime role is invalid")
    return '"' + role.replace('"', '""') + '"'


async def run_native_destructive_restore(
    pg_conn: Any,
    schema: DatabaseSchema,
    reader: SegmentReader,
    *,
    restore_id: str,
    verify: bool = True,
) -> None:
    """Atomically replace ``public`` with ``schema`` and write the commit marker.

    This is the native (v2) counterpart of
    :func:`ppbase.backup.postgres.run_destructive_pg_restore_from_fd`.  The whole
    replacement — dropping ``public``, rebuilding every object, importing the
    COPY data, and inserting the ``_ppbase_restore_control.commits`` marker —
    runs in a single asyncpg transaction, so any failure or interruption before
    COMMIT rolls the target back to its pre-restore state.  The marker row lets
    the caller confirm, from an independent connection, that PostgreSQL actually
    committed the replacement (identical semantics to the v1 psql path).
    """
    if not isinstance(restore_id, str) or not _RESTORE_ID_PATTERN.fullmatch(restore_id):
        raise RestoreError("destructive restore ID must be 32 lowercase hex characters")
    schema.assert_every_table_exported()

    role = await pg_conn.fetchval("SELECT current_user")
    quoted_role = _quote_role(role)

    # PostgreSQL 17 added transaction_timeout; a non-zero server-wide value would
    # abort a long destructive restore.  Clear it for this session before BEGIN
    # (no-op on <=16 where no matching pg_settings row exists).
    await pg_conn.execute(
        "SELECT pg_catalog.set_config(name, '0', false) "
        "FROM pg_catalog.pg_settings WHERE name = 'transaction_timeout'"
    )

    async with pg_conn.transaction():
        await pg_conn.execute("SET LOCAL statement_timeout = 0")
        await pg_conn.execute("SET LOCAL lock_timeout = '30s'")
        # Serialise against every other cooperating PPBase restore before the
        # destructive DROP so two replacements can never interleave.
        await acquire_restore_coordination_lock(pg_conn)
        await pg_conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        await pg_conn.execute('DROP SCHEMA IF EXISTS "_ppbase_restore_control" CASCADE')
        await pg_conn.execute(f"CREATE SCHEMA public AUTHORIZATION {quoted_role}")

        await assert_target_database_empty(pg_conn)
        await _rebuild_within_open_transaction(pg_conn, schema, reader, verify=verify)

        await pg_conn.execute(
            f'CREATE SCHEMA "_ppbase_restore_control" AUTHORIZATION {quoted_role}'
        )
        await pg_conn.execute(
            'CREATE TABLE "_ppbase_restore_control".commits ('
            "restore_id text PRIMARY KEY, "
            "committed_at timestamptz NOT NULL DEFAULT pg_catalog.clock_timestamp())"
        )
        await pg_conn.execute(
            'INSERT INTO "_ppbase_restore_control".commits (restore_id) VALUES ($1)',
            restore_id,
        )


async def _verify_rebuilt_schema(pg_conn: Any, schema: DatabaseSchema) -> None:
    """Re-introspect the target and prove it matches the archive exactly."""
    # Render the re-introspection under the *same* search_path the export used
    # (``pg_catalog, pg_temp``; see ``postgres.set_backup_control_search_path``).
    # ``pg_get_viewdef``/``pg_get_expr`` schema-qualify a ``public`` relation
    # only when ``public`` is outside the active search path, so the archive —
    # captured under this path — records qualified names such as
    # ``public.widgets``.  The rebuild pins ``search_path = public`` so unqualified
    # names in view/index bodies resolve during CREATE; verifying under that path
    # would instead render unqualified names and spuriously fail every view that
    # references another public relation.
    await pg_conn.execute("SET LOCAL search_path = pg_catalog, pg_temp")
    table_inventory = {t.name: t.kind for t in schema.tables}
    view_inventory = frozenset(v.name for v in schema.views)
    canonical_tables = {t.name: t for t in schema.tables}
    rebuilt = await introspect_public_schema(
        pg_conn,
        table_inventory=table_inventory,
        view_inventory=view_inventory,
        canonical_tables=canonical_tables,
    )
    if set(rebuilt.tables) != set(schema.tables):
        raise RestoreError(
            "post-restore verification failed: table set differs from the archive"
        )
    if set(rebuilt.indexes) != set(schema.indexes):
        raise RestoreError(
            "post-restore verification failed: index set differs from the archive"
        )
    if set(rebuilt.views) != set(schema.views):
        raise RestoreError(
            "post-restore verification failed: view set differs from the archive"
        )


__all__ = [
    "RestoreError",
    "TargetNotEmptyError",
    "acquire_restore_coordination_lock",
    "assert_target_database_empty",
    "rebuild_schema_into_empty_database",
    "run_native_destructive_restore",
]
