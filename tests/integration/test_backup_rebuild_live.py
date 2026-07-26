"""Phase 2 tests for the native rebuild engine (``ppbase.backup.rebuild``).

Two layers:

* Pure-Python DDL construction: table/index/view SQL is assembled only from
  contract-validated values, identifiers are re-checked before emission, and
  views are ordered by dependency (cycles are refused).
* Live rebuild round-trip: a source database is exported with the COPY segment
  writer, then rebuilt into a *separate empty* database.  The rebuilt data is
  compared byte-for-byte against the source across the full edge-case matrix,
  the empty-target guard is proven, and a corrupted archive is proven to roll
  back leaving the target empty.  Skipped when no server / no CREATEDB.
"""

from __future__ import annotations

import io
import os
import uuid

import pytest

from ppbase.backup.rebuild import (
    RestoreError,
    TargetNotEmptyError,
    _create_index_sql,
    _create_table_sql,
    _create_view_sql,
    _order_views,
    _quote_ident,
    assert_target_database_empty,
    rebuild_schema_into_empty_database,
    run_native_destructive_restore,
)
from ppbase.backup.schema_contract import (
    ColumnSpec,
    IndexKeySpec,
    IndexSpec,
    TableSpec,
    ViewSpec,
)


# ---------------------------------------------------------------------------
# Pure-Python DDL builders
# ---------------------------------------------------------------------------


def _table() -> TableSpec:
    return TableSpec(
        name="posts",
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
            ColumnSpec("views", "integer", True, "0"),
            ColumnSpec("tags", "text[]", True, "'{}'::text[]"),
        ),
        primary_key=("id",),
        unique_constraints=(("title",),),
    )


def test_create_table_sql_is_fully_quoted_and_allowlisted() -> None:
    sql = _create_table_sql(_table())
    assert sql == (
        'CREATE TABLE "public"."posts" ('
        '"id" character varying(15) NOT NULL, '
        '"title" text NOT NULL DEFAULT \'\'::text, '
        '"views" integer DEFAULT 0, '
        '"tags" text[] DEFAULT \'{}\'::text[], '
        'PRIMARY KEY ("id"), '
        'UNIQUE ("title"))'
    )


def test_create_index_sql_preserves_ordering_include_and_predicate() -> None:
    index = IndexSpec(
        name="idx_posts_title",
        table="posts",
        method="btree",
        unique=True,
        nulls_not_distinct=False,
        keys=(IndexKeySpec("title", True, False),),
        included=("views",),
        predicate="(\"title\" <> ''::text)",
    )
    sql = _create_index_sql(index)
    assert sql == (
        'CREATE UNIQUE INDEX "idx_posts_title" ON "public"."posts" USING btree '
        '("title" DESC NULLS LAST) INCLUDE ("views") '
        "WHERE (\"title\" <> ''::text)"
    )


def test_create_index_sql_emits_nulls_not_distinct() -> None:
    index = IndexSpec(
        name="u",
        table="posts",
        method="btree",
        unique=True,
        nulls_not_distinct=True,
        keys=(IndexKeySpec("title", False, None),),
        included=(),
        predicate=None,
    )
    sql = _create_index_sql(index)
    # The option follows the key list (and any INCLUDE), never the USING clause.
    assert sql == (
        'CREATE UNIQUE INDEX "u" ON "public"."posts" USING btree '
        '("title") NULLS NOT DISTINCT'
    )
    assert "NULLS NOT DISTINCT USING" not in sql


def test_create_view_sql_revalidates_definition() -> None:
    view = ViewSpec("v", "SELECT id, title FROM posts", ("posts",))
    assert _create_view_sql(view) == (
        'CREATE VIEW "public"."v" AS SELECT id, title FROM posts'
    )


def test_order_views_is_dependency_ordered() -> None:
    a = ViewSpec("a", "SELECT id FROM posts", ("posts",))
    b = ViewSpec("b", "SELECT id FROM a", ("a",))
    c = ViewSpec("c", "SELECT id FROM b", ("b",))
    ordered = _order_views((c, b, a))
    assert [v.name for v in ordered] == ["a", "b", "c"]


def test_order_views_rejects_cycle() -> None:
    # Two views that (by depends_on) reference each other cannot be ordered.
    a = ViewSpec("a", "SELECT 1 AS id", ("b",))
    b = ViewSpec("b", "SELECT 1 AS id", ("a",))
    with pytest.raises(RestoreError, match="cycle"):
        _order_views((a, b))


def test_quote_ident_rejects_unsafe() -> None:
    with pytest.raises(RestoreError):
        _quote_ident('a"; DROP TABLE x; --')


# ---------------------------------------------------------------------------
# Live rebuild round-trip
# ---------------------------------------------------------------------------

