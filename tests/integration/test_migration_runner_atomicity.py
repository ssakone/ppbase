"""PostgreSQL integration tests for transactional application migrations.

Every test receives a newly-created database derived from ``pg_url``.  The
engine in this module is deliberately local and never touches PPBase's global
engine singleton or the database used by the API integration fixtures.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ppbase.db.system_tables import Base, CollectionRecord
from ppbase.services.database_preparation import (
    prepare_database,
    prepare_database_and_revert,
)
from ppbase.services.migration_snapshot import create_migration_snapshot
from ppbase.services.migration_runner import (
    MigrationLockError,
    get_migration_status,
    migration_lock,
    run_migrations_down,
    run_migrations_up,
)


pytestmark = pytest.mark.asyncio


def _sqlstate(exc: DBAPIError) -> str | None:
    """Extract a PostgreSQL SQLSTATE from SQLAlchemy/asyncpg wrappers."""
    current: Any = exc.orig
    for _ in range(4):
        state = getattr(current, "sqlstate", None) or getattr(
            current,
            "pgcode",
            None,
        )
        if state:
            return str(state)
        current = getattr(current, "__cause__", None)
        if current is None:
            break
    return None


@pytest_asyncio.fixture
async def isolated_migration_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """Yield an empty temporary PostgreSQL database with only system tables.

    Creating databases requires a cluster-level privilege.  Developer-managed
    PostgreSQL users may intentionally lack it, in which case these destructive
    isolation tests are skipped rather than falling back to a shared database.
    """
    database_name = f"ppbase_migrations_{uuid.uuid4().hex[:16]}"
    admin_url = make_url(pg_url)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    target_engine: AsyncEngine | None = None
    database_created = False

    try:
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(
                    text(f'CREATE DATABASE "{database_name}" TEMPLATE template0')
                )
            database_created = True
        except DBAPIError as exc:
            if _sqlstate(exc) == "42501":
                pytest.skip(
                    "PostgreSQL user cannot CREATE DATABASE; isolated migration "
                    "atomicity tests require CREATEDB privilege."
                )
            raise

        target_url = admin_url.set(database=database_name)
        target_engine = create_async_engine(target_url, poolclass=NullPool)
        async with target_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        yield target_engine
    finally:
        if target_engine is not None:
            await target_engine.dispose()

        try:
            if database_created:
                async with admin_engine.connect() as connection:
                    await connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) "
                            "FROM pg_stat_activity "
                            "WHERE datname = :database_name "
                            "AND pid <> pg_backend_pid()"
                        ),
                        {"database_name": database_name},
                    )
                    await connection.execute(
                        text(f'DROP DATABASE IF EXISTS "{database_name}"')
                    )
        finally:
            await admin_engine.dispose()


def _write_migration(directory: Path, filename: str, source: str) -> str:
    """Create a native Python migration in a pytest-owned temp directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        textwrap.dedent(source).lstrip(),
        encoding="utf-8",
    )
    return filename


async def _scalar(
    engine: AsyncEngine,
    statement: str,
    params: dict[str, Any] | None = None,
) -> Any:
    async with engine.connect() as connection:
        result = await connection.execute(text(statement), params or {})
        return result.scalar()


async def _history(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text('SELECT "file" FROM "_migrations" ORDER BY "file"')
        )
        return list(result.scalars().all())


async def _collection_exists(engine: AsyncEngine, collection_id: str) -> bool:
    value = await _scalar(
        engine,
        'SELECT EXISTS(SELECT 1 FROM "_collections" WHERE "id" = :id)',
        {"id": collection_id},
    )
    return bool(value)


async def _relation_exists(engine: AsyncEngine, relation_name: str) -> bool:
    value = await _scalar(
        engine,
        "SELECT to_regclass(:relation_name)",
        {"relation_name": f"public.{relation_name}"},
    )
    return value is not None


async def _column_names(engine: AsyncEngine, table_name: str) -> set[str]:
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :table_name"
            ),
            {"table_name": table_name},
        )
        return set(result.scalars().all())


