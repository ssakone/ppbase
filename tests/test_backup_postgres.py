from __future__ import annotations

from pathlib import Path

import pytest

from ppbase.backup.postgres import (
    _forbidden_table_write_privileges,
    CollationContract,
    CommandResult,
    DatabaseContract,
    DumpResult,
    ExtensionContract,
    ObjectSecuritySummary,
    PostgresCommandError,
    PostgresVersionMismatchError,
    PostgresBackupError,
    inspect_pg_restore_archive,
    _stream_restore_sql_to_psql,
    preflight_dump_role,
    preflight_destructive_restore_role,
    run_destructive_pg_restore_from_fd,
    run_pg_dump,
    sqlalchemy_url_to_libpq,
    temporary_pgpass,
)


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


def test_forbidden_table_privileges_are_server_version_aware() -> None:
    assert "MAINTAIN" not in _forbidden_table_write_privileges(160014)
    assert "MAINTAIN" in _forbidden_table_write_privileges(170000)
    assert "MAINTAIN" in _forbidden_table_write_privileges(180001)


@pytest.mark.asyncio
async def test_dump_preflight_matches_public_schema_dump_scope() -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, *, row=None, rows=None, scalar=None):
            self._row = row
            self._rows = rows or []
            self._scalar = scalar

        def mappings(self):
            return self

        def one(self):
            return self._row

        def all(self):
            return self._rows

        def scalar_one(self):
            return self._scalar

    class Connection:
        async def execute(self, statement, _params=None):
            sql = str(statement)
            statements.append(sql)
            if "AS server_version_num" in sql:
                return Result(
                    row={
                        "rolname": "dump",
                        "rolsuper": False,
                        "rolcreatedb": False,
                        "rolcreaterole": False,
                        "rolreplication": False,
                        "rolbypassrls": False,
                        "server_version_num": 160004,
                        "can_connect": True,
                        "read_server_files": False,
                        "write_server_files": False,
                        "execute_server_program": False,
                    }
                )
            if "SELECT count(*)" in sql:
                return Result(scalar=0)
            return Result(rows=[])

    report = await preflight_dump_role(Connection())

    assert report.ok is True
    scoped_sql = "\n".join(statements[2:])
    assert "n.nspname = 'public'" in scoped_sql
    assert "information_schema" not in scoped_sql
    assert "pg_largeobject" not in scoped_sql
    assert "large_object" not in scoped_sql


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
    assert dump_argv[:9] == (
        "/tools/pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--no-tablespaces",
        "--schema=public",
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
async def test_run_pg_dump_accepts_a_newer_client_for_an_older_server(
    tmp_path: Path,
) -> None:
    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        if "--version" in argv_tuple:
            return CommandResult(argv_tuple, 0, "pg_dump (PostgreSQL) 17.5\n", "")
        Path(argv_tuple[argv_tuple.index("--file") + 1]).write_bytes(b"PGDMP")
        return CommandResult(argv_tuple, 0, "", "")

    destination = tmp_path / "database.dump"
    await run_pg_dump(
        "postgresql+asyncpg://dump:secret@localhost/source",
        destination,
        expected_server_major=16,
        runner=runner,
    )

    assert destination.read_bytes() == b"PGDMP"


@pytest.mark.asyncio
async def test_archive_inspection_rejects_non_public_schema_before_mutation(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")

    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        if "--version" in argv_tuple:
            return CommandResult(argv_tuple, 0, "pg_restore (PostgreSQL) 17.5\n", "")
        return CommandResult(
            argv_tuple,
            0,
            "6; 2615 2200 SCHEMA - public owner\n"
            "7; 2615 2201 SCHEMA - private owner\n"
            "214; 1259 16385 TABLE private secret owner\n",
            "",
        )

    with pytest.raises(PostgresBackupError, match="only the public"):
        await inspect_pg_restore_archive(
            archive,
            pg_restore="/tools/pg_restore",
            expected_server_major=16,
            runner=runner,
        )


@pytest.mark.asyncio
async def test_archive_inspection_accepts_public_only_toc(tmp_path: Path) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP")
    toc = (
        "2; 3079 16384 EXTENSION - plpgsql owner\n"
        "6; 2615 2200 SCHEMA - public owner\n"
        "214; 1259 16385 TABLE public records owner\n"
        "3348; 0 16385 TABLE DATA public records owner\n"
        "3350; 0 16385 ACL public TABLE records owner\n"
    )

    async def runner(argv, env, redactions):
        argv_tuple = tuple(argv)
        if "--version" in argv_tuple:
            return CommandResult(argv_tuple, 0, "pg_restore (PostgreSQL) 17.5\n", "")
        return CommandResult(argv_tuple, 0, toc, "")

    inspection = await inspect_pg_restore_archive(
        archive,
        pg_restore="/tools/pg_restore",
        expected_server_major=16,
        runner=runner,
    )

    assert inspection.toc == toc


@pytest.mark.asyncio
async def test_preflight_accepts_effective_database_and_schema_owner() -> None:
    statements: list[str] = []

    class Result:
        def __init__(self, row: dict[str, object] | None = None) -> None:
            self.row = row

        def mappings(self) -> Result:
            return self

        def one(self) -> dict[str, object]:
            assert self.row is not None
            return self.row

    class Connection:
        async def execute(self, statement, *_args, **_kwargs):
            rendered = str(statement)
            statements.append(rendered)
            if rendered.startswith("SET LOCAL"):
                return Result()
            return Result(
                {
                    "role": "runtime_owner",
                    "is_superuser": False,
                    "owns_database": True,
                    "owns_schema": True,
                }
            )

    report = await preflight_destructive_restore_role(Connection())  # type: ignore[arg-type]

    assert report.ok
    assert report.warnings == ()
    assert len(statements) == 2


@pytest.mark.asyncio
async def test_destructive_restore_wraps_generated_sql_and_marker_in_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP test")
    capture = tmp_path / "restore.sql"
    pg_restore = tmp_path / "pg_restore"
    psql = tmp_path / "psql"
    pg_restore.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'pg_restore (PostgreSQL) 17.5'\n"
        "elif [ \"$1\" = \"--list\" ]; then\n"
        "  echo '214; 1259 16385 TABLE public restored owner'\n"
        "else\n"
        "  echo 'CREATE TABLE public.restored (id integer);'\n"
        "fi\n",
        encoding="utf-8",
    )
    psql.write_text(
        "#!/bin/sh\ncat > \"$PPBASE_TEST_SQL_CAPTURE\"\n",
        encoding="utf-8",
    )
    pg_restore.chmod(0o700)
    psql.chmod(0o700)
    monkeypatch.setenv("PPBASE_TEST_SQL_CAPTURE", str(capture))

    descriptor = archive.open("rb")
    try:
        result = await run_destructive_pg_restore_from_fd(
            "postgresql+asyncpg://Runtime%20Owner:secret@localhost/active",
            descriptor.fileno(),
            archive_label=archive,
            restore_id="e" * 32,
            pg_restore=str(pg_restore),
            psql=str(psql),
            expected_server_major=16,
        )
    finally:
        descriptor.close()

    sql = capture.read_text(encoding="utf-8")
    assert sql.index("BEGIN;") < sql.index("DROP SCHEMA IF EXISTS public CASCADE;")
    assert 'CREATE SCHEMA public AUTHORIZATION "Runtime Owner";' in sql
    assert sql.index("CREATE TABLE public.restored") < sql.index(
        'CREATE SCHEMA "_ppbase_restore_control"'
    )
    assert sql.rstrip().endswith("COMMIT;")
    assert result.runtime_role == "Runtime Owner"
    assert result.restore_id == "e" * 32


@pytest.mark.asyncio
async def test_restore_stream_strips_only_header_transaction_timeout() -> None:
    """The PostgreSQL 17 header GUC is dropped without touching COPY payloads.

    A ``SET transaction_timeout`` line that appears in the leading header (past
    the ``\\restrict`` guard) must be removed so a 17 client can restore into a
    16 server, while an identical string inside COPY data is streamed verbatim.
    """
    payload = (
        b"--\n-- PostgreSQL database dump\n--\n\n"
        b"\\restrict abc123\n\n"
        b"SET statement_timeout = 0;\n"
        b"SET transaction_timeout = 0;\n"
        b"SET client_encoding = 'UTF8';\n"
        b"CREATE TABLE public.records (payload text);\n"
        b"COPY public.records (payload) FROM stdin;\n"
        b"SET transaction_timeout = 0;\n"  # legitimate row data, must survive
        b"\\.\n"
    )

    class Source:
        """Yield tiny fixed-size chunks so lines straddle read boundaries."""

        def __init__(self, data: bytes, *, chunk: int) -> None:
            self._data = data
            self._chunk = chunk

        async def read(self, size: int) -> bytes:
            take = min(self._chunk, size)
            chunk = self._data[:take]
            self._data = self._data[take:]
            return chunk

    class Destination:
        def __init__(self) -> None:
            self.buffer = bytearray()

        def write(self, data: bytes) -> None:
            self.buffer.extend(data)

        async def drain(self) -> None:
            return None

    destination = Destination()
    # A 3-byte cap forces every header line (and the boundary between the
    # header and the COPY body) to span several reads, exercising the
    # partial-line buffering path rather than a single whole-buffer read.
    await _stream_restore_sql_to_psql(Source(payload, chunk=3), destination)
    result = bytes(destination.buffer)

    # Header GUC removed exactly once; the COPY data copy is preserved.
    assert result.count(b"SET transaction_timeout = 0;\n") == 1
    assert b"CREATE TABLE public.records" in result
    assert b"COPY public.records" in result
    assert result.index(b"COPY public.records") < result.index(
        b"SET transaction_timeout = 0;\n"
    )
    assert b"\\restrict abc123\n" in result
    assert b"SET statement_timeout = 0;\n" in result


@pytest.mark.asyncio
async def test_destructive_restore_recreates_public_for_modern_toc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PostgreSQL 15+ archive lists ``SCHEMA - public`` yet never re-creates it.

    ``pg_restore --schema=public`` emits no ``CREATE SCHEMA public`` statement
    for the built-in schema, so the restore wrapper must recreate ``public``
    itself after the drop or every ``CREATE TABLE public.*`` would fail.
    """
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP test")
    capture = tmp_path / "restore.sql"
    pg_restore = tmp_path / "pg_restore"
    psql = tmp_path / "psql"
    pg_restore.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'pg_restore (PostgreSQL) 17.5'\n"
        "elif [ \"$1\" = \"--list\" ]; then\n"
        "  echo '4; 2615 2200 SCHEMA - public pg_database_owner'\n"
        "  echo '214; 1259 16385 TABLE public restored owner'\n"
        "else\n"
        "  echo 'CREATE TABLE public.restored (id integer);'\n"
        "fi\n",
        encoding="utf-8",
    )
    psql.write_text(
        "#!/bin/sh\ncat > \"$PPBASE_TEST_SQL_CAPTURE\"\n",
        encoding="utf-8",
    )
    pg_restore.chmod(0o700)
    psql.chmod(0o700)
    monkeypatch.setenv("PPBASE_TEST_SQL_CAPTURE", str(capture))

    descriptor = archive.open("rb")
    try:
        await run_destructive_pg_restore_from_fd(
            "postgresql+asyncpg://runtime:secret@localhost/active",
            descriptor.fileno(),
            archive_label=archive,
            restore_id="a" * 32,
            pg_restore=str(pg_restore),
            psql=str(psql),
            expected_server_major=16,
        )
    finally:
        descriptor.close()

    sql = capture.read_text(encoding="utf-8")
    assert sql.index("DROP SCHEMA IF EXISTS public CASCADE;") < sql.index(
        "CREATE SCHEMA public AUTHORIZATION"
    )
    assert sql.index("CREATE SCHEMA public AUTHORIZATION") < sql.index(
        "CREATE TABLE public.restored"
    )


@pytest.mark.asyncio
async def test_destructive_restore_reports_psql_failure_as_postgres_error(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "database.dump"
    archive.write_bytes(b"PGDMP test")
    pg_restore = tmp_path / "pg_restore"
    psql = tmp_path / "psql"
    pg_restore.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  echo 'pg_restore (PostgreSQL) 17.5'\n"
        "elif [ \"$1\" = \"--list\" ]; then\n"
        "  echo '214; 1259 16385 TABLE public restored owner'\n"
        "else\n"
        "  echo 'CREATE TABLE public.restored (id integer);'\n"
        "fi\n",
        encoding="utf-8",
    )
    psql.write_text(
        "#!/bin/sh\ncat >/dev/null\necho 'synthetic SQL failure' >&2\nexit 3\n",
        encoding="utf-8",
    )
    pg_restore.chmod(0o700)
    psql.chmod(0o700)

    descriptor = archive.open("rb")
    try:
        with pytest.raises(PostgresCommandError) as rejected:
            await run_destructive_pg_restore_from_fd(
                "postgresql+asyncpg://runtime:secret@localhost/active",
                descriptor.fileno(),
                archive_label=archive,
                restore_id="f" * 32,
                pg_restore=str(pg_restore),
                psql=str(psql),
                expected_server_major=16,
            )
    finally:
        descriptor.close()

    assert rejected.value.executable == str(psql)
    assert rejected.value.returncode == 3
    assert "synthetic SQL failure" in str(rejected.value)


def test_database_contract_manifest_round_trip() -> None:
    contract = _contract()

    assert DatabaseContract.from_dict(contract.to_dict()) == contract