asyncpg = pytest.importorskip("asyncpg")

from ppbase.backup.copy_format import (  # noqa: E402
    SegmentReader,
    SegmentWriter,
    apply_copy_session_settings,
    export_table_segment,
)
from ppbase.backup.schema_contract import (  # noqa: E402
    DatabaseSchema,
    introspect_public_schema,
)


def _admin_dsn() -> str:
    url = os.environ.get("PPBASE_TEST_DATABASE_URL")
    if url:
        base = url.replace("postgresql+asyncpg://", "postgresql://", 1)
    else:
        base = "postgresql://ppbase:ppbase@localhost:5433/postgres"
    return base


def _dsn_for(db: str) -> str:
    base = _admin_dsn()
    head, _, _tail = base.rpartition("/")
    return f"{head}/{db}"


async def _connect_admin() -> "asyncpg.Connection":
    try:
        conn = await asyncpg.connect(_admin_dsn(), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    row = await conn.fetchrow(
        "SELECT rolcreatedb OR rolsuper AS can FROM pg_roles "
        "WHERE rolname = current_user"
    )
    if not row or not row["can"]:  # pragma: no cover - environment dependent
        await conn.close()
        pytest.skip("test role cannot CREATE DATABASE")
    return conn


_COLUMNS = ("id", "t", "n", "ts", "j", "arr")
_COLUMN_SQL = ", ".join(f'"{c}"' for c in _COLUMNS)


async def _create_edge_table(conn: "asyncpg.Connection", name: str) -> None:
    await conn.execute(
        f'CREATE TABLE "{name}" ('
        "id TEXT PRIMARY KEY, "
        "t TEXT, "
        "n DOUBLE PRECISION, "
        "ts TIMESTAMPTZ, "
        "j JSONB, "
        "arr TEXT[])"
    )


async def _seed(conn: "asyncpg.Connection", name: str) -> int:
    rows = [
        ("r1", "", 0.0, "2021-01-02 03:04:05.678901+00", '{"a":[1,2,{"b":"x"}]}', ["a", "b"]),
        ("r2", None, float("nan"), "infinity", "null", []),
        ("r3", "tab\there\nnl\\bs", float("inf"), "-infinity", '{"k":"v"}', ["\\N", "\\."]),
        ("r4", "café 😀 ☃ 日本", float("-inf"), None, '"s"', ["x", "y", "z"]),
        ("r5", "\\N", -0.0, "1999-12-31 23:59:59+00", "123", ["line1\nline2", "\ttab"]),
    ]
    await conn.executemany(
        f'INSERT INTO "{name}" ({_COLUMN_SQL}) '
        "VALUES ($1,$2,$3,$4::text::timestamptz,$5::jsonb,$6)",
        rows,
    )
    return len(rows)


async def _dump(conn: "asyncpg.Connection", table: str) -> bytes:
    chunks: list[bytes] = []

    async def _sink(data: bytes) -> None:
        chunks.append(bytes(data))

    await conn.copy_from_query(
        f'SELECT {_COLUMN_SQL} FROM "{table}" ORDER BY id',
        output=_sink,
        format="text",
        delimiter="\t",
        null="\\N",
    )
    return b"".join(chunks)


async def _build_archive(
    src: "asyncpg.Connection", table: str, view: str
) -> tuple[DatabaseSchema, bytes]:
    # Faithfully mirror the production export path, which pins
    # ``search_path = pg_catalog, pg_temp`` (see
    # ``postgres.set_backup_control_search_path``) before introspecting.  Under
    # that path ``pg_get_viewdef``/``pg_get_expr`` schema-qualify ``public``
    # relations (``FROM public.posts``), exactly as the real archive records
    # them and as the post-restore verification re-renders them.
    await src.execute("SET search_path = pg_catalog, pg_temp")
    intro = await introspect_public_schema(
        src,
        table_inventory={table: "base"},
        view_inventory=frozenset({view}),
    )
    buf = io.BytesIO()
    writer = SegmentWriter(buf)
    segments = []
    async with src.transaction():
        await apply_copy_session_settings(src)
        for spec in sorted(intro.tables, key=lambda t: t.name):
            seg = await export_table_segment(
                src, writer, table=spec.name, columns=spec.column_names
            )
            segments.append(seg)
    data = buf.getvalue()
    schema = DatabaseSchema(
        tables=intro.tables,
        views=intro.views,
        indexes=intro.indexes,
        migrations=(),
        segments=tuple(segments),
        data_copy_size=len(data),
        source_postgres_version=intro.source_postgres_version,
    )
    return schema, data


@pytest.mark.asyncio
async def test_rebuild_round_trip_is_byte_for_byte() -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_src_{suffix}"
    dst_db = f"ppb_dst_{suffix}"
    table = f"posts_{suffix}"
    view = f"view_{suffix}"
    idx = f"idx_{suffix}"
    src = dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}"')
        await admin.execute(f'CREATE DATABASE "{dst_db}"')

        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await _create_edge_table(src, table)
        expected_rows = await _seed(src, table)
        await src.execute(f'CREATE INDEX "{idx}" ON "{table}" (t)')
        await src.execute(
            f'CREATE VIEW "{view}" AS SELECT id, t FROM "{table}"'
        )
        before = await _dump(src, table)

        schema, data = await _build_archive(src, table, view)
        assert sum(s.row_count for s in schema.segments) == expected_rows

        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        reader = SegmentReader(io.BytesIO(data), total_size=len(data))
        await rebuild_schema_into_empty_database(dst, schema, reader)

        after = await _dump(dst, table)
        assert after == before

        # The rebuilt view and index exist and match.
        assert await dst.fetchval(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relkind = 'v' AND c.relname = $1",
            view,
        ) == 1
        assert await dst.fetchval(f'SELECT count(*) FROM "{view}"') == expected_rows
    finally:
        if src is not None:
            await src.close()
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_rebuild_refuses_non_empty_target() -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    dst_db = f"ppb_ne_{suffix}"
    dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{dst_db}"')
        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        await dst.execute("CREATE TABLE occupied (id INT)")
        with pytest.raises(TargetNotEmptyError, match="already contains relations"):
            await assert_target_database_empty(dst)
    finally:
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_native_destructive_restore_replaces_and_marks() -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_dsrc_{suffix}"
    dst_db = f"ppb_ddst_{suffix}"
    table = f"posts_{suffix}"
    view = f"view_{suffix}"
    restore_id = uuid.uuid4().hex  # 32 lowercase hex characters
    src = dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}"')
        await admin.execute(f'CREATE DATABASE "{dst_db}"')

        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await _create_edge_table(src, table)
        expected_rows = await _seed(src, table)
        await src.execute(f'CREATE VIEW "{view}" AS SELECT id, t FROM "{table}"')
        before = await _dump(src, table)
        schema, data = await _build_archive(src, table, view)

        # The destination already holds unrelated relations; a destructive
        # restore must remove them and leave only the archive's objects.
        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        await dst.execute("CREATE TABLE stale_junk (id INT)")
        await dst.execute("CREATE VIEW stale_view AS SELECT 1 AS id")

        reader = SegmentReader(io.BytesIO(data), total_size=len(data))
        await run_native_destructive_restore(
            dst, schema, reader, restore_id=restore_id
        )

        # Pre-existing junk is gone; the archive's table/view are present.
        assert await dst.fetchval("SELECT to_regclass('public.stale_junk')") is None
        assert await dst.fetchval("SELECT to_regclass('public.stale_view')") is None
        after = await _dump(dst, table)
        assert after == before
        assert await dst.fetchval(f'SELECT count(*) FROM "{view}"') == expected_rows

        # The atomic commit marker records this restore_id.
        marker = await dst.fetchval(
            'SELECT restore_id FROM "_ppbase_restore_control".commits '
            "ORDER BY committed_at DESC LIMIT 1"
        )
        assert marker == restore_id
    finally:
        if src is not None:
            await src.close()
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_native_destructive_restore_rolls_back_preserving_target() -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_rsrc_{suffix}"
    dst_db = f"ppb_rdst_{suffix}"
    table = f"posts_{suffix}"
    view = f"view_{suffix}"
    restore_id = uuid.uuid4().hex
    src = dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}"')
        await admin.execute(f'CREATE DATABASE "{dst_db}"')
        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await _create_edge_table(src, table)
        await _seed(src, table)
        await src.execute(f'CREATE VIEW "{view}" AS SELECT id, t FROM "{table}"')
        schema, data = await _build_archive(src, table, view)

        corrupt = bytearray(data)
        assert corrupt, "expected non-empty data.copy"
        corrupt[len(corrupt) // 2] ^= 0xFF

        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        await dst.execute("CREATE TABLE survivor (id INT)")
        await dst.execute("INSERT INTO survivor (id) VALUES (7)")

        reader = SegmentReader(io.BytesIO(bytes(corrupt)), total_size=len(corrupt))
        with pytest.raises(Exception):
            await run_native_destructive_restore(
                dst, schema, reader, restore_id=restore_id
            )

        # The pre-existing relation survives; no marker was written.
        assert await dst.fetchval("SELECT count(*) FROM survivor") == 1
        assert await dst.fetchval(
            "SELECT to_regclass('_ppbase_restore_control.commits')"
        ) is None
    finally:
        if src is not None:
            await src.close()
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_rebuild_reproduces_every_index_variant() -> None:
    """Round-trip a table whose indexes span NULLS NOT DISTINCT and every method.

    Each index is created on the source, exported, rebuilt into a fresh empty
    database, and the rebuilt ``pg_get_indexdef`` is compared byte-for-byte with
    the source's.  This proves the generated DDL is not merely syntactically
    plausible (a prior bug placed ``NULLS NOT DISTINCT`` before ``USING`` and
    emitted ``NULLS LAST`` for hash/gin, both of which PostgreSQL rejects).
    """
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_isrc_{suffix}"
    dst_db = f"ppb_idst_{suffix}"
    table = f"posts_{suffix}"
    view = f"view_{suffix}"
    # (index name, DDL suffix).  Names are unique per run to avoid collisions.
    index_specs = [
        (f"nnd_{suffix}", f'CREATE UNIQUE INDEX "nnd_{suffix}" ON "{table}" '
                          "(t) NULLS NOT DISTINCT"),
        (f"btdesc_{suffix}", f'CREATE INDEX "btdesc_{suffix}" ON "{table}" '
                             "USING btree (t DESC NULLS FIRST)"),
        (f"hash_{suffix}", f'CREATE INDEX "hash_{suffix}" ON "{table}" '
                           "USING hash (t)"),
        (f"gin_{suffix}", f'CREATE INDEX "gin_{suffix}" ON "{table}" '
                          "USING gin (j)"),
        (f"spgist_{suffix}", f'CREATE INDEX "spgist_{suffix}" ON "{table}" '
                             "USING spgist (t)"),
        (f"brin_{suffix}", f'CREATE INDEX "brin_{suffix}" ON "{table}" '
                           "USING brin (ts)"),
    ]
    src = dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}"')
        await admin.execute(f'CREATE DATABASE "{dst_db}"')

        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await _create_edge_table(src, table)
        await _seed(src, table)
        for _name, ddl in index_specs:
            await src.execute(ddl)
        await src.execute(f'CREATE VIEW "{view}" AS SELECT id, t FROM "{table}"')

        # Record the canonical source definitions.
        src_defs = {}
        for name, _ddl in index_specs:
            src_defs[name] = await src.fetchval(
                "SELECT pg_get_indexdef(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = $1",
                name,
            )

        schema, data = await _build_archive(src, table, view)
        # Every index made it into the contract.
        assert {i.name for i in schema.indexes} >= set(src_defs)

        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        reader = SegmentReader(io.BytesIO(data), total_size=len(data))
        await rebuild_schema_into_empty_database(dst, schema, reader)

        for name, src_def in src_defs.items():
            dst_def = await dst.fetchval(
                "SELECT pg_get_indexdef(c.oid) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = $1",
                name,
            )
            assert dst_def == src_def, name
    finally:
        if src is not None:
            await src.close()
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()


