"""PostgreSQL integration tests for collection index ownership."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ppbase.db.schema_manager import (
    _managed_indexes,
    create_collection_table,
    update_collection_table,
)


def _collection(
    name: str,
    *,
    collection_type: str = "base",
    schema: list[dict] | None = None,
    indexes: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        type=collection_type,
        schema=schema or [],
        indexes=indexes or [],
        options={},
    )


def _long_collection_names() -> tuple[str, str]:
    unique_prefix = f"index_collision_{uuid.uuid4().hex}_"
    shared_prefix = unique_prefix.ljust(59, "x")[:59]
    return f"{shared_prefix}aaaa", f"{shared_prefix}bbbb"


@pytest.mark.asyncio
async def test_long_collection_names_receive_distinct_created_indexes(
    pg_url: str,
) -> None:
    """Managed indexes must not collide at PostgreSQL's 63-byte limit."""
    first_name, second_name = _long_collection_names()
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        await create_collection_table(engine, _collection(first_name))
        await create_collection_table(engine, _collection(second_name))

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT table_relation.relname, index_relation.relname "
                    "FROM pg_catalog.pg_index AS index_metadata "
                    "JOIN pg_catalog.pg_class AS index_relation "
                    "ON index_relation.oid = index_metadata.indexrelid "
                    "JOIN pg_catalog.pg_class AS table_relation "
                    "ON table_relation.oid = index_metadata.indrelid "
                    "JOIN pg_catalog.pg_namespace AS index_namespace "
                    "ON index_namespace.oid = index_relation.relnamespace "
                    "JOIN pg_catalog.pg_attribute AS table_attribute "
                    "ON table_attribute.attrelid = table_relation.oid "
                    "AND table_attribute.attname = 'created' "
                    "WHERE index_namespace.nspname = 'public' "
                    "AND table_relation.relname IN (:first_name, :second_name) "
                    "AND table_attribute.attnum = ANY(index_metadata.indkey)"
                ),
                {"first_name": first_name, "second_name": second_name},
            )
            rows = result.all()

        indexes_by_table = {table_name: index_name for table_name, index_name in rows}
        assert set(indexes_by_table) == {first_name, second_name}
        assert indexes_by_table[first_name] != indexes_by_table[second_name]
        assert all(
            len(index_name.encode("utf-8")) <= 63
            for index_name in indexes_by_table.values()
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{first_name}" CASCADE')
            )
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{second_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_existing_truncated_managed_index_remains_compatible(
    pg_url: str,
) -> None:
    """Keep an ambiguous legacy index while installing the bounded one."""
    table_name, _ = _long_collection_names()
    legacy_index_name = f"idx_{table_name}_created"
    stored_legacy_name = legacy_index_name.encode("utf-8")[:63].decode("utf-8")
    bounded_index_name = next(iter(_managed_indexes(_collection(table_name))))
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'CREATE TABLE "public"."{table_name}" ('
                    '"id" VARCHAR(15) PRIMARY KEY, '
                    '"created" TIMESTAMPTZ NOT NULL DEFAULT NOW(), '
                    '"updated" TIMESTAMPTZ NOT NULL DEFAULT NOW())'
                )
            )
            await connection.execute(
                text(
                    f'CREATE INDEX "{legacy_index_name}" '
                    f'ON "public"."{table_name}" ("created")'
                )
            )

        await create_collection_table(engine, _collection(table_name))

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT index_relation.relname "
                    "FROM pg_catalog.pg_index AS index_metadata "
                    "JOIN pg_catalog.pg_class AS index_relation "
                    "ON index_relation.oid = index_metadata.indexrelid "
                    "JOIN pg_catalog.pg_class AS table_relation "
                    "ON table_relation.oid = index_metadata.indrelid "
                    "JOIN pg_catalog.pg_attribute AS table_attribute "
                    "ON table_attribute.attrelid = table_relation.oid "
                    "AND table_attribute.attname = 'created' "
                    "WHERE table_relation.relname = :table_name "
                    "AND table_attribute.attnum = ANY(index_metadata.indkey)"
                ),
                {"table_name": table_name},
            )
            index_names = list(result.scalars())

        assert set(index_names) == {stored_legacy_name, bounded_index_name}
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_truncation_cannot_mask_auth_managed_indexes(
    pg_url: str,
) -> None:
    table_name, _ = _long_collection_names()
    collection = _collection(table_name, collection_type="auth")
    managed_indexes = _managed_indexes(collection)
    legacy_index_name = f"idx_{table_name}_created"
    stored_legacy_name = legacy_index_name.encode("utf-8")[:63].decode("utf-8")
    email_index = next(
        name
        for name, statement in managed_indexes.items()
        if '("email")' in statement
    )
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'CREATE TABLE "public"."{table_name}" ('
                    '"id" VARCHAR(15) PRIMARY KEY, '
                    '"created" TIMESTAMPTZ NOT NULL DEFAULT NOW(), '
                    '"updated" TIMESTAMPTZ NOT NULL DEFAULT NOW(), '
                    '"email" VARCHAR(255) NOT NULL DEFAULT \'\', '
                    '"email_visibility" BOOLEAN NOT NULL DEFAULT FALSE, '
                    '"verified" BOOLEAN NOT NULL DEFAULT FALSE, '
                    '"password_hash" TEXT NOT NULL DEFAULT \'\', '
                    '"token_key" VARCHAR(50) NOT NULL DEFAULT \'\')'
                )
            )
            await connection.execute(
                text(
                    f'CREATE INDEX "{legacy_index_name}" '
                    f'ON "public"."{table_name}" ("created")'
                )
            )

        await create_collection_table(engine, collection)

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT index_relation.relname, index_metadata.indisunique "
                    "FROM pg_catalog.pg_index AS index_metadata "
                    "JOIN pg_catalog.pg_class AS index_relation "
                    "ON index_relation.oid = index_metadata.indexrelid "
                    "JOIN pg_catalog.pg_class AS table_relation "
                    "ON table_relation.oid = index_metadata.indrelid "
                    "WHERE table_relation.relname = :table_name"
                ),
                {"table_name": table_name},
            )
            actual_indexes = dict(result.all())

        assert set(managed_indexes).issubset(actual_indexes)
        assert stored_legacy_name in actual_indexes
        assert actual_indexes[email_index] is True
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_index_sync_refuses_to_drop_another_tables_index(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    owner_table = f"index_owner_{suffix}"
    target_table = f"index_target_{suffix}"
    shared_index = f"shared_index_{suffix}"
    engine = create_async_engine(pg_url, poolclass=NullPool)
    old_collection = _collection(target_table)
    old_collection.indexes = [
        f'CREATE INDEX "{shared_index}" '
        f'ON "{target_table}" ("created")'
    ]

    try:
        await create_collection_table(engine, _collection(owner_table))
        await create_collection_table(engine, _collection(target_table))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    f'CREATE INDEX "{shared_index}" '
                    f'ON "public"."{owner_table}" ("created")'
                )
            )

        with pytest.raises(ValueError, match=owner_table):
            await update_collection_table(
                engine,
                old_collection,
                _collection(target_table),
            )

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT table_relation.relname "
                    "FROM pg_catalog.pg_index AS index_metadata "
                    "JOIN pg_catalog.pg_class AS index_relation "
                    "ON index_relation.oid = index_metadata.indexrelid "
                    "JOIN pg_catalog.pg_class AS table_relation "
                    "ON table_relation.oid = index_metadata.indrelid "
                    "JOIN pg_catalog.pg_namespace AS index_namespace "
                    "ON index_namespace.oid = index_relation.relnamespace "
                    "WHERE index_namespace.nspname = 'public' "
                    "AND index_relation.relname = :index_name"
                ),
                {"index_name": shared_index},
            )
            assert result.scalar_one() == owner_table
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{owner_table}" CASCADE')
            )
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{target_table}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_table_rename_replaces_only_its_public_managed_index(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    old_name = f"rename_old_{suffix}"
    new_name = f"rename_new_{suffix}"
    old_index = f"idx_{old_name}_created"
    new_index = f"idx_{new_name}_created"
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        await create_collection_table(engine, _collection(old_name))
        await update_collection_table(
            engine,
            _collection(old_name),
            _collection(new_name),
        )

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT index_relation.relname, table_relation.relname "
                    "FROM pg_catalog.pg_index AS index_metadata "
                    "JOIN pg_catalog.pg_class AS index_relation "
                    "ON index_relation.oid = index_metadata.indexrelid "
                    "JOIN pg_catalog.pg_class AS table_relation "
                    "ON table_relation.oid = index_metadata.indrelid "
                    "JOIN pg_catalog.pg_namespace AS index_namespace "
                    "ON index_namespace.oid = index_relation.relnamespace "
                    "WHERE index_namespace.nspname = 'public' "
                    "AND index_relation.relname IN (:old_index, :new_index)"
                ),
                {"old_index": old_index, "new_index": new_index},
            )
            assert result.all() == [(new_index, new_name)]
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{old_name}" CASCADE')
            )
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{new_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_index_relation_cannot_silently_mask_managed_index(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    table_name = f"relation_target_{suffix}"
    conflicting_relation = f"idx_{table_name}_created"
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'CREATE TABLE "public"."{conflicting_relation}" (id INT)')
            )

        with pytest.raises(ValueError, match="not an index"):
            await create_collection_table(engine, _collection(table_name))

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT to_regclass(:target), to_regclass(:conflict)"
                ),
                {
                    "target": f"public.{table_name}",
                    "conflict": f"public.{conflicting_relation}",
                },
            )
            target, conflict = result.one()
            assert target is None
            assert conflict is not None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
            await connection.execute(
                text(
                    f'DROP TABLE IF EXISTS "public".'
                    f'"{conflicting_relation}" CASCADE'
                )
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_unquoted_custom_index_name_uses_postgres_case_folding(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    table_name = f"custom_case_{suffix}"
    declared_index = f"MixedCaseIndex_{suffix}"
    stored_index = declared_index.lower()
    old_collection = _collection(
        table_name,
        indexes=[
            f'CREATE INDEX {declared_index} '
            f'ON "{table_name}" ("created")'
        ],
    )
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        await create_collection_table(engine, old_collection)
        await update_collection_table(
            engine,
            old_collection,
            _collection(table_name),
        )

        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT to_regclass(:index_name)"),
                {"index_name": f"public.{stored_index}"},
            )
            assert result.scalar_one_or_none() is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_base_to_auth_rejects_custom_managed_index_collision(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    table_name = f"auth_collision_{suffix}"
    email_index = f"idx_{table_name}_email"
    custom_statement = (
        f'CREATE INDEX "{email_index}" '
        f'ON "{table_name}" ("created")'
    )
    old_collection = _collection(
        table_name,
        indexes=[custom_statement],
    )
    new_collection = _collection(
        table_name,
        collection_type="auth",
        indexes=[custom_statement],
    )
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        await create_collection_table(engine, old_collection)

        with pytest.raises(ValueError, match="PPBase-managed index"):
            await update_collection_table(
                engine,
                old_collection,
                new_collection,
            )

        async with engine.connect() as connection:
            columns = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table_name"
                ),
                {"table_name": table_name},
            )
            owner = await connection.execute(
                text(
                    "SELECT table_relation.relname "
                    "FROM pg_catalog.pg_index AS index_metadata "
                    "JOIN pg_catalog.pg_class AS index_relation "
                    "ON index_relation.oid = index_metadata.indexrelid "
                    "JOIN pg_catalog.pg_class AS table_relation "
                    "ON table_relation.oid = index_metadata.indrelid "
                    "WHERE index_relation.relname = :index_name"
                ),
                {"index_name": email_index},
            )
            assert "email" not in set(columns.scalars())
            assert owner.scalar_one() == table_name
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_indexed_field_rejects_custom_managed_name_collision(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    table_name = f"field_collision_{suffix}"
    field_index = f"idx_{table_name}_tags"
    custom_statement = (
        f'CREATE INDEX "{field_index}" '
        f'ON "{table_name}" ("created")'
    )
    old_collection = _collection(
        table_name,
        indexes=[custom_statement],
    )
    new_collection = _collection(
        table_name,
        schema=[{
            "id": "field_tags",
            "name": "tags",
            "type": "select",
            "options": {"maxSelect": 2},
        }],
        indexes=[custom_statement],
    )
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        await create_collection_table(engine, old_collection)

        with pytest.raises(ValueError, match="PPBase-managed index"):
            await update_collection_table(
                engine,
                old_collection,
                new_collection,
            )

        async with engine.connect() as connection:
            columns = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table_name"
                ),
                {"table_name": table_name},
            )
            assert "tags" not in set(columns.scalars())
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_collection_names_over_63_bytes_are_rejected_before_postgres(
    pg_url: str,
) -> None:
    shared_prefix = "z" * 63
    first_name = f"{shared_prefix}x"
    second_name = f"{shared_prefix}y"
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        with pytest.raises(ValueError, match="64 bytes"):
            await create_collection_table(engine, _collection(first_name))
        with pytest.raises(ValueError, match="64 bytes"):
            await create_collection_table(engine, _collection(second_name))

        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": f"public.{shared_prefix}"},
            )
            assert result.scalar_one_or_none() is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{shared_prefix}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_field_names_over_63_bytes_are_rejected_before_postgres(
    pg_url: str,
) -> None:
    table_name = f"long_field_{uuid.uuid4().hex[:12]}"
    collection = _collection(
        table_name,
        schema=[{
            "id": "too_long_field",
            "name": "é" * 32,
            "type": "text",
            "options": {},
        }],
    )
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        with pytest.raises(ValueError, match="Field name.*64 bytes"):
            await create_collection_table(engine, collection)

        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT to_regclass(:table_name)"),
                {"table_name": f"public.{table_name}"},
            )
            assert result.scalar_one_or_none() is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrong_index_sql",
    [
        (
            'CREATE UNIQUE INDEX "{index_name}" '
            'ON "public"."{table_name}" ("updated") '
            'WHERE "updated" IS NOT NULL'
        ),
        (
            'CREATE INDEX "{index_name}" '
            'ON "public"."{table_name}" USING hash ("created")'
        ),
    ],
)
async def test_existing_managed_index_must_match_its_full_definition(
    pg_url: str,
    wrong_index_sql: str,
) -> None:
    """A matching name/owner must not hide a semantically wrong index."""
    suffix = uuid.uuid4().hex[:12]
    table_name = f"wrong_index_{suffix}"
    collection = _collection(table_name)
    index_name = next(iter(_managed_indexes(collection)))
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        async with engine.begin() as connection:
            await connection.execute(text(
                f'CREATE TABLE "public"."{table_name}" ('
                '"id" VARCHAR(15) PRIMARY KEY, '
                '"created" TIMESTAMPTZ NOT NULL DEFAULT NOW(), '
                '"updated" TIMESTAMPTZ NOT NULL DEFAULT NOW())'
            ))
            await connection.execute(text(wrong_index_sql.format(
                index_name=index_name,
                table_name=table_name,
            )))

        with pytest.raises(ValueError, match="different definition"):
            await create_collection_table(engine, collection)

        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT pg_get_indexdef(index_relation.oid) "
                    "FROM pg_catalog.pg_class AS index_relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = index_relation.relnamespace "
                    "WHERE namespace.nspname = 'public' "
                    "AND index_relation.relname = :index_name"
                ),
                {"index_name": index_name},
            )
            actual_definition = result.scalar_one()

        assert (
            'updated' in actual_definition
            or 'USING hash' in actual_definition
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{table_name}" CASCADE')
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_custom_collection_index_cannot_target_another_table(
    pg_url: str,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    target_table = f"custom_target_{suffix}"
    other_table = f"custom_other_{suffix}"
    index_name = f"custom_wrong_target_{suffix}"
    old_collection = _collection(target_table)
    new_collection = _collection(
        target_table,
        indexes=[
            f'CREATE INDEX "{index_name}" '
            f'ON "public"."{other_table}" ("created")'
        ],
    )
    engine = create_async_engine(pg_url, poolclass=NullPool)

    try:
        await create_collection_table(engine, _collection(other_table))
        await create_collection_table(engine, old_collection)

        with pytest.raises(ValueError, match="targets table"):
            await update_collection_table(engine, old_collection, new_collection)

        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT to_regclass(:index_name)"),
                {"index_name": f"public.{index_name}"},
            )
            assert result.scalar_one_or_none() is None
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{target_table}" CASCADE')
            )
            await connection.execute(
                text(f'DROP TABLE IF EXISTS "public"."{other_table}" CASCADE')
            )
        await engine.dispose()
