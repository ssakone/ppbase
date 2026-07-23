from __future__ import annotations

import secrets
import stat
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ppbase.backup.postgres import (
    preflight_destructive_restore_role,
    preflight_dump_role,
)
from ppbase.backup.provision import (
    BackupProvisionError,
    _dump_role_confinement_violations,
    build_provision_plan,
    execute_postgres_init,
    execute_provision,
    read_secret_sink,
)
from ppbase.config import Settings


pytestmark = pytest.mark.asyncio


async def _require_superuser(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            is_superuser = await connection.scalar(
                text(
                    "SELECT r.rolsuper FROM pg_catalog.pg_roles AS r "
                    "WHERE r.rolname = current_user"
                )
            )
    finally:
        await engine.dispose()
    if not bool(is_superuser):
        pytest.skip("provisioning integration requires a PostgreSQL superuser fixture")


async def _cleanup_project(
    bootstrap_url: str,
    *,
    database: str,
    roles: tuple[str, ...],
) -> None:
    engine = create_async_engine(
        bootstrap_url,
        poolclass=NullPool,
        isolation_level="AUTOCOMMIT",
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            )
            for role in roles:
                await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
    finally:
        await engine.dispose()


async def test_real_postgres_init_and_optional_dump_provision(
    pg_url: str,
    tmp_path: Path,
) -> None:
    await _require_superuser(pg_url)
    project = f"ppinit_{secrets.token_hex(6)}"
    dump_role = f"ppdump_{secrets.token_hex(6)}"
    bootstrap = make_url(pg_url)
    bootstrap_url = bootstrap.render_as_string(hide_password=False)
    runtime_sink = tmp_path / "runtime.env"
    dump_sink = tmp_path / "dump.env"
    settings = Settings(
        data_dir=str(tmp_path / "pb_data"),
        backup_root=str(tmp_path / "pb_backups"),
        backup_control_dir=str(tmp_path / "pb_backup_control"),
        backup_staging_root=str(tmp_path / "unused_restore_staging"),
        backup_target_root=str(tmp_path / "unused_restore_target"),
    )

    try:
        initialized = await execute_postgres_init(
            settings,
            bootstrap_database_url=bootstrap_url,
            project_name=project,
            secret_sink=runtime_sink,
        )
        runtime_values = read_secret_sink(runtime_sink)
        assert runtime_values is not None
        assert set(runtime_values) == {"PPBASE_DATABASE_URL"}
        runtime_url = runtime_values["PPBASE_DATABASE_URL"]

        assert initialized["createdRoles"] == [project]
        assert initialized["databaseCreated"] is True
        assert initialized["configurationKeys"] == ["PPBASE_DATABASE_URL"]
        assert stat.S_IMODE(runtime_sink.stat().st_mode) == 0o600
        for directory in (
            settings.data_dir,
            settings.backup_root,
            settings.backup_control_dir,
        ):
            assert stat.S_IMODE(Path(directory).stat().st_mode) == 0o700
        assert not Path(settings.backup_staging_root).exists()
        assert not Path(settings.backup_target_root).exists()

        admin_engine = create_async_engine(bootstrap_url, poolclass=NullPool)
        try:
            async with admin_engine.connect() as connection:
                role = (
                    await connection.execute(
                        text(
                            "SELECT rolcanlogin, rolsuper, rolcreatedb, "
                            "rolcreaterole, rolreplication, rolbypassrls "
                            "FROM pg_catalog.pg_roles WHERE rolname = :role"
                        ),
                        {"role": project},
                    )
                ).one()
                assert role == (True, False, False, False, False, False)
                assert int(
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_catalog.pg_auth_members AS m "
                            "JOIN pg_catalog.pg_roles AS member ON member.oid = m.member "
                            "JOIN pg_catalog.pg_roles AS granted ON granted.oid = m.roleid "
                            "WHERE member.rolname = :role OR granted.rolname = :role"
                        ),
                        {"role": project},
                    )
                ) == 0
                assert await connection.scalar(
                    text(
                        "SELECT pg_catalog.pg_get_userbyid(datdba) "
                        "FROM pg_catalog.pg_database WHERE datname = :database"
                    ),
                    {"database": project},
                ) == project
                legacy_names = (
                    f"{project}_backup_dump",
                    f"{project}_backup_restore",
                    f"{project}_backup_owner",
                )
                assert int(
                    await connection.scalar(
                        text(
                            "SELECT count(*) FROM pg_catalog.pg_roles "
                            "WHERE rolname IN (:role_one, :role_two, :role_three)"
                        ),
                        {
                            "role_one": legacy_names[0],
                            "role_two": legacy_names[1],
                            "role_three": legacy_names[2],
                        },
                    )
                ) == 0
        finally:
            await admin_engine.dispose()

        runtime_engine = create_async_engine(runtime_url, poolclass=NullPool)
        try:
            async with runtime_engine.begin() as connection:
                restore_report = await preflight_destructive_restore_role(connection)
                assert restore_report.ok
                await connection.execute(
                    text(
                        "CREATE TABLE public.provision_sample "
                        "(id integer PRIMARY KEY, value text NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO public.provision_sample VALUES (1, 'readable')"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE FUNCTION public.provision_mutate() RETURNS void "
                        "LANGUAGE sql SECURITY DEFINER "
                        "AS $$ INSERT INTO public.provision_sample "
                        "VALUES (3, 'must-not-run') $$"
                    )
                )
        finally:
            await runtime_engine.dispose()

        first_inode = runtime_sink.stat().st_ino
        repeated = await execute_postgres_init(
            settings,
            bootstrap_database_url=bootstrap_url,
            project_name=project,
            secret_sink=runtime_sink,
        )
        assert repeated["noOp"] is True
        assert runtime_sink.stat().st_ino == first_inode

        provision_settings = settings.model_copy(
            update={
                "database_url": runtime_url,
                "backup_dump_database_url": make_url(runtime_url).set(
                    username=dump_role,
                    password="name-selection-only",
                ).render_as_string(hide_password=False),
            }
        )
        provisioned = await execute_provision(
            provision_settings,
            bootstrap_database_url=bootstrap_url,
            secret_sink=dump_sink,
        )
        dump_values = read_secret_sink(dump_sink)
        assert dump_values is not None
        assert set(dump_values) == {"PPBASE_BACKUP_DUMP_DATABASE_URL"}
        assert provisioned["createdRoles"] == [dump_role]
        assert provisioned["configurationKeys"] == [
            "PPBASE_BACKUP_DUMP_DATABASE_URL"
        ]
        assert stat.S_IMODE(dump_sink.stat().st_mode) == 0o600

        app_admin_engine = create_async_engine(
            bootstrap.set(database=project).render_as_string(hide_password=False),
            poolclass=NullPool,
        )
        try:
            async with app_admin_engine.connect() as connection:
                assert await _dump_role_confinement_violations(
                    connection,
                    dump_role,
                ) == []
        finally:
            await app_admin_engine.dispose()

        dump_engine = create_async_engine(
            dump_values["PPBASE_BACKUP_DUMP_DATABASE_URL"],
            poolclass=NullPool,
        )
        try:
            async with dump_engine.connect() as connection:
                report = await preflight_dump_role(connection)
                assert report.ok
                assert await connection.scalar(
                    text("SELECT value FROM public.provision_sample WHERE id = 1")
                ) == "readable"
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "INSERT INTO public.provision_sample VALUES (2, 'blocked')"
                        )
                    )
                await connection.rollback()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text("SELECT public.provision_mutate()")
                    )
                await connection.rollback()
        finally:
            await dump_engine.dispose()

        drift_engine = create_async_engine(
            bootstrap.set(database=project).render_as_string(hide_password=False),
            poolclass=NullPool,
        )
        try:
            async with drift_engine.begin() as connection:
                await connection.execute(
                    text(f'CREATE SCHEMA private_drift AUTHORIZATION "{dump_role}"')
                )
        finally:
            await drift_engine.dispose()

        drifted_plan = await build_provision_plan(provision_settings)
        assert drifted_plan["executable"] is False
        assert any(
            "owns schema" in collision["reason"]
            for collision in drifted_plan["collisions"]
        )
        with pytest.raises(
            BackupProvisionError,
            match="unsafe role collisions",
        ):
            await execute_provision(
                provision_settings,
                bootstrap_database_url=bootstrap_url,
                secret_sink=dump_sink,
            )
    finally:
        await _cleanup_project(
            bootstrap_url,
            database=project,
            roles=(dump_role, project),
        )
