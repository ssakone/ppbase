from __future__ import annotations

from pathlib import Path

import pytest

from ppbase.backup.postgres import (
    _forbidden_table_write_privileges,
    _role_and_target_check_sql,
    CollationContract,
    CommandResult,
    DatabaseContract,
    DumpResult,
    ExtensionContract,
    ObjectSecuritySummary,
    PostgresCommandError,
    PostgresContractError,
    PostgresVersionMismatchError,
    PostgresBackupError,
    TargetDatabaseExistsError,
    create_target_database,
    replace_sqlalchemy_database,
    run_pg_dump,
    run_pg_restore,
    sqlalchemy_url_to_libpq,
    temporary_pgpass,
)
from ppbase.backup.storage import LocalBackupStore


def _contract() -> DatabaseContract:
    return DatabaseContract(
        database="source_db",
        server_version_num=160004,
        server_major=16,
        encoding="UTF8",
        locale_provider="libc",
        collate="C",
        ctype="C",
        icu_locale=None,
        icu_rules=None,
        collation_version=None,
        actual_collation_version=None,
        database_owner="source_owner",
        database_acl=None,
        extensions=(ExtensionContract("plpgsql", "1.0", "pg_catalog"),),
        collations=(
            CollationContract(
                schema="pg_catalog",
                name="C",
                provider="libc",
                collate="C",
                ctype="C",
                locale=None,
                rules=None,
                version=None,
                actual_version=None,
            ),
        ),
        object_security=(
            ObjectSecuritySummary("relation", 4, ("source_owner",), 0),
        ),
    )


def _creator_identity() -> dict[str, object]:
    return {
        "role": "creator",
        "database": "postgres",
        "server_address": "127.0.0.1",
        "server_port": 5432,
        "postmaster_started_at": "1710000000.000000",
        "server_version_num": "160004",
    }


def test_forbidden_table_privileges_are_server_version_aware() -> None:
    assert "MAINTAIN" not in _forbidden_table_write_privileges(160014)
    assert "MAINTAIN" in _forbidden_table_write_privileges(170000)
    assert "MAINTAIN" in _forbidden_table_write_privileges(180001)


def test_target_role_sql_requires_exact_set_only_memberships() -> None:
    sql = _role_and_target_check_sql(
        target="staging_123",
        creator="ppbase_creator",
        restore="ppbase_restore",
        owner="ppbase_owner",
    )

    assert sql.count("AND NOT rolinherit") == 2
    assert sql.count("AND NOT membership.admin_option") == 3
    assert sql.count("membership.admin_option") == 5
    assert sql.count("to_jsonb(membership)->>'set_option'") == 5
    assert sql.count("to_jsonb(membership)->>'inherit_option'") == 5
    assert sql.count("member_role.rolinherit") == 5
    assert sql.count("AND NOT EXISTS (") == 2
    assert sql.count("OR NOT COALESCE(") == 2
    assert "OR granted_role.rolname IN (" in sql
    assert "'ppbase_creator', 'ppbase_restore', 'ppbase_owner'" in sql


def test_sqlalchemy_url_to_libpq_separates_password_and_maps_ssl() -> None:
    connection = sqlalchemy_url_to_libpq(
        "postgresql+asyncpg://restore:p%3Aa%5Css@db.example:5439/target?ssl=require"
    )

    assert connection.conninfo == (
        "host='db.example' port='5439' dbname='target' user='restore' "
        "sslmode='require'"
    )
    assert "p:a" not in connection.conninfo
    assert connection.password == "p:a\\ss"
    assert connection.pgpass_line() == (
        "db.example:5439:target:restore:p\\:a\\\\ss\n"
    )


def test_replace_sqlalchemy_database_preserves_credentials_and_options() -> None:
    replaced = replace_sqlalchemy_database(
        "postgresql+asyncpg://restore:secret@db/source?sslmode=verify-full",
        "staging_123",
    )

    assert replaced.database == "staging_123"
    assert replaced.username == "restore"
    assert replaced.password == "secret"
    assert replaced.query["sslmode"] == "verify-full"


def test_temporary_pgpass_is_0600_and_removed(tmp_path: Path) -> None:
    connection = sqlalchemy_url_to_libpq(
        "postgresql+asyncpg://restore:p%3Aa%5Css@localhost/source"
    )
    passfile_path: Path | None = None

    with temporary_pgpass(connection, directory=tmp_path) as passfile:
        assert passfile is not None
        passfile_path = passfile
        assert passfile.stat().st_mode & 0o777 == 0o600
        assert passfile.read_text(encoding="utf-8") == (
            "localhost:5432:source:restore:p\\:a\\\\ss\n"
        )

    assert passfile_path is not None
    assert not passfile_path.exists()


