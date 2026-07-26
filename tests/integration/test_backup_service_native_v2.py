"""End-to-end service test for the native (v2) backup + destructive restore.

Drives :class:`NativeBackupService` so the native (v2) backup is the only
create path: it streams the live schema out with COPY (schema.json + data.copy) and
the restore path rebuilds it in-process — no ``pg_dump``/``pg_restore``/``psql``
binaries are used at any point.

Each test creates a disposable database on the developer PostgreSQL cluster
(CREATEDB required) because the destructive restore drops and recreates the
public schema. The process restart that the HTTP layer would trigger is *not*
invoked: the service only reserves the restart slot, which the test releases via
the returned cutover guard.
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import ppbase.services.process_control as process_control
from ppbase.backup.canonical import (
    CanonicalModelError,
    validate_canonical_collection_objects,
)
from ppbase.backup.models import (
    DATA_COPY_RESOURCE,
    JWT_SECRET_RESOURCE_PATH,
    SCHEMA_JSON_RESOURCE,
)
from ppbase.backup.schema_contract import DatabaseSchema
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.backup.postgres import set_backup_control_search_path
from ppbase.config import Settings
from ppbase.db import schema_manager
from ppbase.db.bootstrap import bootstrap_system_collections
from ppbase.db.system_tables import create_system_tables

pytestmark = pytest.mark.asyncio


def _admin_base() -> str:
    url = os.environ.get("PPBASE_TEST_DATABASE_URL")
    if url:
        base = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    else:
        base = "postgresql+asyncpg://ppbase:ppbase@localhost:5433/postgres"
    return base.rsplit("/", 1)[0]


async def _can_create_database(base: str) -> bool:
    engine = create_async_engine(
        f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            can = await connection.scalar(
                text(
                    "SELECT rolcreatedb OR rolsuper FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        return bool(can)
    except Exception:
        return False
    finally:
        await engine.dispose()


async def _create_database(base: str) -> str:
    database = f"ppbase_v2_{secrets.token_hex(8)}"
    engine = create_async_engine(
        f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f'CREATE DATABASE "{database}" TEMPLATE template0')
            )
    finally:
        await engine.dispose()
    return f"{base}/{database}"


async def _drop_database(base: str, url: str) -> None:
    database = url.rsplit("/", 1)[1]
    engine = create_async_engine(
        f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
    finally:
        await engine.dispose()


async def _scalar(url: str, statement: str) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(statement))
    finally:
        await engine.dispose()


# A real PPBase-managed base collection: the physical table is emitted through
# ``schema_manager`` and its field schema is registered in ``_collections`` so
# the strict v2 canonical model reconciles it as an authored table.
SENTINEL_SCHEMA: list[dict[str, object]] = [{"name": "note", "type": "text"}]
SENTINEL_ROW_ID = "sentinelrow01"
SENTINEL_EXTRA_ID = "sentinelrow02"


async def _seed_managed_collection(engine: object) -> None:
    """Create + register the ``sentinel`` base collection with one seeded row."""
    collection = SimpleNamespace(
        name="sentinel", type="base", schema=SENTINEL_SCHEMA, options={}
    )
    await schema_manager.create_collection_table(engine, collection)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text("INSERT INTO sentinel (id, note) VALUES (:id, :note)"),
            {"id": SENTINEL_ROW_ID, "note": "from-backup"},
        )
        await connection.execute(
            text(
                'INSERT INTO "_collections" (id, name, type, schema) '
                "VALUES (:id, 'sentinel', 'base', CAST(:schema AS jsonb))"
            ),
            {"id": secrets.token_hex(7), "schema": json.dumps(SENTINEL_SCHEMA)},
        )


async def _validate_collection_contract(engine: object) -> None:
    """Run the same view/index reconciliation used by backup creation."""
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await set_backup_control_search_path(connection)
        await validate_canonical_collection_objects(connection)


@pytest_asyncio.fixture
async def canonical_validation_database() -> AsyncIterator[dict[str, object]]:
    """Fresh PPBase schema for live canonical view/index refusal tests."""
    base = _admin_base()
    try:
        engine_probe = create_async_engine(
            f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine_probe.connect():
                pass
        finally:
            await engine_probe.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    if not await _can_create_database(base):  # pragma: no cover - env dependent
        pytest.skip("test role cannot CREATE DATABASE")

    url = await _create_database(base)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await create_system_tables(engine)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            async with session.begin():
                await bootstrap_system_collections(session, engine)
        yield {"engine": engine, "url": url}
    finally:
        await engine.dispose()
        await _drop_database(base, url)


async def test_canonical_contract_refuses_divergent_view(
    canonical_validation_database: dict[str, object],
) -> None:
    engine = canonical_validation_database["engine"]
    await _seed_managed_collection(engine)
    expected_query = "SELECT id, note FROM public.sentinel"
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text("CREATE VIEW sentinel_view AS SELECT id FROM public.sentinel")
        )
        await connection.execute(
            text(
                'INSERT INTO "_collections" '
                '(id, name, type, schema, options) '
                "VALUES (:id, 'sentinel_view', 'view', '[]'::jsonb, "
                "CAST(:options AS jsonb))"
            ),
            {
                "id": secrets.token_hex(7),
                "options": json.dumps({"query": expected_query}),
            },
        )

    with pytest.raises(CanonicalModelError, match="view.*diverge"):
        await _validate_collection_contract(engine)


async def test_canonical_contract_accepts_matching_view_and_custom_index(
    canonical_validation_database: dict[str, object],
) -> None:
    engine = canonical_validation_database["engine"]
    await _seed_managed_collection(engine)
    view_query = "SELECT id, note FROM public.sentinel"
    custom_index = "CREATE INDEX sentinel_note_custom ON sentinel (note)"
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(text(custom_index))
        await connection.execute(
            text(
                'UPDATE "_collections" SET indexes = CAST(:indexes AS jsonb) '
                "WHERE name = 'sentinel'"
            ),
            {"indexes": json.dumps([custom_index])},
        )
        await connection.execute(
            text(f"CREATE VIEW sentinel_view AS {view_query}")
        )
        await connection.execute(
            text(
                'INSERT INTO "_collections" '
                '(id, name, type, schema, options) '
                "VALUES (:id, 'sentinel_view', 'view', '[]'::jsonb, "
                "CAST(:options AS jsonb))"
            ),
            {
                "id": secrets.token_hex(7),
                "options": json.dumps({"query": view_query}),
            },
        )

    await _validate_collection_contract(engine)


async def test_canonical_contract_refuses_extra_index(
    canonical_validation_database: dict[str, object],
) -> None:
    engine = canonical_validation_database["engine"]
    await _seed_managed_collection(engine)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text("CREATE INDEX rogue_sentinel_note ON sentinel (note)")
        )

    with pytest.raises(CanonicalModelError, match="unexpected"):
        await _validate_collection_contract(engine)


async def test_canonical_contract_refuses_missing_index(
    canonical_validation_database: dict[str, object],
) -> None:
    engine = canonical_validation_database["engine"]
    await _seed_managed_collection(engine)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(text('DROP INDEX "idx_sentinel_created"'))

    with pytest.raises(CanonicalModelError, match="missing"):
        await _validate_collection_contract(engine)


async def test_canonical_contract_refuses_divergent_index(
    canonical_validation_database: dict[str, object],
) -> None:
    engine = canonical_validation_database["engine"]
    await _seed_managed_collection(engine)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(text('DROP INDEX "idx_sentinel_created"'))
        await connection.execute(
            text('CREATE INDEX "idx_sentinel_created" ON sentinel (note)')
        )

    with pytest.raises(CanonicalModelError, match="index.*diverge"):
        await _validate_collection_contract(engine)


async def test_canonical_contract_refuses_custom_index_wrong_target(
    canonical_validation_database: dict[str, object],
) -> None:
    """A custom index whose SQL targets another table is refused.

    The registered ``_collections.indexes`` statement for collection ``alpha``
    names table ``beta``, yet a real index of the same name exists on ``alpha``.
    Without validating the declared target the canonicalizer would rewrite it
    onto ``alpha``'s clone and accept the inconsistent contract; the reconciler
    must instead reject the divergent target before any rewrite.
    """
    engine = canonical_validation_database["engine"]
    collection = SimpleNamespace(
        name="alpha", type="base", schema=SENTINEL_SCHEMA, options={}
    )
    await schema_manager.create_collection_table(engine, collection)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        # A real index on alpha whose name matches the registered custom one.
        await connection.execute(
            text('CREATE INDEX "alpha_wrong_target" ON alpha (created)')
        )
        # Register alpha with a custom index whose SQL targets *beta*, not alpha.
        await connection.execute(
            text(
                'INSERT INTO "_collections" (id, name, type, schema, indexes) '
                "VALUES (:id, 'alpha', 'base', CAST(:schema AS jsonb), "
                "CAST(:indexes AS jsonb))"
            ),
            {
                "id": secrets.token_hex(7),
                "schema": json.dumps(SENTINEL_SCHEMA),
                "indexes": json.dumps(
                    ['CREATE INDEX "alpha_wrong_target" ON beta (created)']
                ),
            },
        )

    with pytest.raises(CanonicalModelError, match="targets table"):
        await _validate_collection_contract(engine)


async def test_canonical_contract_refuses_non_textual_index_entry(
    canonical_validation_database: dict[str, object],
) -> None:
    """A non-textual ``_collections.indexes`` entry is refused, not skipped.

    The collection machinery silently drops non-string index entries; the
    backup contract must fail closed so malformed index metadata can never pass
    unnoticed.
    """
    engine = canonical_validation_database["engine"]
    await _seed_managed_collection(engine)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text(
                "UPDATE \"_collections\" SET indexes = CAST(:indexes AS jsonb) "
                "WHERE name = 'sentinel'"
            ),
            {"indexes": json.dumps([{"not": "a string"}])},
        )

    with pytest.raises(CanonicalModelError, match="non-textual or empty"):
        await _validate_collection_contract(engine)


async def test_canonical_contract_refuses_unknown_collection_type(
    canonical_validation_database: dict[str, object],
) -> None:
    engine = canonical_validation_database["engine"]
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text(
                'INSERT INTO "_collections" (id, name, type) '
                "VALUES (:id, 'unknown_kind', 'other')"
            ),
            {"id": secrets.token_hex(7)},
        )

    with pytest.raises(CanonicalModelError, match="unsupported type"):
        await _validate_collection_contract(engine)


@pytest_asyncio.fixture
async def seeded_v2_backup() -> AsyncIterator[dict[str, object]]:
    base = _admin_base()
    try:
        engine_probe = create_async_engine(
            f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine_probe.connect():
                pass
        finally:
            await engine_probe.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    if not await _can_create_database(base):  # pragma: no cover - env dependent
        pytest.skip("test role cannot CREATE DATABASE")

    url = await _create_database(base)

    previous_restart_cmd = os.environ.get("PPBASE_RESTART_CMD")
    os.environ["PPBASE_RESTART_CMD"] = json.dumps(["/usr/bin/true"])

    tmp = Path(tempfile.mkdtemp(prefix="ppbase-native-v2-")).resolve(strict=True)
    data_dir = tmp / "pb_data"
    data_dir.mkdir(mode=0o700)
    storage_dir = data_dir / "storage"
    storage_dir.mkdir(mode=0o700)
    storage_file = storage_dir / "hello.txt"
    storage_file.write_text("ORIGINAL", encoding="utf-8")

    settings = Settings(
        database_url=url,
        data_dir=str(data_dir),
        backup_root=str(tmp / "pb_backups"),
        backup_control_dir=str(tmp / "pb_control"),
        jwt_secret="",
        auto_migrate=False,
    )

    engine = create_async_engine(url, poolclass=NullPool)
    await create_system_tables(engine)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        async with session.begin():
            await bootstrap_system_collections(session, engine)

    await _seed_managed_collection(engine)

    service = NativeBackupService(engine, settings)
    try:
        backup = await service.create_local_backup(actor_id=None)
    finally:
        service.close()

    try:
        yield {
            "base": base,
            "url": url,
            "settings": settings,
            "engine": engine,
            "backup_id": str(backup["id"]),
            "storage_file": storage_file,
        }
    finally:
        await engine.dispose()
        await _drop_database(base, url)
        process_control._clear_restart_scheduled()
        if previous_restart_cmd is None:
            os.environ.pop("PPBASE_RESTART_CMD", None)
        else:
            os.environ["PPBASE_RESTART_CMD"] = previous_restart_cmd


async def test_native_v2_backup_is_format_version_two(
    seeded_v2_backup: dict[str, object],
) -> None:
    settings = seeded_v2_backup["settings"]
    backup_id = str(seeded_v2_backup["backup_id"])
    set_dir = Path(settings.backup_root) / "sets" / backup_id  # type: ignore[attr-defined]
    resources = set_dir / "resources"
    # v2 writes schema.json + data.copy under resources/database/ and never a
    # pg_dump custom archive.
    assert (resources / "database" / "schema.json").is_file()
    assert (resources / "database" / "data.copy").is_file()
    assert not (resources / "database.dump").exists()


async def test_native_v2_destructive_restore_round_trip(
    seeded_v2_backup: dict[str, object],
) -> None:
    url = str(seeded_v2_backup["url"])
    settings = seeded_v2_backup["settings"]
    engine = seeded_v2_backup["engine"]
    backup_id = str(seeded_v2_backup["backup_id"])
    storage_file = seeded_v2_backup["storage_file"]
    assert isinstance(storage_file, Path)

    # Mutate database + storage away from the backup contents.
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text("UPDATE sentinel SET note = 'mutated' WHERE id = :id"),
            {"id": SENTINEL_ROW_ID},
        )
        await connection.execute(
            text("INSERT INTO sentinel (id, note) VALUES (:id, 'extra-row')"),
            {"id": SENTINEL_EXTRA_ID},
        )
    storage_file.write_text("MUTATED", encoding="utf-8")

    service = NativeBackupService(engine, settings)  # type: ignore[arg-type]
    cutover_guard = None
    try:
        prepared = await service.restore_local_backup(backup_id, actor_id=None)
        assert prepared["destructive"] is True
        assert prepared["status"] == "restart_scheduled"
        assert prepared["backupId"] == backup_id
        cutover_guard = getattr(prepared, "cutover_guard", None)
        assert cutover_guard is not None
    finally:
        if cutover_guard is not None:
            await cutover_guard.close()
        service.close()

    # Database is back to the backup: mutation gone, extra row absent.
    note = await _scalar(
        url, f"SELECT note FROM sentinel WHERE id = '{SENTINEL_ROW_ID}'"
    )
    assert note == "from-backup"
    extra = await _scalar(
        url, f"SELECT count(*) FROM sentinel WHERE id = '{SENTINEL_EXTRA_ID}'"
    )
    assert int(extra) == 0  # type: ignore[arg-type]
    # The bootstrapped system schema is present again.
    collections = await _scalar(
        url,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = '_collections'",
    )
    assert int(collections) == 1  # type: ignore[arg-type]
    # Storage was restored in place.
    assert storage_file.read_text(encoding="utf-8") == "ORIGINAL"


async def test_native_v2_zip_round_trip_without_pg_binaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full acceptance flow with pg_dump/pg_restore/psql physically unavailable.

    Empties ``PATH`` for the entire lifecycle so no ``pg_dump``/``pg_restore``/
    ``psql`` binary can be resolved or invoked, then drives the native (v2)
    engine end to end: create backup -> export ZIP -> delete the original set ->
    re-import the ZIP -> destructive restore -> verify data, schema, secret and
    storage all round-trip.
    """
    # No PostgreSQL client binaries are resolvable for the whole test.
    monkeypatch.setenv("PATH", "")
    for tool in ("pg_dump", "pg_restore", "psql", "createdb", "dropdb"):
        assert shutil.which(tool) is None

    base = _admin_base()
    try:
        engine_probe = create_async_engine(
            f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine_probe.connect():
                pass
        finally:
            await engine_probe.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    if not await _can_create_database(base):  # pragma: no cover - env dependent
        pytest.skip("test role cannot CREATE DATABASE")

    monkeypatch.setenv("PPBASE_RESTART_CMD", json.dumps(["/usr/bin/true"]))

    url = await _create_database(base)
    tmp = Path(tempfile.mkdtemp(prefix="ppbase-native-v2-accept-")).resolve(
        strict=True
    )
    data_dir = tmp / "pb_data"
    data_dir.mkdir(mode=0o700)
    storage_dir = data_dir / "storage"
    storage_dir.mkdir(mode=0o700)
    storage_file = storage_dir / "hello.txt"
    storage_file.write_text("ORIGINAL", encoding="utf-8")

    settings = Settings(
        database_url=url,
        data_dir=str(data_dir),
        backup_root=str(tmp / "pb_backups"),
        backup_control_dir=str(tmp / "pb_control"),
        jwt_secret="",
        auto_migrate=False,
    )

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await create_system_tables(engine)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            async with session.begin():
                await bootstrap_system_collections(session, engine)

        await _seed_managed_collection(engine)

        # 1. Create the native (v2) backup under absent pg tools.
        service = NativeBackupService(engine, settings)
        try:
            backup = await service.create_local_backup(actor_id=None)
            backup_id = str(backup["id"])

            # The project secret was persisted and carried into the backup.
            assert (data_dir / ".jwt_secret").is_file()
            original_secret = (data_dir / ".jwt_secret").read_bytes()

            # 2. Export the sealed set as a standalone transport ZIP.
            pinned = await service.materialize_local_backup_zip(backup_id)
            buffer = io.BytesIO()
            for chunk in pinned.iter_bytes(64 * 1024):
                buffer.write(chunk)
            zip_bytes = buffer.getvalue()
            assert zip_bytes[:2] == b"PK"

            # 3. Delete the canonical set so the re-import cannot alias it.
            await service.delete_local_backup(backup_id)
            set_dir = Path(settings.backup_root) / "sets" / backup_id
            assert not set_dir.exists()

            # 4. Re-import the ZIP; the manifest carries the same identity plus
            #    the copy-pair resources and the included JWT secret.
            inspection = await service.upload_local_backup(io.BytesIO(zip_bytes))
            assert inspection["id"] == backup_id
            assert inspection["metadata"]["jwt_secret"]["mode"] == "included_resource"
            resource_paths = {res["path"] for res in inspection["resources"]}
            assert SCHEMA_JSON_RESOURCE in resource_paths
            assert DATA_COPY_RESOURCE in resource_paths
            assert JWT_SECRET_RESOURCE_PATH in resource_paths
        finally:
            service.close()

        # 5. Mutate database + storage away from the backup contents.
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE sentinel SET note = 'mutated' WHERE id = :id"),
                {"id": SENTINEL_ROW_ID},
            )
            await connection.execute(
                text("INSERT INTO sentinel (id, note) VALUES (:id, 'extra-row')"),
                {"id": SENTINEL_EXTRA_ID},
            )
        storage_file.write_text("MUTATED", encoding="utf-8")

        # 6. Destructively restore from the re-imported set.
        service = NativeBackupService(engine, settings)
        cutover_guard = None
        try:
            prepared = await service.restore_local_backup(backup_id, actor_id=None)
            assert prepared["destructive"] is True
            assert prepared["status"] == "restart_scheduled"
            assert prepared["backupId"] == backup_id
            cutover_guard = getattr(prepared, "cutover_guard", None)
            assert cutover_guard is not None
        finally:
            if cutover_guard is not None:
                await cutover_guard.close()
            service.close()

        # 7. Data round-tripped: mutation gone, extra row absent.
        note = await _scalar(
            url, f"SELECT note FROM sentinel WHERE id = '{SENTINEL_ROW_ID}'"
        )
        assert note == "from-backup"
        extra = await _scalar(
            url, f"SELECT count(*) FROM sentinel WHERE id = '{SENTINEL_EXTRA_ID}'"
        )
        assert int(extra) == 0  # type: ignore[arg-type]
        # Schema round-tripped: system + sentinel collections restored.
        collections = await _scalar(
            url,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = '_collections'",
        )
        assert int(collections) == 1  # type: ignore[arg-type]
        sentinel_registered = await _scalar(
            url,
            "SELECT count(*) FROM \"_collections\" WHERE name = 'sentinel'",
        )
        assert int(sentinel_registered) == 1  # type: ignore[arg-type]
        # Storage round-tripped in place.
        assert storage_file.read_text(encoding="utf-8") == "ORIGINAL"
        # Secret is intact on disk and matches what the backup carried.
        assert (data_dir / ".jwt_secret").read_bytes() == original_secret
    finally:
        await engine.dispose()
        await _drop_database(base, url)
        process_control._clear_restart_scheduled()


