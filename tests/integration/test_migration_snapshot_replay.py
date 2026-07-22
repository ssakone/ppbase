"""Regression tests for replayable, baseline-style collection snapshots."""

from __future__ import annotations

import textwrap
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from ppbase.services.database_preparation import (
    prepare_database,
    prepare_database_and_revert,
)
from ppbase.services.migration_snapshot import create_migration_snapshot


pytestmark = pytest.mark.asyncio


def _sqlstate(exc: DBAPIError) -> str | None:
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
async def isolated_snapshot_engines(
    pg_url: str,
) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    """Yield independent source and replay databases in the same cluster."""
    names = [f"ppbase_snapshot_{uuid.uuid4().hex[:16]}" for _ in range(2)]
    admin_url = make_url(pg_url)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    engines: list[AsyncEngine] = []
    created_names: list[str] = []

    try:
        try:
            async with admin_engine.connect() as connection:
                for name in names:
                    await connection.execute(
                        text(f'CREATE DATABASE "{name}" TEMPLATE template0')
                    )
                    created_names.append(name)
        except DBAPIError as exc:
            if _sqlstate(exc) == "42501":
                pytest.skip(
                    "PostgreSQL user cannot CREATE DATABASE; snapshot replay "
                    "tests require CREATEDB privilege."
                )
            raise

        for name in names:
            engine = create_async_engine(
                admin_url.set(database=name),
                poolclass=NullPool,
            )
            engines.append(engine)

        yield engines[0], engines[1]
    finally:
        for engine in engines:
            await engine.dispose()

        try:
            async with admin_engine.connect() as connection:
                for name in created_names:
                    await connection.execute(
                        text(
                            "SELECT pg_terminate_backend(pid) "
                            "FROM pg_stat_activity "
                            "WHERE datname = :database_name "
                            "AND pid <> pg_backend_pid()"
                        ),
                        {"database_name": name},
                    )
                    await connection.execute(
                        text(f'DROP DATABASE IF EXISTS "{name}"')
                    )
        finally:
            await admin_engine.dispose()


def _write_migration(directory: Path, filename: str, source: str) -> str:
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


async def _prepare_legacy_users_source(
    engine: AsyncEngine,
    migrations_dir: Path,
) -> str:
    """Create a legacy users ID and one collection that relates to it."""
    filename = _write_migration(
        migrations_dir,
        "1701000003_legacy_users_relation.py",
        """
        async def up(app):
            await app.execute_sql(
                'UPDATE "_collections" SET "id" = :legacy_id '
                'WHERE "id" = :stable_id',
                {
                    "legacy_id": "legacyusers001",
                    "stable_id": "_pb_users_auth_",
                },
            )
            await app.create_collection({
                "id": "legacyrefs0001",
                "name": "legacy_user_refs",
                "type": "base",
                "schema": [{
                    "id": "legacy_owner",
                    "name": "owner",
                    "type": "relation",
                    "required": False,
                    "options": {
                        "collectionId": "legacyusers001",
                        "maxSelect": 1,
                        "cascadeDelete": False,
                    },
                }],
                "indexes": [],
                "options": {},
            })


        async def down(app):
            pass
        """,
    )
    assert await prepare_database(engine, migrations_dir) == [filename]
    return filename