@pytest.mark.asyncio
async def test_run_pg_dump_uses_safe_argv_and_passfile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, str], tuple[str, ...]]] = []

    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        calls.append((argv_tuple, dict(env), tuple(redactions)))
        if "--version" in argv_tuple:
            return CommandResult(argv_tuple, 0, "pg_dump (PostgreSQL) 16.4\n", "")
        destination = Path(argv_tuple[argv_tuple.index("--file") + 1])
        destination.write_bytes(b"PGDMP\x01\x10test")
        passfile = Path(env["PGPASSFILE"])
        assert passfile.exists()
        assert passfile.stat().st_mode & 0o777 == 0o600
        assert "secret" in passfile.read_text(encoding="utf-8")
        assert "PGPASSWORD" not in env
        return CommandResult(argv_tuple, 0, "", "")

    monkeypatch.setenv("PGPASSWORD", "ambient-secret-must-not-leak")
    destination = tmp_path / "database.dump"
    result = await run_pg_dump(
        "postgresql+asyncpg://dump:secret@localhost/source",
        destination,
        pg_dump="/tools/pg_dump",
        expected_server_major=16,
        lock_wait_timeout_seconds=17,
        snapshot_id="00000003-0000001B-1",
        passfile_directory=tmp_path,
        runner=runner,
    )

    assert isinstance(result, DumpResult)
    assert destination.read_bytes().startswith(b"PGDMP")
    dump_argv = calls[1][0]
    assert dump_argv[:8] == (
        "/tools/pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--lock-wait-timeout=17s",
        "--snapshot=00000003-0000001B-1",
        "--no-password",
    )
    assert "secret" not in " ".join(dump_argv)
    assert not list(tmp_path.glob(".ppbase-pgpass-*"))


@pytest.mark.asyncio
async def test_run_pg_dump_rejects_unsafe_snapshot_id(tmp_path: Path) -> None:
    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        return CommandResult(argv_tuple, 0, "pg_dump (PostgreSQL) 16.4\n", "")

    with pytest.raises(ValueError, match="snapshot_id"):
        await run_pg_dump(
            "postgresql+asyncpg://dump:secret@localhost/source",
            tmp_path / "database.dump",
            snapshot_id="00000003-1\n--dbname=attacker",
            runner=runner,
        )
    assert not (tmp_path / "database.dump").exists()


@pytest.mark.asyncio
async def test_run_pg_dump_removes_partial_archive_on_failure(tmp_path: Path) -> None:
    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        if "--version" in argv_tuple:
            return CommandResult(argv_tuple, 0, "pg_dump (PostgreSQL) 16.4\n", "")
        destination = Path(argv_tuple[argv_tuple.index("--file") + 1])
        destination.write_bytes(b"partial")
        raise PostgresCommandError(str(argv_tuple[0]), 1, "dump failed")

    destination = tmp_path / "database.dump"
    with pytest.raises(PostgresCommandError):
        await run_pg_dump(
            "postgresql+asyncpg://dump:secret@localhost/source",
            destination,
            passfile_directory=tmp_path,
            runner=runner,
        )
    assert not destination.exists()


@pytest.mark.asyncio
async def test_run_pg_dump_never_overwrites_existing_archive(tmp_path: Path) -> None:
    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        return CommandResult(argv_tuple, 0, "pg_dump (PostgreSQL) 16.4\n", "")

    destination = tmp_path / "database.dump"
    destination.write_bytes(b"existing")
    with pytest.raises(PostgresBackupError, match="overwrite is forbidden"):
        await run_pg_dump(
            "postgresql+asyncpg://dump:secret@localhost/source",
            destination,
            runner=runner,
        )
    assert destination.read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_run_pg_dump_rejects_client_server_major_mismatch(
    tmp_path: Path,
) -> None:
    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        return CommandResult(argv_tuple, 0, "pg_dump (PostgreSQL) 15.8\n", "")

    destination = tmp_path / "database.dump"
    with pytest.raises(PostgresVersionMismatchError):
        await run_pg_dump(
            "postgresql+asyncpg://dump:secret@localhost/source",
            destination,
            expected_server_major=16,
            runner=runner,
        )
    assert not destination.exists()


