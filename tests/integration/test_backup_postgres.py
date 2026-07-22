from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import re
import json
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
import tarfile
from typing import Any, Generator
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from ppbase.backup.postgres import (
    CommandResult,
    LEGACY_RUNTIME_SUPERUSER_WARNING,
    PostgresCommandError,
    PostgresContractError,
    create_target_database,
    grant_dump_role_read_access,
    inspect_database_contract,
    preflight_database_contract,
    preflight_dump_role,
    replace_sqlalchemy_database,
    run_pg_dump,
    run_pg_restore,
)
from ppbase.backup.validation import (
    rotate_clone_database_secrets,
    validate_staged_database,
)
from ppbase.backup.provision import (
    BackupProvisionError,
    backup_doctor,
    build_provision_plan,
    build_postgres_init_plan,
    doctor_human,
    execute_postgres_init,
    execute_provision,
    write_secret_sink,
)
from ppbase.backup import provision as provision_module
from ppbase.config import Settings
from ppbase.db.system_tables import create_system_tables


@pytest.fixture(
    scope="module",
    params=(16, 17),
    ids=("postgres-16", "postgres-17"),
)
def backup_postgres_cluster(
    request: pytest.FixtureRequest,
) -> Generator[tuple[str, int, Any], None, None]:
    """Always use a disposable PostgreSQL instance for destructive restore tests."""
    major = int(request.param)
    with PostgresContainer(
        image=f"postgres:{major}-alpine",
        username="pptest",
        password="pptest",
        dbname="pptest",
    ) as postgres:
        host = postgres.get_container_host_ip()
        port = postgres.get_exposed_port(5432)
        url = f"postgresql+asyncpg://pptest:pptest@{host}:{port}/pptest"
        yield url, major, postgres.get_wrapped_container()


def _put_container_file(container: Any, path: str, payload: bytes) -> None:
    archive = BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo(name=Path(path).name)
        info.size = len(payload)
        info.mode = 0o600
        tar.addfile(info, BytesIO(payload))
    container.put_archive(str(Path(path).parent), archive.getvalue())


def _get_container_file(container: Any, path: str) -> bytes:
    stream, _ = container.get_archive(path)
    archive = BytesIO(b"".join(stream))
    with tarfile.open(fileobj=archive, mode="r") as tar:
        extracted = tar.extractfile(Path(path).name)
        assert extracted is not None
        return extracted.read()


def _container_tool_runner(container: Any):
    async def runner(argv, env, redactions):
        return await asyncio.to_thread(
            _run_container_tool,
            container,
            tuple(str(item) for item in argv),
            dict(env),
            tuple(redactions),
        )

    return runner