async def test_native_v2_refuses_unmanaged_registered_table() -> None:
    """A raw table merely *registered* in ``_collections`` is refused.

    The v2 canonical model rebuilds each collection's shape from its stored
    field schema. A physically divergent table — here a collection whose real
    columns PPBase never authored — must fail the backup before any set is
    sealed, rather than being silently captured.
    """
    base = _admin_base()
    try:
        engine_probe = create_async_engine(
            f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine_probe.connect():
                pass
        finally:
            await engine_probe.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    if not await _can_create_database(base):  # pragma: no cover - env dependent
        pytest.skip("test role cannot CREATE DATABASE")

    previous_restart_cmd = os.environ.get("PPBASE_RESTART_CMD")
    os.environ["PPBASE_RESTART_CMD"] = json.dumps(["/usr/bin/true"])

    url = await _create_database(base)
    tmp = Path(tempfile.mkdtemp(prefix="ppbase-native-v2-refuse-")).resolve(
        strict=True
    )
    data_dir = tmp / "pb_data"
    data_dir.mkdir(mode=0o700)
    (data_dir / "storage").mkdir(mode=0o700)

    settings = Settings(
        database_url=url,
        data_dir=str(data_dir),
        backup_root=str(tmp / "pb_backups"),
        backup_control_dir=str(tmp / "pb_control"),
        jwt_secret="",
        auto_migrate=False,
    )

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await create_system_tables(engine)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            async with session.begin():
                await bootstrap_system_collections(session, engine)

        # A raw table PPBase never authored, registered as a base collection
        # (its stored schema is empty, so the canonical shape is id/created/
        # updated only — the physical columns diverge).
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE rogue (weird integer, note text)")
            )
            await connection.execute(
                text(
                    'INSERT INTO "_collections" (id, name, type) '
                    "VALUES (:id, 'rogue', 'base')"
                ),
                {"id": secrets.token_hex(7)},
            )

        service = NativeBackupService(engine, settings)
        try:
            with pytest.raises(BackupServiceError):
                await service.create_local_backup(actor_id=None)
        finally:
            service.close()

        # Nothing was sealed: the backup failed closed.
        sets_dir = Path(settings.backup_root) / "sets"
        published = list(sets_dir.glob("*")) if sets_dir.exists() else []
        assert published == []
    finally:
        await engine.dispose()
        await _drop_database(base, url)
        process_control._clear_restart_scheduled()
        if previous_restart_cmd is None:
            os.environ.pop("PPBASE_RESTART_CMD", None)
        else:
            os.environ["PPBASE_RESTART_CMD"] = previous_restart_cmd


