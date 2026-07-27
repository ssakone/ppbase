"""Tests for the declarative schema.json model and the strict schema contract.

Two layers are covered:

* Pure-Python contract validation: the closed type allowlist, the unquoted
  identifier grammar, exact column defaults, the structural (plain-column) index
  model, single-SELECT view validation, ``copy_settings`` validation, segment
  column-order/coverage enforcement and canonical serialisation round-trips.
* Live fail-closed introspection: ``assert_no_unmanaged_objects`` and
  ``introspect_public_schema`` against a real PostgreSQL server, proving that
  unmanaged objects, unknown tables/views, expression/opclass indexes and
  extensions all abort the backup, while DESC/NULLS/INCLUDE/partial indexes are
  captured faithfully.  Skipped with no server.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from ppbase.backup.canonical import _LITERAL_DEFAULTS, _field_column_spec
from ppbase.backup.copy_format import COMPRESSION_ZLIB, CopySegment
from ppbase.backup.schema_contract import (
    ColumnSpec,
    DatabaseSchema,
    IndexKeySpec,
    IndexSpec,
    MigrationSpec,
    SchemaContractError,
    TableSpec,
    UnmanagedObjectError,
    ViewSpec,
    assert_no_unmanaged_objects,
    introspect_public_schema,
    normalize_column_type,
)
from ppbase.models.field_types import FieldDefinition, FieldType


def _col(name: str, coltype: str = "text", *, nullable: bool = False,
         default: str | None = None) -> ColumnSpec:
    return ColumnSpec(name=name, type=coltype, nullable=nullable, default=default)


def test_number_defaults_preserve_collection_and_orm_rendering() -> None:
    floating = _field_column_spec(
        FieldDefinition(name="amount", type=FieldType.NUMBER)
    )
    integer = _field_column_spec(
        FieldDefinition(
            name="count",
            type=FieldType.NUMBER,
            options={"onlyInt": True},
        )
    )

    # Collection columns come from schema_manager's raw
    # ``DOUBLE PRECISION DEFAULT 0`` DDL and introspect as ``0``. System-table
    # Float defaults emitted through SQLAlchemy introspect with an explicit
    # quoted cast; the two canonical paths must remain distinct.
    assert floating.default == "0"
    assert integer.default == "0"
    assert _LITERAL_DEFAULTS[("0", "double precision")] == (
        "'0'::double precision"
    )


# ---------------------------------------------------------------------------
# Column-type allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "coltype",
    [
        "text",
        "integer",
        "bigint",
        "smallint",
        "double precision",
        "real",
        "boolean",
        "timestamp with time zone",
        "jsonb",
        "json",
        "character varying(255)",
        "character varying(15)",
        "text[]",
        "character varying(15)[]",
    ],
)
def test_normalize_column_type_accepts_allowed(coltype: str) -> None:
    assert normalize_column_type(coltype) == coltype


@pytest.mark.parametrize(
    "coltype",
    [
        "serial",
        "uuid",
        "bytea",
        "numeric(10,2)",
        "int4range",
        "geometry",
        "text ; drop table x",
        "integer[]",
        "timestamp without time zone",
        123,
    ],
)
def test_normalize_column_type_rejects_disallowed(coltype: object) -> None:
    with pytest.raises(SchemaContractError):
        normalize_column_type(coltype)


# ---------------------------------------------------------------------------
# Column defaults (exact expression capture)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "default",
    ["''::text", "0", "false", "now()", "'{}'::text[]", "'null'::jsonb", None],
)
def test_column_accepts_ppbase_defaults(default: str | None) -> None:
    assert _col("f", default=default).default == default


@pytest.mark.parametrize(
    "default",
    ["nextval('s')", "(SELECT 1); DROP TABLE x", "1 -- comment", ""],
)
def test_column_rejects_dangerous_default(default: str) -> None:
    with pytest.raises(SchemaContractError):
        _col("f", default=default)


# ---------------------------------------------------------------------------
# Identifier grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["drop table", "a-b", "1col", 'x"y', "sel;ect", "café", "a b", ""],
)
def test_table_rejects_injectable_names(name: str) -> None:
    with pytest.raises(SchemaContractError):
        TableSpec(
            name=name,
            kind="base",
            columns=(_col("id"),),
            primary_key=("id",),
            unique_constraints=(),
        )


def test_identifier_rejects_over_63_bytes() -> None:
    with pytest.raises(SchemaContractError, match="63 bytes"):
        _col("a" * 64)


# ---------------------------------------------------------------------------
# TableSpec structural validation
# ---------------------------------------------------------------------------


def test_table_rejects_unknown_kind() -> None:
    with pytest.raises(SchemaContractError, match="kind"):
        TableSpec(
            name="posts",
            kind="magic",
            columns=(_col("id"),),
            primary_key=(),
            unique_constraints=(),
        )


def test_table_rejects_pk_referencing_unknown_column() -> None:
    with pytest.raises(SchemaContractError, match="primary key"):
        TableSpec(
            name="posts",
            kind="base",
            columns=(_col("id"),),
            primary_key=("missing",),
            unique_constraints=(),
        )


def test_table_rejects_unique_referencing_unknown_column() -> None:
    with pytest.raises(SchemaContractError, match="unique constraint"):
        TableSpec(
            name="posts",
            kind="base",
            columns=(_col("id"),),
            primary_key=("id",),
            unique_constraints=(("nope",),),
        )


def test_table_rejects_duplicate_columns() -> None:
    with pytest.raises(SchemaContractError, match="duplicate columns"):
        TableSpec(
            name="posts",
            kind="base",
            columns=(_col("id"), _col("id")),
            primary_key=("id",),
            unique_constraints=(),
        )


# ---------------------------------------------------------------------------
# IndexKeySpec / IndexSpec (structural, plain-column only)
# ---------------------------------------------------------------------------


def test_index_key_captures_ordering() -> None:
    key = IndexKeySpec(column="title", descending=True, nulls_first=False)
    assert key.to_dict() == {
        "column": "title",
        "descending": True,
        "nulls_first": False,
    }
    assert IndexKeySpec.from_dict(key.to_dict()) == key


def test_index_key_rejects_bad_column() -> None:
    with pytest.raises(SchemaContractError):
        IndexKeySpec(column="lower(x)", descending=False, nulls_first=None)


def test_index_rejects_unknown_method() -> None:
    with pytest.raises(SchemaContractError, match="method"):
        IndexSpec(
            name="idx",
            table="posts",
            method="rtree",
            unique=False,
            nulls_not_distinct=False,
            keys=(IndexKeySpec("id", False, None),),
            included=(),
            predicate=None,
        )


def test_index_rejects_no_keys() -> None:
    with pytest.raises(SchemaContractError, match="no keys"):
        IndexSpec(
            name="idx",
            table="posts",
            method="btree",
            unique=False,
            nulls_not_distinct=False,
            keys=(),
            included=(),
            predicate=None,
        )


def test_index_rejects_dangerous_predicate() -> None:
    with pytest.raises(SchemaContractError, match="predicate"):
        IndexSpec(
            name="idx",
            table="posts",
            method="btree",
            unique=False,
            nulls_not_distinct=False,
            keys=(IndexKeySpec("id", False, None),),
            included=(),
            predicate="id > 0; DROP TABLE posts",
        )


def test_index_accepts_managed_partial_predicate() -> None:
    idx = IndexSpec(
        name="idx",
        table="posts",
        method="btree",
        unique=True,
        nulls_not_distinct=False,
        keys=(IndexKeySpec("email", False, None),),
        included=(),
        predicate="(\"email\" <> ''::text)",
    )
    assert IndexSpec.from_dict(idx.to_dict()) == idx


# ---------------------------------------------------------------------------
# ViewSpec single-SELECT validation
# ---------------------------------------------------------------------------


def test_view_accepts_select() -> None:
    v = ViewSpec("v", "SELECT id, title FROM posts", ("posts",))
    assert ViewSpec.from_dict(v.to_dict()) == v


def test_view_accepts_quoted_identifier_containing_keyword() -> None:
    # A column named "update" must not trip the keyword guard (it is quoted).
    v = ViewSpec("v", 'SELECT "update" FROM posts', ("posts",))
    assert v.definition == 'SELECT "update" FROM posts'


@pytest.mark.parametrize(
    "definition",
    [
        "DELETE FROM posts",
        "SELECT 1; DROP TABLE posts",
        "SELECT 1 -- sneaky",
        "SELECT 1 /* block */",
        "INSERT INTO posts VALUES (1)",
        "UPDATE posts SET x = 1",
        "TABLE posts",
        "",
    ],
)
def test_view_rejects_non_select_or_dangerous(definition: str) -> None:
    with pytest.raises(SchemaContractError):
        ViewSpec("v", definition, ())


# ---------------------------------------------------------------------------
# Phase 1.2: quoted-identifier / qualified-call allowlist bypass is closed.
# A regex blacklist over raw text let ``"pg_sleep"(10)`` through because the
# quotes hid the callable's name; the token-classifying lexer must reject every
# non-allowlisted call regardless of quoting, qualification or casing.
# ---------------------------------------------------------------------------

_VIEW_BYPASS_ATTEMPTS = [
    'SELECT "pg_sleep"(10)',
    'SELECT "PG_SLEEP"(10)',
    'SELECT "pg_terminate_backend"(pg_backend_pid())',
    "SELECT pg_terminate_backend(pg_backend_pid())",
    "SELECT \"nextval\"('rogue_seq'::regclass)",
    "SELECT nextval('rogue_seq'::regclass)",
    "SELECT pg_catalog.pg_read_file('/etc/passwd')",
    "SELECT \"pg_catalog\".\"pg_ls_dir\"('/')",
    "SELECT id FROM information_schema.tables",
    "SELECT 1::regclass",
    "SELECT dblink('', '')",
    "SELECT lo_import('/etc/passwd')",
    # A schema-qualified but individually allowlisted function must still fail
    # closed at the lexer (it would resolve outside the pinned search_path).
    "SELECT evil.lower(name) FROM users",
]


@pytest.mark.parametrize("definition", _VIEW_BYPASS_ATTEMPTS)
def test_view_rejects_quoted_or_qualified_call_bypass(definition: str) -> None:
    with pytest.raises(SchemaContractError):
        ViewSpec("v", definition, ())


_PREDICATE_BYPASS_ATTEMPTS = [
    '"pg_sleep"(10) IS NOT NULL',
    "id = nextval('rogue_seq'::regclass)",
    "pg_catalog.pg_read_file('x') = ''",
    "id::regclass IS NOT NULL",
    '"pg_terminate_backend"(1) OR true',
    # Schema-qualified allowlisted function resolves outside the search_path.
    "evil.lower(name) = 'x'::text",
]


# Legitimate PPBase collections may be named with a ``pg_`` prefix; the schema
# contract must accept these on their name alone (security is namespace + OID +
# inventory based, never prefix based).
@pytest.mark.parametrize(
    "definition",
    [
        "SELECT id FROM pg_posts",
        "SELECT id, title FROM pg_status",
        'SELECT "pg_posts".id FROM pg_posts',
    ],
)
def test_view_accepts_pg_prefixed_collection_names(definition: str) -> None:
    v = ViewSpec("v", definition, ())
    assert v.definition == definition


@pytest.mark.parametrize(
    "predicate",
    ["pg_views_count > 0", "pg_flag IS TRUE"],
)
def test_index_predicate_accepts_pg_prefixed_columns(predicate: str) -> None:
    spec = IndexSpec(
        name="idx",
        table="pg_posts",
        method="btree",
        unique=False,
        nulls_not_distinct=False,
        keys=(IndexKeySpec("id", False, None),),
        included=(),
        predicate=predicate,
    )
    assert spec.predicate == predicate


@pytest.mark.parametrize("predicate", _PREDICATE_BYPASS_ATTEMPTS)
def test_index_predicate_rejects_call_bypass(predicate: str) -> None:
    with pytest.raises(SchemaContractError):
        IndexSpec(
            name="idx",
            table="posts",
            method="btree",
            unique=False,
            nulls_not_distinct=False,
            keys=(IndexKeySpec("id", False, None),),
            included=(),
            predicate=predicate,
        )


@pytest.mark.parametrize(
    "default",
    [
        "\"nextval\"('rogue_seq'::regclass)",
        "nextval('rogue_seq'::regclass)",
        "pg_read_file('/etc/passwd')",
        "'x'::text",  # non-empty literal is not a PPBase default form
        "'{}'::json",  # json (not jsonb) is not a PPBase default form
    ],
)
def test_column_rejects_quoted_or_call_default_bypass(default: str) -> None:
    with pytest.raises(SchemaContractError):
        _col("f", default=default)


# ---------------------------------------------------------------------------
# Phase 1.2: restorable-archive completeness is mandatory on load.
# ---------------------------------------------------------------------------


def test_from_archive_dict_rejects_incomplete_snapshot() -> None:
    schema_only = DatabaseSchema(
        tables=(_posts_table(),),
        views=(),
        indexes=(),
        migrations=(),
        segments=(),
        data_copy_size=0,
    )
    with pytest.raises(SchemaContractError, match="without a COPY segment"):
        DatabaseSchema.from_archive_dict(schema_only.to_dict())


def test_from_archive_bytes_accepts_complete_archive() -> None:
    full = _schema()
    restored = DatabaseSchema.from_archive_bytes(full.to_canonical_bytes())
    assert restored == full


# ---------------------------------------------------------------------------
# from_dict rejects unknown / missing fields
# ---------------------------------------------------------------------------


def test_column_from_dict_rejects_unknown_fields() -> None:
    payload = _col("id").to_dict()
    payload["evil"] = 1
    with pytest.raises(SchemaContractError, match="unknown or missing"):
        ColumnSpec.from_dict(payload)


def test_table_from_dict_rejects_missing_fields() -> None:
    payload = TableSpec(
        name="posts",
        kind="base",
        columns=(_col("id"),),
        primary_key=("id",),
        unique_constraints=(),
    ).to_dict()
    del payload["kind"]
    with pytest.raises(SchemaContractError, match="unknown or missing"):
        TableSpec.from_dict(payload)


def test_index_from_dict_rejects_unknown_fields() -> None:
    payload = IndexSpec(
        name="idx",
        table="posts",
        method="btree",
        unique=False,
        nulls_not_distinct=False,
        keys=(IndexKeySpec("id", False, None),),
        included=(),
        predicate=None,
    ).to_dict()
    payload["evil"] = 1
    with pytest.raises(SchemaContractError, match="unknown or missing"):
        IndexSpec.from_dict(payload)


# ---------------------------------------------------------------------------
# MigrationSpec
# ---------------------------------------------------------------------------


def test_migration_accepts_null_and_valid_hash() -> None:
    assert MigrationSpec("0001_init.py", None, "2021-01-01").sha256 is None
    good = "a" * 64
    assert MigrationSpec("0002.py", good, "2021-01-02").sha256 == good


@pytest.mark.parametrize("bad", ["short", "z" * 64, "A" * 64, ""])
def test_migration_rejects_bad_hash(bad: str) -> None:
    with pytest.raises(SchemaContractError, match="sha256"):
        MigrationSpec("0001.py", bad, "2021-01-01")


# ---------------------------------------------------------------------------
# DatabaseSchema cross-reference + round-trip
# ---------------------------------------------------------------------------


def _segment(
    table: str, columns: tuple[str, ...], offset: int, comp: int, unc: int
) -> CopySegment:
    payload = b"x" * comp
    return CopySegment(
        table=table,
        columns=columns,
        offset=offset,
        compressed_size=comp,
        compressed_sha256=hashlib.sha256(payload).hexdigest(),
        uncompressed_size=unc,
        uncompressed_sha256=hashlib.sha256(b"y" * unc).hexdigest(),
        row_count=0,
        compression=COMPRESSION_ZLIB,
    )


_POSTS_COLUMNS = ("id", "title", "tags")


def _posts_table() -> TableSpec:
    return TableSpec(
        name="posts",
        kind="base",
        columns=(
            _col("id", "character varying(15)"),
            _col("title", "text", nullable=True),
            _col("tags", "text[]", nullable=True, default="'{}'::text[]"),
        ),
        primary_key=("id",),
        unique_constraints=(("title",),),
    )


def _schema() -> DatabaseSchema:
    view = ViewSpec(
        name="posts_public",
        definition="SELECT id, title FROM posts",
        depends_on=("posts",),
    )
    index = IndexSpec(
        name="idx_posts_title",
        table="posts",
        method="btree",
        unique=False,
        nulls_not_distinct=False,
        keys=(IndexKeySpec("title", descending=True, nulls_first=None),),
        included=("id",),
        predicate="(\"title\" <> ''::text)",
    )
    migration = MigrationSpec("0001_init.py", "a" * 64, "2021-01-01T00:00:00Z")
    segment = _segment("posts", _POSTS_COLUMNS, 0, 20, 40)
    return DatabaseSchema(
        tables=(_posts_table(),),
        views=(view,),
        indexes=(index,),
        migrations=(migration,),
        segments=(segment,),
        data_copy_size=20,
        source_postgres_version=160014,
    )


def test_database_schema_canonical_round_trip() -> None:
    schema = _schema()
    restored = DatabaseSchema.from_canonical_bytes(schema.to_canonical_bytes())
    assert restored == schema


def test_database_schema_canonical_is_stable() -> None:
    schema = _schema()
    assert schema.to_canonical_bytes() == schema.to_canonical_bytes()


def test_schema_rejects_bad_copy_settings() -> None:
    payload = _schema().to_dict()
    payload["copy_settings"]["format"] = "binary"
    with pytest.raises(SchemaContractError, match="copy_settings"):
        DatabaseSchema.from_dict(payload)


def test_schema_rejects_view_dep_on_unknown_relation() -> None:
    with pytest.raises(SchemaContractError, match="unknown relation"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(ViewSpec("v", "SELECT 1", ("ghost",)),),
            indexes=(),
            migrations=(),
            segments=(),
            data_copy_size=0,
        )


def test_schema_rejects_index_on_unknown_table() -> None:
    with pytest.raises(SchemaContractError, match="unknown table"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(),
            indexes=(
                IndexSpec(
                    name="idx",
                    table="ghost",
                    method="btree",
                    unique=False,
                    nulls_not_distinct=False,
                    keys=(IndexKeySpec("id", False, None),),
                    included=(),
                    predicate=None,
                ),
            ),
            migrations=(),
            segments=(),
            data_copy_size=0,
        )


def test_schema_rejects_index_key_on_unknown_column() -> None:
    with pytest.raises(SchemaContractError, match="unknown column"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(),
            indexes=(
                IndexSpec(
                    name="idx",
                    table="posts",
                    method="btree",
                    unique=False,
                    nulls_not_distinct=False,
                    keys=(IndexKeySpec("ghost", False, None),),
                    included=(),
                    predicate=None,
                ),
            ),
            migrations=(),
            segments=(),
            data_copy_size=0,
        )


def test_schema_rejects_segment_column_order_mismatch() -> None:
    with pytest.raises(SchemaContractError, match="column order"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(),
            indexes=(),
            migrations=(),
            segments=(_segment("posts", ("id", "tags", "title"), 0, 10, 10),),
            data_copy_size=10,
        )


def test_schema_rejects_duplicate_segments_for_table() -> None:
    with pytest.raises(SchemaContractError, match="multiple segments"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(),
            indexes=(),
            migrations=(),
            segments=(
                _segment("posts", _POSTS_COLUMNS, 0, 10, 10),
                _segment("posts", _POSTS_COLUMNS, 10, 10, 10),
            ),
            data_copy_size=20,
        )


def test_schema_rejects_segment_on_unknown_table() -> None:
    with pytest.raises(SchemaContractError, match="unknown table"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(),
            indexes=(),
            migrations=(),
            segments=(_segment("ghost", ("id",), 0, 10, 10),),
            data_copy_size=10,
        )


def test_assert_every_table_exported() -> None:
    # Schema-only snapshot (no segments) is allowed to construct...
    schema_only = DatabaseSchema(
        tables=(_posts_table(),),
        views=(),
        indexes=(),
        migrations=(),
        segments=(),
        data_copy_size=0,
    )
    # ...but a "complete" backup must cover every table exactly once.
    with pytest.raises(SchemaContractError, match="without a COPY segment"):
        schema_only.assert_every_table_exported()
    _schema().assert_every_table_exported()  # full schema passes


def test_schema_rejects_unknown_contract_version() -> None:
    with pytest.raises(SchemaContractError, match="contract version"):
        DatabaseSchema(
            tables=(_posts_table(),),
            views=(),
            indexes=(),
            migrations=(),
            segments=(),
            data_copy_size=0,
            contract_version=999,
        )


def test_schema_from_dict_rejects_unknown_fields() -> None:
    payload = _schema().to_dict()
    payload["evil"] = 1
    with pytest.raises(SchemaContractError, match="unknown or missing"):
        DatabaseSchema.from_dict(payload)


# ---------------------------------------------------------------------------
# Live fail-closed introspection
# ---------------------------------------------------------------------------

asyncpg = pytest.importorskip("asyncpg")


def _dsn() -> str:
    url = os.environ.get("PPBASE_TEST_DATABASE_URL")
    if url:
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return "postgresql://ppbase:ppbase@localhost:5433/postgres"


async def _connect() -> "asyncpg.Connection":
    try:
        return await asyncpg.connect(_dsn(), timeout=5)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")


async def _assert_in_txn(conn: "asyncpg.Connection", setup_sql: list[str]) -> None:
    """Create ``setup_sql`` objects in ``public`` then run the guard.

    Everything happens inside a transaction that is always rolled back, so no
    object survives the test.  Any exception from the guard propagates cleanly
    because the rollback in ``finally`` does not itself raise.
    """
    tr = conn.transaction()
    await tr.start()
    try:
        for stmt in setup_sql:
            await conn.execute(stmt)
        await assert_no_unmanaged_objects(conn)
    finally:
        await tr.rollback()


async def _introspect_in_txn(
    conn: "asyncpg.Connection",
    setup_sql: list[str],
    *,
    table_inventory: dict[str, str],
    view_inventory: frozenset[str] = frozenset(),
) -> DatabaseSchema:
    tr = conn.transaction()
    await tr.start()
    try:
        for stmt in setup_sql:
            await conn.execute(stmt)
        return await introspect_public_schema(
            conn,
            table_inventory=table_inventory,
            view_inventory=view_inventory,
        )
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_introspect_clean_schema_round_trips() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"contract_posts_{suffix}"
    idx = f"contract_idx_{suffix}"
    inc = f"contract_inc_{suffix}"
    view = f"contract_view_{suffix}"
    try:
        try:
            schema = await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "title TEXT NOT NULL DEFAULT '', "
                    "tags TEXT[])",
                    f'CREATE INDEX "{idx}" ON "{posts}" (title DESC)',
                    f'CREATE INDEX "{inc}" ON "{posts}" (title) INCLUDE (tags)',
                    f'CREATE VIEW "{view}" AS SELECT id, title FROM "{posts}"',
                ],
                table_inventory={posts: "base"},
                view_inventory=frozenset({view}),
            )
        except UnmanagedObjectError as exc:  # pragma: no cover - env dependent
            pytest.skip(f"public schema not clean for introspection test: {exc}")
        target = next(t for t in schema.tables if t.name == posts)
        assert target.primary_key == ("id",)
        title_col = next(c for c in target.columns if c.name == "title")
        assert title_col.default == "''::text"
        # DESC ordering preserved
        desc_idx = next(i for i in schema.indexes if i.name == idx)
        assert desc_idx.keys[0].descending is True
        # INCLUDE column preserved
        inc_idx = next(i for i in schema.indexes if i.name == inc)
        assert inc_idx.included == ("tags",)
        assert any(v.name == view for v in schema.views)
        restored = DatabaseSchema.from_canonical_bytes(schema.to_canonical_bytes())
        assert restored == schema
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_unknown_table() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    known = f"known_{suffix}"
    rogue = f"rogue_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="not a known PPBase"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{known}" (id VARCHAR(15) PRIMARY KEY)',
                    f'CREATE TABLE "{rogue}" (id VARCHAR(15) PRIMARY KEY)',
                ],
                table_inventory={known: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_unknown_view() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    rogue = f"rogueview_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="not a known PPBase"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY)',
                    f'CREATE VIEW "{rogue}" AS SELECT id FROM "{posts}"',
                ],
                table_inventory={posts: "base"},
                view_inventory=frozenset(),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_view_dep_on_foreign_schema() -> None:
    # A public view that reads from a relation in *another* schema passes the
    # text lexer (``ext.t`` looks like ``table.column``) but its real
    # pg_depend dependency is outside the public inventory and must be refused.
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    view = f"view_{suffix}"
    ext = f"ext_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="outside the PPBase contract"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE SCHEMA "{ext}"',
                    f'CREATE TABLE "{ext}"."secret" (id INT)',
                    f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY)',
                    f'CREATE VIEW "{view}" AS SELECT id FROM "{ext}"."secret"',
                ],
                table_inventory={posts: "base"},
                view_inventory=frozenset({view}),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_accepts_pg_prefixed_collection_names() -> None:
    # ``pg_`` is a legitimate PPBase collection/field prefix (only the *schema*
    # names pg_catalog/information_schema are reserved).  A whole schema of
    # pg_-prefixed relations, columns, partial-index predicates and a view must
    # round-trip through introspection untouched — the security gate is
    # namespace + OID + inventory based, never a name-prefix heuristic.
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"pg_posts_{suffix}"
    status = f"pg_status_{suffix}"
    idx = f"pg_idx_{suffix}"
    try:
        try:
            schema = await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "pg_views_count INTEGER NOT NULL DEFAULT 0, "
                    "pg_flag BOOLEAN)",
                    f'CREATE INDEX "{idx}" ON "{posts}" (pg_views_count) '
                    "WHERE pg_views_count > 0",
                    f'CREATE VIEW "{status}" AS '
                    f'SELECT id, pg_views_count FROM "{posts}"',
                ],
                table_inventory={posts: "base"},
                view_inventory=frozenset({status}),
            )
        except UnmanagedObjectError as exc:  # pragma: no cover - env dependent
            pytest.skip(f"public schema not clean for introspection test: {exc}")
        assert any(t.name == posts for t in schema.tables)
        assert any(v.name == status for v in schema.views)
        pidx = next(i for i in schema.indexes if i.name == idx)
        assert pidx.predicate is not None
        restored = DatabaseSchema.from_canonical_bytes(schema.to_canonical_bytes())
        assert restored == schema
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_index_predicate_external_function() -> None:
    # A partial-index predicate that calls a function from another schema
    # records a real pg_proc dependency (classid=pg_class, objid=index).  The
    # OID gate must refuse it even though the predicate text is otherwise a
    # plain boolean expression.
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    ext = f"ext_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="outside the PPBase contract"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE SCHEMA "{ext}"',
                    f'CREATE FUNCTION "{ext}".ispos(n integer) RETURNS boolean '
                    "LANGUAGE sql IMMUTABLE AS 'SELECT $1 > 0'",
                    f'CREATE TABLE "{posts}" '
                    "(id VARCHAR(15) PRIMARY KEY, n INTEGER)",
                    f'SET LOCAL search_path = "{ext}", public',
                    f'CREATE INDEX cidx ON "{posts}" (n) WHERE ispos(n)',
                    "SET LOCAL search_path = public",
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_index_predicate_external_collation() -> None:
    # A COLLATE clause referencing a collation in another schema slips past the
    # text lexer (``ext.mycoll`` looks like ``table.column``) but records a real
    # pg_collation dependency that the OID gate must refuse.
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    ext = f"ext_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="outside the PPBase contract"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE SCHEMA "{ext}"',
                    f'CREATE COLLATION "{ext}".mycoll (locale = "C")',
                    f'CREATE TABLE "{posts}" '
                    "(id VARCHAR(15) PRIMARY KEY, t TEXT)",
                    f'CREATE INDEX cidx ON "{posts}" (t) '
                    f'WHERE t > (\'a\' COLLATE "{ext}".mycoll)',
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_view_external_collation() -> None:
    # A view whose body applies a collation from another schema records a
    # pg_collation dependency on the rewrite rule; the view OID gate must
    # resolve pg_collation and refuse it.
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    view = f"view_{suffix}"
    ext = f"ext_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="outside the PPBase contract"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE SCHEMA "{ext}"',
                    f'CREATE COLLATION "{ext}".mycoll (locale = "C")',
                    f'CREATE TABLE "{posts}" '
                    "(id VARCHAR(15) PRIMARY KEY, t TEXT)",
                    f'CREATE VIEW "{view}" AS '
                    f'SELECT id, (t COLLATE "{ext}".mycoll) AS t FROM "{posts}"',
                ],
                table_inventory={posts: "base"},
                view_inventory=frozenset({view}),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_expression_index() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    try:
        with pytest.raises(SchemaContractError, match="expression index"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY, t TEXT)',
                    f'CREATE INDEX ON "{posts}" (lower(t))',
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_opclass_index() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    try:
        with pytest.raises(SchemaContractError, match="opclass"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY, t TEXT)',
                    f'CREATE INDEX ON "{posts}" (t text_pattern_ops)',
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_index_storage_parameters() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    try:
        with pytest.raises(SchemaContractError, match="storage parameters"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY, t TEXT)',
                    f'CREATE INDEX ON "{posts}" (t) WITH (fillfactor = 70)',
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


# NOTE: A non-default index tablespace is refused by the same guard exercised
# by ``test_introspect_rejects_index_storage_parameters`` (a sibling boolean
# check on ``ic.reltablespace <> 0`` right beside ``ic.reloptions``). It has no
# live test because provisioning a real tablespace requires a superuser, a
# server-accessible directory, and non-transactional ``CREATE TABLESPACE`` DDL,
# none of which are portable against the shared developer database. Naming the
# default tablespace explicitly stores ``reltablespace = 0``, so it cannot stand
# in for the non-default case.


@pytest.mark.asyncio
async def test_introspect_rejects_view_options() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    view = f"view_{suffix}"
    try:
        with pytest.raises(SchemaContractError, match="view options"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY, t TEXT)',
                    f'CREATE VIEW "{view}" WITH (security_barrier = true) AS '
                    f'SELECT id, t FROM "{posts}"',
                ],
                table_inventory={posts: "base"},
                view_inventory=frozenset({view}),
            )
    finally:
        await conn.close()


@pytest.mark.parametrize(
    "setup_sql,match",
    [
        (
            [
                "CREATE TABLE posts (id VARCHAR(15) PRIMARY KEY)",
                "CREATE FUNCTION f() RETURNS trigger AS $$ BEGIN RETURN NEW; END; $$ LANGUAGE plpgsql",
                "CREATE TRIGGER t BEFORE INSERT ON posts FOR EACH ROW EXECUTE FUNCTION f()",
            ],
            "trigger",
        ),
        (
            [
                "CREATE FUNCTION g() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql",
            ],
            "function",
        ),
        (
            ["CREATE SEQUENCE seq_test"],
            "relation",
        ),
        (
            [
                "CREATE TABLE a (id INT PRIMARY KEY)",
                "CREATE TABLE b (a_id INT REFERENCES a(id))",
            ],
            "constraint",
        ),
        (
            [
                "CREATE TABLE c (id INT, CONSTRAINT ck CHECK (id > 0))",
            ],
            "constraint",
        ),
        (
            ["CREATE TYPE mood AS ENUM ('a', 'b')"],
            "type",
        ),
        (
            [
                "CREATE TABLE secure (id INT)",
                "ALTER TABLE secure ENABLE ROW LEVEL SECURITY",
            ],
            "row_security",
        ),
        (
            [
                "CREATE TABLE part (id INT) PARTITION BY RANGE (id)",
            ],
            "relation",
        ),
        (
            [
                "CREATE MATERIALIZED VIEW mv AS SELECT 1 AS x",
            ],
            "relation",
        ),
        (
            [
                "CREATE TABLE ruled (id INT)",
                "CREATE RULE noins AS ON INSERT TO ruled DO INSTEAD NOTHING",
            ],
            "rule",
        ),
        (
            [
                "CREATE TABLE deferred (id INT PRIMARY KEY, e TEXT, "
                "CONSTRAINT uq UNIQUE (e) DEFERRABLE)",
            ],
            "deferrable_constraint",
        ),
    ],
)
@pytest.mark.asyncio
async def test_assert_no_unmanaged_objects_blocks(
    setup_sql: list[str], match: str
) -> None:
    conn = await _connect()
    try:
        with pytest.raises(UnmanagedObjectError, match=match):
            await _assert_in_txn(conn, setup_sql)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_assert_no_unmanaged_objects_passes_on_clean_schema() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    try:
        try:
            await _assert_in_txn(
                conn,
                [
                    f"CREATE TABLE clean_{suffix} "
                    "(id VARCHAR(15) PRIMARY KEY, title TEXT)"
                ],
            )
        except UnmanagedObjectError as exc:  # pragma: no cover - env dependent
            pytest.skip(f"public schema not clean: {exc}")
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Phase 1.2 (live): bidirectional reconciliation + unrepresented objects.
# ---------------------------------------------------------------------------


async def _introspect_direct(
    conn: "asyncpg.Connection",
    setup_sql: list[str],
    *,
    table_inventory: dict[str, str],
    view_inventory: frozenset[str] = frozenset(),
    canonical_tables: dict[str, TableSpec] | None = None,
) -> DatabaseSchema:
    tr = conn.transaction()
    await tr.start()
    try:
        for stmt in setup_sql:
            await conn.execute(stmt)
        return await introspect_public_schema(
            conn,
            table_inventory=table_inventory,
            view_inventory=view_inventory,
            canonical_tables=canonical_tables,
        )
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_introspect_rejects_missing_expected_table() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    present = f"present_{suffix}"
    absent = f"absent_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="missing from the database"):
            await _introspect_in_txn(
                conn,
                [f'CREATE TABLE "{present}" (id VARCHAR(15) PRIMARY KEY)'],
                table_inventory={present: "base", absent: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_missing_expected_view() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    absent = f"absentview_{suffix}"
    try:
        with pytest.raises(UnmanagedObjectError, match="views are missing"):
            await _introspect_in_txn(
                conn,
                [f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY)'],
                table_inventory={posts: "base"},
                view_inventory=frozenset({absent}),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_canonical_divergence() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        # Right name, wrong shape (column ``body`` instead of ``title``): must
        # not be auto-accepted against the canonical model.
        with pytest.raises(UnmanagedObjectError, match="diverge"):
            await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "body TEXT NOT NULL DEFAULT '')"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_accepts_canonical_match() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        try:
            schema = await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "title TEXT NOT NULL DEFAULT '')"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
        except UnmanagedObjectError as exc:  # pragma: no cover - env dependent
            pytest.skip(f"public schema not clean: {exc}")
        assert any(t.name == posts for t in schema.tables)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_accepts_reordered_columns() -> None:
    """Identical column set/definitions in a different physical order is fine.

    PPBase emits a collection's columns in ``_collections.schema`` field order,
    but a later ``ALTER TABLE ADD COLUMN`` appends physically at the end, so an
    authored table may carry a physical order that differs from the canonical
    (schema-JSON) order.  Reconciliation is by name, so this must seal.
    """
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    # Canonical order: id, title, note.
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
            ColumnSpec("note", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        try:
            # Live physical order: id, note, title (note before title).
            schema = await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "note TEXT NOT NULL DEFAULT '', "
                    "title TEXT NOT NULL DEFAULT '')"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
        except UnmanagedObjectError as exc:  # pragma: no cover - env dependent
            pytest.skip(f"public schema not clean: {exc}")
        # The sealed table keeps the live physical order, not the canonical one.
        table = next(t for t in schema.tables if t.name == posts)
        assert table.column_names == ("id", "note", "title")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_column_type_mismatch() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("count", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        with pytest.raises(UnmanagedObjectError, match="diverge"):
            await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "count INTEGER NOT NULL DEFAULT 0)"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_column_default_mismatch() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        with pytest.raises(UnmanagedObjectError, match="diverge"):
            # Live column carries no default; canonical expects ``''::text``.
            # Both are valid contract values, so this exercises the reconciler's
            # per-column default comparison rather than the introspection
            # allowlist that rejects non-reproducible defaults up front.
            await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "title TEXT NOT NULL)"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_column_nullability_mismatch() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        with pytest.raises(UnmanagedObjectError, match="diverge"):
            await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "title TEXT NULL DEFAULT '')"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_missing_column() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(
            ColumnSpec("id", "character varying(15)", False, None),
            ColumnSpec("title", "text", False, "''::text"),
        ),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        with pytest.raises(UnmanagedObjectError, match="diverge"):
            # Live table is missing the canonical ``title`` column.
            await _introspect_direct(
                conn,
                [f'CREATE TABLE "{posts}" (id VARCHAR(15) PRIMARY KEY)'],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_extra_column() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"posts_{suffix}"
    canonical = TableSpec(
        name=posts,
        kind="base",
        columns=(ColumnSpec("id", "character varying(15)", False, None),),
        primary_key=("id",),
        unique_constraints=(),
    )
    try:
        with pytest.raises(UnmanagedObjectError, match="diverge"):
            # Live table carries an extra ``title`` column absent from canonical.
            await _introspect_direct(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "title TEXT NOT NULL DEFAULT '')"
                ],
                table_inventory={posts: "base"},
                canonical_tables={posts: canonical},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_generated_column() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"gen_{suffix}"
    try:
        with pytest.raises(SchemaContractError, match="generated column"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    "n INTEGER, "
                    "m INTEGER GENERATED ALWAYS AS (n + 1) STORED)"
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_identity_column() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"ident_{suffix}"
    try:
        # An identity column also creates an owned sequence, so either the
        # sequence guard or the identity guard fires; both refuse the backup.
        with pytest.raises(UnmanagedObjectError, match="relation|identity"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                    "t TEXT)"
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_introspect_rejects_nonstandard_collation() -> None:
    conn = await _connect()
    suffix = uuid.uuid4().hex[:12]
    posts = f"coll_{suffix}"
    try:
        with pytest.raises(SchemaContractError, match="collation"):
            await _introspect_in_txn(
                conn,
                [
                    f'CREATE TABLE "{posts}" ('
                    "id VARCHAR(15) PRIMARY KEY, "
                    'name TEXT COLLATE "C")'
                ],
                table_inventory={posts: "base"},
            )
    finally:
        await conn.close()