@pytest.mark.asyncio
async def test_native_backup_provision_is_idempotent_and_doctor_needs_no_admin(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_url, _major, _container = backup_postgres_cluster
    runtime_password = "runtime-password"
    admin_engine = create_async_engine(bootstrap_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE ROLE provision_runtime LOGIN INHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                "PASSWORD 'runtime-password'"
            )
        )
    async with admin_engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await connection.execute(
            text(
                "CREATE DATABASE provision_app WITH TEMPLATE template0 "
                "OWNER provision_runtime ENCODING 'UTF8'"
            )
        )
    await admin_engine.dispose()

    runtime_url = make_url(bootstrap_url).set(
        username="provision_runtime",
        password=runtime_password,
        database="provision_app",
    ).render_as_string(hide_password=False)
    runtime_engine = create_async_engine(runtime_url)
    async with runtime_engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE public.before_provision (id integer PRIMARY KEY)")
        )
        await connection.execute(text("SELECT pg_catalog.lo_create(0)"))
    await runtime_engine.dispose()
    for name in ("data", "backups", "control", "staging", "targets"):
        (tmp_path / name).mkdir(mode=0o700)
    settings = Settings(
        database_url=runtime_url,
        data_dir=str(tmp_path / "data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
        backup_pg_dump_path=sys.executable,
        backup_pg_restore_path=sys.executable,
        backup_psql_path=sys.executable,
    )

    first_sink = tmp_path / "backup-first.env"
    real_ensure_role = provision_module._ensure_role
    injected = False

    async def fail_once(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise OSError("simulated connection loss before commit")
        return await real_ensure_role(*args, **kwargs)

    monkeypatch.setattr(provision_module, "_ensure_role", fail_once)
    with pytest.raises(BackupProvisionError, match="rerun the same command"):
        await execute_provision(
            settings,
            bootstrap_database_url=bootstrap_url,
            secret_sink=first_sink,
        )
    assert first_sink.exists()
    first_sink_identity = (first_sink.stat().st_ino, first_sink.stat().st_mtime_ns)
    monkeypatch.setattr(provision_module, "_ensure_role", real_ensure_role)
    first = await execute_provision(
        settings,
        bootstrap_database_url=bootstrap_url,
        secret_sink=first_sink,
    )
    assert set(first["createdRoles"]) == {
        "ppbase_backup_dump",
        "ppbase_backup_creator",
        "ppbase_backup_restore",
        "ppbase_backup_owner",
    }
    assert stat.S_IMODE(first_sink.stat().st_mode) == 0o600
    assert (first_sink.stat().st_ino, first_sink.stat().st_mtime_ns) == (
        first_sink_identity
    )
    assert "pptest:pptest@" not in first_sink.read_text(encoding="utf-8")

    values = {}
    for line in first_sink.read_text(encoding="utf-8").splitlines():
        key, raw = line.split("=", 1)
        values[key] = json.loads(raw)
    configured = settings.model_copy(
        update={
            "backup_dump_database_url": values["PPBASE_BACKUP_DUMP_DATABASE_URL"],
            "backup_creator_database_url": values[
                "PPBASE_BACKUP_CREATOR_DATABASE_URL"
            ],
            "backup_restore_database_url": values[
                "PPBASE_BACKUP_RESTORE_DATABASE_URL"
            ],
            "backup_target_owner": values["PPBASE_BACKUP_TARGET_OWNER"],
        }
    )
    second = await execute_provision(
        configured,
        bootstrap_database_url=bootstrap_url,
        secret_sink=tmp_path / "backup-second.env",
    )
    assert second["createdRoles"] == []

    runtime_engine = create_async_engine(runtime_url)
    async with runtime_engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE public.after_provision (id integer PRIMARY KEY)")
        )
        await connection.execute(text("SELECT pg_catalog.lo_create(0)"))
    await runtime_engine.dispose()
    dump_engine = create_async_engine(values["PPBASE_BACKUP_DUMP_DATABASE_URL"])
    async with dump_engine.connect() as connection:
        before_refresh = await preflight_dump_role(connection)
        assert before_refresh.ok is False
        assert any("large_object" in error for error in before_refresh.errors)
        await connection.rollback()
    await dump_engine.dispose()
    refreshed = await execute_provision(
        configured,
        bootstrap_database_url=bootstrap_url,
        secret_sink=first_sink,
    )
    assert refreshed["createdRoles"] == []

    monkeypatch.setenv("PPBASE_RESTART_CMD", json.dumps([sys.executable, "-m", "ppbase", "serve"]))
    async def matching_tool_version(executable: str):
        return SimpleNamespace(executable=executable, version=str(_major), major=_major)

    monkeypatch.setattr(
        provision_module,
        "detect_postgres_tool_version",
        matching_tool_version,
    )
    doctor = await backup_doctor(configured)
    assert doctor["ready"] is True, doctor
    assert bootstrap_url not in json.dumps(doctor)


@pytest.mark.asyncio
async def test_backup_provision_accepts_legacy_runtime_superuser_without_mutation(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_url, major, _container = backup_postgres_cluster
    runtime_role = "legacy_product_runtime"
    database = "legacy_product_app"
    dump_role = "legacy_product_dump"
    creator_role = "legacy_product_creator"
    restore_role = "legacy_product_restore"
    owner_role = "legacy_product_owner"
    runtime_password = "legacy-runtime-password"
    bootstrap = make_url(bootstrap_url)
    admin_engine = create_async_engine(bootstrap_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                f"CREATE ROLE {runtime_role} LOGIN INHERIT SUPERUSER CREATEDB "
                "CREATEROLE REPLICATION BYPASSRLS "
                f"PASSWORD '{runtime_password}'"
            )
        )
    async with admin_engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await connection.execute(
            text(
                f"CREATE DATABASE {database} WITH TEMPLATE template0 "
                f"OWNER {runtime_role} ENCODING 'UTF8'"
            )
        )

    def role_url(role: str, password: str, *, maintenance: bool = False) -> str:
        return bootstrap.set(
            username=role,
            password=password,
            database="pptest" if maintenance else database,
        ).render_as_string(hide_password=False)

    runtime_url = role_url(runtime_role, runtime_password)
    for name in ("data", "backups", "control", "staging", "targets"):
        (tmp_path / name).mkdir(mode=0o700)
    settings = Settings(
        database_url=runtime_url,
        data_dir=str(tmp_path / "data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
        backup_dump_database_url=role_url(dump_role, "planned-dump-password"),
        backup_creator_database_url=role_url(
            creator_role,
            "planned-creator-password",
            maintenance=True,
        ),
        backup_restore_database_url=role_url(
            restore_role,
            "planned-restore-password",
            maintenance=True,
        ),
        backup_target_owner=owner_role,
        backup_pg_dump_path=sys.executable,
        backup_pg_restore_path=sys.executable,
        backup_psql_path=sys.executable,
    )

    async def runtime_snapshot() -> dict[str, Any]:
        async with admin_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT oid, rolname, rolpassword, rolcanlogin, rolinherit, "
                        "rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                        "rolbypassrls, rolconnlimit, rolvaliduntil "
                        "FROM pg_catalog.pg_authid WHERE rolname = :role"
                    ),
                    {"role": runtime_role},
                )
            ).mappings().one()
            await connection.rollback()
            return dict(row)

    before = await runtime_snapshot()
    plan = await build_provision_plan(settings)
    expected_warning = {
        "code": "legacy_runtime_superuser",
        "detail": "PostgreSQL superuser runtime",
        "role": runtime_role,
    }
    assert plan["executable"] is True, plan
    assert plan["collisions"] == []
    assert plan["warnings"] == [expected_warning]

    sink = tmp_path / "legacy-superuser.env"
    first = await execute_provision(
        settings,
        bootstrap_database_url=bootstrap_url,
        secret_sink=sink,
    )
    assert set(first["createdRoles"]) == {
        dump_role,
        creator_role,
        restore_role,
        owner_role,
    }
    assert first["warnings"] == [expected_warning]
    assert await runtime_snapshot() == before
    sink_identity = (sink.stat().st_ino, sink.stat().st_mtime_ns)

    values: dict[str, str] = {}
    for line in sink.read_text(encoding="utf-8").splitlines():
        key, raw = line.split("=", 1)
        values[key] = json.loads(raw)
    configured = settings.model_copy(
        update={
            "backup_dump_database_url": values[
                "PPBASE_BACKUP_DUMP_DATABASE_URL"
            ],
            "backup_creator_database_url": values[
                "PPBASE_BACKUP_CREATOR_DATABASE_URL"
            ],
            "backup_restore_database_url": values[
                "PPBASE_BACKUP_RESTORE_DATABASE_URL"
            ],
            "backup_target_owner": values["PPBASE_BACKUP_TARGET_OWNER"],
        }
    )
    second = await execute_provision(
        configured,
        bootstrap_database_url=bootstrap_url,
        secret_sink=sink,
    )
    assert second["createdRoles"] == []
    assert second["warnings"] == [expected_warning]
    assert (sink.stat().st_ino, sink.stat().st_mtime_ns) == sink_identity
    assert await runtime_snapshot() == before

    async with admin_engine.connect() as connection:
        managed = (
            await connection.execute(
                text(
                    "SELECT rolname, rolcanlogin, rolcreatedb, rolinherit, "
                    "rolsuper, rolcreaterole, rolreplication, rolbypassrls "
                    "FROM pg_catalog.pg_roles "
                    "WHERE rolname = ANY(CAST(:roles AS text[])) "
                    "ORDER BY rolname"
                ),
                {"roles": [dump_role, creator_role, restore_role, owner_role]},
            )
        ).mappings().all()
        await connection.rollback()
    observed = {str(row["rolname"]): dict(row) for row in managed}
    assert observed[dump_role] == {
        "rolname": dump_role,
        "rolcanlogin": True,
        "rolcreatedb": False,
        "rolinherit": False,
        "rolsuper": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    }
    assert observed[creator_role] == {
        "rolname": creator_role,
        "rolcanlogin": True,
        "rolcreatedb": True,
        "rolinherit": False,
        "rolsuper": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    }
    assert observed[restore_role] == {
        "rolname": restore_role,
        "rolcanlogin": True,
        "rolcreatedb": False,
        "rolinherit": False,
        "rolsuper": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    }
    assert observed[owner_role] == {
        "rolname": owner_role,
        "rolcanlogin": False,
        "rolcreatedb": False,
        "rolinherit": False,
        "rolsuper": False,
        "rolcreaterole": False,
        "rolreplication": False,
        "rolbypassrls": False,
    }

    monkeypatch.setenv(
        "PPBASE_RESTART_CMD",
        json.dumps([sys.executable, "-m", "ppbase", "serve"]),
    )

    async def matching_tool_version(executable: str):
        return SimpleNamespace(executable=executable, version=str(major), major=major)

    monkeypatch.setattr(
        provision_module,
        "detect_postgres_tool_version",
        matching_tool_version,
    )
    doctor = await backup_doctor(configured)
    runtime_warning = next(
        check for check in doctor["checks"] if check["name"] == "runtime_role"
    )
    assert runtime_warning == {
        "name": "runtime_role",
        "ready": True,
        "status": "warn",
        **expected_warning,
    }
    assert doctor["ready"] is True, doctor
    assert doctor["exitCode"] == 0
    assert doctor_human(doctor).endswith("Ready with security warnings.")
    assert bootstrap_url not in json.dumps(doctor)

    async with admin_engine.begin() as connection:
        await connection.execute(text(f"ALTER ROLE {creator_role} SUPERUSER"))
    blocked_managed_role = await build_provision_plan(configured)
    assert blocked_managed_role["executable"] is False
    assert any(
        collision["role"] == creator_role
        for collision in blocked_managed_role["collisions"]
    )
    async with admin_engine.begin() as connection:
        await connection.execute(text(f"ALTER ROLE {creator_role} NOSUPERUSER"))
        await connection.execute(text(f"ALTER ROLE {runtime_role} NOSUPERUSER"))
    blocked_runtime = await build_provision_plan(configured)
    assert blocked_runtime["executable"] is False
    assert blocked_runtime["warnings"] == []
    assert any(
        collision["role"] == runtime_role
        for collision in blocked_runtime["collisions"]
    )
    async with admin_engine.begin() as connection:
        await connection.execute(text(f"ALTER ROLE {runtime_role} SUPERUSER"))
    assert await runtime_snapshot() == before
    await admin_engine.dispose()