async def test_create_migration_commits_metadata_ddl_and_history(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000001_created_atomic_create.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "atomiccreate01",
                "name": "atomic_create",
                "type": "base",
                "system": False,
                "schema": [{
                    "id": "atomic_title",
                    "name": "title",
                    "type": "text",
                    "required": True,
                    "options": {},
                }],
                "indexes": [],
                "options": {},
            })


        async def down(app):
            await app.delete_collection("atomiccreate01")
        """,
    )

    applied = await run_migrations_up(isolated_migration_engine, tmp_path)

    assert applied == [filename]
    assert await _collection_exists(isolated_migration_engine, "atomiccreate01")
    assert await _relation_exists(isolated_migration_engine, "atomic_create")
    assert "title" in await _column_names(
        isolated_migration_engine,
        "atomic_create",
    )
    assert await _history(isolated_migration_engine) == [filename]


async def test_failed_create_rolls_back_metadata_ddl_and_history(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000002_created_rollback_create.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "rollbackcreate1",
                "name": "rollback_create",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })
            await app.execute_sql(
                'INSERT INTO "rollback_create" ("id") VALUES (:id)',
                {"id": "temporaryrow01"},
            )
            raise RuntimeError("fail after collection DDL")


        async def down(app):
            await app.delete_collection("rollbackcreate1")
        """,
    )

    with pytest.raises(RuntimeError, match="fail after collection DDL"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert not await _collection_exists(
        isolated_migration_engine,
        "rollbackcreate1",
    )
    assert not await _relation_exists(isolated_migration_engine, "rollback_create")
    assert filename not in await _history(isolated_migration_engine)


async def test_migration_filenotfounderror_propagates_with_atomic_rollback(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000002_file_not_found.py",
        """
        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "filenotfound_rollback" ("id" integer)'
            )
            raise FileNotFoundError("migration-owned file is missing")


        async def down(app):
            pass
        """,
    )

    with pytest.raises(
        FileNotFoundError,
        match="migration-owned file is missing",
    ):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert not await _relation_exists(
        isolated_migration_engine,
        "filenotfound_rollback",
    )
    assert filename not in await _history(isolated_migration_engine)


async def test_non_transactional_raw_sql_fails_without_autocommit_or_history(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000002_non_transactional_sql.py",
        """
        async def up(app):
            await app.execute_sql("VACUUM")


        async def down(app):
            pass
        """,
    )

    with pytest.raises(DBAPIError):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert filename not in await _history(isolated_migration_engine)


async def test_previous_migration_stays_committed_when_next_alter_fails(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    first_filename = _write_migration(
        tmp_path,
        "1700000003_created_nminus_base.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "committedbase1",
                "name": "nminus_base",
                "type": "base",
                "schema": [{
                    "id": "nminus_title",
                    "name": "title",
                    "type": "text",
                    "required": False,
                    "options": {},
                }],
                "indexes": [],
                "options": {},
            })


        async def down(app):
            await app.delete_collection("committedbase1")
        """,
    )
    second_filename = _write_migration(
        tmp_path,
        "1700000004_updated_nminus_base.py",
        """
        async def up(app):
            await app.update_collection("committedbase1", {
                "schema": [
                    {
                        "id": "nminus_title",
                        "name": "title",
                        "type": "text",
                        "required": False,
                        "options": {},
                    },
                    {
                        "id": "nminus_subtitle",
                        "name": "subtitle",
                        "type": "text",
                        "required": False,
                        "options": {},
                    },
                ],
            })
            raise RuntimeError("fail after alter table")


        async def down(app):
            pass
        """,
    )

    with pytest.raises(RuntimeError, match="fail after alter table"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert await _collection_exists(isolated_migration_engine, "committedbase1")
    assert await _relation_exists(isolated_migration_engine, "nminus_base")
    columns = await _column_names(isolated_migration_engine, "nminus_base")
    assert "title" in columns
    assert "subtitle" not in columns
    assert await _history(isolated_migration_engine) == [first_filename]
    assert second_filename not in await _history(isolated_migration_engine)

    stored_schema = await _scalar(
        isolated_migration_engine,
        'SELECT "schema" FROM "_collections" WHERE "id" = :id',
        {"id": "committedbase1"},
    )
    assert [field["name"] for field in stored_schema] == ["title"]


async def test_failed_type_change_restores_original_column_and_data(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    first_filename = _write_migration(
        tmp_path,
        "1700000004_created_type_rollback.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "typerollback01",
                "name": "type_rollback",
                "type": "base",
                "schema": [{
                    "id": "stable_value_field",
                    "name": "value",
                    "type": "text",
                    "required": False,
                    "options": {},
                }],
                "indexes": [],
                "options": {},
            })
            await app.execute_sql(
                'INSERT INTO "type_rollback" ("id", "value") VALUES (:id, :value)',
                {"id": "stablevalue001", "value": "keep-me"},
            )


        async def down(app):
            await app.delete_collection("typerollback01")
        """,
    )
    second_filename = _write_migration(
        tmp_path,
        "1700000005_updated_type_rollback.py",
        """
        async def up(app):
            await app.update_collection("typerollback01", {
                "schema": [{
                    "id": "stable_value_field",
                    "name": "value",
                    "type": "number",
                    "required": False,
                    "options": {"onlyInt": True},
                }],
            })
            raise RuntimeError("fail after destructive type change")


        async def down(app):
            pass
        """,
    )

    with pytest.raises(RuntimeError, match="destructive type change"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert await _scalar(
        isolated_migration_engine,
        'SELECT "value" FROM "type_rollback" WHERE "id" = :id',
        {"id": "stablevalue001"},
    ) == "keep-me"
    assert await _scalar(
        isolated_migration_engine,
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'type_rollback' "
        "AND column_name = 'value'",
    ) == "text"
    assert await _history(isolated_migration_engine) == [first_filename]
    assert second_filename not in await _history(isolated_migration_engine)


async def test_failed_down_rolls_back_drop_metadata_and_history_removal(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000005_created_down_failure.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "downfailure001",
                "name": "down_failure",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })


        async def down(app):
            await app.delete_collection("downfailure001")
            raise RuntimeError("fail after down drop")
        """,
    )
    assert await run_migrations_up(isolated_migration_engine, tmp_path) == [filename]

    with pytest.raises(RuntimeError, match="fail after down drop"):
        await run_migrations_down(isolated_migration_engine, tmp_path)

    assert await _collection_exists(isolated_migration_engine, "downfailure001")
    assert await _relation_exists(isolated_migration_engine, "down_failure")
    assert await _history(isolated_migration_engine) == [filename]


async def test_successful_down_removes_schema_metadata_and_history(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000005_created_down_success.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "downsuccess001",
                "name": "down_success",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })


        async def down(app):
            await app.delete_collection("downsuccess001")
        """,
    )
    assert await run_migrations_up(isolated_migration_engine, tmp_path) == [filename]

    assert await prepare_database_and_revert(
        isolated_migration_engine,
        tmp_path,
    ) == [filename]
    assert not await _collection_exists(
        isolated_migration_engine,
        "downsuccess001",
    )
    assert not await _relation_exists(isolated_migration_engine, "down_success")
    assert await _history(isolated_migration_engine) == []


async def test_concurrent_runners_apply_each_file_only_once(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000006_created_concurrent.py",
        """
        import asyncio


        async def up(app):
            await asyncio.sleep(0.25)
            await app.create_collection({
                "id": "concurrent001",
                "name": "concurrent_collection",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })


        async def down(app):
            await app.delete_collection("concurrent001")
        """,
    )

    results = await asyncio.wait_for(
        asyncio.gather(
            run_migrations_up(
                isolated_migration_engine,
                tmp_path,
                lock_timeout_seconds=5,
            ),
            run_migrations_up(
                isolated_migration_engine,
                tmp_path,
                lock_timeout_seconds=5,
            ),
        ),
        timeout=10,
    )

    assert sorted(len(result) for result in results) == [0, 1]
    assert [item for result in results for item in result] == [filename]
    assert await _collection_exists(isolated_migration_engine, "concurrent001")
    assert await _relation_exists(
        isolated_migration_engine,
        "concurrent_collection",
    )
    assert await _history(isolated_migration_engine) == [filename]


