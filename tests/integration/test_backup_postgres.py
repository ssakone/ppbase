from __future__ import annotations

import asyncio
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import re
import tarfile
from typing import Any, Generator
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.postgres import PostgresContainer

from ppbase.backup.postgres import (
    CommandResult,
    PostgresCommandError,
    PostgresContractError,
    create_target_database,
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


def _container_creator_identity(container: Any) -> dict[str, Any]:
    execution = container.exec_run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "ppbase_creator",
            "-d",
            "pptest",
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
        environment={"PGPASSWORD": "creator-password"},
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
    backup_postgres_url, postgres_major, _ = backup_postgres_cluster
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
            "GRANT contract_owner TO contract_creator WITH INHERIT FALSE",
            "GRANT contract_owner TO contract_creator WITH SET TRUE",
            "GRANT contract_owner TO contract_restore WITH INHERIT FALSE",
            "GRANT contract_owner TO contract_restore WITH SET TRUE",
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

    restored = await role_report()
    assert restored.ok, restored.errors

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
            "GRANT path_owner TO path_creator WITH INHERIT FALSE",
            "GRANT path_owner TO path_creator WITH SET TRUE",
            "GRANT path_owner TO path_restore WITH INHERIT FALSE",
            "GRANT path_owner TO path_restore WITH SET TRUE",
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
            CREATE ROLE ppbase_dump
                LOGIN PASSWORD 'dump-password' NOCREATEDB
                NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS
            """,
            "GRANT ppbase_owner TO ppbase_creator WITH INHERIT FALSE",
            "GRANT ppbase_owner TO ppbase_creator WITH SET TRUE",
            "GRANT ppbase_owner TO ppbase_restore WITH INHERIT FALSE",
            "GRANT ppbase_owner TO ppbase_restore WITH SET TRUE",
            "GRANT pg_read_all_data TO ppbase_dump",
            "GRANT CONNECT ON DATABASE pptest TO ppbase_dump",
            "REVOKE TEMPORARY ON DATABASE pptest FROM PUBLIC",
        )
        for statement in role_statements:
            await connection.execute(text(statement))
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
    tool_creator_identity = _container_creator_identity(container)
    with pytest.raises(PostgresCommandError):
        await create_target_database(
            creator_url,
            "staging_identity_mismatch",
            target_owner="ppbase_owner",
            restore_role="ppbase_restore",
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
    async with target_engine.connect() as connection:
        await connection.execute(text("SET ROLE ppbase_owner"))
        validation = await validate_staged_database(
            connection,
            expected_database="staging_restore_test",
            expected_owner="ppbase_owner",
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
    await source_engine.dispose()