@pytest.mark.asyncio
async def test_backup_provision_rejects_bad_existing_credential_without_mutation(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
) -> None:
    bootstrap_url, _major, _container = backup_postgres_cluster
    database = "provision_credential_guard"
    runtime_role = "provision_guard_runtime"
    dump_role = "provision_guard_dump"
    creator_role = "provision_guard_creator"
    restore_role = "provision_guard_restore"
    owner_role = "provision_guard_owner"
    managed_roles = (
        runtime_role,
        dump_role,
        creator_role,
        restore_role,
        owner_role,
    )

    admin_engine = create_async_engine(bootstrap_url)
    async with admin_engine.begin() as connection:
        statements = (
            f"CREATE ROLE {runtime_role} LOGIN INHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'runtime-password'",
            f"CREATE ROLE {dump_role} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'dump-password'",
            f"CREATE ROLE {creator_role} LOGIN NOINHERIT NOSUPERUSER CREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'creator-password'",
            f"CREATE ROLE {restore_role} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD 'restore-password'",
        )
        for statement in statements:
            await connection.execute(text(statement))
    async with admin_engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await connection.execute(
            text(
                f"CREATE DATABASE {database} WITH TEMPLATE template0 "
                f"OWNER {runtime_role} ENCODING 'UTF8'"
            )
        )

    bootstrap = make_url(bootstrap_url)

    def role_url(role: str, password: str) -> str:
        return bootstrap.set(
            username=role,
            password=password,
            database=database,
        ).render_as_string(hide_password=False)

    runtime_url = role_url(runtime_role, "runtime-password")
    target_admin_engine = create_async_engine(
        bootstrap.set(database=database),
    )
    runtime_engine = create_async_engine(runtime_url)
    async with runtime_engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE public.provision_guard_table (id integer PRIMARY KEY)")
        )
        await connection.execute(
            text("CREATE SEQUENCE public.provision_guard_sequence")
        )
        await connection.execute(text("SELECT pg_catalog.lo_create(0)"))
    await runtime_engine.dispose()

    def freeze(rows: list[dict[str, Any]]) -> tuple[tuple[tuple[str, str | None], ...], ...]:
        return tuple(
            tuple(
                (str(key), None if value is None else str(value))
                for key, value in row.items()
            )
            for row in rows
        )

    async def snapshot() -> dict[str, tuple[tuple[tuple[str, str | None], ...], ...]]:
        queries = {
            "roles": (
                "SELECT rolname, oid::text AS oid, rolcanlogin, rolcreatedb, "
                "rolinherit, rolsuper, rolcreaterole, rolreplication, rolbypassrls, "
                "rolpassword FROM pg_catalog.pg_authid "
                "WHERE rolname = ANY(CAST(:roles AS text[])) ORDER BY rolname"
            ),
            "memberships": (
                "SELECT member_role.rolname AS member, "
                "granted_role.rolname AS granted, grantor_role.rolname AS grantor, "
                "membership.admin_option, "
                "COALESCE((pg_catalog.to_jsonb(membership)->>'set_option')::boolean, true) "
                "AS set_option, "
                "COALESCE((pg_catalog.to_jsonb(membership)->>'inherit_option')::boolean, "
                "member_role.rolinherit) AS inherit_option "
                "FROM pg_catalog.pg_auth_members AS membership "
                "JOIN pg_catalog.pg_roles AS member_role ON member_role.oid = membership.member "
                "JOIN pg_catalog.pg_roles AS granted_role ON granted_role.oid = membership.roleid "
                "JOIN pg_catalog.pg_roles AS grantor_role ON grantor_role.oid = membership.grantor "
                "WHERE member_role.rolname = ANY(CAST(:roles AS text[])) "
                "OR granted_role.rolname = ANY(CAST(:roles AS text[])) "
                "ORDER BY member, granted, grantor"
            ),
            "database_acl": (
                "SELECT owner.rolname AS owner, database.datacl::text AS acl "
                "FROM pg_catalog.pg_database AS database "
                "JOIN pg_catalog.pg_roles AS owner ON owner.oid = database.datdba "
                "WHERE database.datname = pg_catalog.current_database()"
            ),
            "schemas": (
                "SELECT namespace.nspname AS schema, "
                "pg_catalog.pg_get_userbyid(namespace.nspowner) AS owner, "
                "namespace.nspacl::text AS acl "
                "FROM pg_catalog.pg_namespace AS namespace "
                "WHERE namespace.nspname <> 'information_schema' "
                "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                "ORDER BY schema"
            ),
            "relations": (
                "SELECT namespace.nspname AS schema, relation.relname AS name, "
                "relation.relkind AS kind, "
                "pg_catalog.pg_get_userbyid(relation.relowner) AS owner, "
                "relation.relacl::text AS acl "
                "FROM pg_catalog.pg_class AS relation "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = relation.relnamespace "
                "WHERE namespace.nspname <> 'information_schema' "
                "AND namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\' "
                "ORDER BY schema, name, kind"
            ),
            "large_objects": (
                "SELECT large_object.oid::text AS oid, "
                "pg_catalog.pg_get_userbyid(large_object.lomowner) AS owner, "
                "large_object.lomacl::text AS acl "
                "FROM pg_catalog.pg_largeobject_metadata AS large_object "
                "ORDER BY large_object.oid"
            ),
            "default_acls": (
                "SELECT owner.rolname AS owner, COALESCE(namespace.nspname, '') AS schema, "
                "defaults.defaclobjtype AS kind, defaults.defaclacl::text AS acl "
                "FROM pg_catalog.pg_default_acl AS defaults "
                "JOIN pg_catalog.pg_roles AS owner ON owner.oid = defaults.defaclrole "
                "LEFT JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = defaults.defaclnamespace "
                "ORDER BY owner, schema, kind"
            ),
        }
        result: dict[str, tuple[tuple[tuple[str, str | None], ...], ...]] = {}
        async with target_admin_engine.connect() as connection:
            for label, statement in queries.items():
                parameters = (
                    {"roles": list(managed_roles)}
                    if ":roles" in statement
                    else {}
                )
                rows = (
                    await connection.execute(
                        text(statement),
                        parameters,
                    )
                ).mappings().all()
                result[label] = freeze([dict(row) for row in rows])
            await connection.rollback()
        return result

    settings = Settings(
        database_url=runtime_url,
        data_dir=str(tmp_path / "data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
        backup_dump_database_url=role_url(dump_role, "dump-password"),
        backup_creator_database_url=role_url(creator_role, "creator-password"),
        backup_restore_database_url=role_url(
            restore_role,
            "wrong-restore-password",
        ),
        backup_target_owner=owner_role,
    )
    plan = await provision_module.build_provision_plan(settings)
    assert plan["executable"] is True, plan
    assert {"action": "create_role", "role": owner_role} in plan["actions"]

    before = await snapshot()
    sink = tmp_path / "bad-existing-credential.env"
    with pytest.raises(
        BackupProvisionError,
        match="PPBASE_BACKUP_RESTORE_DATABASE_URL could not authenticate",
    ):
        await execute_provision(
            settings,
            bootstrap_database_url=bootstrap_url,
            secret_sink=sink,
        )
    after = await snapshot()

    assert not sink.exists()
    assert after == before
    await target_admin_engine.dispose()
    await admin_engine.dispose()


@pytest.mark.asyncio
async def test_postgres_init_bootstraps_a_fresh_project_and_is_a_strict_noop(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_url, major, _container = backup_postgres_cluster
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
        backup_pg_dump_path=sys.executable,
        backup_pg_restore_path=sys.executable,
        backup_psql_path=sys.executable,
    )
    sink = tmp_path / "onboard.env"
    plan = await build_postgres_init_plan(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert plan["readOnly"] is True
    assert plan["executable"] is True
    assert not sink.exists()
    assert not any(Path(path).exists() for path in plan["directories"].values())
    assert "pptest:pptest" not in json.dumps(plan)

    first = await execute_postgres_init(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert first["databaseCreated"] is True
    assert first["noOp"] is False
    assert set(first["createdRoles"]) == {
        "onboard_app",
        "onboard_app_backup_dump",
        "onboard_app_backup_creator",
        "onboard_app_backup_restore",
        "onboard_app_backup_owner",
    }
    assert stat.S_IMODE(sink.stat().st_mode) == 0o600
    first_identity = (sink.stat().st_ino, sink.stat().st_mtime_ns)

    second = await execute_postgres_init(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert second["databaseCreated"] is False
    assert second["createdRoles"] == []
    assert second["createdDirectories"] == []
    assert second["noOp"] is True
    assert (sink.stat().st_ino, sink.stat().st_mtime_ns) == first_identity

    values: dict[str, str] = {}
    for line in sink.read_text(encoding="utf-8").splitlines():
        key, raw = line.split("=", 1)
        values[key] = json.loads(raw)
    assert set(values) == {
        "PPBASE_DATABASE_URL",
        "PPBASE_BACKUP_DUMP_DATABASE_URL",
        "PPBASE_BACKUP_CREATOR_DATABASE_URL",
        "PPBASE_BACKUP_RESTORE_DATABASE_URL",
        "PPBASE_BACKUP_TARGET_OWNER",
    }
    assert "pptest:pptest@" not in sink.read_text(encoding="utf-8")

    configured = settings.model_copy(
        update={
            "database_url": values["PPBASE_DATABASE_URL"],
            "backup_dump_database_url": values[
                "PPBASE_BACKUP_DUMP_DATABASE_URL"
            ],
            "backup_creator_database_url": values[
                "PPBASE_BACKUP_CREATOR_DATABASE_URL"
            ],
            "backup_restore_database_url": values[
                "PPBASE_BACKUP_RESTORE_DATABASE_URL"
            ],
            "backup_target_owner": values["PPBASE_BACKUP_TARGET_OWNER"],
        }
    )

    async def matching_tool_version(executable: str):
        return SimpleNamespace(executable=executable, version=str(major), major=major)

    monkeypatch.delenv("PPBASE_RESTART_CMD", raising=False)
    monkeypatch.setattr(
        provision_module,
        "detect_postgres_tool_version",
        matching_tool_version,
    )
    doctor = await backup_doctor(configured)
    restart = next(check for check in doctor["checks"] if check["name"] == "restart")
    assert restart["status"] == "skip"
    assert doctor["ready"] is True, doctor
    assert doctor["fullyVerified"] is False
    assert doctor["exitCode"] == 0
    assert bootstrap_url not in json.dumps(doctor)

    admin_engine = create_async_engine(bootstrap_url)
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE ROLE onboard_intruder NOLOGIN NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )
        await connection.execute(text("GRANT onboard_app TO onboard_intruder"))
    incoming_membership = await build_postgres_init_plan(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert incoming_membership["executable"] is False
    assert {
        "resource": "onboard_intruder",
        "reason": "unexpected membership in onboard_app",
    } in incoming_membership["collisions"]
    with pytest.raises(BackupProvisionError, match="unsafe collisions"):
        await execute_postgres_init(
            settings,
            bootstrap_database_url=bootstrap_url,
            project_name="onboard_app",
            secret_sink=sink,
        )
    async with admin_engine.begin() as connection:
        await connection.execute(text("REVOKE onboard_app FROM onboard_intruder"))

    real_grant_memberships = provision_module._grant_owner_memberships

    async def inject_incoming_membership(connection, *, roles, server_version_num):
        await real_grant_memberships(
            connection,
            roles=roles,
            server_version_num=server_version_num,
        )
        await connection.execute(text("GRANT onboard_app TO onboard_intruder"))

    with monkeypatch.context() as membership_race:
        membership_race.setattr(
            provision_module,
            "_grant_owner_memberships",
            inject_incoming_membership,
        )
        with pytest.raises(
            BackupProvisionError,
            match="membership graph changed unsafely",
        ):
            await execute_postgres_init(
                settings,
                bootstrap_database_url=bootstrap_url,
                project_name="onboard_app",
                secret_sink=sink,
            )
    async with admin_engine.begin() as connection:
        incoming_still_exists = bool(
            (
                await connection.execute(
                    text(
                        "SELECT pg_catalog.pg_has_role("
                        "'onboard_intruder', 'onboard_app', 'MEMBER')"
                    )
                )
            ).scalar_one()
        )
        assert incoming_still_exists is False
        await connection.execute(text("DROP ROLE onboard_intruder"))

        await connection.execute(text("COMMENT ON DATABASE onboard_app IS NULL"))
    unmarked_database = await build_postgres_init_plan(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert unmarked_database["executable"] is False
    assert any(
        collision["resource"] == "onboard_app"
        and collision["reason"].startswith("unmarked database")
        for collision in unmarked_database["collisions"]
    )
    with pytest.raises(BackupProvisionError, match="unsafe collisions"):
        await execute_postgres_init(
            settings,
            bootstrap_database_url=bootstrap_url,
            project_name="onboard_app",
            secret_sink=sink,
        )
    database_marker = provision_module._postgres_init_database_marker(
        provision_module.resolve_postgres_init_spec("onboard_app")
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "COMMENT ON DATABASE onboard_app IS "
                f"{provision_module._sql_literal(database_marker)}"
            )
        )

    async with admin_engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="AUTOCOMMIT"
        )
        await connection.execute(text("DROP DATABASE onboard_app"))
        await connection.execute(
            text(
                "CREATE DATABASE onboard_app WITH TEMPLATE template0 "
                "OWNER onboard_app ENCODING 'UTF8'"
            )
        )
    pristine_resume_plan = await build_postgres_init_plan(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert pristine_resume_plan["executable"] is True, pristine_resume_plan
    assert {
        "action": "resume_pristine_database",
        "database": "onboard_app",
    } in pristine_resume_plan["actions"]
    pristine_resume = await execute_postgres_init(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name="onboard_app",
        secret_sink=sink,
    )
    assert pristine_resume["databaseCreated"] is False
    assert pristine_resume["noOp"] is False
    await admin_engine.dispose()

    def unreachable_server(*_args, **_kwargs):
        raise provision_module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(provision_module.urllib.request, "urlopen", unreachable_server)
    strict_server_doctor = await backup_doctor(
        configured,
        server_url="http://127.0.0.1:9",
    )
    restart = next(
        check
        for check in strict_server_doctor["checks"]
        if check["name"] == "restart"
    )
    assert restart["status"] == "fail"
    assert restart["ready"] is False
    assert strict_server_doctor["ready"] is False
    assert strict_server_doctor["exitCode"] == 2


@pytest.mark.asyncio
async def test_postgres_init_rejects_bad_reused_password_before_any_mutation(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
) -> None:
    bootstrap_url, _major, _container = backup_postgres_cluster
    spec = provision_module.resolve_postgres_init_spec("credential_guard")
    settings = Settings(
        data_dir=str(tmp_path / "data"),
        backup_root=str(tmp_path / "backups"),
        backup_control_dir=str(tmp_path / "control"),
        backup_staging_root=str(tmp_path / "staging"),
        backup_target_root=str(tmp_path / "targets"),
    )
    sink = tmp_path / "credential-guard.env"
    bootstrap = make_url(bootstrap_url)

    def limited_url(role: str, password: str) -> str:
        return bootstrap.set(
            username=role,
            password=password,
            database=spec.database,
        ).render_as_string(hide_password=False)

    write_secret_sink(
        sink,
        {
            "PPBASE_DATABASE_URL": limited_url(
                spec.roles.runtime,
                "wrong-runtime-password",
            ),
            "PPBASE_BACKUP_DUMP_DATABASE_URL": limited_url(
                spec.roles.dump,
                "future-dump-password",
            ),
            "PPBASE_BACKUP_CREATOR_DATABASE_URL": limited_url(
                spec.roles.creator,
                "future-creator-password",
            ),
            "PPBASE_BACKUP_RESTORE_DATABASE_URL": limited_url(
                spec.roles.restore,
                "future-restore-password",
            ),
            "PPBASE_BACKUP_TARGET_OWNER": spec.roles.owner,
        },
    )

    admin_engine = create_async_engine(bootstrap_url)
    role_marker = provision_module._postgres_init_role_marker(
        spec,
        spec.roles.runtime,
    )
    async with admin_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE ROLE credential_guard LOGIN INHERIT NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                "PASSWORD 'actual-runtime-password'"
            )
        )
        await connection.execute(
            text(
                "COMMENT ON ROLE credential_guard IS "
                f"{provision_module._sql_literal(role_marker)}"
            )
        )

    plan = await build_postgres_init_plan(
        settings,
        bootstrap_database_url=bootstrap_url,
        project_name=spec.project,
        secret_sink=sink,
    )
    assert plan["executable"] is False
    assert {
        "resource": spec.roles.runtime,
        "reason": (
            "stored credential PPBASE_DATABASE_URL could not authenticate"
        ),
    } in plan["collisions"]
    assert any(action["action"] == "create_database" for action in plan["actions"])
    assert sum(
        action["action"] == "create_role" for action in plan["actions"]
    ) == 4

    with pytest.raises(BackupProvisionError, match="unsafe collisions"):
        await execute_postgres_init(
            settings,
            bootstrap_database_url=bootstrap_url,
            project_name=spec.project,
            secret_sink=sink,
        )

    async with admin_engine.connect() as connection:
        observed_roles = set(
            (
                await connection.execute(
                    text(
                        "SELECT rolname FROM pg_catalog.pg_roles "
                        "WHERE rolname = ANY(CAST(:roles AS text[]))"
                    ),
                    {"roles": list(spec.roles.as_dict().values())},
                )
            ).scalars().all()
        )
        database_exists = bool(
            (
                await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_database "
                        "WHERE datname = :database)"
                    ),
                    {"database": spec.database},
                )
            ).scalar_one()
        )
    await admin_engine.dispose()

    assert observed_roles == {spec.roles.runtime}
    assert database_exists is False
    assert not any(
        Path(path).exists()
        for path in provision_module.resolve_postgres_init_layout(
            settings
        ).as_dict().values()
    )


