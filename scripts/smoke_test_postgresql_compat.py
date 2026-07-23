#!/usr/bin/env python3
"""Install a PPBase wheel and exercise its tools against PostgreSQL 16/17."""

from __future__ import annotations

import argparse
import asyncio
from importlib import metadata
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]


class CompatibilitySmokeError(RuntimeError):
    """Raised when a published-wheel compatibility guarantee is not met."""


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    if project.get("name") != "ppbase" or not isinstance(project.get("version"), str):
        raise CompatibilitySmokeError("pyproject must declare one ppbase version")
    return project["version"]


def _postgresql_client_version() -> str:
    value = json.loads(
        (ROOT / "scripts" / "postgresql-source.json").read_text(encoding="utf-8")
    ).get("version")
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        raise CompatibilitySmokeError("PostgreSQL source lock version is invalid")
    return value


def _pip_install(wheelhouse: Path, version: str) -> None:
    wheels = sorted(wheelhouse.glob("ppbase-*.whl"))
    if len(wheels) != 4:
        raise CompatibilitySmokeError(
            f"pip selection smoke requires all four wheels, found {len(wheels)}"
        )
    common = (sys.executable, "-m", "pip", "--disable-pip-version-check")
    # Resolve ppbase by project name from a directory containing multiple wheel
    # candidates in release CI. pip, not the user, selects the current platform.
    subprocess.run(
        (
            *common,
            "install",
            "--no-index",
            "--no-deps",
            "--find-links",
            str(wheelhouse),
            f"ppbase=={version}",
        ),
        check=True,
        cwd=wheelhouse,
    )
    # A normal `pip install ppbase` pass installs runtime dependencies while
    # retaining the already-selected local wheel at the exact version.
    subprocess.run(
        (*common, "install", f"ppbase=={version}"),
        check=True,
        cwd=wheelhouse,
    )