async def test_lock_timeout_is_clear_and_lock_is_released_after_context(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    async with migration_lock(isolated_migration_engine):
        with pytest.raises(MigrationLockError, match="Timed out"):
            await run_migrations_up(
                isolated_migration_engine,
                tmp_path,
                lock_timeout_seconds=0.05,
            )

    assert await run_migrations_up(
        isolated_migration_engine,
        tmp_path,
        lock_timeout_seconds=1,
    ) == []


async def test_view_query_update_replaces_view_in_same_migration_transaction(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    first_filename = _write_migration(
        tmp_path,
        "1700000007_created_view_objects.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "viewsource0001",
                "name": "view_source",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })
            await app.execute_sql(
                'INSERT INTO "view_source" ("id") VALUES (:id)',
                {"id": "viewrow0000001"},
            )
            await app.create_collection({
                "id": "viewsummary001",
                "name": "view_summary",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {
                    "query": (
                        'SELECT "id", 1::integer AS "version" '
                        'FROM "view_source"'
                    ),
                },
            })


        async def down(app):
            await app.delete_collection("viewsummary001")
            await app.delete_collection("viewsource0001")
        """,
    )
    second_filename = _write_migration(
        tmp_path,
        "1700000008_updated_view_query.py",
        """
        async def up(app):
            await app.update_collection("viewsummary001", {
                "options": {
                    "query": (
                        'SELECT "id", 2::integer AS "version" '
                        'FROM "view_source"'
                    ),
                },
            })


        async def down(app):
            await app.update_collection("viewsummary001", {
                "options": {
                    "query": (
                        'SELECT "id", 1::integer AS "version" '
                        'FROM "view_source"'
                    ),
                },
            })
        """,
    )

    applied = await run_migrations_up(isolated_migration_engine, tmp_path)

    assert applied == [first_filename, second_filename]
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "version" FROM "view_summary"',
    ) == 2
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "options" ->> \'query\' FROM "_collections" WHERE "id" = :id',
        {"id": "viewsummary001"},
    ) == 'SELECT "id", 2::integer AS "version" FROM "view_source"'
    assert await _history(isolated_migration_engine) == [
        first_filename,
        second_filename,
    ]


async def test_parent_view_update_preserves_dependent_view_and_metadata(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    first_filename = _write_migration(
        tmp_path,
        "1700000007_created_dependent_views.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "depviewsource01",
                "name": "dependent_view_source",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })
            await app.execute_sql(
                'INSERT INTO "dependent_view_source" ("id") VALUES (:id)',
                {"id": "depviewrow00001"},
            )
            await app.create_collection({
                "id": "depviewparent1",
                "name": "dependent_parent_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {
                    "query": (
                        'SELECT "id", 1::integer AS "version" '
                        'FROM "dependent_view_source"'
                    ),
                },
            })
            await app.create_collection({
                "id": "depviewchild01",
                "name": "dependent_child_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {
                    "query": (
                        'SELECT "id", "version" + 10 AS "child_version" '
                        'FROM "dependent_parent_view"'
                    ),
                },
            })


        async def down(app):
            await app.delete_collection("depviewchild01")
            await app.delete_collection("depviewparent1")
            await app.delete_collection("depviewsource01")
        """,
    )
    second_filename = _write_migration(
        tmp_path,
        "1700000008_updated_dependent_parent_view.py",
        """
        async def up(app):
            await app.update_collection("depviewparent1", {
                "options": {
                    "query": (
                        'SELECT "id", 2::integer AS "version" '
                        'FROM "dependent_view_source"'
                    ),
                },
            })


        async def down(app):
            await app.update_collection("depviewparent1", {
                "options": {
                    "query": (
                        'SELECT "id", 1::integer AS "version" '
                        'FROM "dependent_view_source"'
                    ),
                },
            })
        """,
    )

    applied = await run_migrations_up(isolated_migration_engine, tmp_path)

    assert applied == [first_filename, second_filename]
    assert await _relation_exists(
        isolated_migration_engine,
        "dependent_child_view",
    )
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "child_version" FROM "dependent_child_view"',
    ) == 12
    assert await _collection_exists(isolated_migration_engine, "depviewchild01")

    incompatible_filename = _write_migration(
        tmp_path,
        "1700000009_updated_incompatible_parent_view.py",
        """
        async def up(app):
            await app.update_collection("depviewparent1", {
                "options": {
                    "query": (
                        'SELECT "id", 3::integer AS "replacement" '
                        'FROM "dependent_view_source"'
                    ),
                },
            })


        async def down(app):
            pass
        """,
    )

    with pytest.raises(ValueError, match="dependent_child_view") as exc_info:
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert isinstance(exc_info.value.__cause__, DBAPIError)
    assert _sqlstate(exc_info.value.__cause__) == "42P16"
    assert incompatible_filename not in await _history(isolated_migration_engine)
    assert await _relation_exists(
        isolated_migration_engine,
        "dependent_child_view",
    )
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "child_version" FROM "dependent_child_view"',
    ) == 12
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "options" ->> \'query\' FROM "_collections" WHERE "id" = :id',
        {"id": "depviewparent1"},
    ) == (
        'SELECT "id", 2::integer AS "version" '
        'FROM "dependent_view_source"'
    )