@pytest.mark.asyncio
async def test_postgres_init_console_command_needs_no_manual_sql_or_directories(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
) -> None:
    bootstrap_url, _major, _container = backup_postgres_cluster
    console = Path(sys.executable).with_name("ppbase")
    sink = tmp_path / "cli.env"
    environment = {
        **os.environ,
        "PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL": bootstrap_url,
    }

    def run(mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(console),
                "init",
                "postgres",
                mode,
                "--name",
                "cli_app",
                "--output-env",
                str(sink),
            ],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    planned = await asyncio.to_thread(run, "--plan")
    assert planned.returncode == 0, planned.stderr
    assert json.loads(planned.stdout)["readOnly"] is True
    assert not sink.exists()
    assert not (tmp_path / "pb_data").exists()
    assert not (tmp_path / "pb_backups").exists()

    executed = await asyncio.to_thread(run, "--execute")
    assert executed.returncode == 0, executed.stderr
    assert json.loads(executed.stdout)["noOp"] is False
    for name in (
        "pb_data",
        "pb_backups",
        "pb_backup_control",
        "pb_restore_staging",
        "pb_restore_staging_targets",
    ):
        path = tmp_path / name
        assert path.is_dir()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700

    repeated = await asyncio.to_thread(run, "--execute")
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["noOp"] is True