@asynccontextmanager
async def _provisioned_backup_env(prefix: str) -> AsyncIterator[dict[str, object]]:
    """Yield a bootstrapped disposable database + settings for backup creation."""
    base = _admin_base()
    try:
        engine_probe = create_async_engine(
            f"{base}/postgres", poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine_probe.connect():
                pass
        finally:
            await engine_probe.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no reachable PostgreSQL server: {exc}")
    if not await _can_create_database(base):  # pragma: no cover - env dependent
        pytest.skip("test role cannot CREATE DATABASE")

    previous_restart_cmd = os.environ.get("PPBASE_RESTART_CMD")
    os.environ["PPBASE_RESTART_CMD"] = json.dumps(["/usr/bin/true"])
    url = await _create_database(base)
    tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve(strict=True)
    data_dir = tmp / "pb_data"
    data_dir.mkdir(mode=0o700)
    (data_dir / "storage").mkdir(mode=0o700)
    settings = Settings(
        database_url=url,
        data_dir=str(data_dir),
        backup_root=str(tmp / "pb_backups"),
        backup_control_dir=str(tmp / "pb_control"),
        jwt_secret="",
        auto_migrate=False,
    )
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await create_system_tables(engine)
        factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            async with session.begin():
                await bootstrap_system_collections(session, engine)
        yield {"engine": engine, "url": url, "settings": settings}
    finally:
        await engine.dispose()
        await _drop_database(base, url)
        process_control._clear_restart_scheduled()
        if previous_restart_cmd is None:
            os.environ.pop("PPBASE_RESTART_CMD", None)
        else:
            os.environ["PPBASE_RESTART_CMD"] = previous_restart_cmd


async def test_native_v2_refuses_divergent_view_via_create_path() -> None:
    """A divergent registered view refuses the public ``create_local_backup``.

    Proves the canonical view/index reconciliation is wired into the public
    creation path, not merely reachable through the standalone validation
    helper: the live view definition drops a column its registered query
    promises, so backup creation must fail closed before any set is sealed.
    """
    async with _provisioned_backup_env("ppbase-native-v2-view-") as env:
        engine = env["engine"]
        settings = env["settings"]
        await _seed_managed_collection(engine)
        async with engine.begin() as connection:  # type: ignore[union-attr]
            await connection.execute(
                text(
                    "CREATE VIEW sentinel_view AS "
                    "SELECT id FROM public.sentinel"
                )
            )
            await connection.execute(
                text(
                    'INSERT INTO "_collections" '
                    '(id, name, type, schema, options) '
                    "VALUES (:id, 'sentinel_view', 'view', '[]'::jsonb, "
                    "CAST(:options AS jsonb))"
                ),
                {
                    "id": secrets.token_hex(7),
                    "options": json.dumps(
                        {"query": "SELECT id, note FROM public.sentinel"}
                    ),
                },
            )

        service = NativeBackupService(engine, settings)
        try:
            with pytest.raises(BackupServiceError):
                await service.create_local_backup(actor_id=None)
        finally:
            service.close()

        sets_dir = Path(settings.backup_root) / "sets"
        published = list(sets_dir.glob("*")) if sets_dir.exists() else []
        assert published == []


async def test_native_v2_refuses_metadata_drift_during_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An index appearing between validation and the snapshot is caught.

    The write barrier only fences cooperating PPBase writers, so an external
    PostgreSQL client can still mutate a view, index, or ``_collections`` row in
    the gap between the canonical validation transaction and the REPEATABLE
    READ, READ ONLY export snapshot. The fingerprint recorded during validation
    is re-compared inside the snapshot before the first COPY; a mismatch must
    fail the backup closed.
    """
    async with _provisioned_backup_env("ppbase-native-v2-drift-") as env:
        engine = env["engine"]
        settings = env["settings"]
        await _seed_managed_collection(engine)

        import ppbase.backup.service as service_module

        real_fingerprint = service_module.fingerprint_collection_objects
        calls = {"n": 0}

        async def drifting_fingerprint(connection: object) -> str:
            calls["n"] += 1
            # The first call records the validated fingerprint; before the
            # second (in-snapshot) call, an external client creates an index —
            # exactly the drift the re-comparison must catch.
            if calls["n"] == 2:
                async with engine.begin() as external:  # type: ignore[union-attr]
                    await external.execute(
                        text("CREATE INDEX drift_probe ON sentinel (note)")
                    )
            return await real_fingerprint(connection)

        monkeypatch.setattr(
            service_module,
            "fingerprint_collection_objects",
            drifting_fingerprint,
        )

        service = NativeBackupService(engine, settings)
        try:
            with pytest.raises(BackupServiceError) as excinfo:
                await service.create_local_backup(actor_id=None)
        finally:
            service.close()
        assert excinfo.value.code == "schema_drift_during_backup"
        assert calls["n"] == 2

        sets_dir = Path(settings.backup_root) / "sets"
        published = list(sets_dir.glob("*")) if sets_dir.exists() else []
        assert published == []


async def test_native_v2_refuses_drift_between_validation_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift after validation but before the first fingerprint is still caught.

    Validation and the fingerprint that records the validated state share one
    REPEATABLE READ snapshot, so an index an external client creates in that
    window is invisible to the recorded fingerprint yet visible to the export
    snapshot. The re-comparison must therefore refuse the backup. Under a
    READ COMMITTED validation transaction the recorded fingerprint would instead
    capture the unvalidated state and the export re-check would pass.
    """
    async with _provisioned_backup_env("ppbase-native-v2-drift2-") as env:
        engine = env["engine"]
        settings = env["settings"]
        await _seed_managed_collection(engine)

        import ppbase.backup.service as service_module

        real_validate = service_module.validate_canonical_collection_objects
        state = {"injected": False}

        async def validate_then_drift(connection: object) -> None:
            await real_validate(connection)
            # Inject the external index *after* validation has passed but before
            # the validated fingerprint is taken in the same transaction.
            if not state["injected"]:
                state["injected"] = True
                async with engine.begin() as external:  # type: ignore[union-attr]
                    await external.execute(
                        text("CREATE INDEX drift_probe2 ON sentinel (note)")
                    )

        monkeypatch.setattr(
            service_module,
            "validate_canonical_collection_objects",
            validate_then_drift,
        )

        service = NativeBackupService(engine, settings)
        try:
            with pytest.raises(BackupServiceError) as excinfo:
                await service.create_local_backup(actor_id=None)
        finally:
            service.close()
        assert excinfo.value.code == "schema_drift_during_backup"
        assert state["injected"] is True

        sets_dir = Path(settings.backup_root) / "sets"
        published = list(sets_dir.glob("*")) if sets_dir.exists() else []
        assert published == []


async def test_native_v2_accepts_matching_view_via_create_path() -> None:
    """A valid matching registered view backs up successfully via the create path.

    Guards the search-path fingerprint hazard: ``pg_get_viewdef`` schema-
    qualifies relations only when they fall outside the active search path, so
    the validation and export fingerprints must resolve names identically. A
    view whose live definition matches its registered query must seal a set
    rather than be mistaken for ``schema_drift_during_backup``.
    """
    async with _provisioned_backup_env("ppbase-native-v2-view-ok-") as env:
        engine = env["engine"]
        settings = env["settings"]
        await _seed_managed_collection(engine)
        view_query = "SELECT id, note FROM public.sentinel"
        async with engine.begin() as connection:  # type: ignore[union-attr]
            await connection.execute(
                text(f"CREATE VIEW sentinel_view AS {view_query}")
            )
            await connection.execute(
                text(
                    'INSERT INTO "_collections" '
                    '(id, name, type, schema, options) '
                    "VALUES (:id, 'sentinel_view', 'view', '[]'::jsonb, "
                    "CAST(:options AS jsonb))"
                ),
                {
                    "id": secrets.token_hex(7),
                    "options": json.dumps({"query": view_query}),
                },
            )

        service = NativeBackupService(engine, settings)
        try:
            backup = await service.create_local_backup(actor_id=None)
        finally:
            service.close()

        assert backup["id"]
        set_dir = Path(settings.backup_root) / "sets" / str(backup["id"])
        assert (set_dir / "resources" / "database" / "schema.json").is_file()


# The physical column order of ``reordered`` (id, created, updated, alpha, beta,
# gamma) is what ``ALTER TABLE ADD COLUMN`` leaves behind, but ``gamma`` is
# registered in the *middle* of ``_collections.schema`` (alpha, gamma, beta).
# This reproduces the real-world case where a field added after a collection was
# created is appended physically yet ordered arbitrarily in the schema JSON.
REORDERED_PHYSICAL_COLUMNS = (
    "id",
    "created",
    "updated",
    "alpha",
    "beta",
    "gamma",
)
REORDERED_SCHEMA_JSON: list[dict[str, object]] = [
    {"name": "alpha", "type": "text"},
    {"name": "gamma", "type": "text"},
    {"name": "beta", "type": "text"},
]
REORDERED_ROW_ID = "reorderedrow1"


async def _seed_reordered_collection(engine: object) -> None:
    """Create ``reordered`` whose physical order differs from its schema JSON.

    ``alpha``/``beta`` are emitted by ``schema_manager`` in that order, then
    ``gamma`` is physically appended with ``ALTER TABLE ADD COLUMN`` — matching
    exactly the ``text NOT NULL DEFAULT ''`` shape the canonical model expects.
    The registered ``_collections.schema`` lists ``gamma`` between ``alpha`` and
    ``beta`` so the canonical (schema-JSON) column order intentionally diverges
    from the live physical order.
    """
    collection = SimpleNamespace(
        name="reordered",
        type="base",
        schema=[{"name": "alpha", "type": "text"}, {"name": "beta", "type": "text"}],
        options={},
    )
    await schema_manager.create_collection_table(engine, collection)
    async with engine.begin() as connection:  # type: ignore[union-attr]
        # Physically append ``gamma`` at the end (as a later field add would).
        await connection.execute(
            text(
                "ALTER TABLE reordered ADD COLUMN gamma TEXT NOT NULL DEFAULT ''"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO reordered (id, alpha, beta, gamma) "
                "VALUES (:id, 'a-val', 'b-val', 'g-val')"
            ),
            {"id": REORDERED_ROW_ID},
        )
        # Register the collection with ``gamma`` in the *middle* of the schema.
        await connection.execute(
            text(
                'INSERT INTO "_collections" (id, name, type, schema) '
                "VALUES (:id, 'reordered', 'base', CAST(:schema AS jsonb))"
            ),
            {
                "id": secrets.token_hex(7),
                "schema": json.dumps(REORDERED_SCHEMA_JSON),
            },
        )


async def test_native_v2_accepts_reordered_columns_and_keeps_physical_order() -> None:
    """A field added mid-schema but physically appended still backs up.

    Reproduces the real ``bumo_drill`` failure: a table whose column *set*,
    types, defaults, nullability, PK and uniques all match the canonical model
    but whose physical order differs from ``_collections.schema`` field order.
    Reconciliation is by name, so the backup must seal — and ``schema.json`` plus
    the COPY segment must record the live *physical* order, not the schema order.
    """
    async with _provisioned_backup_env("ppbase-native-v2-reorder-") as env:
        engine = env["engine"]
        settings = env["settings"]
        await _seed_reordered_collection(engine)

        service = NativeBackupService(engine, settings)
        try:
            backup = await service.create_local_backup(actor_id=None)
        finally:
            service.close()

        set_dir = Path(settings.backup_root) / "sets" / str(backup["id"])
        schema_path = set_dir / "resources" / "database" / "schema.json"
        schema = DatabaseSchema.from_canonical_bytes(schema_path.read_bytes())

        table = next(t for t in schema.tables if t.name == "reordered")
        # schema.json preserves the live PostgreSQL physical order, not the
        # canonical (schema-JSON) order (id, created, updated, alpha, gamma, beta).
        assert table.column_names == REORDERED_PHYSICAL_COLUMNS
        # The COPY segment streams columns in the same physical order.
        segment = next(s for s in schema.segments if s.table == "reordered")
        assert segment.columns == REORDERED_PHYSICAL_COLUMNS


async def test_native_v2_reordered_collection_destructive_round_trip() -> None:
    """Create + destructive restore of a physically-reordered collection.

    Proves the whole native pipeline round-trips a table whose physical column
    order diverges from its schema-JSON order: the data is restored and the
    restored table keeps the exported physical column order.
    """
    async with _provisioned_backup_env("ppbase-native-v2-reorder-rt-") as env:
        engine = env["engine"]
        settings = env["settings"]
        url = str(env["url"])
        await _seed_reordered_collection(engine)

        service = NativeBackupService(engine, settings)
        try:
            backup = await service.create_local_backup(actor_id=None)
        finally:
            service.close()
        backup_id = str(backup["id"])

        # Mutate away from the backup contents before restoring.
        async with engine.begin() as connection:  # type: ignore[union-attr]
            await connection.execute(
                text("UPDATE reordered SET gamma = 'mutated' WHERE id = :id"),
                {"id": REORDERED_ROW_ID},
            )

        service = NativeBackupService(engine, settings)
        cutover_guard = None
        try:
            prepared = await service.restore_local_backup(backup_id, actor_id=None)
            assert prepared["destructive"] is True
            cutover_guard = getattr(prepared, "cutover_guard", None)
            assert cutover_guard is not None
        finally:
            if cutover_guard is not None:
                await cutover_guard.close()
            service.close()

        # Data restored to the backup contents.
        gamma = await _scalar(
            url, f"SELECT gamma FROM reordered WHERE id = '{REORDERED_ROW_ID}'"
        )
        assert gamma == "g-val"
        # The restored table keeps the exported physical column order.
        order = await _scalar(
            url,
            "SELECT array_agg(attname ORDER BY attnum) "
            "FROM pg_attribute "
            "WHERE attrelid = 'public.reordered'::regclass "
            "AND attnum > 0 AND NOT attisdropped",
        )
        assert tuple(order) == REORDERED_PHYSICAL_COLUMNS  # type: ignore[arg-type]


async def test_native_v2_destructive_restore_round_trip_with_view() -> None:
    """A backup containing a collection view survives a destructive restore.

    Regression for the post-restore verification search-path hazard: the export
    introspects under ``search_path = pg_catalog, pg_temp`` so ``pg_get_viewdef``
    records ``public.``-qualified relations, but the rebuild pins
    ``search_path = public`` for its DDL.  Re-introspecting the rebuilt view
    under the rebuild path renders unqualified names, which previously made every
    view look like drift ("view set differs from the archive") and failed the
    restore.  The verification must render views the same way the archive did.
    """
    async with _provisioned_backup_env("ppbase-native-v2-view-rt-") as env:
        engine = env["engine"]
        settings = env["settings"]
        url = str(env["url"])
        await _seed_managed_collection(engine)
        # A collection view whose body references another public relation, so
        # its captured definition is ``public``-qualified.
        view_query = "SELECT id, note FROM public.sentinel"
        async with engine.begin() as connection:  # type: ignore[union-attr]
            await connection.execute(
                text(f"CREATE VIEW sentinel_view AS {view_query}")
            )
            await connection.execute(
                text(
                    'INSERT INTO "_collections" '
                    '(id, name, type, schema, options) '
                    "VALUES (:id, 'sentinel_view', 'view', '[]'::jsonb, "
                    "CAST(:options AS jsonb))"
                ),
                {
                    "id": secrets.token_hex(7),
                    "options": json.dumps({"query": view_query}),
                },
            )

        service = NativeBackupService(engine, settings)
        try:
            backup = await service.create_local_backup(actor_id=None)
        finally:
            service.close()
        backup_id = str(backup["id"])

        # Drop the view and mutate the base data away from the backup contents.
        async with engine.begin() as connection:  # type: ignore[union-attr]
            await connection.execute(text("DROP VIEW sentinel_view"))
            await connection.execute(
                text("UPDATE sentinel SET note = 'mutated' WHERE id = :id"),
                {"id": SENTINEL_ROW_ID},
            )

        service = NativeBackupService(engine, settings)
        cutover_guard = None
        try:
            prepared = await service.restore_local_backup(backup_id, actor_id=None)
            assert prepared["destructive"] is True
            assert prepared["status"] == "restart_scheduled"
            cutover_guard = getattr(prepared, "cutover_guard", None)
            assert cutover_guard is not None
        finally:
            if cutover_guard is not None:
                await cutover_guard.close()
            service.close()

        # The view is back and queries the restored base data.
        note = await _scalar(
            url, f"SELECT note FROM sentinel_view WHERE id = '{SENTINEL_ROW_ID}'"
        )
        assert note == "from-backup"
        # Rendered under the export search_path (public outside the path), the
        # restored view keeps its ``public``-qualified definition — exactly the
        # form the archive recorded and post-restore verification compares.
        verify_engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with verify_engine.connect() as connection:
                await set_backup_control_search_path(connection)
                definition = await connection.scalar(
                    text(
                        "SELECT pg_catalog.pg_get_viewdef("
                        "'public.sentinel_view'::regclass, true)"
                    )
                )
        finally:
            await verify_engine.dispose()
        assert "public.sentinel" in str(definition)
