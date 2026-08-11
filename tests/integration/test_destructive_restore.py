"""Acceptance tests for the simplified destructive in-place restore engine.

These tests exercise the *current* destructive restore flow end-to-end against
a real PostgreSQL server:

* build and seed a small PPBase database,
* create a local backup ZIP,
* drive :meth:`NativeBackupService.restore_local_backup`, which is the same
  entry point the ``POST /backups/{id}/restore-destructive`` endpoint calls.

Isolation strategy
------------------
Each test spins up an isolated **PostgreSQL 16 testcontainer**. A disposable
cluster is used (rather than the shared dev ``ppbase`` database on
localhost:5433) for two reasons:

* the destructive restore drops and recreates the public schema, so it must
  never run against a database that other work depends on, and
* PostgreSQL 16 is one of the server majors the native asyncpg engine
  supports, so it exercises the cross-major compatibility contract without any
  ``pg_dump``/``pg_restore``/``psql`` client binaries.

Restart stubbing
----------------
The real HTTP endpoint restarts the process with ``os.execvpe`` *after* the
service returns its prepared restore. That restart lives in the API layer
(``ppbase.api.backups._schedule_destructive_restore`` →
``schedule_process_restart``), **not** in ``restore_local_backup`` itself, so
calling the service directly never replaces the test process.

The service does, however, require a restart *command* to be configured and it
reserves the single-flight restart slot via ``reserve_process_restart``. We
therefore point ``PPBASE_RESTART_CMD`` at a harmless command and, after a
successful restore, close the returned cutover guard (which releases the write
barrier and the restart reservation) and reset the process-control singleton so
tests remain independent. No ``os.execvpe`` is ever invoked.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import ppbase.services.process_control as process_control
from ppbase.backup.postgres import preflight_destructive_restore_role
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.backup.storage import DATA_COPY_RESOURCE
from ppbase.config import Settings
from ppbase.db import schema_manager
from ppbase.db.bootstrap import bootstrap_system_collections
from ppbase.db.system_tables import create_system_tables


pytestmark = pytest.mark.asyncio


# A real PPBase-managed base collection: the physical table is emitted through
# ``schema_manager`` (id/created/updated + managed indexes) and its field schema
# is registered in ``_collections`` so the strict v2 canonical model reconciles
# it as an authored table. VARCHAR(15) ids match the PPBase id contract.
SENTINEL_SCHEMA: list[dict[str, object]] = [{"name": "note", "type": "text"}]
SENTINEL_ROW_ID = "sentinelrow01"
SENTINEL_EXTRA_ID = "sentinelrow02"


async def _seed_sentinel_collection(engine: object) -> None:
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


try:  # pragma: no cover - import guard only
    from testcontainers.postgres import PostgresContainer

    _TESTCONTAINERS_IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - environment specific
    PostgresContainer = None  # type: ignore[assignment]
    _TESTCONTAINERS_IMPORT_ERROR = exc


# Exercise every PostgreSQL major the native asyncpg engine supports. The
# in-process restore must rebuild the schema against both a 16 and a 17 server
# without any external PostgreSQL client binaries.
_POSTGRES_IMAGES = ("postgres:16-alpine", "postgres:17-alpine")


@pytest.fixture(scope="module", params=_POSTGRES_IMAGES)
def destructive_restore_cluster(request: pytest.FixtureRequest) -> Iterator[str]:
    """Yield an isolated PostgreSQL base URL per supported major, or skip.

    The returned URL points at the default ``pptest`` database, which is used
    only as an entry point to create/drop a uniquely-named throwaway database
    per test.
    """
    if PostgresContainer is None:
        pytest.skip(f"testcontainers is unavailable: {_TESTCONTAINERS_IMPORT_ERROR}")
    try:
        container = PostgresContainer(
            image=request.param,
            username="pptest",
            password="pptest",
            dbname="pptest",
        )
        container.start()
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.skip(f"Docker/PostgreSQL testcontainer is unavailable: {exc}")
    try:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield f"postgresql+asyncpg://pptest:pptest@{host}:{port}"
    finally:
        container.stop()


async def _create_throwaway_database(base_url: str) -> str:
    """Create a uniquely-named database and return its full URL."""
    database = f"ppbase_restore_{secrets.token_hex(8)}"
    admin_engine = create_async_engine(
        f"{base_url}/pptest", poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database}"'))
    finally:
        await admin_engine.dispose()
    return f"{base_url}/{database}"


async def _drop_throwaway_database(base_url: str, url: str) -> None:
    database = url.rsplit("/", 1)[1]
    admin_engine = create_async_engine(
        f"{base_url}/pptest", poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
    finally:
        await admin_engine.dispose()


@pytest_asyncio.fixture
async def seeded_backup(
    destructive_restore_cluster: str,
) -> Iterator[dict[str, object]]:
    """Bootstrap a fresh database, seed it, and create one backup ZIP.

    Yields a mapping with everything a test needs to drive and verify a
    destructive restore. All process-control state and temporary directories
    are cleaned up afterwards regardless of test outcome.
    """
    base_url = destructive_restore_cluster

    url = await _create_throwaway_database(base_url)

    # A restart command must be *configured* for the service to accept the
    # cutover. It is never executed: the service only reserves a restart slot;
    # os.execvpe is triggered by the API layer, which we do not call.
    previous_restart_cmd = os.environ.get("PPBASE_RESTART_CMD")
    os.environ["PPBASE_RESTART_CMD"] = json.dumps(["/usr/bin/true"])

    # Resolve the temp dir to a real path: the backup control plane rejects any
    # symlink in its path chain (macOS /var -> /private/var).
    tmp = Path(tempfile.mkdtemp(prefix="ppbase-destructive-")).resolve(strict=True)
    data_dir = tmp / "pb_data"
    data_dir.mkdir(mode=0o700)
    storage_dir = data_dir / "storage"
    storage_dir.mkdir(mode=0o700)
    storage_file = storage_dir / "hello.txt"
    storage_file.write_text("ORIGINAL", encoding="utf-8")

    settings = Settings(
        database_url=url,
        data_dir=str(data_dir),
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

    # A sentinel collection lets us prove the active database is (or is not)
    # mutated. It is a real PPBase-managed base collection (emitted through
    # schema_manager with id/created/updated + managed indexes and registered
    # with its field schema) so the strict native v2 canonical contract
    # reconciles it as an authored object instead of failing closed.
    await _seed_sentinel_collection(engine)

    service = NativeBackupService(engine, settings)
    try:
        backup = await service.create_local_backup(actor_id=None)
    finally:
        service.close()
    backup_id = str(backup["key"])

    payload = {
        "url": url,
        "settings": settings,
        "engine": engine,
        "backup_id": backup_id,
        "data_dir": data_dir,
        "storage_file": storage_file,
    }
    try:
        yield payload
    finally:
        await engine.dispose()
        await _drop_throwaway_database(base_url, url)
        # Reset the single-flight restart slot so tests do not leak state.
        process_control._clear_restart_scheduled()
        if previous_restart_cmd is None:
            os.environ.pop("PPBASE_RESTART_CMD", None)
        else:
            os.environ["PPBASE_RESTART_CMD"] = previous_restart_cmd


async def _scalar(url: str, statement: str) -> object:
    """Read a single value on a fresh connection (post-restore safe)."""
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(statement))
    finally:
        await engine.dispose()


async def test_invalid_archive_rejected_before_mutation(
    seeded_backup: dict[str, object],
) -> None:
    """A corrupt archive must be rejected before any active target changes.

    This is the core verify-before-mutate acceptance criterion: the resource
    checksum is validated up front, so a tampered ``data.copy``
    aborts the restore while the live database and its sentinel row are left
    completely untouched.
    """
    url = str(seeded_backup["url"])
    settings = seeded_backup["settings"]
    engine = seeded_backup["engine"]
    backup_id = str(seeded_backup["backup_id"])

    # Corrupt the native COPY payload while leaving backup.json unchanged, so
    # restore must reject the resource checksum before mutating active state.
    archive_path = (  # type: ignore[attr-defined]
        Path(settings.data_dir) / "backups" / backup_id
    )
    rewritten_path = archive_path.with_name(f".{archive_path.name}.corrupt")
    with zipfile.ZipFile(archive_path, "r") as source:
        members = [(info, source.read(info.filename)) for info in source.infolist()]
    with zipfile.ZipFile(rewritten_path, "w") as target:
        for info, payload in members:
            if info.filename == DATA_COPY_RESOURCE:
                raw = bytearray(payload)
                assert raw, "unexpectedly empty database COPY resource"
                raw[0] ^= 0xFF
                payload = bytes(raw)
            target.writestr(info, payload)
    os.replace(rewritten_path, archive_path)

    service = NativeBackupService(engine, settings)  # type: ignore[arg-type]
    try:
        with pytest.raises(BackupServiceError) as excinfo:
            await service.restore_local_backup(backup_id, actor_id=None)
    finally:
        service.close()

    # The restore must have failed for an integrity reason and reserved no
    # restart (nothing was cut over).
    assert excinfo.value.code == "backup_integrity_failed"
    assert not process_control.is_restart_scheduled()

    # The live database is unchanged: the sentinel row is still present and the
    # bootstrapped schema still exists.
    note = await _scalar(
        url, f"SELECT note FROM sentinel WHERE id = '{SENTINEL_ROW_ID}'"
    )
    assert note == "from-backup"
    collections = await _scalar(
        url,
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = '_collections'",
    )
    assert int(collections) == 1  # type: ignore[arg-type]


async def test_destructive_restore_round_trip(
    seeded_backup: dict[str, object],
) -> None:
    """Mutating then restoring returns both database and storage to the backup.

    The restart that the HTTP endpoint would trigger is not invoked here (we
    call the service directly). We assert the destructive restore committed by
    reading the restored values, then close the returned cutover guard to
    release the retained write barrier and restart reservation.
    """
    url = str(seeded_backup["url"])
    settings = seeded_backup["settings"]
    engine = seeded_backup["engine"]
    backup_id = str(seeded_backup["backup_id"])
    storage_file = seeded_backup["storage_file"]
    assert isinstance(storage_file, Path)

    # Mutate the database and the storage file away from the backup contents.
    async with engine.begin() as connection:  # type: ignore[union-attr]
        await connection.execute(
            text(
                "UPDATE sentinel SET note = 'mutated' "
                f"WHERE id = '{SENTINEL_ROW_ID}'"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO sentinel (id, note) "
                f"VALUES ('{SENTINEL_EXTRA_ID}', 'extra-row')"
            )
        )
    storage_file.write_text("MUTATED", encoding="utf-8")

    service = NativeBackupService(engine, settings)  # type: ignore[arg-type]
    cutover_guard = None
    try:
        prepared = await service.restore_local_backup(backup_id, actor_id=None)
        assert prepared["destructive"] is True
        assert prepared["status"] == "restart_scheduled"
        assert prepared["backupId"] == backup_id
        # The service retains a cutover guard that the API would hold until the
        # process restarts; we own its release in this direct-call test.
        cutover_guard = getattr(prepared, "cutover_guard", None)
        assert cutover_guard is not None
    finally:
        if cutover_guard is not None:
            await cutover_guard.close()
        service.close()

    # The database is back to the backup contents: the mutation is gone and the
    # extra row was not part of the backup.
    note = await _scalar(
        url, f"SELECT note FROM sentinel WHERE id = '{SENTINEL_ROW_ID}'"
    )
    assert note == "from-backup"
    extra = await _scalar(
        url, f"SELECT count(*) FROM sentinel WHERE id = '{SENTINEL_EXTRA_ID}'"
    )
    assert int(extra) == 0  # type: ignore[arg-type]

    # The storage file was restored in place from the backup.
    assert storage_file.read_text(encoding="utf-8") == "ORIGINAL"


async def test_nonsuperuser_database_owner_can_replace_public_schema(
    destructive_restore_cluster: str,
) -> None:
    """NOCREATEDB runtime owners need not own every object below public."""
    role = f"runtime_{secrets.token_hex(6)}"
    database = f"owner_restore_{secrets.token_hex(6)}"
    password = "runtime-owner-password"
    base = make_url(f"{destructive_restore_cluster}/pptest")
    admin_engine = create_async_engine(
        base,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    runtime_url = base.set(
        username=role,
        password=password,
        database=database,
    )
    runtime_engine = None
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(
                    f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' '
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE"
                )
            )
            await connection.execute(
                text(f'CREATE DATABASE "{database}" OWNER "{role}"')
            )

        target_admin_engine = create_async_engine(
            base.set(database=database),
            poolclass=NullPool,
        )
        try:
            async with target_admin_engine.begin() as connection:
                await connection.execute(
                    text("CREATE TABLE public.foreign_owned (id integer)")
                )
        finally:
            await target_admin_engine.dispose()

        runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
        async with runtime_engine.connect() as connection:
            flags = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolcreatedb FROM pg_catalog.pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).one()
            assert flags == (False, False)
            foreign_owner = await connection.scalar(
                text(
                    "SELECT pg_catalog.pg_get_userbyid(relowner) "
                    "FROM pg_catalog.pg_class "
                    "WHERE oid = 'public.foreign_owned'::pg_catalog.regclass"
                )
            )
            assert foreign_owner == "pptest"
            report = await preflight_destructive_restore_role(connection)
            assert report.ok

            # Exercise the exact permission the destructive transaction needs.
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.rollback()
    finally:
        if runtime_engine is not None:
            await runtime_engine.dispose()
        async with admin_engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
            await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        await admin_engine.dispose()
