"""End-to-end native backup and non-destructive restore staging tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import asynccontextmanager, suppress
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from typing import Any
import uuid

import jwt
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from ppbase.backup import postgres as postgres_module
from ppbase.backup.postgres import (
    CommandResult,
    PostgresCommandError,
    replace_sqlalchemy_database,
)
from ppbase.backup.provision import (
    build_postgres_init_plan,
    execute_postgres_init,
)
from ppbase.backup.activation import (
    apply_activation_runtime_overlay,
    verify_activation_target,
)
from ppbase.backup import service as backup_service_module
from ppbase.backup.service import BackupServiceError, NativeBackupService
from ppbase.backup.storage import (
    AnchoredStagingDataDir,
    AuthenticatedBackupInspection,
    JWT_SECRET_RESOURCE,
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


@pytest.fixture(
    scope="module",
    params=(16, 17),
    ids=("postgres-16", "postgres-17"),
)
def native_backup_lifecycle_cluster(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, int, Any]]:
    major = int(request.param)
    with PostgresContainer(
        image=f"postgres:{major}-alpine",
        username="clusteradmin",
        password="clusteradmin-password",
        dbname="backup_control",
    ) as postgres:
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(5432)
        yield (
            "postgresql+asyncpg://clusteradmin:clusteradmin-password@"
            f"{host}:{port}/backup_control",
            major,
            postgres.get_wrapped_container(),
        )


@pytest.fixture
def native_backup_http_admin_url() -> Iterator[str]:
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


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as selected:
        selected.bind(("127.0.0.1", 0))
        return int(selected.getsockname()[1])


def _put_container_file(container: Any, path: str, payload: bytes) -> None:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo(name=Path(path).name)
        info.size = len(payload)
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(payload))
    container.put_archive(str(Path(path).parent), archive.getvalue())


def _get_container_file(container: Any, path: str) -> bytes:
    stream, _ = container.get_archive(path)
    archive = io.BytesIO(b"".join(stream))
    with tarfile.open(fileobj=archive, mode="r") as tar:
        extracted = tar.extractfile(Path(path).name)
        assert extracted is not None
        return extracted.read()


def _run_container_tool(
    container: Any,
    argv: tuple[str, ...],
    env: dict[str, str],
    redactions: tuple[str, ...],
) -> CommandResult:
    tool = Path(argv[0]).name
    container_argv = [tool, *argv[1:]]
    container_env: dict[str, str] = {}
    token = uuid.uuid4().hex

    passfile_value = env.get("PGPASSFILE")
    if passfile_value:
        passfile_line = Path(passfile_value).read_text(encoding="utf-8")
        fields = passfile_line.rstrip("\n").split(":", 4)
        if len(fields) == 5:
            fields[0] = "127.0.0.1"
            fields[1] = "5432"
            passfile_line = ":".join(fields) + "\n"
        container_passfile = f"/tmp/ppbase-pgpass-{token}"
        _put_container_file(
            container,
            container_passfile,
            passfile_line.encode("utf-8"),
        )
        container_env["PGPASSFILE"] = container_passfile

    if "--dbname" in container_argv:
        index = container_argv.index("--dbname") + 1
        conninfo = container_argv[index]
        conninfo = re.sub(r"host='[^']*'", "host='127.0.0.1'", conninfo)
        conninfo = re.sub(
            r"hostaddr='[^']*'",
            "hostaddr='127.0.0.1'",
            conninfo,
        )
        conninfo = re.sub(r"port='[^']*'", "port='5432'", conninfo)
        container_argv[index] = conninfo

    host_output: Path | None = None
    container_output: str | None = None
    if tool == "pg_dump" and "--file" in container_argv:
        index = container_argv.index("--file") + 1
        host_output = Path(container_argv[index])
        container_output = f"/tmp/ppbase-dump-{token}.dump"
        container_argv[index] = container_output

    if tool == "pg_restore" and "--version" not in container_argv:
        host_archive = Path(container_argv[-1])
        container_archive = f"/tmp/ppbase-restore-{token}.dump"
        _put_container_file(
            container,
            container_archive,
            host_archive.read_bytes(),
        )
        container_argv[-1] = container_archive

    execution = container.exec_run(
        container_argv,
        environment=container_env,
        demux=True,
    )
    stdout_bytes, stderr_bytes = execution.output or (b"", b"")
    stdout = (stdout_bytes or b"").decode("utf-8", errors="replace")
    stderr = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
    for secret_value in redactions:
        if secret_value:
            stderr = stderr.replace(secret_value, "[REDACTED]")
    if execution.exit_code != 0:
        raise PostgresCommandError(tool, execution.exit_code, stderr)
    if host_output is not None and container_output is not None:
        host_output.write_bytes(_get_container_file(container, container_output))
    return CommandResult(argv, execution.exit_code, stdout, stderr)


def _patch_service_container_postgres_tools(
    monkeypatch: pytest.MonkeyPatch,
    container: Any,
    temporary_root: Path,
) -> None:
    async def runner(argv, env, redactions):
        return await asyncio.to_thread(
            _run_container_tool,
            container,
            tuple(str(item) for item in argv),
            dict(env),
            tuple(redactions),
        )

    async def detect_versions(connection, **_kwargs):
        return await postgres_module.detect_postgres_versions(
            connection,
            pg_dump="pg_dump",
            pg_restore="pg_restore",
            runner=runner,
        )

    async def dump(database_url, destination, **kwargs):
        return await postgres_module.run_pg_dump(
            database_url,
            destination,
            pg_dump="pg_dump",
            expected_server_major=kwargs.get("expected_server_major"),
            lock_wait_timeout_seconds=kwargs.get(
                "lock_wait_timeout_seconds",
                30,
            ),
            snapshot_id=kwargs.get("snapshot_id"),
            passfile_directory=kwargs.get("passfile_directory"),
            runner=runner,
        )

    async def inspect_archive(archive, **kwargs):
        return await postgres_module.inspect_pg_restore_archive(
            archive,
            pg_restore="pg_restore",
            expected_server_major=kwargs.get("expected_server_major"),
            runner=runner,
        )

    async def create_database(creator_database_url, target_database, **kwargs):
        creator_info = postgres_module.sqlalchemy_url_to_libpq(
            creator_database_url
        )
        identity_execution = await asyncio.to_thread(
            container.exec_run,
            [
                "psql",
                "--no-password",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--field-separator=|",
                "--host=127.0.0.1",
                "--port=5432",
                f"--username={creator_info.username}",
                f"--dbname={creator_info.database}",
                "--command=SELECT COALESCE(pg_catalog.inet_server_addr()::text, ''), "
                "COALESCE(pg_catalog.inet_server_port(), 0)",
            ],
            environment={"PGPASSWORD": creator_info.password or ""},
            demux=True,
        )
        stdout_bytes, stderr_bytes = identity_execution.output or (b"", b"")
        assert identity_execution.exit_code == 0, (stderr_bytes or b"").decode(
            "utf-8",
            errors="replace",
        )
        tool_address, tool_port = (stdout_bytes or b"").decode(
            "utf-8"
        ).strip().split("|", 1)
        container_identity = dict(kwargs["expected_server_identity"])
        container_identity["server_address"] = tool_address
        container_identity["server_port"] = int(tool_port)
        return await postgres_module.create_target_database(
            creator_database_url,
            target_database,
            target_owner=kwargs["target_owner"],
            restore_role=kwargs["restore_role"],
            runtime_role=kwargs["runtime_role"],
            dump_role=kwargs.get("dump_role"),
            contract=kwargs["contract"],
            expected_server_identity=container_identity,
            psql="psql",
            passfile_directory=kwargs.get("passfile_directory"),
            passfile_factory=kwargs.get("passfile_factory"),
            runner=runner,
        )

    async def restore_from_fd(
        restore_database_url,
        archive_fd,
        *,
        archive_label,
        target_owner,
        expected_server_major=None,
        **_kwargs,
    ):
        archive_copy = temporary_root / f".container-restore-{uuid.uuid4().hex}.dump"
        duplicate = os.dup(archive_fd)
        try:
            os.lseek(duplicate, 0, os.SEEK_SET)
            with os.fdopen(duplicate, "rb") as handle:
                duplicate = -1
                archive_copy.write_bytes(handle.read())
            return await postgres_module.run_pg_restore(
                restore_database_url,
                archive_copy,
                target_owner=target_owner,
                pg_restore="pg_restore",
                expected_server_major=expected_server_major,
                passfile_directory=temporary_root,
                runner=runner,
            )
        finally:
            if duplicate >= 0:
                os.close(duplicate)
            archive_copy.unlink(missing_ok=True)

    monkeypatch.setattr(backup_service_module, "detect_postgres_versions", detect_versions)
    monkeypatch.setattr(backup_service_module, "run_pg_dump", dump)
    monkeypatch.setattr(
        backup_service_module,
        "inspect_pg_restore_archive",
        inspect_archive,
    )
    monkeypatch.setattr(
        backup_service_module,
        "create_target_database",
        create_database,
    )
    monkeypatch.setattr(
        backup_service_module,
        "run_pg_restore_from_fd",
        restore_from_fd,
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
                INHERIT SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS
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
            "GRANT ppbase_stage_owner TO ppbase_runtime "
            "WITH SET TRUE, ADMIN FALSE, INHERIT TRUE",
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


async def _role_snapshot(admin_url: str, role: str) -> dict[str, object]:
    engine = create_async_engine(admin_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT oid, rolname, rolpassword, rolcanlogin, rolinherit, "
                        "rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                        "rolbypassrls, rolconnlimit, rolvaliduntil "
                        "FROM pg_catalog.pg_authid WHERE rolname = :role"
                    ),
                    {"role": role},
                )
            ).mappings().one()
            await connection.rollback()
            return dict(row)
    finally:
        await engine.dispose()


async def test_http_activation_survives_real_exec_restart_and_allows_next_backup(
    native_backup_http_admin_url: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "active-data"
    migrations = tmp_path / "migrations"
    backup_root = tmp_path / "backups"
    control_root = tmp_path / "control"
    staging_root = tmp_path / "staging"
    target_root = tmp_path / "targets"
    onboarding_settings = Settings(
        data_dir=str(data_dir),
        migrations_dir=str(migrations),
        backup_root=str(backup_root),
        backup_control_dir=str(control_root),
        backup_staging_root=str(staging_root),
        backup_target_root=str(target_root),
        backup_pg_dump_path=_postgres_16_tool("pg_dump"),
        backup_pg_restore_path=_postgres_16_tool("pg_restore"),
        backup_psql_path=_postgres_16_tool("psql"),
    )
    sink = tmp_path / "ppbase.env"
    onboarding_plan = await build_postgres_init_plan(
        onboarding_settings,
        bootstrap_database_url=native_backup_http_admin_url,
        project_name="onboard_http",
        secret_sink=sink,
    )
    assert onboarding_plan["readOnly"] is True
    assert onboarding_plan["executable"] is True
    assert not sink.exists()
    initialized = await execute_postgres_init(
        onboarding_settings,
        bootstrap_database_url=native_backup_http_admin_url,
        project_name="onboard_http",
        secret_sink=sink,
    )
    assert initialized["databaseCreated"] is True
    initialized_again = await execute_postgres_init(
        onboarding_settings,
        bootstrap_database_url=native_backup_http_admin_url,
        project_name="onboard_http",
        secret_sink=sink,
    )
    assert initialized_again["noOp"] is True

    limited: dict[str, str] = {}
    for line in sink.read_text(encoding="utf-8").splitlines():
        key, raw = line.split("=", 1)
        limited[key] = json.loads(raw)
    source_url = limited["PPBASE_DATABASE_URL"]
    dump_url = limited["PPBASE_BACKUP_DUMP_DATABASE_URL"]
    creator_url = limited["PPBASE_BACKUP_CREATOR_DATABASE_URL"]
    restore_url = limited["PPBASE_BACKUP_RESTORE_DATABASE_URL"]
    bootstrap_engine = create_async_engine(
        native_backup_http_admin_url,
        poolclass=NullPool,
    )
    async with bootstrap_engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER ROLE onboard_http SUPERUSER CREATEDB CREATEROLE "
                "REPLICATION BYPASSRLS"
            )
        )
    await bootstrap_engine.dispose()
    runtime_before = await _role_snapshot(
        native_backup_http_admin_url,
        "onboard_http",
    )

    (data_dir / "storage").mkdir(mode=0o700)
    (data_dir / ".jwt_secret").write_text("H" * 64 + "\n", encoding="utf-8")
    (data_dir / ".jwt_secret").chmod(0o600)
    migrations.mkdir(mode=0o700)

    source_engine = create_async_engine(source_url, pool_size=1, max_overflow=0)
    await create_system_tables(source_engine)
    factory = async_sessionmaker(
        bind=source_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    email = "http-backup-admin@example.test"
    password = "correct horse battery staple"
    async with factory() as session:
        async with session.begin():
            await bootstrap_system_collections(session, source_engine)
            await create_admin(session, email, password)
    await source_engine.dispose()

    port = _free_tcp_port()
    base_url = f"http://127.0.0.1:{port}"
    environment = {
        **os.environ,
        "PPBASE_DATABASE_URL": source_url,
        "PPBASE_DATA_DIR": str(data_dir),
        "PPBASE_MIGRATIONS_DIR": str(migrations),
        "PPBASE_BACKUP_ROOT": str(backup_root),
        "PPBASE_BACKUP_CONTROL_DIR": str(control_root),
        "PPBASE_BACKUP_STAGING_ROOT": str(staging_root),
        "PPBASE_BACKUP_TARGET_ROOT": str(target_root),
        "PPBASE_BACKUP_DUMP_DATABASE_URL": dump_url,
        "PPBASE_BACKUP_CREATOR_DATABASE_URL": creator_url,
        "PPBASE_BACKUP_RESTORE_DATABASE_URL": restore_url,
        "PPBASE_BACKUP_TARGET_OWNER": limited["PPBASE_BACKUP_TARGET_OWNER"],
        "PPBASE_BACKUP_PG_DUMP_PATH": _postgres_16_tool("pg_dump"),
        "PPBASE_BACKUP_PG_RESTORE_PATH": _postgres_16_tool("pg_restore"),
        "PPBASE_BACKUP_PSQL_PATH": _postgres_16_tool("psql"),
        "PPBASE_AUTO_MIGRATE": "false",
        "PPBASE_APPLY_MIGRATIONS_ON_START": "false",
        "PPBASE_GENERATE_MIGRATIONS": "false",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ppbase",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-automigrate",
        "--no-apply-migrations-on-start",
        "--no-generate-migrations",
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            for _ in range(120):
                try:
                    health = await client.get("/api/health")
                    if health.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            else:
                output = (
                    await process.stdout.read() if process.stdout is not None else b""
                )
                pytest.fail(f"PPBase subprocess did not start: {output.decode(errors='replace')}")

            doctor_process = await asyncio.to_thread(
                subprocess.run,
                [
                    str(Path(sys.executable).with_name("ppbase")),
                    "backup",
                    "doctor",
                    "--server",
                    base_url,
                    "--json",
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            assert doctor_process.returncode == 0, doctor_process.stderr
            doctor = json.loads(doctor_process.stdout)
            restart = next(
                check for check in doctor["checks"] if check["name"] == "restart"
            )
            assert restart["status"] == "pass", doctor
            runtime_warning = next(
                check
                for check in doctor["checks"]
                if check["name"] == "runtime_role"
            )
            assert runtime_warning == {
                "name": "runtime_role",
                "ready": True,
                "status": "warn",
                "code": "legacy_runtime_superuser",
                "detail": "PostgreSQL superuser runtime",
                "role": "onboard_http",
            }
            assert doctor["ready"] is True, doctor
            assert doctor["exitCode"] == 0, doctor

            login = await client.post(
                "/api/admins/auth-with-password",
                json={"identity": email, "password": password},
            )
            assert login.status_code == 200, login.text
            token = login.json()["token"]
            admin_headers = {"Authorization": token}
            created = await client.post("/api/backups", headers=admin_headers)
            assert created.status_code == 201, created.text
            backup_id = created.json()["id"]
            planned = await client.post(
                f"/api/backups/{backup_id}/staging-plans",
                headers=admin_headers,
                json={"jwtSecretMode": "disaster_recovery"},
            )
            assert planned.status_code == 201, planned.text
            plan = planned.json()
            executed = await client.post(
                f"/api/backup-staging/{plan['id']}/execute",
                headers=admin_headers,
                json={"planHash": plan["planHash"]},
            )
            assert executed.status_code == 200, executed.text
            target_data_dir = Path(executed.json()["targetDataDir"])
            assert target_data_dir.is_relative_to(target_root)
            assert not target_data_dir.is_relative_to(staging_root)

            activation_id = secrets.token_hex(16)
            resume_token = secrets.token_urlsafe(48)
            activated = await client.post(
                f"/api/backup-staging/{plan['id']}/activate",
                headers=admin_headers,
                json={
                    "planHash": plan["planHash"],
                    "activationId": activation_id,
                    "resumeToken": resume_token,
                },
            )
            assert activated.status_code == 202, activated.text

            terminal = None
            for _ in range(240):
                try:
                    polled = await client.get(
                        f"/api/backup-activations/{activation_id}",
                        headers={"X-PPBase-Activation-Token": resume_token},
                    )
                    if polled.status_code == 200:
                        terminal = polled.json()
                        if terminal["status"] in {
                            "succeeded",
                            "rolled_back",
                            "failed",
                            "action_required",
                        }:
                            break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
            assert terminal is not None
            assert terminal["status"] == "succeeded", terminal

            listed = await client.get("/api/backups", headers=admin_headers)
            assert listed.status_code == 200, listed.text
            second = await client.post("/api/backups", headers=admin_headers)
            assert second.status_code == 201, second.text
            assert second.json()["id"] != backup_id
    finally:
        if process.returncode is None:
            process.terminate()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=10)
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert await _role_snapshot(
        native_backup_http_admin_url,
        "onboard_http",
    ) == runtime_before


async def test_runtime_superuser_lifecycle_on_postgres_16_and_17(
    native_backup_lifecycle_cluster: tuple[str, int, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_url, _postgres_major, postgres_container = (
        native_backup_lifecycle_cluster
    )
    _patch_service_container_postgres_tools(
        monkeypatch,
        postgres_container,
        tmp_path,
    )
    source_url, dump_url, creator_url, restore_url = await _prepare_cluster(
        admin_url
    )
    runtime_before = await _role_snapshot(admin_url, "ppbase_runtime")
    active_data_dir = tmp_path / "active-data"
    (active_data_dir / "storage").mkdir(parents=True, mode=0o700)
    (active_data_dir / ".jwt_secret").write_text(
        "L" * 64 + "\n",
        encoding="utf-8",
    )
    os.chmod(active_data_dir / ".jwt_secret", 0o600)
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir(mode=0o700)
    settings = Settings(
        database_url=source_url,
        data_dir=str(active_data_dir),
        jwt_secret="",
        auto_migrate=False,
        migrations_dir=str(migrations_dir),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "backup-control"),
        backup_staging_root=str(tmp_path / "restore-staging"),
        backup_target_root=str(tmp_path / "restore-targets"),
        backup_dump_database_url=dump_url,
        backup_creator_database_url=creator_url,
        backup_restore_database_url=restore_url,
        backup_target_owner="ppbase_stage_owner",
        backup_pg_dump_path="pg_dump",
        backup_pg_restore_path="pg_restore",
        backup_psql_path="psql",
    )
    file_storage._set_storage_settings_unchecked(settings)
    file_storage._clear_runtime_storage_overrides_unchecked()
    source_engine = create_async_engine(
        source_url,
        pool_size=1,
        max_overflow=0,
    )
    service: NativeBackupService | None = None
    active_engine = None
    active_service: NativeBackupService | None = None
    try:
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
                    "runtime-superuser-lifecycle@example.test",
                    "correct horse battery staple",
                )

        service = NativeBackupService(source_engine, settings)
        first_backup = await service.create_local_backup(actor_id=admin.id)
        assert first_backup["status"] == "sealed"
        assert first_backup["authenticated"] is True

        dr_plan = await service.create_staging_plan(
            first_backup["id"],
            jwt_secret_mode="disaster_recovery",
            actor_id=admin.id,
        )
        assert dr_plan["preflightWarnings"].count(
            "legacy_runtime_superuser: PostgreSQL superuser runtime"
        ) == 1
        dr_result = await service.execute_staging_plan(
            dr_plan["id"],
            expected_plan_hash=dr_plan["planHash"],
        )
        assert dr_result["status"] == "validated"
        monkeypatch.setenv(
            "PPBASE_RESTART_CMD",
            json.dumps(
                ["python", "-m", "ppbase", "serve", "--db", source_url]
            ),
        )
        dr_activation = await service.activate_staging_plan(
            dr_plan["id"],
            expected_plan_hash=dr_plan["planHash"],
            actor_id=admin.id,
        )
        dr_overlay = SimpleNamespace(
            backup_control_dir=settings.backup_control_dir,
            backup_staging_root=settings.backup_staging_root,
            backup_target_root=settings.backup_target_root,
            database_url=settings.database_url,
            backup_dump_database_url=settings.backup_dump_database_url,
            data_dir=settings.data_dir,
        )
        dr_overlay.get_jwt_secret = lambda: Path(
            dr_overlay.data_dir
        ).joinpath(".jwt_secret").read_text(encoding="utf-8").strip()
        assert apply_activation_runtime_overlay(dr_overlay) is not None
        dr_starting = service.activations.mark_starting(
            dr_activation["activationId"]
        )
        assert verify_activation_target(dr_overlay, dr_starting) == {
            "dataDir": "ok",
            "storage": "ok",
            "jwtSecret": "ok",
        }
        service.activations.mark_healthy(dr_activation["activationId"])
        assert service.inspect_activation(dr_activation["activationId"])[
            "status"
        ] == "succeeded"
        await dr_activation.cutover_guard.close()

        active_settings = settings.model_copy(
            update={
                "database_url": replace_sqlalchemy_database(
                    source_url,
                    dr_result["targetDatabase"],
                ).render_as_string(hide_password=False),
                "backup_dump_database_url": replace_sqlalchemy_database(
                    dump_url,
                    dr_result["targetDatabase"],
                ).render_as_string(hide_password=False),
                "data_dir": dr_result["targetDataDir"],
            }
        )
        file_storage._set_storage_settings_unchecked(active_settings)
        active_engine = create_async_engine(
            active_settings.database_url,
            pool_size=1,
            max_overflow=0,
        )
        active_service = NativeBackupService(active_engine, active_settings)
        second_backup = await active_service.create_local_backup(actor_id=admin.id)
        assert second_backup["status"] == "sealed"
        assert second_backup["id"] != first_backup["id"]

        clone_plan = await active_service.create_staging_plan(
            second_backup["id"],
            jwt_secret_mode="clone",
            actor_id=admin.id,
        )
        assert clone_plan["preflightWarnings"].count(
            "legacy_runtime_superuser: PostgreSQL superuser runtime"
        ) == 1
        clone_result = await active_service.execute_staging_plan(
            clone_plan["id"],
            expected_plan_hash=clone_plan["planHash"],
        )
        assert clone_result["status"] == "validated"
        assert clone_result["cloneRotation"]["authRecordCount"] >= 1
        clone_activation = await active_service.activate_staging_plan(
            clone_plan["id"],
            expected_plan_hash=clone_plan["planHash"],
            actor_id=admin.id,
        )
        clone_overlay = SimpleNamespace(
            backup_control_dir=active_settings.backup_control_dir,
            backup_staging_root=active_settings.backup_staging_root,
            backup_target_root=active_settings.backup_target_root,
            database_url=active_settings.database_url,
            backup_dump_database_url=active_settings.backup_dump_database_url,
            data_dir=active_settings.data_dir,
        )
        clone_overlay.get_jwt_secret = lambda: Path(
            clone_overlay.data_dir
        ).joinpath(".jwt_secret").read_text(encoding="utf-8").strip()
        assert apply_activation_runtime_overlay(clone_overlay) is not None
        clone_starting = active_service.activations.mark_starting(
            clone_activation["activationId"]
        )
        assert verify_activation_target(clone_overlay, clone_starting) == {
            "dataDir": "ok",
            "storage": "ok",
            "jwtSecret": "ok",
        }
        active_service.activations.mark_healthy(clone_activation["activationId"])
        assert active_service.inspect_activation(clone_activation["activationId"])[
            "status"
        ] == "succeeded"
        await clone_activation.cutover_guard.close()
        assert await _role_snapshot(admin_url, "ppbase_runtime") == runtime_before
    finally:
        if active_service is not None:
            active_service.close()
        if active_engine is not None:
            await active_engine.dispose()
        if service is not None:
            service.close()
        await source_engine.dispose()
        file_storage._set_storage_settings_unchecked(settings)
        file_storage._clear_runtime_storage_overrides_unchecked()


async def test_local_backup_roundtrip_dr_and_clone_to_new_targets_only(
    native_backup_admin_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_url, dump_url, creator_url, restore_url = await _prepare_cluster(
        native_backup_admin_url
    )
    runtime_before = await _role_snapshot(
        native_backup_admin_url,
        "ppbase_runtime",
    )
    active_data_dir = tmp_path / "active-data"
    active_data_dir.mkdir(mode=0o700)
    original_jwt_secret = "A" * 64
    jwt_path = active_data_dir / ".jwt_secret"
    jwt_path.write_text(original_jwt_secret + "\n", encoding="utf-8")
    os.chmod(jwt_path, 0o600)

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "1700000000_initial.py").write_text(
        "async def up(app):\n    return None\n\n"
        "async def down(app):\n    return None\n",
        encoding="utf-8",
    )
    pending_migration = "1700000001_restored_probe.py"
    (migrations_dir / pending_migration).write_text(
        "from sqlalchemy import text\n\n"
        "async def up(app):\n"
        "    await app.session.execute(text(\"CREATE TABLE public.restore_migration_probe (id integer PRIMARY KEY)\"))\n\n"
        "async def down(app):\n"
        "    await app.session.execute(text(\"DROP TABLE public.restore_migration_probe\"))\n",
        encoding="utf-8",
    )

    settings = Settings(
        database_url=source_url,
        data_dir=str(active_data_dir),
        jwt_secret="",
        auto_migrate=False,
        migrations_dir=str(migrations_dir),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "backup-control"),
        backup_staging_root=str(tmp_path / "restore-staging"),
        backup_target_root=str(tmp_path / "restore-targets"),
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
    assert missing_staged_file_error.value.code == "backup_integrity_failed", repr(
        missing_staged_file_error.value.__cause__
    )
    assert service.inspect_staging_plan(missing_staged_file_plan["id"])[
        "status"
    ] == "quarantined"

    dr_plan = await service.create_staging_plan(
        backup_id,
        jwt_secret_mode="disaster_recovery",
        actor_id=admin.id,
    )
    assert dr_plan["preflightWarnings"].count(
        "legacy_runtime_superuser: PostgreSQL superuser runtime"
    ) == 1
    dr_result = await service.execute_staging_plan(
        dr_plan["id"],
        expected_plan_hash=dr_plan["planHash"],
    )
    assert dr_result["status"] == "validated"
    assert dr_result["activationPerformed"] is False
    assert dr_result["migrationsApplied"] == [pending_migration]
    dr_data_dir = Path(dr_result["targetDataDir"])
    assert dr_data_dir != active_data_dir
    assert dr_data_dir.is_relative_to(Path(settings.backup_target_root))
    assert not dr_data_dir.is_relative_to(Path(settings.backup_staging_root))
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
        dr_database_identity = await service._postgres_server_identity(connection)
        assert dr_database_identity["database_oid"] > 0
        assert dr_database_identity["database_marker"].startswith(
            "ppbase-restore-marker:"
        )
        await connection.rollback()
        assert (
            await connection.execute(
                text("SELECT to_regclass('public.restore_migration_probe')")
            )
        ).scalar_one() == "restore_migration_probe"
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

    monkeypatch.setenv(
        "PPBASE_RESTART_CMD",
        json.dumps(["python", "-m", "ppbase", "serve", "--db", source_url]),
    )
    async with source_engine.connect() as connection:
        source_database_identity = await service._postgres_server_identity(connection)
        await connection.rollback()
    dr_activation = await service.activate_staging_plan(
        dr_plan["id"],
        expected_plan_hash=dr_plan["planHash"],
        actor_id=admin.id,
    )
    assert dr_activation["status"] == "restart_scheduled"
    assert service.authenticate_activation(
        dr_activation["activationId"],
        dr_activation["resumeToken"],
    )
    activation_state = service.activations.inspect(dr_activation["activationId"])
    assert activation_state["expectedDatabaseIdentity"]["databaseOid"] == (
        dr_database_identity["database_oid"]
    )
    assert activation_state["expectedDatabaseIdentity"]["databaseMarker"] == (
        dr_database_identity["database_marker"]
    )
    assert activation_state["expectedPreviousDatabaseIdentity"][
        "databaseOid"
    ] == source_database_identity["database_oid"]
    assert activation_state["expectedPreviousDatabaseIdentity"][
        "databaseMarker"
    ] == source_database_identity["database_marker"]
    overlay_settings = SimpleNamespace(
        backup_control_dir=settings.backup_control_dir,
        backup_staging_root=settings.backup_staging_root,
        backup_target_root=settings.backup_target_root,
        database_url=source_url,
        backup_dump_database_url=dump_url,
        data_dir=str(active_data_dir),
    )
    overlay_settings.get_jwt_secret = lambda: Path(
        overlay_settings.data_dir
    ).joinpath(".jwt_secret").read_text(encoding="utf-8").strip()
    selected = apply_activation_runtime_overlay(overlay_settings)
    assert selected is not None
    assert overlay_settings.database_url.endswith(
        f"/{dr_result['targetDatabase']}"
    )
    assert overlay_settings.data_dir == dr_result["targetDataDir"]
    assert overlay_settings.backup_dump_database_url.endswith(
        f"/{dr_result['targetDatabase']}"
    )
    starting = service.activations.mark_starting(dr_activation["activationId"])
    assert verify_activation_target(overlay_settings, starting) == {
        "dataDir": "ok",
        "storage": "ok",
        "jwtSecret": "ok",
    }
    service.activations.mark_healthy(dr_activation["activationId"])
    assert service.inspect_activation(dr_activation["activationId"])["status"] == (
        "succeeded"
    )
    # The test simulates startup in-process; a real successful exec closes the
    # old process descriptors and releases these cutover guards atomically.
    await dr_activation.cutover_guard.close()
    dr_runtime_engine = create_async_engine(
        replace_sqlalchemy_database(source_url, dr_result["targetDatabase"]),
        poolclass=NullPool,
    )
    async with dr_runtime_engine.connect() as connection:
        runtime_database_identity = await service._postgres_server_identity(connection)
        assert runtime_database_identity["database_oid"] == (
            dr_database_identity["database_oid"]
        )
        assert runtime_database_identity["database_marker"] == (
            dr_database_identity["database_marker"]
        )
        assert (
            await connection.execute(
                text('SELECT count(*) FROM public."_collections"')
            )
        ).scalar_one() > 0
    await dr_runtime_engine.dispose()

    restarted_settings = settings.model_copy(
        update={
            "database_url": replace_sqlalchemy_database(
                source_url,
                dr_result["targetDatabase"],
            ).render_as_string(hide_password=False),
            "backup_dump_database_url": replace_sqlalchemy_database(
                dump_url,
                dr_result["targetDatabase"],
            ).render_as_string(hide_password=False),
            "data_dir": str(dr_data_dir),
        }
    )
    file_storage._set_storage_settings_unchecked(restarted_settings)
    restarted_engine = create_async_engine(
        restarted_settings.database_url,
        pool_size=1,
        max_overflow=0,
    )
    restarted_service = NativeBackupService(restarted_engine, restarted_settings)
    try:
        assert backup_id in {
            item["id"] for item in await restarted_service.list_local_backups()
        }
        privilege_admin_engine = create_async_engine(
            native_backup_admin_url,
            poolclass=NullPool,
        )
        async with privilege_admin_engine.begin() as connection:
            await connection.execute(
                text("REVOKE pg_read_all_data FROM ppbase_dump")
            )
        try:
            async with restarted_engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE TABLE public.runtime_after_activation "
                        "(id integer PRIMARY KEY)"
                    )
                )
                await connection.execute(
                    text("CREATE SEQUENCE public.runtime_after_activation_seq")
                )
            second_created = await restarted_service.create_local_backup(
                actor_id=admin.id
            )
        finally:
            async with privilege_admin_engine.begin() as connection:
                await connection.execute(
                    text("GRANT pg_read_all_data TO ppbase_dump")
                )
            await privilege_admin_engine.dispose()
        second_plan = await restarted_service.create_staging_plan(
            second_created["id"],
            jwt_secret_mode="disaster_recovery",
            actor_id=admin.id,
        )
        second_result = await restarted_service.execute_staging_plan(
            second_plan["id"],
            expected_plan_hash=second_plan["planHash"],
        )
        assert second_result["status"] == "validated"
        assert Path(second_result["targetDataDir"]).is_relative_to(
            Path(settings.backup_target_root)
        )
        second_activation = await restarted_service.activate_staging_plan(
            second_plan["id"],
            expected_plan_hash=second_plan["planHash"],
            actor_id=admin.id,
        )
        restarted_service.activations.mark_starting(
            second_activation["activationId"]
        )
        restarted_service.activations.mark_healthy(
            second_activation["activationId"]
        )
        assert restarted_service.inspect_activation(
            second_activation["activationId"]
        )["status"] == "succeeded"
        await second_activation.cutover_guard.close()
    finally:
        restarted_service.close()
        await restarted_engine.dispose()
        file_storage._set_storage_settings_unchecked(settings)

    clone_plan = await service.create_staging_plan(
        backup_id,
        jwt_secret_mode="clone",
        actor_id=admin.id,
    )
    assert clone_plan["preflightWarnings"].count(
        "legacy_runtime_superuser: PostgreSQL superuser runtime"
    ) == 1
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

    async with source_engine.connect() as connection:
        assert (
            await connection.execute(
                text("SELECT to_regclass('public.restore_migration_probe')")
            )
        ).scalar_one() is None
        assert int(
            (
                await connection.execute(
                    text('SELECT count(*) FROM public."_migrations"')
                )
            ).scalar_one()
        ) == 1

    settings.jwt_secret = "external-source-secret"
    external_created = await service.create_local_backup(actor_id=admin.id)
    assert external_created["metadata"]["jwt_secret"]["mode"] == (
        "included_resource"
    )
    assert JWT_SECRET_RESOURCE in {
        item["path"] for item in external_created["resources"]
    }
    external_dr_plan = await service.create_staging_plan(
        external_created["id"],
        jwt_secret_mode="disaster_recovery",
        actor_id=admin.id,
    )
    external_dr_result = await service.execute_staging_plan(
        external_dr_plan["id"],
        expected_plan_hash=external_dr_plan["planHash"],
    )
    assert Path(external_dr_result["targetDataDir"]).joinpath(
        ".jwt_secret"
    ).read_text(encoding="utf-8").strip() == "external-source-secret"

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

    pinned_transport = await service.materialize_local_backup_zip(backup_id)
    transport_bytes = b"".join(pinned_transport.iter_bytes(4096))
    assert transport_bytes.startswith(b"PK\x03\x04")
    abandoned_clone = await service.abandon_staging_plan(
        clone_plan["id"],
        expected_plan_hash=clone_plan["planHash"],
        actor_id=admin.id,
    )
    assert abandoned_clone["status"] == "abandoned"
    await service.delete_local_backup(backup_id)
    assert backup_id not in {
        item["id"] for item in await service.list_local_backups()
    }
    with service.mutation_operation() as operation_lease:
        uploaded = await service.upload_local_backup(
            io.BytesIO(transport_bytes),
            operation_lease=operation_lease,
        )
    assert uploaded["id"] == backup_id
    assert uploaded["trustStatus"] == "trusted_local"
    assert (await service.inspect_local_backup(backup_id))["resourcesVerified"] is True
    monkeypatch.setattr(
        backup_service_module,
        "run_pg_restore_from_fd",
        real_run_pg_restore,
    )

    clone_source_settings = restarted_settings.model_copy(
        update={
            "database_url": replace_sqlalchemy_database(
                restarted_settings.database_url,
                second_result["targetDatabase"],
            ).render_as_string(hide_password=False),
            "backup_dump_database_url": replace_sqlalchemy_database(
                restarted_settings.backup_dump_database_url,
                second_result["targetDatabase"],
            ).render_as_string(hide_password=False),
            "data_dir": second_result["targetDataDir"],
        }
    )
    clone_source_engine = create_async_engine(
        clone_source_settings.database_url,
        pool_size=1,
        max_overflow=0,
    )
    clone_source_service = NativeBackupService(
        clone_source_engine,
        clone_source_settings,
    )
    try:
        activated_clone_plan = await clone_source_service.create_staging_plan(
            backup_id,
            jwt_secret_mode="clone",
            actor_id=admin.id,
        )
        assert activated_clone_plan["preflightWarnings"].count(
            "legacy_runtime_superuser: PostgreSQL superuser runtime"
        ) == 1
        activated_clone_result = await clone_source_service.execute_staging_plan(
            activated_clone_plan["id"],
            expected_plan_hash=activated_clone_plan["planHash"],
        )
        clone_activation = await clone_source_service.activate_staging_plan(
            activated_clone_plan["id"],
            expected_plan_hash=activated_clone_plan["planHash"],
            actor_id=admin.id,
        )
        clone_overlay = SimpleNamespace(
            backup_control_dir=clone_source_settings.backup_control_dir,
            backup_staging_root=clone_source_settings.backup_staging_root,
            backup_target_root=clone_source_settings.backup_target_root,
            database_url=clone_source_settings.database_url,
            backup_dump_database_url=clone_source_settings.backup_dump_database_url,
            data_dir=clone_source_settings.data_dir,
        )
        clone_overlay.get_jwt_secret = lambda: Path(
            clone_overlay.data_dir
        ).joinpath(".jwt_secret").read_text(encoding="utf-8").strip()
        assert apply_activation_runtime_overlay(clone_overlay) is not None
        clone_starting = clone_source_service.activations.mark_starting(
            clone_activation["activationId"]
        )
        assert verify_activation_target(clone_overlay, clone_starting) == {
            "dataDir": "ok",
            "storage": "ok",
            "jwtSecret": "ok",
        }
        clone_source_service.activations.mark_healthy(
            clone_activation["activationId"]
        )
        assert clone_source_service.inspect_activation(
            clone_activation["activationId"]
        )["status"] == "succeeded"
        assert clone_overlay.database_url.endswith(
            f"/{activated_clone_result['targetDatabase']}"
        )
        await clone_activation.cutover_guard.close()
    finally:
        clone_source_service.close()
        await clone_source_engine.dispose()

    destination_settings = settings.model_copy(
        update={
            "backup_root": str(tmp_path / "server-b-backups"),
            "backup_control_dir": str(tmp_path / "server-b-control"),
            "backup_staging_root": str(tmp_path / "server-b-staging"),
            "backup_target_root": str(tmp_path / "server-b-targets"),
        }
    )
    destination = NativeBackupService(source_engine, destination_settings)
    try:
        with destination.mutation_operation() as operation_lease:
            foreign_upload = await destination.upload_local_backup(
                io.BytesIO(transport_bytes),
                operation_lease=operation_lease,
            )
        assert foreign_upload["status"] == "quarantined"
        assert foreign_upload["trustStatus"] == "authenticated_untrusted"
        with pytest.raises(BackupServiceError) as unapproved_plan:
            await destination.create_staging_plan(
                backup_id,
                jwt_secret_mode="clone",
                actor_id=admin.id,
            )
        assert unapproved_plan.value.code == "backup_signer_untrusted"

        approved = await destination.approve_backup_signer(
            backup_id,
            expected_fingerprint_sha256=service.identity.fingerprint_sha256,
            actor_id=admin.id,
        )
        assert approved["trustStatus"] == "trusted_external"
        foreign_plan = await destination.create_staging_plan(
            backup_id,
            jwt_secret_mode="clone",
            actor_id=admin.id,
        )
        foreign_result = await destination.execute_staging_plan(
            foreign_plan["id"],
            expected_plan_hash=foreign_plan["planHash"],
        )
        assert foreign_result["status"] == "validated"
        assert foreign_result["migrationsApplied"] == [pending_migration]
    finally:
        destination.close()

    assert active_data_dir.exists()
    assert active_before == b"native backup file payload"
    assert await _role_snapshot(
        native_backup_admin_url,
        "ppbase_runtime",
    ) == runtime_before
    service.close()
    await source_engine.dispose()
