"""End-to-end native backup and non-destructive restore staging tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
import json
import os
from pathlib import Path
import shutil

import jwt
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from ppbase.backup.postgres import replace_sqlalchemy_database
from ppbase.backup import service as backup_service_module
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.backup.storage import (
    AnchoredStagingDataDir,
    AuthenticatedBackupInspection,
)
from ppbase.config import Settings
from ppbase.db.bootstrap import bootstrap_system_collections
from ppbase.db.system_tables import (
    CollectionRecord,
    MigrationRecord,
    SuperuserRecord,
    create_system_tables,
)
from ppbase.services import file_storage
from ppbase.services.admin_service import create_admin
from ppbase.services.auth_service import (
    create_admin_token,
    get_collection_token_config,
)
from ppbase.services.write_barrier import mutation_write_barrier


pytestmark = pytest.mark.asyncio


def _postgres_16_tool(name: str) -> str:
    """Locate the PostgreSQL 16 client used by the disposable PG16 cluster."""
    candidates = [
        shutil.which(name),
        f"/opt/homebrew/opt/postgresql@16/bin/{name}",
        f"/usr/local/opt/postgresql@16/bin/{name}",
        f"/usr/lib/postgresql/16/bin/{name}",
    ]
    for raw_candidate in candidates:
        if not raw_candidate:
            continue
        candidate = Path(raw_candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    pytest.fail(f"PostgreSQL 16 client tool is unavailable: {name}")


@pytest.fixture(scope="module")
def native_backup_admin_url() -> Iterator[str]:
    with PostgresContainer(
        image="postgres:16-alpine",
        username="clusteradmin",
        password="clusteradmin-password",
        dbname="backup_control",
    ) as postgres:
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(5432)
        yield (
            "postgresql+asyncpg://clusteradmin:clusteradmin-password@"
            f"{host}:{port}/backup_control"
        )


def _role_url(admin_url: str, role: str, password: str, database: str) -> str:
    host = admin_url.split("@", 1)[1].rsplit("/", 1)[0]
    return f"postgresql+asyncpg://{role}:{password}@{host}/{database}"


async def _prepare_cluster(admin_url: str) -> tuple[str, str, str, str]:
    admin_engine = create_async_engine(admin_url, poolclass=NullPool)
    async with admin_engine.begin() as connection:
        statements = (
            """
            CREATE ROLE ppbase_runtime LOGIN PASSWORD 'runtime-password'
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_dump LOGIN PASSWORD 'dump-password'
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_stage_owner NOLOGIN
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_stage_creator LOGIN PASSWORD 'creator-password'
                CREATEDB NOINHERIT NOSUPERUSER NOCREATEROLE
                NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_stage_restore LOGIN PASSWORD 'restore-password'
                NOCREATEDB NOINHERIT NOSUPERUSER NOCREATEROLE
                NOREPLICATION NOBYPASSRLS
            """,
            "GRANT ppbase_stage_owner TO ppbase_stage_creator "
            "WITH SET TRUE, ADMIN FALSE, INHERIT FALSE",
            "GRANT ppbase_stage_owner TO ppbase_stage_restore "
            "WITH SET TRUE, ADMIN FALSE, INHERIT FALSE",
            "GRANT pg_read_all_data TO ppbase_dump",
        )
        for statement in statements:
            await connection.execute(text(statement))

    async with admin_engine.connect() as base_connection:
        connection = await base_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await connection.execute(
            text(
                "CREATE DATABASE ppbase_source WITH TEMPLATE template0 "
                "OWNER ppbase_runtime ENCODING 'UTF8'"
            )
        )
        await connection.execute(
            text(
                "REVOKE TEMPORARY ON DATABASE ppbase_source FROM PUBLIC"
            )
        )
    await admin_engine.dispose()

    source_url = _role_url(
        admin_url,
        "ppbase_runtime",
        "runtime-password",
        "ppbase_source",
    )
    dump_url = _role_url(
        admin_url,
        "ppbase_dump",
        "dump-password",
        "ppbase_source",
    )
    creator_url = _role_url(
        admin_url,
        "ppbase_stage_creator",
        "creator-password",
        "backup_control",
    )
    restore_url = _role_url(
        admin_url,
        "ppbase_stage_restore",
        "restore-password",
        "backup_control",
    )
    return source_url, dump_url, creator_url, restore_url


async def test_local_backup_roundtrip_dr_and_clone_to_new_targets_only(
    native_backup_admin_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url, dump_url, creator_url, restore_url = await _prepare_cluster(
        native_backup_admin_url
    )
    active_data_dir = tmp_path / "active-data"
    active_data_dir.mkdir(mode=0o700)
    original_jwt_secret = "A" * 64
    jwt_path = active_data_dir / ".jwt_secret"
    jwt_path.write_text(original_jwt_secret + "\n", encoding="utf-8")
    os.chmod(jwt_path, 0o600)

    settings = Settings(
        database_url=source_url,
        data_dir=str(active_data_dir),
        jwt_secret="",
        auto_migrate=False,
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "backup-control"),
        backup_staging_root=str(tmp_path / "restore-staging"),
        backup_dump_database_url=dump_url,
        backup_creator_database_url=creator_url,
        backup_restore_database_url=restore_url,
        backup_target_owner="ppbase_stage_owner",
        backup_pg_dump_path=_postgres_16_tool("pg_dump"),
        backup_pg_restore_path=_postgres_16_tool("pg_restore"),
        backup_psql_path=_postgres_16_tool("psql"),
    )
    file_storage._set_storage_settings_unchecked(settings)
    file_storage._clear_runtime_storage_overrides_unchecked()

    source_engine = create_async_engine(
        source_url,
        pool_size=1,
        max_overflow=0,
    )
    await create_system_tables(source_engine)
    factory = async_sessionmaker(
        bind=source_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        async with session.begin():
            await bootstrap_system_collections(session, source_engine)
            admin = await create_admin(
                session,
                "backup-admin@example.test",
                "correct horse battery staple",
            )
            session.add(
                MigrationRecord(file="1700000000_initial.py")
            )
        superusers_collection = (
            await session.execute(
                select(CollectionRecord).where(
                    CollectionRecord.name == "_superusers"
                )
            )
        ).scalars().one()
        old_admin_token = create_admin_token(
            admin,
            settings,
            superusers_collection=superusers_collection,
        )
        old_password_hash = admin.password_hash

    collection_id = "docs00000000001"
    record_id = "rec000000000001"
    async with mutation_write_barrier(source_engine) as lease:
        saved_name = file_storage.save_files(
            collection_id,
            record_id,
            "attachment",
            [("evidence.txt", b"native backup file payload")],
            lease=lease,
        )[0]
    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO public."_collections" (
                    id, name, type, system, "schema", indexes, options
                ) VALUES (
                    :id, 'documents', 'base', false,
                    CAST(:schema AS jsonb), '[]', '{}'
                )
                """
            ),
            {
                "id": collection_id,
                "schema": json.dumps(
                    [{"name": "attachment", "type": "file"}]
                ),
            },
        )
        await connection.execute(
            text(
                """
                CREATE TABLE public.documents (
                    id varchar(15) PRIMARY KEY,
                    attachment text NOT NULL
                )
                """
            )
        )
        await connection.execute(
            text(
                "INSERT INTO public.documents (id, attachment) "
                "VALUES (:id, :attachment)"
            ),
            {"id": record_id, "attachment": saved_name},
        )

    active_before = file_storage.read_file_bytes(
        collection_id,
        record_id,
        saved_name,
    )
    service = NativeBackupService(source_engine, settings)
    created = await service.create_local_backup(actor_id=admin.id)
    backup_id = created["id"]
    assert created["status"] == "sealed"
    assert created["authenticated"] is True
    assert created["metadata"]["jwt_secret"]["mode"] == "included_resource"

    listed = await service.list_local_backups()
    assert [item["id"] for item in listed] == [backup_id]
    inspected = await service.inspect_local_backup(backup_id)
    assert inspected["resourcesVerified"] is True
    assert inspected["metadata"]["local_file_reference_inventory"]["count"] == 1
    assert "resources/database.dump" in {
        resource["path"] for resource in inspected["resources"]
    }

    sealed_before_missing_storage = {
        path.name
        for path in (Path(settings.backup_root) / "sets").iterdir()
        if (path / "SEALED").is_file()
    }
    active_storage = active_data_dir / "storage"
    offline_storage = active_data_dir / "storage-offline"
    active_storage.rename(offline_storage)
    try:
        with pytest.raises(BackupServiceError) as missing_storage_error:
            await service.create_local_backup(actor_id=admin.id)
        assert missing_storage_error.value.code == "backup_integrity_failed"
        assert {
            path.name
            for path in (Path(settings.backup_root) / "sets").iterdir()
            if (path / "SEALED").is_file()
        } == sealed_before_missing_storage
        assert not list((Path(settings.backup_root) / "sets").glob(".partial-*"))
    finally:
        offline_storage.rename(active_storage)

    missing_staged_file_plan = await service.create_staging_plan(
        backup_id,
        jwt_secret_mode="clone",
        actor_id=admin.id,
    )
    real_restore_files = AnchoredStagingDataDir.restore_files

    def restore_then_remove_referenced_file(
        target: AnchoredStagingDataDir,
        authenticated: AuthenticatedBackupInspection,
    ) -> Path:
        restored = real_restore_files(target, authenticated)
        (
            target.path
            / "storage"
            / collection_id
            / record_id
            / saved_name
        ).unlink()
        return restored

    with monkeypatch.context() as staged_file_patch:
        staged_file_patch.setattr(
            AnchoredStagingDataDir,
            "restore_files",
            restore_then_remove_referenced_file,
        )
        with pytest.raises(BackupServiceError) as missing_staged_file_error:
            await service.execute_staging_plan(
                missing_staged_file_plan["id"],
                expected_plan_hash=missing_staged_file_plan["planHash"],
            )
    assert missing_staged_file_error.value.code == "backup_integrity_failed"
    assert service.inspect_staging_plan(missing_staged_file_plan["id"])[
        "status"
    ] == "quarantined"

    dr_plan = await service.create_staging_plan(
        backup_id,
        jwt_secret_mode="disaster_recovery",
        actor_id=admin.id,
    )
    dr_result = await service.execute_staging_plan(
        dr_plan["id"],
        expected_plan_hash=dr_plan["planHash"],
    )
    assert dr_result["status"] == "validated"
    assert dr_result["activationPerformed"] is False
    dr_data_dir = Path(dr_result["targetDataDir"])
    assert dr_data_dir != active_data_dir
    assert (dr_data_dir / ".jwt_secret").read_text(encoding="utf-8").strip() == (
        original_jwt_secret
    )
    assert file_storage.read_file_bytes(
        collection_id,
        record_id,
        saved_name,
    ) == active_before
    assert (
        dr_data_dir / "storage" / collection_id / record_id / saved_name
    ).read_bytes() == b"native backup file payload"

    dr_url = replace_sqlalchemy_database(restore_url, dr_result["targetDatabase"])
    dr_engine = create_async_engine(dr_url, poolclass=NullPool)
    async with dr_engine.connect() as connection:
        marker = (
            await connection.execute(
                text(
                    "SELECT shobj_description(d.oid, 'pg_database') "
                    "FROM pg_database AS d "
                    "WHERE d.datname = current_database()"
                )
            )
        ).scalar_one()
        assert marker is None
        await connection.execute(text("SET ROLE ppbase_stage_owner"))
        row = (
            await connection.execute(
                text(
                    'SELECT token_key, password_hash FROM public."_superusers" '
                    "WHERE id = :id"
                ),
                {"id": admin.id},
            )
        ).mappings().one()
        options = (
            await connection.execute(
                text(
                    'SELECT options FROM public."_collections" '
                    "WHERE name = '_superusers'"
                )
            )
        ).scalar_one()
        auth_secret, _ = get_collection_token_config({"options": options}, "authToken")
        assert jwt.decode(
            old_admin_token,
            str(row["token_key"]) + auth_secret,
            algorithms=["HS256"],
        )["id"] == admin.id
        assert row["password_hash"] == old_password_hash
    await dr_engine.dispose()

    clone_plan = await service.create_staging_plan(
        backup_id,
        jwt_secret_mode="clone",
        actor_id=admin.id,
    )
    clone_result = await service.execute_staging_plan(
        clone_plan["id"],
        expected_plan_hash=clone_plan["planHash"],
    )
    assert clone_result["status"] == "validated"
    clone_data_dir = Path(clone_result["targetDataDir"])
    assert clone_data_dir != dr_data_dir
    assert (clone_data_dir / ".jwt_secret").read_text(
        encoding="utf-8"
    ).strip() != original_jwt_secret
    assert clone_result["cloneRotation"]["authRecordCount"] >= 1

    clone_url = replace_sqlalchemy_database(
        restore_url,
        clone_result["targetDatabase"],
    )
    clone_engine = create_async_engine(clone_url, poolclass=NullPool)
    async with clone_engine.connect() as connection:
        await connection.execute(text("SET ROLE ppbase_stage_owner"))
        row = (
            await connection.execute(
                text(
                    'SELECT token_key, password_hash FROM public."_superusers" '
                    "WHERE id = :id"
                ),
                {"id": admin.id},
            )
        ).mappings().one()
        options = (
            await connection.execute(
                text(
                    'SELECT options FROM public."_collections" '
                    "WHERE name = '_superusers'"
                )
            )
        ).scalar_one()
        auth_secret, _ = get_collection_token_config({"options": options}, "authToken")
        with pytest.raises(jwt.InvalidTokenError):
            jwt.decode(
                old_admin_token,
                str(row["token_key"]) + auth_secret,
                algorithms=["HS256"],
            )
        assert row["password_hash"] == old_password_hash
    await clone_engine.dispose()

    settings.jwt_secret = "external-source-secret"
    external_created = await service.create_local_backup(actor_id=admin.id)
    assert external_created["metadata"]["jwt_secret"]["mode"] == (
        "external_required"
    )
    with pytest.raises(BackupServiceError) as external_dr_error:
        await service.create_staging_plan(
            external_created["id"],
            jwt_secret_mode="disaster_recovery",
            actor_id=admin.id,
        )
    assert external_dr_error.value.code == "external_jwt_secret_unverifiable"

    tamper_plan = await service.create_staging_plan(
        external_created["id"],
        jwt_secret_mode="clone",
        actor_id=admin.id,
    )
    real_run_pg_restore = backup_service_module.run_pg_restore_from_fd

    async def restore_then_tamper(*args, **kwargs):
        result = await real_run_pg_restore(*args, **kwargs)
        dump_path = Path(kwargs["archive_label"])
        payload = bytearray(dump_path.read_bytes())
        payload[-1] ^= 0x01
        dump_path.write_bytes(payload)
        return result

    monkeypatch.setattr(
        backup_service_module,
        "run_pg_restore_from_fd",
        restore_then_tamper,
    )
    with pytest.raises(BackupServiceError) as tamper_error:
        await service.execute_staging_plan(
            tamper_plan["id"],
            expected_plan_hash=tamper_plan["planHash"],
        )
    assert tamper_error.value.code == "backup_integrity_failed"
    assert service.inspect_staging_plan(tamper_plan["id"])["status"] == (
        "quarantined"
    )

    guard_loss_plan = await service.create_staging_plan(
        backup_id,
        jwt_secret_mode="clone",
        actor_id=admin.id,
    )
    restore_child_connected = asyncio.Event()
    guard_terminated = asyncio.Event()
    restore_state = {"cancelled": False, "quiescent": False}

    async def block_restore_then_terminate_guard(*args, **kwargs):
        target_database = str(args[0].database)
        observer_engine = create_async_engine(
            native_backup_admin_url,
            poolclass=NullPool,
        )
        target_admin_engine = create_async_engine(
            replace_sqlalchemy_database(
                native_backup_admin_url,
                target_database,
            ),
            poolclass=NullPool,
        )
        killer_task: asyncio.Task[None] | None = None
        try:
            async with target_admin_engine.begin() as target_admin:
                await target_admin.execute(
                    text(
                        "CREATE FUNCTION public.ppbase_test_block_restore() "
                        "RETURNS event_trigger LANGUAGE plpgsql SECURITY DEFINER "
                        "SET search_path = pg_catalog AS $$ "
                        "BEGIN PERFORM pg_catalog.pg_sleep(60); END $$"
                    )
                )
                await target_admin.execute(
                    text(
                        "CREATE EVENT TRIGGER ppbase_test_block_restore "
                        "ON ddl_command_start WHEN TAG IN ('CREATE TABLE') "
                        "EXECUTE FUNCTION public.ppbase_test_block_restore()"
                    )
                )
            async with observer_engine.connect() as observer:
                guard_pids = (
                    await observer.execute(
                        text(
                            "SELECT pid FROM pg_stat_activity "
                            "WHERE datname = :database "
                            "AND usename = 'ppbase_stage_restore' "
                            "ORDER BY pid"
                        ),
                        {"database": target_database},
                    )
                ).scalars().all()
                assert len(guard_pids) == 1
                guard_pid = int(guard_pids[0])
                await observer.rollback()

            async def kill_guard_after_restore_blocks() -> None:
                async with observer_engine.connect() as observer:
                    while True:
                        restore_pids = (
                            await observer.execute(
                                text(
                                    "SELECT pid FROM pg_stat_activity "
                                    "WHERE datname = :database "
                                    "AND usename = 'ppbase_stage_restore' "
                                    "AND pid <> :guard_pid "
                                    "AND wait_event = 'PgSleep'"
                                ),
                                {
                                    "database": target_database,
                                    "guard_pid": guard_pid,
                                },
                            )
                        ).scalars().all()
                        await observer.rollback()
                        if restore_pids:
                            restore_child_connected.set()
                            terminated = bool(
                                (
                                    await observer.execute(
                                        text("SELECT pg_terminate_backend(:pid)"),
                                        {"pid": guard_pid},
                                    )
                                ).scalar_one()
                            )
                            await observer.commit()
                            assert terminated
                            guard_terminated.set()
                            return
                        await asyncio.sleep(0.02)

            killer_task = asyncio.create_task(
                kill_guard_after_restore_blocks()
            )
            try:
                return await real_run_pg_restore(*args, **kwargs)
            except asyncio.CancelledError:
                restore_state["cancelled"] = True
                raise
        finally:
            if killer_task is not None and not killer_task.done():
                killer_task.cancel()
                with suppress(asyncio.CancelledError):
                    await killer_task
            await target_admin_engine.dispose()
            await observer_engine.dispose()
            restore_state["quiescent"] = True

    monkeypatch.setattr(
        backup_service_module,
        "run_pg_restore_from_fd",
        block_restore_then_terminate_guard,
    )
    with pytest.raises(BackupServiceError) as guard_loss_error:
        await asyncio.wait_for(
            service.execute_staging_plan(
                guard_loss_plan["id"],
                expected_plan_hash=guard_loss_plan["planHash"],
            ),
            timeout=30,
        )
    assert guard_loss_error.value.code == "postgres_restore_contract_failed"
    assert restore_child_connected.is_set()
    assert guard_terminated.is_set()
    assert restore_state == {"cancelled": True, "quiescent": True}
    assert service.inspect_staging_plan(guard_loss_plan["id"])["status"] == (
        "quarantined"
    )
    target_admin_engine = create_async_engine(
        replace_sqlalchemy_database(
            native_backup_admin_url,
            guard_loss_plan["targetDatabase"],
        ),
        poolclass=NullPool,
    )
    try:
        async with target_admin_engine.connect() as connection:
            assert (
                await connection.execute(
                    text('SELECT to_regclass(\'public."_collections"\')')
                )
            ).scalar_one() is None
            assert int(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() "
                            "AND usename = 'ppbase_stage_restore'"
                        )
                    )
                ).scalar_one()
            ) == 0
    finally:
        await target_admin_engine.dispose()

    real_backup_write_barrier = backup_service_module.backup_write_barrier

    @asynccontextmanager
    async def switch_to_s3_before_backup_snapshot(*args, **kwargs):
        async with real_backup_write_barrier(*args, **kwargs) as lease:
            file_storage._configure_storage_runtime_from_settings_payload_unchecked(
                {
                    "s3": {
                        "enabled": True,
                        "endpoint": "https://objects.invalid",
                        "bucket": "backup-race",
                        "accessKey": "test-access",
                        "secret": "test-secret",
                    }
                }
            )
            yield lease

    monkeypatch.setattr(
        backup_service_module,
        "backup_write_barrier",
        switch_to_s3_before_backup_snapshot,
    )
    try:
        with pytest.raises(BackupServiceError) as backend_race_error:
            await service.create_local_backup(actor_id=admin.id)
        assert backend_race_error.value.code == "unsupported_storage_backend"
    finally:
        file_storage._clear_runtime_storage_overrides_unchecked()
        monkeypatch.setattr(
            backup_service_module,
            "backup_write_barrier",
            real_backup_write_barrier,
        )

    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO public."_params" (id, key, value)
                VALUES ('backuprace00001', 'settings', CAST(:value AS jsonb))
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """
            ),
            {
                "value": json.dumps(
                    {
                        "s3": {
                            "enabled": True,
                            "endpoint": "https://objects.invalid",
                            "bucket": "durable-backup-race",
                            "accessKey": "durable-access",
                            "secret": "durable-secret",
                        }
                    }
                )
            },
        )
    with pytest.raises(BackupServiceError) as durable_race_error:
        await service.create_local_backup(actor_id=admin.id)
    assert durable_race_error.value.code == "storage_runtime_not_reconciled"
    assert not list((Path(settings.backup_root) / "sets").glob(".partial-*"))

    assert active_data_dir.exists()
    assert active_before == b"native backup file payload"
    await source_engine.dispose()