def _run(argv: tuple[str, ...], env: dict[str, str]) -> str:
    completed = subprocess.run(
        argv,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return (completed.stdout or completed.stderr).strip()


def _psql(
    executable: str,
    *,
    host: str,
    port: int,
    user: str,
    database: str,
    sql: str,
    env: dict[str, str],
) -> str:
    return _run(
        (
            executable,
            "--no-password",
            "--host",
            host,
            "--port",
            str(port),
            "--username",
            user,
            "--dbname",
            database,
            "--set",
            "ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--command",
            sql,
        ),
        env,
    )


def _tool_major(output: str) -> int:
    match = re.search(r"\(PostgreSQL\)\s+(\d+)(?:\.|\s|$)", output)
    if match is None:
        raise CompatibilitySmokeError(f"unexpected PostgreSQL version output: {output}")
    return int(match.group(1))


def smoke(args: argparse.Namespace) -> dict[str, object]:
    version = _project_version()
    client_version = _postgresql_client_version()
    _pip_install(args.wheelhouse.resolve(), version)

    sys.path = [
        entry
        for entry in sys.path
        if Path(entry or os.getcwd()).resolve() != ROOT
    ]
    from ppbase.backup.postgres import run_destructive_pg_restore_from_fd
    from ppbase.backup.tools import resolve_postgres_tool

    installed = metadata.distribution("ppbase")
    if installed.version != version or installed.metadata["Name"].lower() != "ppbase":
        raise CompatibilitySmokeError("pip installed a different project or version")

    source_database = "ppbase_wheel_source"
    target_database = "ppbase_wheel_target"
    with tempfile.TemporaryDirectory(prefix="ppbase-wheel-compat-") as temporary:
        temporary_path = Path(temporary)
        empty_path = temporary_path / "empty-path"
        empty_path.mkdir()
        archive = temporary_path / "smoke.dump"
        env = os.environ.copy()
        env["PATH"] = str(empty_path)
        tools = {
            name: resolve_postgres_tool(name, name)
            for name in ("pg_dump", "pg_restore", "psql")
        }
        versions = {
            name: _run((executable, "--version"), env)
            for name, executable in tools.items()
        }
        expected_client_major = int(client_version.split(".", 1)[0])
        if any(
            _tool_major(output) != expected_client_major
            or client_version not in output
            for output in versions.values()
        ):
            raise CompatibilitySmokeError(
                f"wheel did not install PostgreSQL {client_version} tools: {versions}"
            )

        admin = {
            "executable": tools["psql"],
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "database": "postgres",
            "env": env,
        }
        try:
            for database in (source_database, target_database):
                _psql(sql=f'DROP DATABASE IF EXISTS "{database}";', **admin)
                _psql(sql=f'CREATE DATABASE "{database}";', **admin)

            server_version = _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=source_database,
                sql="SHOW server_version_num;",
                env=env,
            )
            server_major = int(server_version) // 10000
            if server_major != args.server_major:
                raise CompatibilitySmokeError(
                    f"expected PostgreSQL {args.server_major}, got {server_version}"
                )
            _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=source_database,
                sql=(
                    "CREATE TABLE wheel_smoke (id integer PRIMARY KEY, value text NOT NULL);"
                    "INSERT INTO wheel_smoke VALUES (1, 'one'), (2, 'two'), (3, 'three');"
                ),
                env=env,
            )
            connection_args = (
                "--no-password",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--username",
                args.user,
            )
            _run(
                (
                    tools["pg_dump"],
                    *connection_args,
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--file",
                    str(archive),
                    source_database,
                ),
                env,
            )
            if not archive.is_file() or archive.stat().st_size == 0:
                raise CompatibilitySmokeError("pg_dump did not create an archive")
            _run(
                (
                    tools["pg_restore"],
                    *connection_args,
                    "--exit-on-error",
                    "--no-owner",
                    "--no-acl",
                    "--dbname",
                    target_database,
                    str(archive),
                ),
                env,
            )
            restored = _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=target_database,
                sql="SELECT count(*)::text || ':' || sum(id)::text FROM wheel_smoke;",
                env=env,
            )
            if restored != "3:6":
                raise CompatibilitySmokeError(f"restored data is incorrect: {restored!r}")

            # Exercise the *real* destructive in-place restore path that the
            # HTTP endpoint uses, not just a plain ``pg_restore --dbname``. This
            # proves the bundled 17 client recreates ``public`` and streams the
            # pg_restore SQL through psql (filtering the 17-only header GUC) into
            # both a 16 and a 17 server. A pre-existing object confirms the
            # public schema is genuinely dropped and rebuilt.
            _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=target_database,
                sql="CREATE TABLE public.pre_existing (id integer);",
                env=env,
            )
            public_archive = temporary_path / "smoke-public.dump"
            _run(
                (
                    tools["pg_dump"],
                    *connection_args,
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    "--schema=public",
                    "--file",
                    str(public_archive),
                    source_database,
                ),
                env,
            )
            restore_id = secrets.token_hex(16)
            password = os.environ.get("PGPASSWORD", "")
            target_url = (
                f"postgresql+asyncpg://{quote(args.user)}:{quote(password)}"
                f"@{args.host}:{args.port}/{target_database}"
            )
            descriptor = os.open(str(public_archive), os.O_RDONLY)
            try:
                asyncio.run(
                    run_destructive_pg_restore_from_fd(
                        target_url,
                        descriptor,
                        archive_label=public_archive,
                        restore_id=restore_id,
                        pg_restore=tools["pg_restore"],
                        psql=tools["psql"],
                        expected_server_major=args.server_major,
                    )
                )
            finally:
                os.close(descriptor)

            destructive = _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=target_database,
                sql="SELECT count(*)::text || ':' || sum(id)::text FROM wheel_smoke;",
                env=env,
            )
            if destructive != "3:6":
                raise CompatibilitySmokeError(
                    f"destructive restore data is incorrect: {destructive!r}"
                )
            leftover = _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=target_database,
                sql=(
                    "SELECT count(*)::text FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'pre_existing';"
                ),
                env=env,
            )
            if leftover != "0":
                raise CompatibilitySmokeError(
                    "destructive restore did not drop pre-existing public objects"
                )
            committed = _psql(
                tools["psql"],
                host=args.host,
                port=args.port,
                user=args.user,
                database=target_database,
                sql=(
                    "SELECT count(*)::text FROM \"_ppbase_restore_control\".commits "
                    f"WHERE restore_id = '{restore_id}';"
                ),
                env=env,
            )
            if committed != "1":
                raise CompatibilitySmokeError(
                    "destructive restore did not record its commit marker"
                )
        finally:
            for database in (source_database, target_database):
                try:
                    _psql(sql=f'DROP DATABASE IF EXISTS "{database}";', **admin)
                except (OSError, subprocess.CalledProcessError):
                    pass

    return {
        "project": installed.metadata["Name"],
        "version": installed.version,
        "serverMajor": args.server_major,
        "tools": versions,
        "pathWasEmpty": True,
        "dumpRestoreRoundTrip": True,
        "destructiveRestoreRoundTrip": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--server-major", type=int, choices=(16, 17), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    return parser


def main() -> int:
    try:
        result = smoke(_parser().parse_args())
    except (CompatibilitySmokeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