async def test_invalid_view_rolls_back_collection_metadata_and_history(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000008_created_invalid_view.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "invalidview001",
                "name": "invalid_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {"query": "SELECT missing_column FROM missing_table"},
            })


        async def down(app):
            await app.delete_collection("invalidview001")
        """,
    )

    with pytest.raises(ValueError, match="Invalid view query"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert not await _collection_exists(isolated_migration_engine, "invalidview001")
    assert not await _relation_exists(isolated_migration_engine, "invalid_view")
    assert filename not in await _history(isolated_migration_engine)


async def test_database_preparation_bootstraps_users_before_migrations_and_is_idempotent(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000009_updated_users.py",
        """
        async def up(app):
            await app.update_collection("_pb_users_auth_", {
                "schema": [
                    {
                        "id": "users_name",
                        "name": "name",
                        "type": "text",
                        "required": False,
                        "options": {"max": 255},
                    },
                    {
                        "id": "users_avatar",
                        "name": "avatar",
                        "type": "file",
                        "required": False,
                        "options": {"maxSelect": 1},
                    },
                    {
                        "id": "startup_marker",
                        "name": "startup_marker",
                        "type": "text",
                        "required": False,
                        "options": {},
                    },
                ],
            })


        async def down(app):
            pass
        """,
    )

    first = await prepare_database(
        isolated_migration_engine,
        tmp_path,
        apply_migrations=True,
    )
    second = await prepare_database(
        isolated_migration_engine,
        tmp_path,
        apply_migrations=True,
    )

    assert first == [filename]
    assert second == []
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "id" FROM "_collections" WHERE "name" = \'users\'',
    ) == "_pb_users_auth_"
    assert "startup_marker" in await _column_names(
        isolated_migration_engine,
        "users",
    )
    assert await _history(isolated_migration_engine) == [filename]


async def test_legacy_external_auth_collection_does_not_rename_oauth_table(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    async with AsyncSession(
        bind=isolated_migration_engine,
        expire_on_commit=False,
    ) as session:
        async with session.begin():
            session.add(
                CollectionRecord(
                    id="legacyextern001",
                    name="_external_auths",
                    type="base",
                    system=True,
                    schema=[
                        {"name": "collectionRef", "type": "text", "options": {}},
                        {"name": "recordRef", "type": "text", "options": {}},
                        {"name": "provider", "type": "text", "options": {}},
                        {"name": "providerId", "type": "text", "options": {}},
                    ],
                    indexes=[],
                    options={},
                    created=now,
                    updated=now,
                )
            )

    await prepare_database(
        isolated_migration_engine,
        tmp_path,
        apply_migrations=False,
    )

    assert await _scalar(
        isolated_migration_engine,
        'SELECT "name" FROM "_collections" WHERE "id" = :id',
        {"id": "legacyextern001"},
    ) == "_externalAuths"
    snake_columns = await _column_names(
        isolated_migration_engine,
        "_external_auths",
    )
    camel_columns = await _column_names(
        isolated_migration_engine,
        "_externalAuths",
    )
    assert {"collection_id", "record_id", "provider_id"}.issubset(snake_columns)
    assert {"collectionRef", "recordRef", "providerId"}.issubset(camel_columns)


async def test_lifespan_finishes_bootstrap_and_migrations_before_serving(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path / "migrations",
        "1700000010_updated_users_startup.py",
        """
        async def up(app):
            await app.update_collection("_pb_users_auth_", {
                "schema": [
                    {
                        "id": "users_name",
                        "name": "name",
                        "type": "text",
                        "required": False,
                        "options": {"max": 255},
                    },
                    {
                        "id": "users_avatar",
                        "name": "avatar",
                        "type": "file",
                        "required": False,
                        "options": {"maxSelect": 1},
                    },
                    {
                        "id": "lifespan_marker",
                        "name": "lifespan_marker",
                        "type": "bool",
                        "required": False,
                        "options": {},
                    },
                ],
            })


        async def down(app):
            pass
        """,
    )
    database_url = isolated_migration_engine.url.render_as_string(
        hide_password=False
    )
    migrations_dir = tmp_path / "migrations"
    data_dir = tmp_path / "data"
    script = textwrap.dedent(
        f"""
        import asyncio
        from ppbase.app import create_app
        from ppbase.config import Settings

        async def main():
            settings = Settings(
                database_url={database_url!r},
                migrations_dir={str(migrations_dir)!r},
                data_dir={str(data_dir)!r},
                jwt_secret="startup-test-secret",
                apply_migrations_on_start=True,
                generate_migrations=False,
            )
            app = create_app(settings)
            async with app.router.lifespan_context(app):
                pass

        asyncio.run(main())
        """
    )

    async def _run_startup() -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            cwd=Path(__file__).resolve().parents[2],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output_bytes, _ = await process.communicate()
        return process.returncode or 0, output_bytes.decode("utf-8", errors="replace")

    first_code, first_output = await _run_startup()
    second_code, second_output = await _run_startup()

    assert first_code == 0, first_output
    assert second_code == 0, second_output
    assert "No admin account found" in first_output
    assert "/_/setup?token=" in first_output
    assert await _scalar(
        isolated_migration_engine,
        'SELECT "id" FROM "_collections" WHERE "name" = \'users\'',
    ) == "_pb_users_auth_"
    assert "lifespan_marker" in await _column_names(
        isolated_migration_engine,
        "users",
    )
    assert await _history(isolated_migration_engine) == [filename]


async def test_snapshot_service_excludes_system_state_and_records_one_file(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    migrations_dir = tmp_path / "source_migrations"
    source_filename = _write_migration(
        migrations_dir,
        "1700000011_created_snapshot_objects.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "snapshotbase01",
                "name": "snapshot_base",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })
            await app.create_collection({
                "id": "snapshotview01",
                "name": "snapshot_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {
                    "query": 'SELECT "id", "created", "updated" FROM "snapshot_base"',
                },
            })


        async def down(app):
            await app.delete_collection("snapshotview01")
            await app.delete_collection("snapshotbase01")
        """,
    )
    assert await prepare_database(
        isolated_migration_engine,
        migrations_dir,
        apply_migrations=True,
    ) == [source_filename]

    snapshots_dir = tmp_path / "snapshots"
    snapshot_path = Path(
        await create_migration_snapshot(
            isolated_migration_engine,
            snapshots_dir,
        )
    )
    source = snapshot_path.read_text(encoding="utf-8")

    assert len(list(snapshots_dir.glob("*.py"))) == 1
    assert "_pb_users_auth_" in source
    assert "snapshot_base" in source
    assert "snapshot_view" in source
    assert source.index("snapshot_base") < source.index("snapshot_view")
    for system_name in ("_superusers", "_externalAuths", "_mfas", "_otps"):
        assert system_name not in source
    assert "clientSecret" not in source
    assert await _history(isolated_migration_engine) == [
        source_filename,
        snapshot_path.name,
    ]
    assert await run_migrations_up(
        isolated_migration_engine,
        snapshots_dir,
    ) == []