def _container_creator_identity(
    container: Any,
    *,
    username: str = "ppbase_creator",
    password: str = "creator-password",
    database: str = "pptest",
) -> dict[str, Any]:
    execution = container.exec_run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            username,
            "-d",
            database,
            "--tuples-only",
            "--no-align",
            "--field-separator=|",
            "--command",
            "SELECT current_user, current_database(), "
            "COALESCE(inet_server_addr()::text, ''), "
            "COALESCE(inet_server_port(), 0), "
            "EXTRACT(EPOCH FROM pg_postmaster_start_time())::text, "
            "current_setting('server_version_num')",
        ],
        environment={"PGPASSWORD": password},
        demux=True,
    )
    stdout_bytes, stderr_bytes = execution.output or (b"", b"")
    assert execution.exit_code == 0, (stderr_bytes or b"").decode(
        "utf-8", errors="replace"
    )
    values = (stdout_bytes or b"").decode("utf-8").strip().split("|")
    assert len(values) == 6
    return {
        "role": values[0],
        "database": values[1],
        "server_address": values[2],
        "server_port": int(values[3]),
        "postmaster_started_at": values[4],
        "server_version_num": values[5],
    }


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
        _put_container_file(container, container_archive, host_archive.read_bytes())
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


@pytest.mark.asyncio
async def test_target_role_contract_requires_set_only_memberships(
    backup_postgres_cluster: tuple[str, int, Any],
) -> None:
    backup_postgres_url, postgres_major, container = backup_postgres_cluster
    source_engine = create_async_engine(backup_postgres_url)
    async with source_engine.begin() as connection:
        role_statements = (
            """
            CREATE ROLE contract_owner
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE contract_creator
                LOGIN PASSWORD 'creator-password' CREATEDB NOINHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE contract_restore
                LOGIN PASSWORD 'restore-password' NOCREATEDB NOINHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE contract_runtime
                LOGIN PASSWORD 'runtime-password' NOCREATEDB INHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            "GRANT contract_owner TO contract_creator WITH INHERIT FALSE",
            "GRANT contract_owner TO contract_creator WITH SET TRUE",
            "GRANT contract_owner TO contract_restore WITH INHERIT FALSE",
            "GRANT contract_owner TO contract_restore WITH SET TRUE",
            "GRANT contract_owner TO contract_runtime WITH INHERIT TRUE",
        )
        for statement in role_statements:
            await connection.execute(text(statement))
        contract = await inspect_database_contract(connection)

    host_port = backup_postgres_url.split("@", 1)[1]
    creator_engine = create_async_engine(
        "postgresql+asyncpg://contract_creator:creator-password@" + host_port
    )

    async def role_report():
        async with creator_engine.connect() as connection:
            return await preflight_database_contract(
                connection,
                contract,
                creator_role="contract_creator",
                restore_role="contract_restore",
                runtime_role="contract_runtime",
                target_owner="contract_owner",
            )

    baseline = await role_report()
    assert baseline.ok, baseline.errors
    async with creator_engine.connect() as connection:
        with pytest.raises(PostgresContractError, match="dedicated role"):
            await preflight_database_contract(
                connection,
                contract,
                creator_role="contract_creator",
                restore_role="contract_restore",
                runtime_role="contract_runtime",
                target_owner="pg_read_all_data",
            )
    async with creator_engine.connect() as connection:
        await connection.execute(text("SET ROLE contract_owner"))
        assert (
            await connection.execute(text("SELECT current_user"))
        ).scalar_one() == "contract_owner"
        await connection.execute(text("RESET ROLE"))

    if postgres_major >= 16:
        membership_mutations = (
            (
                "GRANT contract_owner TO contract_restore WITH SET FALSE",
                "GRANT contract_owner TO contract_restore WITH SET TRUE",
            ),
            (
                "GRANT contract_owner TO contract_restore WITH ADMIN TRUE",
                "GRANT contract_owner TO contract_restore WITH ADMIN FALSE",
            ),
            (
                "GRANT contract_owner TO contract_restore WITH INHERIT TRUE",
                "GRANT contract_owner TO contract_restore WITH INHERIT FALSE",
            ),
        )
        for unsafe_statement, reset_statement in membership_mutations:
            async with source_engine.begin() as connection:
                await connection.execute(text(unsafe_statement))
            report = await role_report()
            assert any(
                "restore role lacks direct SET-only" in error
                for error in report.errors
            )
            async with source_engine.begin() as connection:
                await connection.execute(text(reset_statement))

    for role in ("contract_creator", "contract_restore"):
        async with source_engine.begin() as connection:
            await connection.execute(text(f"ALTER ROLE {role} INHERIT"))
        report = await role_report()
        assert any(
            f"{role.removeprefix('contract_')} role attributes do not match" in error
            for error in report.errors
        )
        async with source_engine.begin() as connection:
            await connection.execute(text(f"ALTER ROLE {role} NOINHERIT"))

    async with source_engine.begin() as connection:
        await connection.execute(text("ALTER ROLE contract_owner LOGIN"))
    elevated_owner = await role_report()
    assert any(
        "target owner attributes do not match" in error
        for error in elevated_owner.errors
    )
    async with source_engine.begin() as connection:
        await connection.execute(text("ALTER ROLE contract_owner NOLOGIN"))

    async with source_engine.begin() as connection:
        await connection.execute(text("ALTER ROLE contract_runtime CREATEDB"))
    elevated_non_superuser = await role_report()
    assert any(
        "runtime role attributes do not match" in error
        for error in elevated_non_superuser.errors
    )
    assert LEGACY_RUNTIME_SUPERUSER_WARNING not in elevated_non_superuser.warnings

    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER ROLE contract_runtime SUPERUSER CREATEDB CREATEROLE "
                "REPLICATION BYPASSRLS NOINHERIT"
            )
        )
    legacy_superuser = await role_report()
    assert legacy_superuser.ok, legacy_superuser.errors
    assert legacy_superuser.warnings.count(LEGACY_RUNTIME_SUPERUSER_WARNING) == 1
    assert tuple(
        warning
        for warning in legacy_superuser.warnings
        if warning != LEGACY_RUNTIME_SUPERUSER_WARNING
    ) == baseline.warnings

    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE ROLE contract_runtime_extra NOLOGIN NOSUPERUSER "
                "NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
            )
        )
        await connection.execute(
            text(
                "GRANT contract_runtime_extra TO contract_runtime "
                "WITH INHERIT TRUE"
            )
        )
    explicit_membership = await role_report()
    assert any(
        "membership graph extends beyond the target owner" in error
        for error in explicit_membership.errors
    )
    async with source_engine.begin() as connection:
        await connection.execute(
            text("REVOKE contract_runtime_extra FROM contract_runtime")
        )

    creator_url = (
        "postgresql+asyncpg://contract_creator:creator-password@" + host_port
    )
    created = await create_target_database(
        creator_url,
        "contract_superuser_target",
        target_owner="contract_owner",
        restore_role="contract_restore",
        runtime_role="contract_runtime",
        contract=contract,
        expected_server_identity=_container_creator_identity(
            container,
            username="contract_creator",
            password="creator-password",
            database="pptest",
        ),
        runner=_container_tool_runner(container),
    )
    assert created.database == "contract_superuser_target"

    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                "ALTER ROLE contract_runtime NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT"
            )
        )

    restored = await role_report()
    assert restored.ok, restored.errors
    assert restored.warnings == baseline.warnings

    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE ROLE contract_privileged NOLOGIN CREATEROLE "
                "NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS"
            )
        )
        await connection.execute(
            text(
                "CREATE ROLE contract_intermediate NOLOGIN NOCREATEROLE "
                "NOSUPERUSER NOCREATEDB NOREPLICATION NOBYPASSRLS"
            )
        )
        await connection.execute(
            text(
                "GRANT contract_privileged TO contract_intermediate "
                "WITH SET TRUE, INHERIT FALSE"
            )
        )
        await connection.execute(
            text(
                "GRANT contract_intermediate TO contract_owner "
                "WITH SET TRUE, INHERIT FALSE"
            )
        )
    transitive_report = await role_report()
    assert any(
        "membership graph extends beyond the target owner" in error
        for error in transitive_report.errors
    )

    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE ROLE contract_second_grantor NOLOGIN "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS"
            )
        )
        await connection.execute(
            text(
                "GRANT contract_owner TO contract_second_grantor "
                "WITH ADMIN TRUE, INHERIT FALSE, SET FALSE"
            )
        )
        await connection.execute(
            text(
                "GRANT contract_owner TO contract_restore "
                "WITH ADMIN TRUE, INHERIT TRUE, SET TRUE "
                "GRANTED BY contract_second_grantor"
            )
        )
        duplicate_grants = (
            await connection.execute(
                text(
                    "SELECT grantor_role.rolname AS grantor, "
                    "membership.admin_option, membership.inherit_option, "
                    "membership.set_option "
                    "FROM pg_auth_members AS membership "
                    "JOIN pg_roles AS member_role "
                    "ON member_role.oid = membership.member "
                    "JOIN pg_roles AS granted_role "
                    "ON granted_role.oid = membership.roleid "
                    "JOIN pg_roles AS grantor_role "
                    "ON grantor_role.oid = membership.grantor "
                    "WHERE member_role.rolname = 'contract_restore' "
                    "AND granted_role.rolname = 'contract_owner' "
                    "ORDER BY grantor_role.rolname"
                )
            )
        ).mappings().all()
    assert len(duplicate_grants) == 2
    assert any(
        bool(grant["admin_option"]) or bool(grant["inherit_option"])
        for grant in duplicate_grants
    )
    duplicate_grant_report = await role_report()
    assert any(
        "restore role lacks direct SET-only" in error
        for error in duplicate_grant_report.errors
    )

    await creator_engine.dispose()
    await source_engine.dispose()


@pytest.mark.asyncio
async def test_target_role_preflight_ignores_hostile_search_path_catalogs(
    backup_postgres_cluster: tuple[str, int, Any],
) -> None:
    backup_postgres_url, _, _ = backup_postgres_cluster
    source_engine = create_async_engine(backup_postgres_url)
    async with source_engine.begin() as connection:
        role_statements = (
            """
            CREATE ROLE path_owner
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE path_creator
                LOGIN PASSWORD 'creator-password' CREATEDB CREATEROLE NOINHERIT
                NOSUPERUSER NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE path_restore
                LOGIN PASSWORD 'restore-password' NOCREATEDB NOINHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE path_runtime
                LOGIN PASSWORD 'runtime-password' NOCREATEDB INHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            "GRANT path_owner TO path_creator WITH INHERIT FALSE",
            "GRANT path_owner TO path_creator WITH SET TRUE",
            "GRANT path_owner TO path_restore WITH INHERIT FALSE",
            "GRANT path_owner TO path_restore WITH SET TRUE",
            "GRANT path_owner TO path_runtime WITH INHERIT TRUE",
            "CREATE SCHEMA path_attack AUTHORIZATION path_creator",
            """
            CREATE TABLE path_attack.pg_roles AS
            SELECT * FROM pg_catalog.pg_roles
            """,
            """
            CREATE TABLE path_attack.pg_available_extension_versions AS
            SELECT * FROM pg_catalog.pg_available_extension_versions
            WITH NO DATA
            """,
            """
            UPDATE path_attack.pg_roles
            SET rolcreaterole = false
            WHERE rolname = 'path_creator'
            """,
            "ALTER TABLE path_attack.pg_roles OWNER TO path_creator",
            """
            ALTER TABLE path_attack.pg_available_extension_versions
            OWNER TO path_creator
            """,
            """
            ALTER ROLE path_creator IN DATABASE pptest
            SET search_path = path_attack, pg_catalog
            """,
        )
        for statement in role_statements:
            await connection.execute(text(statement))
        contract = await inspect_database_contract(connection)

    host_port = backup_postgres_url.split("@", 1)[1]
    creator_engine = create_async_engine(
        "postgresql+asyncpg://path_creator:creator-password@" + host_port
    )
    async with creator_engine.connect() as connection:
        configured_path = str(
            (await connection.execute(text("SHOW search_path"))).scalar_one()
        )
        assert "path_attack" in configured_path
        assert (
            await connection.execute(
                text(
                    "SELECT rolcreaterole FROM pg_roles "
                    "WHERE rolname = 'path_creator'"
                )
            )
        ).scalar_one() is False
        assert (
            await connection.execute(
                text("SELECT count(*) FROM pg_available_extension_versions")
            )
        ).scalar_one() == 0

        report = await preflight_database_contract(
            connection,
            contract,
            creator_role="path_creator",
            restore_role="path_restore",
            runtime_role="path_runtime",
            target_owner="path_owner",
        )

    assert any(
        "creator role attributes do not match" in error
        for error in report.errors
    )
    assert not any("is unavailable on target" in error for error in report.errors)
    await creator_engine.dispose()
    await source_engine.dispose()