async def test_snapshot_replays_over_history_and_down_is_safe_baseline(
    isolated_snapshot_engines: tuple[AsyncEngine, AsyncEngine],
    tmp_path: Path,
) -> None:
    source_engine, replay_engine = isolated_snapshot_engines
    migrations_dir = tmp_path / "migrations"
    original_filename = _write_migration(
        migrations_dir,
        "1701000000_created_snapshot_posts.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "snapshotposts1",
                "name": "snapshot_posts",
                "type": "base",
                "schema": [{
                    "id": "snapshot_title",
                    "name": "title",
                    "type": "text",
                    "required": False,
                    "options": {},
                }],
                "indexes": [],
                "options": {},
            })
            await app.execute_sql(
                'INSERT INTO "snapshot_posts" ("id", "title") '
                'VALUES (:id, :title)',
                {"id": "snapshotrow001", "title": "keep me"},
            )


        async def down(app):
            await app.delete_collection("snapshotposts1")
        """,
    )

    assert await prepare_database(source_engine, migrations_dir) == [original_filename]
    assert await prepare_database(replay_engine, migrations_dir) == [original_filename]

    snapshot_path = Path(
        await create_migration_snapshot(source_engine, migrations_dir)
    )
    assert await prepare_database(replay_engine, migrations_dir) == [snapshot_path.name]
    assert await _scalar(
        replay_engine,
        'SELECT "title" FROM "snapshot_posts" WHERE "id" = :id',
        {"id": "snapshotrow001"},
    ) == "keep me"

    assert await prepare_database_and_revert(
        replay_engine,
        migrations_dir,
        count=1,
    ) == [snapshot_path.name]
    assert await _history(replay_engine) == [original_filename]
    assert await _scalar(
        replay_engine,
        'SELECT "title" FROM "snapshot_posts" WHERE "id" = :id',
        {"id": "snapshotrow001"},
    ) == "keep me"

    assert await prepare_database(replay_engine, migrations_dir) == [snapshot_path.name]
    assert await _scalar(
        replay_engine,
        'SELECT count(*) FROM "snapshot_posts" WHERE "id" = :id',
        {"id": "snapshotrow001"},
    ) == 1


async def test_snapshot_replays_views_in_postgresql_dependency_order(
    isolated_snapshot_engines: tuple[AsyncEngine, AsyncEngine],
    tmp_path: Path,
) -> None:
    source_engine, replay_engine = isolated_snapshot_engines
    source_dir = tmp_path / "source"
    source_filename = _write_migration(
        source_dir,
        "1701000001_created_nested_views.py",
        """
        async def up(app):
            await app.create_collection({
                "id": "viewsource001",
                "name": "view_source_replay",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {},
            })
            await app.execute_sql(
                'INSERT INTO "view_source_replay" ("id") VALUES (:id)',
                {"id": "viewrow0000001"},
            )
            await app.create_collection({
                "id": "parentview001",
                "name": "z_parent_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {
                    "query": (
                        'SELECT "id", 1::integer AS "marker" '
                        'FROM "view_source_replay"'
                    ),
                },
            })
            await app.create_collection({
                "id": "childview0001",
                "name": "a_child_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {
                    "query": (
                        'SELECT "id", "marker" + 1 AS "marker" '
                        'FROM "z_parent_view"'
                    ),
                },
            })


        async def down(app):
            await app.delete_collection("childview0001")
            await app.delete_collection("parentview001")
            await app.delete_collection("viewsource001")
        """,
    )
    assert await prepare_database(source_engine, source_dir) == [source_filename]

    snapshot_dir = tmp_path / "snapshot"
    snapshot_path = Path(
        await create_migration_snapshot(source_engine, snapshot_dir)
    )

    assert await prepare_database(replay_engine, snapshot_dir) == [snapshot_path.name]
    async with replay_engine.begin() as connection:
        await connection.execute(
            text('INSERT INTO "view_source_replay" ("id") VALUES (:id)'),
            {"id": "replayedrow001"},
        )
        result = await connection.execute(
            text('SELECT "marker" FROM "a_child_view"')
        )
        assert result.scalar_one() == 2


async def test_snapshot_recreates_child_view_dropped_by_parent_cascade(
    isolated_snapshot_engines: tuple[AsyncEngine, AsyncEngine],
    tmp_path: Path,
) -> None:
    source_engine, replay_engine = isolated_snapshot_engines
    source_dir = tmp_path / "source_divergent"
    replay_dir = tmp_path / "replay_divergent"

    def _view_migration(marker: int) -> str:
        return f'''
        async def up(app):
            await app.create_collection({{
                "id": "cascadebase001",
                "name": "cascade_view_source",
                "type": "base",
                "schema": [],
                "indexes": [],
                "options": {{}},
            }})
            await app.create_collection({{
                "id": "cascadeparent1",
                "name": "cascade_parent_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {{
                    "query": (
                        'SELECT "id", {marker}::integer AS "marker" '
                        'FROM "cascade_view_source"'
                    ),
                }},
            }})
            await app.create_collection({{
                "id": "cascadechild01",
                "name": "cascade_child_view",
                "type": "view",
                "schema": [],
                "indexes": [],
                "options": {{
                    "query": (
                        'SELECT "id", "marker" + 1 AS "marker" '
                        'FROM "cascade_parent_view"'
                    ),
                }},
            }})


        async def down(app):
            await app.delete_collection("cascadechild01")
            await app.delete_collection("cascadeparent1")
            await app.delete_collection("cascadebase001")
        '''

    source_filename = _write_migration(
        source_dir,
        "1701000002_created_cascade_views.py",
        _view_migration(2),
    )
    replay_filename = _write_migration(
        replay_dir,
        "1701000002_created_cascade_views.py",
        _view_migration(1),
    )
    assert await prepare_database(source_engine, source_dir) == [source_filename]
    assert await prepare_database(replay_engine, replay_dir) == [replay_filename]

    snapshot_path = Path(
        await create_migration_snapshot(source_engine, replay_dir)
    )
    assert await prepare_database(replay_engine, replay_dir) == [snapshot_path.name]

    async with replay_engine.begin() as connection:
        await connection.execute(
            text('INSERT INTO "cascade_view_source" ("id") VALUES (:id)'),
            {"id": "cascadeviewrow1"},
        )
        result = await connection.execute(
            text('SELECT "marker" FROM "cascade_child_view"')
        )
        assert result.scalar_one() == 3


async def test_snapshot_commit_ack_loss_preserves_file_and_history(
    isolated_snapshot_engines: tuple[AsyncEngine, AsyncEngine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_snapshot

    source_engine, _ = isolated_snapshot_engines
    await prepare_database(
        source_engine,
        tmp_path / "bootstrap_snapshot_ack",
        apply_migrations=False,
    )
    real_session_class = migration_snapshot.AsyncSession

    class CommitThenAckLossSession(real_session_class):
        failed = False

        async def commit(self) -> None:
            await super().commit()
            if not type(self).failed:
                type(self).failed = True
                raise OperationalError(
                    "COMMIT",
                    {},
                    ConnectionError("lost snapshot commit acknowledgement"),
                    connection_invalidated=False,
                )

    monkeypatch.setattr(
        migration_snapshot,
        "AsyncSession",
        CommitThenAckLossSession,
    )
    snapshots_dir = tmp_path / "snapshot_ack_loss"

    snapshot_path = Path(
        await create_migration_snapshot(source_engine, snapshots_dir)
    )

    assert CommitThenAckLossSession.failed
    assert snapshot_path.exists()
    assert snapshot_path.name in await _history(source_engine)


async def test_snapshot_rewrites_legacy_users_relations_on_fresh_replay(
    isolated_snapshot_engines: tuple[AsyncEngine, AsyncEngine],
    tmp_path: Path,
) -> None:
    from ppbase.db.system_tables import CollectionRecord
    from ppbase.services.record_service import _relation_targets

    source_engine, replay_engine = isolated_snapshot_engines
    source_dir = tmp_path / "legacy_users_source"
    await _prepare_legacy_users_source(source_engine, source_dir)

    snapshot_dir = tmp_path / "legacy_users_snapshot"
    snapshot_path = Path(
        await create_migration_snapshot(source_engine, snapshot_dir)
    )
    assert await prepare_database(replay_engine, snapshot_dir) == [snapshot_path.name]

    async with AsyncSession(bind=replay_engine, expire_on_commit=False) as session:
        result = await session.execute(select(CollectionRecord))
        collections = list(result.scalars().all())

    users = next(collection for collection in collections if collection.name == "users")
    references = next(
        collection
        for collection in collections
        if collection.name == "legacy_user_refs"
    )
    assert users.id == "_pb_users_auth_"
    assert references.schema[0]["options"]["collectionId"] == "_pb_users_auth_"
    targets = _relation_targets(
        references,
        {collection.id: collection for collection in collections},
        {collection.name: collection for collection in collections},
    )
    assert [(field, target.id, max_select) for field, target, max_select in targets] == [
        ("owner", "_pb_users_auth_", 1)
    ]


async def test_pending_legacy_snapshot_replays_on_its_source_database(
    isolated_snapshot_engines: tuple[AsyncEngine, AsyncEngine],
    tmp_path: Path,
) -> None:
    """Recover a file published before its source history transaction commits."""
    from ppbase.db.system_tables import CollectionRecord
    from ppbase.services.migration_generator import generate_snapshot_migration
    from ppbase.services.record_service import _relation_targets

    source_engine, _ = isolated_snapshot_engines
    await _prepare_legacy_users_source(
        source_engine,
        tmp_path / "legacy_users_recovery_source",
    )

    async with AsyncSession(bind=source_engine, expire_on_commit=False) as session:
        result = await session.execute(select(CollectionRecord))
        source_collections = list(result.scalars().all())

    # This is the durable state left by a process crash after publishing the
    # snapshot file but before committing its _migrations history row.
    snapshot_dir = tmp_path / "legacy_users_pending_snapshot"
    snapshot_path = Path(
        generate_snapshot_migration(source_collections, snapshot_dir)
    )
    assert snapshot_path.name not in await _history(source_engine)

    assert await prepare_database(source_engine, snapshot_dir) == [snapshot_path.name]

    async with AsyncSession(bind=source_engine, expire_on_commit=False) as session:
        result = await session.execute(select(CollectionRecord))
        collections = list(result.scalars().all())

    users = next(collection for collection in collections if collection.name == "users")
    references = next(
        collection
        for collection in collections
        if collection.name == "legacy_user_refs"
    )
    assert users.id == "legacyusers001"
    assert references.schema[0]["options"]["collectionId"] == "legacyusers001"
    targets = _relation_targets(
        references,
        {collection.id: collection for collection in collections},
        {collection.name: collection for collection in collections},
    )
    assert [(field, target.id, max_select) for field, target, max_select in targets] == [
        ("owner", "legacyusers001", 1)
    ]