async def test_dashboard_generation_failure_rolls_back_collection_and_ddl(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.models.collection import CollectionCreate
    from ppbase.services import collection_service, migration_generator

    await prepare_database(
        isolated_migration_engine,
        tmp_path / "bootstrap_only",
        apply_migrations=False,
    )

    def _fail_generation(*_args, **_kwargs):
        raise RuntimeError("cannot write migration")

    monkeypatch.setattr(
        migration_generator,
        "generate_create_migration",
        _fail_generation,
    )

    async with AsyncSession(
        bind=isolated_migration_engine,
        expire_on_commit=False,
    ) as session:
        with pytest.raises(RuntimeError, match="cannot write migration"):
            await collection_service.create_collection(
                session,
                isolated_migration_engine,
                CollectionCreate(
                    id="dashboardfail01",
                    name="dashboard_generation_failure",
                    type="base",
                    schema=[],
                    indexes=[],
                    options={},
                ),
                auto_migrate=True,
                migrations_dir=str(tmp_path / "generated"),
            )

    assert not await _collection_exists(
        isolated_migration_engine,
        "dashboardfail01",
    )
    assert not await _relation_exists(
        isolated_migration_engine,
        "dashboard_generation_failure",
    )
    assert not list((tmp_path / "generated").glob("*.py"))


async def test_dashboard_producer_and_runner_share_the_same_migration_lock(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner must not observe a Dashboard file before its DB commit."""
    from ppbase.models.collection import CollectionCreate
    from ppbase.services import collection_service

    await prepare_database(
        isolated_migration_engine,
        tmp_path / "bootstrap_only",
        apply_migrations=False,
    )

    migrations_dir = tmp_path / "dashboard_migrations"
    published = asyncio.Event()
    allow_commit = asyncio.Event()
    original_commit = collection_service._commit_with_generated_migrations

    async def _pause_after_publication(*args: Any, **kwargs: Any) -> None:
        published.set()
        await allow_commit.wait()
        await original_commit(*args, **kwargs)

    monkeypatch.setattr(
        collection_service,
        "_commit_with_generated_migrations",
        _pause_after_publication,
    )

    async def _produce() -> None:
        async with AsyncSession(
            bind=isolated_migration_engine,
            expire_on_commit=False,
        ) as session:
            await collection_service.create_collection(
                session,
                isolated_migration_engine,
                CollectionCreate(
                    id="dashboardrace01",
                    name="dashboard_race",
                    type="base",
                    schema=[],
                    indexes=[],
                    options={},
                ),
                auto_migrate=True,
                migrations_dir=str(migrations_dir),
            )

    producer = asyncio.create_task(_produce())
    await asyncio.wait_for(published.wait(), timeout=5)
    runner = asyncio.create_task(
        run_migrations_up(
            isolated_migration_engine,
            migrations_dir,
            lock_timeout_seconds=5,
        )
    )

    await asyncio.sleep(0.15)
    assert not runner.done()

    allow_commit.set()
    await asyncio.wait_for(producer, timeout=5)
    assert await asyncio.wait_for(runner, timeout=5) == []
    assert await _collection_exists(isolated_migration_engine, "dashboardrace01")
    assert await _relation_exists(isolated_migration_engine, "dashboard_race")
    generated = [path.name for path in migrations_dir.glob("*.py")]
    assert len(generated) == 1
    assert await _history(isolated_migration_engine) == generated


async def test_dashboard_commit_rollback_cleanup_is_fenced_from_runner(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verifier must delete rolled-back intent while runners stay blocked."""
    from ppbase.models.collection import CollectionCreate
    from ppbase.services import collection_service

    await prepare_database(
        isolated_migration_engine,
        tmp_path / "bootstrap_only",
        apply_migrations=False,
    )
    migrations_dir = tmp_path / "rollback_reconciliation"
    reconciliation_locked = asyncio.Event()
    allow_reconciliation = asyncio.Event()
    original_recorded = collection_service._recorded_generated_migrations

    async def _pause_with_reconciliation_lock(
        session: AsyncSession,
        filenames: list[str],
    ) -> set[str]:
        reconciliation_locked.set()
        await allow_reconciliation.wait()
        return await original_recorded(session, filenames)

    monkeypatch.setattr(
        collection_service,
        "_recorded_generated_migrations",
        _pause_with_reconciliation_lock,
    )

    async def _produce_with_failed_commit() -> None:
        async with AsyncSession(
            bind=isolated_migration_engine,
            expire_on_commit=False,
        ) as session:
            async def _fail_before_commit() -> None:
                raise RuntimeError("forced dashboard commit rollback")

            monkeypatch.setattr(session, "commit", _fail_before_commit)
            await collection_service.create_collection(
                session,
                isolated_migration_engine,
                CollectionCreate(
                    id="rollbackfence01",
                    name="rollback_fence",
                    type="base",
                    schema=[],
                    indexes=[],
                    options={},
                ),
                auto_migrate=True,
                migrations_dir=str(migrations_dir),
                migration_lock_timeout=5,
            )

    producer = asyncio.create_task(_produce_with_failed_commit())
    await asyncio.wait_for(reconciliation_locked.wait(), timeout=5)
    generated = [path.name for path in migrations_dir.glob("*.py")]
    assert len(generated) == 1

    runner = asyncio.create_task(
        run_migrations_up(
            isolated_migration_engine,
            migrations_dir,
            lock_timeout_seconds=5,
        )
    )
    await asyncio.sleep(0.15)
    assert not runner.done()

    allow_reconciliation.set()
    with pytest.raises(RuntimeError, match="forced dashboard commit rollback"):
        await asyncio.wait_for(producer, timeout=5)
    assert await asyncio.wait_for(runner, timeout=5) == []

    assert not list(migrations_dir.glob("*.py"))
    assert not await _collection_exists(isolated_migration_engine, "rollbackfence01")
    assert not await _relation_exists(isolated_migration_engine, "rollback_fence")
    assert await _history(isolated_migration_engine) == []


async def test_dashboard_ambiguous_reconciliation_preserves_file_for_runner(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unverifiable outcome keeps intent and releases it only after fencing."""
    from ppbase.models.collection import CollectionCreate
    from ppbase.services import collection_service
    from ppbase.services.migration_runner import MigrationCommitOutcomeError

    await prepare_database(
        isolated_migration_engine,
        tmp_path / "bootstrap_only",
        apply_migrations=False,
    )
    migrations_dir = tmp_path / "ambiguous_reconciliation"
    reconciliation_locked = asyncio.Event()
    fail_reconciliation = asyncio.Event()

    async def _unverifiable_with_reconciliation_lock(
        _session: AsyncSession,
        _filenames: list[str],
    ) -> set[str]:
        reconciliation_locked.set()
        await fail_reconciliation.wait()
        raise RuntimeError("history verification unavailable")

    monkeypatch.setattr(
        collection_service,
        "_recorded_generated_migrations",
        _unverifiable_with_reconciliation_lock,
    )

    async def _produce_with_ambiguous_commit() -> None:
        async with AsyncSession(
            bind=isolated_migration_engine,
            expire_on_commit=False,
        ) as session:
            async def _fail_before_commit() -> None:
                raise RuntimeError("forced ambiguous dashboard commit")

            monkeypatch.setattr(session, "commit", _fail_before_commit)
            await collection_service.create_collection(
                session,
                isolated_migration_engine,
                CollectionCreate(
                    id="ambiguousfence1",
                    name="ambiguous_fence",
                    type="base",
                    schema=[],
                    indexes=[],
                    options={},
                ),
                auto_migrate=True,
                migrations_dir=str(migrations_dir),
                migration_lock_timeout=5,
            )

    producer = asyncio.create_task(_produce_with_ambiguous_commit())
    await asyncio.wait_for(reconciliation_locked.wait(), timeout=5)
    generated = [path.name for path in migrations_dir.glob("*.py")]
    assert len(generated) == 1

    runner = asyncio.create_task(
        run_migrations_up(
            isolated_migration_engine,
            migrations_dir,
            lock_timeout_seconds=5,
        )
    )
    await asyncio.sleep(0.15)
    assert not runner.done()

    fail_reconciliation.set()
    with pytest.raises(MigrationCommitOutcomeError, match="outcome is unknown"):
        await asyncio.wait_for(producer, timeout=5)
    assert await asyncio.wait_for(runner, timeout=5) == generated

    assert [path.name for path in migrations_dir.glob("*.py")] == generated
    assert await _collection_exists(isolated_migration_engine, "ambiguousfence1")
    assert await _relation_exists(isolated_migration_engine, "ambiguous_fence")
    assert await _history(isolated_migration_engine) == generated


async def test_schema_mutation_without_generation_waits_for_runner(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling generated files must not disable schema serialization."""
    from ppbase.models.collection import CollectionCreate
    from ppbase.services import collection_service, migration_runner

    await prepare_database(
        isolated_migration_engine,
        tmp_path / "bootstrap_only",
        apply_migrations=False,
    )
    migrations_dir = tmp_path / "generation_disabled"
    filename = _write_migration(
        migrations_dir,
        "1700000011_runner_lock_only.py",
        """
        async def up(app):
            pass


        async def down(app):
            pass
        """,
    )
    runner_locked = asyncio.Event()
    release_runner = asyncio.Event()
    original_apply = migration_runner.apply_migration

    async def _pause_locked_runner(*args: Any, **kwargs: Any) -> None:
        runner_locked.set()
        await release_runner.wait()
        await original_apply(*args, **kwargs)

    monkeypatch.setattr(migration_runner, "apply_migration", _pause_locked_runner)

    runner = asyncio.create_task(
        run_migrations_up(
            isolated_migration_engine,
            migrations_dir,
            lock_timeout_seconds=5,
        )
    )
    await asyncio.wait_for(runner_locked.wait(), timeout=5)

    async def _mutate_without_generation() -> None:
        async with AsyncSession(
            bind=isolated_migration_engine,
            expire_on_commit=False,
        ) as session:
            await collection_service.create_collection(
                session,
                isolated_migration_engine,
                CollectionCreate(
                    id="nogeneration01",
                    name="no_generation_lock",
                    type="base",
                    schema=[],
                    indexes=[],
                    options={},
                ),
                auto_migrate=False,
                migrations_dir=None,
                migration_lock_timeout=5,
            )

    producer = asyncio.create_task(_mutate_without_generation())
    await asyncio.sleep(0.15)
    assert not producer.done()

    release_runner.set()
    assert await asyncio.wait_for(runner, timeout=5) == [filename]
    await asyncio.wait_for(producer, timeout=5)

    assert await _collection_exists(isolated_migration_engine, "nogeneration01")
    assert await _relation_exists(isolated_migration_engine, "no_generation_lock")
    assert await _history(isolated_migration_engine) == [filename]


async def test_dashboard_commit_ack_loss_keeps_the_committed_migration_file(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMMIT acknowledged by PostgreSQL must not lose its source file."""
    from ppbase.models.collection import CollectionCreate
    from ppbase.services import collection_service

    await prepare_database(
        isolated_migration_engine,
        tmp_path / "bootstrap_only",
        apply_migrations=False,
    )
    migrations_dir = tmp_path / "ack_loss_migrations"

    async with AsyncSession(
        bind=isolated_migration_engine,
        expire_on_commit=False,
    ) as session:
        real_commit = session.commit

        async def _commit_then_lose_ack() -> None:
            await real_commit()
            raise OperationalError(
                "COMMIT",
                {},
                ConnectionError("server closed before commit acknowledgement"),
                connection_invalidated=True,
            )

        monkeypatch.setattr(session, "commit", _commit_then_lose_ack)
        await collection_service.create_collection(
            session,
            isolated_migration_engine,
            CollectionCreate(
                id="commitackloss1",
                name="commit_ack_loss",
                type="base",
                schema=[],
                indexes=[],
                options={},
            ),
            auto_migrate=True,
            migrations_dir=str(migrations_dir),
        )

    generated = [path.name for path in migrations_dir.glob("*.py")]
    assert len(generated) == 1
    assert await _collection_exists(isolated_migration_engine, "commitackloss1")
    assert await _relation_exists(isolated_migration_engine, "commit_ack_loss")
    assert await _history(isolated_migration_engine) == generated


async def test_app_engine_begin_cannot_commit_outside_the_migration_transaction(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000012_engine_escape.py",
        """
        from sqlalchemy import text


        async def up(app):
            async with app.engine.begin() as connection:
                await connection.execute(
                    text('CREATE TABLE "engine_escape" ("id" integer)')
                )
            raise RuntimeError("fail after nested engine transaction")


        async def down(app):
            pass
        """,
    )

    with pytest.raises(RuntimeError, match="nested engine transaction"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert not await _relation_exists(isolated_migration_engine, "engine_escape")
    assert filename not in await _history(isolated_migration_engine)


async def test_app_session_cannot_commit_the_runner_transaction(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000012_session_escape.py",
        """
        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "session_escape" ("id" integer)'
            )
            await app.session.commit()


        async def down(app):
            pass
        """,
    )

    with pytest.raises(RuntimeError, match="runner-owned root transaction"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert not await _relation_exists(isolated_migration_engine, "session_escape")
    assert filename not in await _history(isolated_migration_engine)


@pytest.mark.parametrize("surface", ["execute_sql", "session", "engine"])
async def test_raw_commit_is_rejected_on_every_public_migration_sql_surface(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    surface: str,
) -> None:
    if surface == "engine":
        migration_source = f"""
        from sqlalchemy import text


        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "{surface}_raw_commit" ("id" integer)'
            )
            async with app.engine.connect() as connection:
                await connection.execute(text('COMMIT'))


        async def down(app):
            pass
        """
    else:
        commit_source = (
            "await app.execute_sql('COMMIT')"
            if surface == "execute_sql"
            else "await app.session.execute(text('COMMIT'))"
        )
        migration_source = f"""
        from sqlalchemy import text


        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "{surface}_raw_commit" ("id" integer)'
            )
            {commit_source}


        async def down(app):
            pass
        """

    filename = _write_migration(
        tmp_path,
        f"1700000012_{surface}_raw_commit.py",
        migration_source,
    )

    with pytest.raises(RuntimeError, match="transaction-control command"):
        await run_migrations_up(isolated_migration_engine, tmp_path)

    assert not await _relation_exists(
        isolated_migration_engine,
        f"{surface}_raw_commit",
    )
    assert filename not in await _history(isolated_migration_engine)


async def test_runner_reconciles_lost_commit_ack_for_up(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner

    filename = _write_migration(
        tmp_path,
        "1700000013_commit_ack_up.py",
        """
        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "runner_ack_up" ("id" integer)'
            )


        async def down(app):
            await app.execute_sql('DROP TABLE "runner_ack_up"')
        """,
    )
    real_session_class = migration_runner.AsyncSession

    class CommitThenAckLossSession(real_session_class):
        failed = False

        async def commit(self) -> None:
            await super().commit()
            if not type(self).failed:
                type(self).failed = True
                raise OperationalError(
                    "COMMIT",
                    {},
                    ConnectionError("lost migration commit acknowledgement"),
                    connection_invalidated=False,
                )

    monkeypatch.setattr(
        migration_runner,
        "AsyncSession",
        CommitThenAckLossSession,
    )

    assert await run_migrations_up(isolated_migration_engine, tmp_path) == [filename]
    assert await _relation_exists(isolated_migration_engine, "runner_ack_up")
    assert await _history(isolated_migration_engine) == [filename]


async def test_runner_reconciles_lost_commit_ack_for_exact_down_target(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner

    first = _write_migration(
        tmp_path,
        "1700000014_down_ack_first.py",
        """
        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "down_ack_first" ("id" integer)'
            )


        async def down(app):
            await app.execute_sql('DROP TABLE "down_ack_first"')
        """,
    )
    second = _write_migration(
        tmp_path,
        "1700000015_down_ack_second.py",
        """
        async def up(app):
            await app.execute_sql(
                'CREATE TABLE "down_ack_second" ("id" integer)'
            )


        async def down(app):
            await app.execute_sql('DROP TABLE "down_ack_second"')
        """,
    )
    assert await run_migrations_up(isolated_migration_engine, tmp_path) == [
        first,
        second,
    ]

    real_session_class = migration_runner.AsyncSession

    class CommitThenAckLossSession(real_session_class):
        failed = False

        async def commit(self) -> None:
            await super().commit()
            if not type(self).failed:
                type(self).failed = True
                raise OperationalError(
                    "COMMIT",
                    {},
                    ConnectionError("lost down commit acknowledgement"),
                    connection_invalidated=False,
                )

    monkeypatch.setattr(
        migration_runner,
        "AsyncSession",
        CommitThenAckLossSession,
    )

    assert await run_migrations_down(
        isolated_migration_engine,
        tmp_path,
        count=1,
    ) == [second]
    assert await _relation_exists(isolated_migration_engine, "down_ack_first")
    assert not await _relation_exists(isolated_migration_engine, "down_ack_second")
    assert await _history(isolated_migration_engine) == [first]


async def test_frozen_clock_keeps_generated_dependency_order(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_generator

    monkeypatch.setattr(migration_generator.time, "time", lambda: 1700000100)
    source = CollectionRecord(
        id="timesource00001",
        name="z_timestamp_source",
        type="base",
        system=False,
        schema=[],
        indexes=[],
        options={},
    )
    dependent_view = CollectionRecord(
        id="timeview000001",
        name="a_timestamp_view",
        type="view",
        system=False,
        schema=[],
        indexes=[],
        options={
            "query": (
                'SELECT "id", "created", "updated" '
                'FROM "z_timestamp_source"'
            )
        },
    )
    child_view = CollectionRecord(
        id="timechild00001",
        name="b_timestamp_child",
        type="view",
        system=False,
        schema=[],
        indexes=[],
        options={
            "query": (
                'SELECT "id", "created", "updated" '
                'FROM "a_timestamp_view"'
            )
        },
    )

    source_path = Path(
        migration_generator.generate_create_migration(source, tmp_path)
    )
    view_path = Path(
        migration_generator.generate_create_migration(dependent_view, tmp_path)
    )
    child_path = Path(
        migration_generator.generate_create_migration(child_view, tmp_path)
    )

    assert source_path.name.startswith("1700000100_")
    assert view_path.name.startswith("1700000101_")
    assert child_path.name.startswith("1700000102_")
    assert await run_migrations_up(isolated_migration_engine, tmp_path) == [
        source_path.name,
        view_path.name,
        child_path.name,
    ]
    assert await _relation_exists(isolated_migration_engine, "z_timestamp_source")
    assert await _relation_exists(isolated_migration_engine, "a_timestamp_view")
    assert await _relation_exists(isolated_migration_engine, "b_timestamp_child")


async def test_migration_status_is_read_only_on_an_uninitialized_database(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000103_status_pending.py",
        """
        async def up(app):
            pass


        async def down(app):
            pass
        """,
    )
    async with isolated_migration_engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))

    async with isolated_migration_engine.connect() as connection:
        async with connection.begin():
            await connection.execute(
                text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
            )
            status = await get_migration_status(connection, tmp_path)

    assert status["initialized"] is False
    assert status["applied"] == []
    assert status["pending"] == [filename]
    assert await _scalar(
        isolated_migration_engine,
        "SELECT to_regclass('public._migrations')",
    ) is None


async def test_cli_migrate_status_does_not_bootstrap_a_blank_database(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000103_cli_status_pending.py",
        """
        async def up(app):
            pass


        async def down(app):
            pass
        """,
    )
    async with isolated_migration_engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ppbase",
        "migrate",
        "status",
        "--db",
        isolated_migration_engine.url.render_as_string(hide_password=False),
        "--dir",
        str(tmp_path),
        cwd=Path(__file__).resolve().parents[2],
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output_bytes, _ = await process.communicate()
    output = output_bytes.decode("utf-8", errors="replace")

    assert process.returncode == 0, output
    assert "status did not modify the database" in output
    assert f"[ ] {filename}" in output
    assert await _scalar(
        isolated_migration_engine,
        "SELECT to_regclass('public._migrations')",
    ) is None


async def test_runner_and_status_work_with_a_single_connection_pool(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    filename = _write_migration(
        tmp_path,
        "1700000104_single_pool.py",
        """
        from sqlalchemy import text


        async def up(app):
            async with app.engine.begin() as connection:
                await connection.execute(
                    text('CREATE TABLE "single_pool_table" ("id" integer)')
                )


        async def down(app):
            await app.execute_sql('DROP TABLE "single_pool_table"')
        """,
    )
    single_connection_engine = create_async_engine(
        isolated_migration_engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=1,
    )
    try:
        assert await prepare_database(
            single_connection_engine,
            tmp_path,
        ) == [filename]
        async with single_connection_engine.connect() as connection:
            status = await get_migration_status(connection, tmp_path)
        assert status["applied"] == [filename]
        assert status["pending"] == []
        assert await _relation_exists(
            single_connection_engine,
            "single_pool_table",
        )

        (tmp_path / filename).unlink()
        async with single_connection_engine.connect() as connection:
            orphaned_status = await get_migration_status(connection, tmp_path)
        assert orphaned_status["orphaned"] == [filename]
        assert orphaned_status["orphaned_count"] == 1
        assert orphaned_status["tracked_total"] == 1
    finally:
        await single_connection_engine.dispose()


async def test_collections_http_mutation_reuses_auth_session_with_single_pool(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admin authentication and collection DDL must share one request session."""
    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db import engine as engine_module
    from ppbase.services import file_storage
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    single_connection_engine = create_async_engine(
        isolated_migration_engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    session_factory = async_sessionmaker(
        bind=single_connection_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    collection_name = f"single_pool_http_{uuid.uuid4().hex[:10]}"
    settings = Settings(
        database_url=single_connection_engine.url.render_as_string(
            hide_password=False
        ),
        data_dir=str(tmp_path / "single_pool_http_data"),
        migrations_dir=str(tmp_path / "single_pool_http_migrations"),
        jwt_secret="single-pool-http-secret",
        pool_size=1,
        max_overflow=0,
        auto_migrate=False,
        apply_migrations_on_start=False,
        generate_migrations=False,
    )
    previous_storage_settings = file_storage._settings

    try:
        await prepare_database(
            single_connection_engine,
            settings.migrations_dir,
            apply_migrations=False,
        )
        async with session_factory() as session:
            admin = await create_admin(
                session,
                "single-pool-admin@example.com",
                "single-pool-password",
            )
            superusers = (
                await session.execute(
                    select(CollectionRecord).where(
                        CollectionRecord.name == "_superusers"
                    )
                )
            ).scalars().one()
            await session.commit()
            admin_token = create_admin_token(
                admin,
                settings,
                superusers_collection=superusers,
            )

        # Route and auth dependencies call the engine module at request time.
        # Point both at this isolated one-connection pool without disturbing
        # the session-scoped API fixtures used elsewhere in the suite.
        monkeypatch.setattr(engine_module, "_engine", single_connection_engine)
        monkeypatch.setattr(engine_module, "_session_factory", session_factory)

        app = create_app(settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await asyncio.wait_for(
                client.post(
                    "/api/collections",
                    headers={"Authorization": admin_token},
                    json={"name": collection_name, "type": "base", "schema": []},
                ),
                timeout=2,
            )

        assert response.status_code == 200, response.text
        assert response.json()["name"] == collection_name
        assert await _relation_exists(single_connection_engine, collection_name)
    finally:
        file_storage.set_storage_settings(previous_storage_settings)
        await single_connection_engine.dispose()


async def test_collections_meta_tables_reuses_auth_session_with_single_pool(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authenticated metadata reads must reuse the auth request session."""
    from ppbase.app import create_app
    from ppbase.config import Settings
    from ppbase.db import engine as engine_module
    from ppbase.services import file_storage
    from ppbase.services.admin_service import create_admin
    from ppbase.services.auth_service import create_admin_token

    single_connection_engine = create_async_engine(
        isolated_migration_engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    session_factory = async_sessionmaker(
        bind=single_connection_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    settings = Settings(
        database_url=single_connection_engine.url.render_as_string(
            hide_password=False
        ),
        data_dir=str(tmp_path / "single_pool_meta_tables_data"),
        migrations_dir=str(tmp_path / "single_pool_meta_tables_migrations"),
        jwt_secret="single-pool-meta-tables-secret",
        pool_size=1,
        max_overflow=0,
        auto_migrate=False,
        apply_migrations_on_start=False,
        generate_migrations=False,
    )
    previous_storage_settings = file_storage._settings

    try:
        await prepare_database(
            single_connection_engine,
            settings.migrations_dir,
            apply_migrations=False,
        )
        async with session_factory() as session:
            admin = await create_admin(
                session,
                "single-pool-meta-admin@example.com",
                "single-pool-meta-password",
            )
            superusers = (
                await session.execute(
                    select(CollectionRecord).where(
                        CollectionRecord.name == "_superusers"
                    )
                )
            ).scalars().one()
            await session.commit()
            admin_token = create_admin_token(
                admin,
                settings,
                superusers_collection=superusers,
            )

        monkeypatch.setattr(engine_module, "_engine", single_connection_engine)
        monkeypatch.setattr(engine_module, "_session_factory", session_factory)

        app = create_app(settings)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await asyncio.wait_for(
                client.get(
                    "/api/collections/meta/tables",
                    headers={"Authorization": admin_token},
                ),
                timeout=2,
            )

        assert response.status_code == 200, response.text
        tables = response.json()
        assert isinstance(tables, list)
        assert any(table.get("name") == "_collections" for table in tables)
    finally:
        file_storage.set_storage_settings(previous_storage_settings)
        await single_connection_engine.dispose()


async def test_migration_lock_timeout_includes_pool_checkout(
    isolated_migration_engine: AsyncEngine,
    tmp_path: Path,
) -> None:
    single_connection_engine = create_async_engine(
        isolated_migration_engine.url,
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    try:
        async with single_connection_engine.connect():
            started = asyncio.get_running_loop().time()
            with pytest.raises(MigrationLockError, match="database connection"):
                await run_migrations_up(
                    single_connection_engine,
                    tmp_path,
                    lock_timeout_seconds=0.05,
                )
            elapsed = asyncio.get_running_loop().time() - started
            assert elapsed < 1
    finally:
        await single_connection_engine.dispose()