@pytest.mark.asyncio
async def test_run_pg_restore_lists_before_safe_transactional_restore(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP\x01\x10test")
    calls: list[tuple[str, ...]] = []

    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        calls.append(argv_tuple)
        if "--version" in argv_tuple:
            return CommandResult(argv_tuple, 0, "pg_restore (PostgreSQL) 16.4\n", "")
        if "--list" in argv_tuple:
            return CommandResult(argv_tuple, 0, "; Archive TOC\nTABLE public users\n", "")
        assert Path(env["PGPASSFILE"]).exists()
        return CommandResult(argv_tuple, 0, "", "")

    result = await run_pg_restore(
        "postgresql+asyncpg://restore:secret@localhost/staging_123",
        archive,
        target_owner="ppbase_owner",
        expected_server_major=16,
        passfile_directory=tmp_path,
        runner=runner,
    )

    assert calls[1] == ("pg_restore", "--list", str(archive))
    restore_argv = calls[2]
    required = {
        "--single-transaction",
        "--exit-on-error",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--no-password",
        "--role=ppbase_owner",
    }
    assert required.issubset(restore_argv)
    assert "--clean" not in restore_argv
    assert "--create" not in restore_argv
    assert "secret" not in " ".join(restore_argv)
    assert result.target_database == "staging_123"


@pytest.mark.asyncio
async def test_run_pg_restore_requires_distinct_restore_login_and_owner(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")

    with pytest.raises(PostgresContractError):
        await run_pg_restore(
            "postgresql+asyncpg://same:secret@localhost/staging",
            archive,
            target_owner="same",
        )


@pytest.mark.asyncio
async def test_create_target_database_checks_roles_and_uses_template0(
    tmp_path: Path,
) -> None:
    calls: list[tuple[tuple[str, ...], tuple[str, ...]]] = []

    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        calls.append((argv_tuple, tuple(redactions)))
        assert "secret" not in " ".join(argv_tuple)
        assert Path(env["PGPASSFILE"]).exists()
        if len(calls) == 1:
            return CommandResult(
                argv_tuple,
                0,
                "creator|f|t|t|t|t|t|f|f\n",
                "",
            )
        return CommandResult(argv_tuple, 0, "CREATE DATABASE\n", "")

    result = await create_target_database(
        "postgresql+asyncpg://creator:secret@localhost/postgres",
        "staging_123",
        target_owner="ppbase_owner",
        restore_role="restore",
        contract=_contract(),
        expected_server_identity=_creator_identity(),
        passfile_directory=tmp_path,
        runner=runner,
    )

    assert result.database == "staging_123"
    assert result.marker_comment.startswith("ppbase-restore-marker:")
    assert len(result.marker_comment) == len("ppbase-restore-marker:") + 64
    assert result.marker_comment not in repr(result)
    assert len(calls) == 2
    check_argv, _ = calls[0]
    check_command_indexes = [
        index
        for index, value in enumerate(check_argv)
        if value == "--command"
    ]
    assert len(check_command_indexes) == 2
    assert check_argv[check_command_indexes[0] + 1] == (
        "SET search_path = pg_catalog, pg_temp"
    )
    assert "current_user AS effective_creator" in check_argv[
        check_command_indexes[1] + 1
    ]
    mutation_argv, mutation_redactions = calls[1]
    command_indexes = [
        index
        for index, value in enumerate(mutation_argv)
        if value == "--command"
    ]
    assert len(command_indexes) == 4
    assert mutation_argv[command_indexes[0] + 1] == (
        "SET search_path = pg_catalog, pg_temp"
    )
    guard_sql = mutation_argv[command_indexes[1] + 1]
    assert "PPBase restore destination role/database changed before create" in guard_sql
    assert "pg_postmaster_start_time" in guard_sql
    assert "role_check.unexpected_membership" in guard_sql
    create_sql = mutation_argv[command_indexes[2] + 1]
    assert 'CREATE DATABASE "staging_123"' in create_sql
    assert "WITH TEMPLATE template0" in create_sql
    assert "ALLOW_CONNECTIONS false" in create_sql
    assert 'OWNER "ppbase_owner"' in create_sql
    assert "ENCODING 'UTF8'" in create_sql
    assert "LOCALE_PROVIDER libc" in create_sql
    acl_sql = mutation_argv[command_indexes[3] + 1]
    assert 'REVOKE ALL ON DATABASE "staging_123" FROM PUBLIC' in acl_sql
    assert (
        'GRANT CONNECT, TEMPORARY ON DATABASE "staging_123" TO "restore"'
        in acl_sql
    )
    assert (
        f"COMMENT ON DATABASE \"staging_123\" IS '{result.marker_comment}'"
        in acl_sql
    )
    assert 'ALTER DATABASE "staging_123" ALLOW_CONNECTIONS true' in acl_sql
    assert acl_sql.index("COMMENT ON DATABASE") < acl_sql.index(
        "ALLOW_CONNECTIONS true"
    )
    assert result.marker_comment in mutation_redactions


@pytest.mark.asyncio
async def test_create_target_database_uses_anchored_anonymous_passfile(
    tmp_path: Path,
) -> None:
    store = LocalBackupStore(tmp_path / "backups")
    staging_root = tmp_path / "restore-staging"
    staging_root.mkdir(mode=0o700)
    staging_root.chmod(0o700)
    target = store.open_staging_data_dir(
        staging_root,
        Path("plan-anonymous-passfile") / "data",
    )
    visible_parent = target.path.parent
    detached_parent = staging_root / "detached-plan"
    active_data_dir = tmp_path / "active-data"
    active_data_dir.mkdir(mode=0o700)
    active_data_dir.chmod(0o700)
    calls = 0
    observed_passfile_paths: list[Path] = []

    async def runner(argv, env, redactions):
        nonlocal calls
        _ = redactions
        calls += 1
        passfile = Path(env["PGPASSFILE"])
        observed_passfile_paths.append(passfile)
        assert passfile.parent in {Path("/proc/self/fd"), Path("/dev/fd")}
        assert passfile.stat().st_mode & 0o777 == 0o600
        assert passfile.read_text(encoding="utf-8") == (
            "localhost:5432:postgres:creator:secret\n"
        )
        argv_tuple = tuple(argv)
        if calls == 1:
            visible_parent.rename(detached_parent)
            visible_parent.symlink_to(active_data_dir, target_is_directory=True)
            return CommandResult(
                argv_tuple,
                0,
                "creator|f|t|t|t|t|t|f|f\n",
                "",
            )
        return CommandResult(argv_tuple, 0, "CREATE DATABASE\n", "")

    try:
        result = await create_target_database(
            "postgresql+asyncpg://creator:secret@localhost/postgres",
            "staging_anchored_passfile",
            target_owner="ppbase_owner",
            restore_role="restore",
            contract=_contract(),
            expected_server_identity=_creator_identity(),
            passfile_factory=target.temporary_file,
            runner=runner,
        )
    finally:
        target.close()

    assert result.database == "staging_anchored_passfile"
    assert calls == 2
    assert observed_passfile_paths[0] == observed_passfile_paths[1]
    assert list(active_data_dir.iterdir()) == []
    assert sorted(path.name for path in detached_parent.iterdir()) == ["data"]
    assert not observed_passfile_paths[0].exists()


@pytest.mark.asyncio
async def test_create_target_database_refuses_existing_target(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        calls.append(argv_tuple)
        return CommandResult(
            argv_tuple,
            0,
            "creator|t|t|t|t|t|t|f|f\n",
            "",
        )

    with pytest.raises(TargetDatabaseExistsError):
        await create_target_database(
            "postgresql+asyncpg://creator:secret@localhost/postgres",
            "staging_123",
            target_owner="ppbase_owner",
            restore_role="restore",
            contract=_contract(),
            expected_server_identity=_creator_identity(),
            passfile_directory=tmp_path,
            runner=runner,
        )
    assert len(calls) == 1
    assert not list(tmp_path.glob(".ppbase-pgpass-*"))


@pytest.mark.asyncio
async def test_create_target_database_rejects_predefined_target_owner(
    tmp_path: Path,
) -> None:
    async def runner(argv, env, redactions):  # pragma: no cover - must not run
        raise AssertionError("PostgreSQL tooling must not run for a predefined owner")

    with pytest.raises(PostgresContractError, match="dedicated role"):
        await create_target_database(
            "postgresql+asyncpg://creator:secret@localhost/postgres",
            "staging_123",
            target_owner="pg_read_all_data",
            restore_role="restore",
            contract=_contract(),
            expected_server_identity=_creator_identity(),
            passfile_directory=tmp_path,
            runner=runner,
        )


def test_database_contract_manifest_round_trip() -> None:
    contract = _contract()

    assert DatabaseContract.from_dict(contract.to_dict()) == contract