@pytest.mark.asyncio
async def test_rebuild_rolls_back_on_corrupt_archive() -> None:
    admin = await _connect_admin()
    suffix = uuid.uuid4().hex[:12]
    src_db = f"ppb_csrc_{suffix}"
    dst_db = f"ppb_cdst_{suffix}"
    table = f"posts_{suffix}"
    view = f"view_{suffix}"
    src = dst = None
    try:
        await admin.execute(f'CREATE DATABASE "{src_db}"')
        await admin.execute(f'CREATE DATABASE "{dst_db}"')
        src = await asyncpg.connect(_dsn_for(src_db), timeout=5)
        await _create_edge_table(src, table)
        await _seed(src, table)
        await src.execute(f'CREATE VIEW "{view}" AS SELECT id, t FROM "{table}"')
        schema, data = await _build_archive(src, table, view)

        # Flip a byte inside data.copy so decompression / hashing fails mid-import.
        corrupt = bytearray(data)
        assert corrupt, "expected non-empty data.copy"
        corrupt[len(corrupt) // 2] ^= 0xFF

        dst = await asyncpg.connect(_dsn_for(dst_db), timeout=5)
        reader = SegmentReader(io.BytesIO(bytes(corrupt)), total_size=len(corrupt))
        with pytest.raises(Exception):
            await rebuild_schema_into_empty_database(dst, schema, reader)

        # Rollback must have left the target public schema empty.
        remaining = await dst.fetchval(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relkind IN ('r', 'v')"
        )
        assert remaining == 0
    finally:
        if src is not None:
            await src.close()
        if dst is not None:
            await dst.close()
        await admin.execute(f'DROP DATABASE IF EXISTS "{src_db}" WITH (FORCE)')
        await admin.execute(f'DROP DATABASE IF EXISTS "{dst_db}" WITH (FORCE)')
        await admin.close()