@pytest.mark.asyncio
async def test_dump_create_restore_validate_and_clone_rotation(
    backup_postgres_cluster: tuple[str, int, Any],
    tmp_path: Path,
) -> None:
    backup_postgres_url, postgres_major, container = backup_postgres_cluster
    tool_runner = _container_tool_runner(container)
    source_engine = create_async_engine(backup_postgres_url)
    await create_system_tables(source_engine)
    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE public.users (
                    id varchar(15) PRIMARY KEY,
                    token_key varchar(50) NOT NULL,
                    updated timestamptz NOT NULL DEFAULT now()
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO public."_collections" (
                    id, name, type, system, "schema", indexes, options
                ) VALUES (
                    'superusers00001', '_superusers', 'auth', true, '[]', '[]',
                    CAST(:options AS jsonb)
                )
                """
            ),
            {
                "options": """
                    {
                      "authToken":{"secret":"old-super-auth"},
                      "passwordResetToken":{"secret":"old-super-reset"},
                      "verificationToken":{"secret":"old-super-verify"},
                      "emailChangeToken":{"secret":"old-super-email"},
                      "fileToken":{"secret":"old-super-file"}
                    }
                """,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO public."_collections" (
                    id, name, type, system, "schema", indexes, options
                ) VALUES (
                    'users0000000001', 'users', 'auth', false, '[]', '[]',
                    CAST(:options AS jsonb)
                )
                """
            ),
            {
                "options": """
                    {
                      "authToken":{"secret":"old-auth","duration":3600},
                      "passwordResetToken":{"secret":"old-reset"},
                      "verificationToken":{"secret":"old-verify"},
                      "emailChangeToken":{"secret":"old-email"},
                      "fileToken":{"secret":"old-file"}
                    }
                """,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO public.users (id, token_key)
                VALUES ('record000000001', 'old-record-token-key')
                """
            )
        )
        role_statements = (
            """
            CREATE ROLE ppbase_owner
                NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_creator
                LOGIN PASSWORD 'creator-password' CREATEDB NOINHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_restore
                LOGIN PASSWORD 'restore-password' NOCREATEDB NOINHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_runtime
                LOGIN PASSWORD 'runtime-password' NOCREATEDB INHERIT
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            """
            CREATE ROLE ppbase_dump
                LOGIN PASSWORD 'dump-password' NOCREATEDB
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            "GRANT ppbase_owner TO ppbase_creator WITH INHERIT FALSE",
            "GRANT ppbase_owner TO ppbase_creator WITH SET TRUE",
            "GRANT ppbase_owner TO ppbase_restore WITH INHERIT FALSE",
            "GRANT ppbase_owner TO ppbase_restore WITH SET TRUE",
            "GRANT ppbase_owner TO ppbase_runtime WITH INHERIT TRUE",
            "GRANT pg_read_all_data TO ppbase_dump",
            "GRANT CONNECT ON DATABASE pptest TO ppbase_dump",
            "REVOKE TEMPORARY ON DATABASE pptest FROM PUBLIC",
        )
        for statement in role_statements:
            await connection.execute(text(statement))
        large_object_oid = int(
            (
                await connection.execute(text("SELECT pg_catalog.lo_create(0)"))
            ).scalar_one()
        )
        await connection.execute(
            text(
                f"GRANT SELECT ON LARGE OBJECT {large_object_oid} TO ppbase_dump"
            )
        )
        contract = await inspect_database_contract(connection)

    host_port = backup_postgres_url.split("@", 1)[1]
    creator_url = (
        "postgresql+asyncpg://ppbase_creator:creator-password@" + host_port
    )
    restore_maintenance_url = (
        "postgresql+asyncpg://ppbase_restore:restore-password@" + host_port
    )
    dump_url = "postgresql+asyncpg://ppbase_dump:dump-password@" + host_port

    dump_engine = create_async_engine(dump_url)
    forbidden_table_privilege = "MAINTAIN" if postgres_major >= 17 else "UPDATE"
    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                f"GRANT {forbidden_table_privilege} "
                "ON public.users TO ppbase_dump"
            )
        )
    async with dump_engine.connect() as connection:
        excessive_dump_preflight = await preflight_dump_role(connection)
        assert not excessive_dump_preflight.ok
        assert any(
            error.startswith("dump role has forbidden write privileges:")
            and f"table public.users {forbidden_table_privilege}" in error
            for error in excessive_dump_preflight.errors
        )
    async with source_engine.begin() as connection:
        await connection.execute(
            text(
                f"REVOKE {forbidden_table_privilege} "
                "ON public.users FROM ppbase_dump"
            )
        )
    async with dump_engine.connect() as connection:
        dump_preflight = await preflight_dump_role(connection)
        assert dump_preflight.ok, dump_preflight.errors
    await dump_engine.dispose()

    creator_engine = create_async_engine(creator_url)
    async with creator_engine.connect() as connection:
        target_preflight = await preflight_database_contract(
            connection,
            contract,
            creator_role="ppbase_creator",
            restore_role="ppbase_restore",
            runtime_role="ppbase_runtime",
            target_owner="ppbase_owner",
        )
        assert target_preflight.ok, target_preflight.errors
        if contract.collations:
            source_collation = contract.collations[0]
            stale_contract = replace(
                contract,
                collations=(
                    replace(
                        source_collation,
                        version="recorded-old",
                        actual_version="actual-new",
                    ),
                    *contract.collations[1:],
                ),
            )
            stale_report = await preflight_database_contract(
                connection,
                stale_contract,
                creator_role="ppbase_creator",
                restore_role="ppbase_restore",
                runtime_role="ppbase_runtime",
                target_owner="ppbase_owner",
            )
            assert any(
                "recorded version differs from its actual version" in error
                for error in stale_report.errors
            )

            mismatched_target_contract = replace(
                contract,
                collations=(
                    replace(
                        source_collation,
                        version="synthetic-version",
                        actual_version="synthetic-version",
                    ),
                    *contract.collations[1:],
                ),
            )
            mismatch_report = await preflight_database_contract(
                connection,
                mismatched_target_contract,
                creator_role="ppbase_creator",
                restore_role="ppbase_restore",
                runtime_role="ppbase_runtime",
                target_owner="ppbase_owner",
            )
            assert any(
                "required collation" in error and "is incompatible" in error
                for error in mismatch_report.errors
            )
    await creator_engine.dispose()

    archive = tmp_path / "database.dump"
    async with source_engine.connect() as snapshot_connection:
        await snapshot_connection.execution_options(
            isolation_level="REPEATABLE READ"
        )
        async with snapshot_connection.begin():
            await snapshot_connection.execute(text("SET TRANSACTION READ ONLY"))
            snapshot_id = str(
                (
                    await snapshot_connection.execute(
                        text("SELECT pg_export_snapshot()")
                    )
                ).scalar_one()
            )
            await run_pg_dump(
                dump_url,
                archive,
                expected_server_major=postgres_major,
                snapshot_id=snapshot_id,
                passfile_directory=tmp_path,
                runner=tool_runner,
            )
    async with source_engine.begin() as connection:
        await connection.execute(text("REVOKE pg_read_all_data FROM ppbase_dump"))
    tool_creator_identity = _container_creator_identity(container)
    with pytest.raises(PostgresCommandError):
        await create_target_database(
            creator_url,
            "staging_identity_mismatch",
            target_owner="ppbase_owner",
            restore_role="ppbase_restore",
            runtime_role="ppbase_runtime",
            contract=contract,
            expected_server_identity={
                **tool_creator_identity,
                "server_version_num": "999999",
            },
            passfile_directory=tmp_path,
            runner=tool_runner,
        )
    async with source_engine.connect() as connection:
        assert not bool(
            (
                await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_database "
                        "WHERE datname = 'staging_identity_mismatch')"
                    )
                )
            ).scalar_one()
        )

    create_result = await create_target_database(
        creator_url,
        "staging_restore_test",
        target_owner="ppbase_owner",
        restore_role="ppbase_restore",
        runtime_role="ppbase_runtime",
        dump_role="ppbase_dump",
        contract=contract,
        expected_server_identity=tool_creator_identity,
        passfile_directory=tmp_path,
        runner=tool_runner,
    )
    async with source_engine.connect() as connection:
        database_acl = (
            await connection.execute(
                text(
                    """
                    SELECT
                        NOT EXISTS (
                            SELECT 1
                            FROM pg_database AS d,
                                 LATERAL aclexplode(d.datacl) AS acl
                            WHERE d.datname = 'staging_restore_test'
                              AND acl.grantee = 0
                              AND acl.privilege_type = 'CONNECT'
                        ) AS public_connect_revoked,
                        has_database_privilege(
                            'ppbase_restore',
                            'staging_restore_test',
                            'CONNECT,TEMPORARY'
                        ) AS restore_access,
                        (
                            SELECT shobj_description(d.oid, 'pg_database')
                            FROM pg_database AS d
                            WHERE d.datname = 'staging_restore_test'
                        ) AS marker_comment
                    """
                )
            )
        ).mappings().one()
        assert database_acl["public_connect_revoked"] is True
        assert database_acl["restore_access"] is True
        assert database_acl["marker_comment"] == create_result.marker_comment
    restore_url = replace_sqlalchemy_database(
        restore_maintenance_url,
        "staging_restore_test",
    )
    await run_pg_restore(
        restore_url,
        archive,
        target_owner="ppbase_owner",
        expected_server_major=postgres_major,
        passfile_directory=tmp_path,
        runner=tool_runner,
    )

    target_engine = create_async_engine(restore_url)
    target_dump_url = replace_sqlalchemy_database(
        dump_url,
        "staging_restore_test",
    )
    target_dump_engine = create_async_engine(target_dump_url)
    async with target_dump_engine.connect() as connection:
        before_dump_grants = await preflight_dump_role(connection)
        assert before_dump_grants.ok is False
        assert any(
            "large_object" in error for error in before_dump_grants.errors
        ), before_dump_grants.errors
        await connection.rollback()

    async with target_engine.begin() as connection:
        await grant_dump_role_read_access(
            connection,
            target_owner="ppbase_owner",
            dump_role="ppbase_dump",
        )
        restored_large_objects = set(
            (
                await connection.execute(
                    text(
                        "SELECT oid FROM pg_catalog.pg_largeobject_metadata "
                        "ORDER BY oid"
                    )
                )
            ).scalars().all()
        )
        assert large_object_oid in restored_large_objects

    async with target_dump_engine.connect() as connection:
        after_dump_grants = await preflight_dump_role(connection)
        assert after_dump_grants.ok, after_dump_grants.errors
        await connection.rollback()

    async with target_engine.connect() as connection:
        await connection.execute(text("SET ROLE ppbase_owner"))
        validation = await validate_staged_database(
            connection,
            expected_database="staging_restore_test",
            expected_owner="ppbase_owner",
            expected_runtime_role="ppbase_runtime",
            expected_dump_role="ppbase_dump",
            expected_contract=contract,
        )
        assert validation.valid, validation.errors
        await connection.execute(
            text(
                "GRANT CONNECT ON DATABASE staging_restore_test TO PUBLIC"
            )
        )
        acl_mismatch_validation = await validate_staged_database(
            connection,
            expected_database="staging_restore_test",
            expected_owner="ppbase_owner",
            expected_runtime_role="ppbase_runtime",
            expected_dump_role="ppbase_dump",
            expected_contract=contract,
        )
        assert any(
            issue.code == "database_acl_mismatch"
            for issue in acl_mismatch_validation.errors
        )
        await connection.execute(
            text(
                "REVOKE CONNECT ON DATABASE staging_restore_test FROM PUBLIC"
            )
        )

        mismatched_extensions = (
            replace(contract.extensions[0], version="synthetic-version"),
            *contract.extensions[1:],
        )
        mismatched_collations = contract.collations
        if contract.collations:
            mismatched_collations = (
                replace(
                    contract.collations[0],
                    version="synthetic-version",
                    actual_version="synthetic-version",
                ),
                *contract.collations[1:],
            )
        mismatched_validation = await validate_staged_database(
            connection,
            expected_database="staging_restore_test",
            expected_owner="ppbase_owner",
            expected_runtime_role="ppbase_runtime",
            expected_dump_role="ppbase_dump",
            expected_contract=replace(
                contract,
                ctype="invalid-locale",
                extensions=mismatched_extensions,
                collations=mismatched_collations,
            ),
        )
        mismatch_codes = {issue.code for issue in mismatched_validation.errors}
        assert "database_contract_mismatch" in mismatch_codes
        assert "extension_contract_mismatch" in mismatch_codes
        if contract.collations:
            assert "collation_contract_mismatch" in mismatch_codes
        original_token = (
            await connection.execute(text("SELECT token_key FROM public.users"))
        ).scalar_one()
        rotation = await rotate_clone_database_secrets(connection)
        rotated_token = (
            await connection.execute(text("SELECT token_key FROM public.users"))
        ).scalar_one()
        options = (
            await connection.execute(
                text(
                    "SELECT options FROM public.\"_collections\" WHERE name = 'users'"
                )
            )
        ).scalar_one()
        assert rotation.auth_collection_count == 2
        assert rotation.auth_record_count == 1
        assert rotated_token != original_token
        assert options["authToken"]["secret"] != "old-auth"
    await target_engine.dispose()
    await target_dump_engine.dispose()
    await source_engine.dispose()
