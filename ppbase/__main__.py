"""Entry point for ``python -m ppbase``.

Supports the following commands:

    ppbase serve          -- Start the HTTP server (background with --daemon)
    ppbase stop           -- Stop a running daemon
    ppbase restart        -- Restart the daemon
    ppbase status         -- Check if PPBase is running
    ppbase db {start|stop|restart|status}  -- Manage PostgreSQL Docker container
    ppbase create-admin   -- Interactively create an admin account
    ppbase migrate {up|down|status|create|snapshot}  -- Manage migrations
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path


# PID file lives next to the package directory
_PROJECT_DIR = Path(__file__).resolve().parent.parent
_PIDFILE = _PROJECT_DIR / ".ppbase.pid"
_LOGFILE = _PROJECT_DIR / ".ppbase.log"


# ---------------------------------------------------------------------------
# Process management helpers
# ---------------------------------------------------------------------------


def _find_pid() -> int | None:
    """Return the PID of a running PPBase server, or None."""
    # Check pidfile
    if _PIDFILE.exists():
        try:
            pid = int(_PIDFILE.read_text().strip())
            os.kill(pid, 0)  # check if alive
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            _PIDFILE.unlink(missing_ok=True)
    return None


def _stop_server() -> bool:
    """Stop a running PPBase server. Returns True if a server was stopped."""
    pid = _find_pid()
    if pid is None:
        print("PPBase is not running.")
        return False

    print(f"Stopping PPBase (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _PIDFILE.unlink(missing_ok=True)
        print("PPBase stopped.")
        return True

    # Wait up to 5 seconds
    for _ in range(10):
        try:
            os.kill(pid, 0)
            time.sleep(0.5)
        except ProcessLookupError:
            break

    # Force kill if still alive
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    _PIDFILE.unlink(missing_ok=True)
    print("PPBase stopped.")
    return True


_DEFAULT_DB_URL = "postgresql+asyncpg://ppbase:ppbase@localhost:5433/ppbase"


def _start_daemon(
    host: str,
    port: int,
    db: str | None = None,
    data_dir: str | None = None,
    public_dir: str | None = None,
    migrations_dir: str | None = None,
    hooks_dir: str | None = None,
    hooks: list[str] | None = None,
    automigrate: bool | None = None,
    apply_migrations_on_start: bool | None = None,
    generate_migrations: bool | None = None,
) -> None:
    """Start PPBase as a background daemon."""
    if _find_pid() is not None:
        print(f"PPBase is already running (PID {_find_pid()}).")
        return

    cmd = [sys.executable, "-m", "ppbase", "serve", "--host", host, "--port", str(port)]
    if db:
        cmd += ["--db", db]
    if data_dir:
        cmd += ["--dir", data_dir]
    if public_dir:
        cmd += ["--publicDir", public_dir]
    if migrations_dir:
        cmd += ["--migrationsDir", migrations_dir]
    if hooks_dir:
        cmd += ["--hooksDir", hooks_dir]
    for target in hooks or []:
        cmd += ["--hooks", target]
    if automigrate is True:
        cmd += ["--automigrate"]
    elif automigrate is False:
        cmd += ["--no-automigrate"]
    if apply_migrations_on_start is True:
        cmd += ["--apply-migrations-on-start"]
    elif apply_migrations_on_start is False:
        cmd += ["--no-apply-migrations-on-start"]
    if generate_migrations is True:
        cmd += ["--generate-migrations"]
    elif generate_migrations is False:
        cmd += ["--no-generate-migrations"]

    env = os.environ.copy()
    env.setdefault("PPBASE_DATABASE_URL", db or _DEFAULT_DB_URL)

    log = open(_LOGFILE, "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,
    )
    _PIDFILE.write_text(str(proc.pid))
    time.sleep(1)

    if proc.poll() is None:
        print(f"PPBase started (PID {proc.pid})")
        print(f"  URL:  http://{host}:{port}")
        print(f"  Logs: {_LOGFILE}")
    else:
        print(f"Failed to start PPBase. Check {_LOGFILE}")
        _PIDFILE.unlink(missing_ok=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_serve(args: argparse.Namespace) -> None:
    """Start the PPBase server (foreground or daemon)."""
    from ppbase import pb

    os.environ["PPBASE_RESTART_CMD"] = json.dumps(
        [sys.executable, "-m", "ppbase", *sys.argv[1:]]
    )

    overrides: dict = {}
    if args.db:
        overrides["database_url"] = args.db
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if args.public_dir:
        overrides["public_dir"] = args.public_dir
    if args.migrations_dir:
        overrides["migrations_dir"] = args.migrations_dir
    if getattr(args, "hooks_dir", None):
        overrides["hooks_dir"] = args.hooks_dir
    if args.automigrate is not None:
        overrides["auto_migrate"] = args.automigrate
    if args.apply_migrations_on_start is not None:
        overrides["apply_migrations_on_start"] = args.apply_migrations_on_start
    if args.generate_migrations is not None:
        overrides["generate_migrations"] = args.generate_migrations

    if overrides:
        pb.configure(**overrides)
    for target in args.hooks:
        pb.load_hooks(target)

    host = args.host or pb.settings.host
    port = args.port or pb.settings.port

    if getattr(args, "daemon", False):
        _start_daemon(
            host,
            port,
            args.db,
            data_dir=args.data_dir,
            public_dir=args.public_dir,
            migrations_dir=args.migrations_dir,
            hooks_dir=getattr(args, "hooks_dir", None),
            hooks=args.hooks,
            automigrate=args.automigrate,
            apply_migrations_on_start=args.apply_migrations_on_start,
            generate_migrations=args.generate_migrations,
        )
    else:
        print(f"Starting PPBase server at http://{host}:{port}")
        pb.start(host=host, port=port)


def _cmd_stop(_args: argparse.Namespace) -> None:
    """Stop a running PPBase daemon."""
    if not _stop_server():
        sys.exit(1)


def _cmd_restart(args: argparse.Namespace) -> None:
    """Restart PPBase daemon."""
    _stop_server()
    time.sleep(1)

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or 8090
    db = getattr(args, "db", None)
    _start_daemon(host, port, db)


def _cmd_status(_args: argparse.Namespace) -> None:
    """Check if PPBase is running."""
    pid = _find_pid()
    if pid is not None:
        print(f"PPBase is running (PID {pid})")
    else:
        print("PPBase is not running.")
        sys.exit(1)


_PG_CONTAINER = "ppbase-pg"
_PG_IMAGE = "postgres:17"
_PG_PORT = 5433
_PG_USER = "ppbase"
_PG_PASSWORD = "ppbase"
_PG_DB = "ppbase"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _pg_container_status() -> str | None:
    """Return container status: 'running', 'exited', or None if not found."""
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", _PG_CONTAINER],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _cmd_db(args: argparse.Namespace) -> None:
    """Manage the PostgreSQL Docker container."""
    if not _docker_available():
        print("Error: docker is not installed or not in PATH.", file=sys.stderr)
        sys.exit(1)

    action = args.action

    if action == "status":
        status = _pg_container_status()
        if status is None:
            print(f"Container '{_PG_CONTAINER}' does not exist.")
        elif status == "running":
            print(f"PostgreSQL is running (container: {_PG_CONTAINER}, port: {_PG_PORT})")
        else:
            print(f"PostgreSQL container exists but is {status}.")
        return

    if action == "stop":
        status = _pg_container_status()
        if status != "running":
            print("PostgreSQL is not running.")
            return
        print(f"Stopping {_PG_CONTAINER}...")
        subprocess.run(["docker", "stop", _PG_CONTAINER], capture_output=True)
        print("PostgreSQL stopped.")
        return

    if action in ("start", "restart"):
        if action == "restart":
            status = _pg_container_status()
            if status == "running":
                print(f"Stopping {_PG_CONTAINER}...")
                subprocess.run(["docker", "stop", _PG_CONTAINER], capture_output=True)
                print("PostgreSQL stopped.")

        status = _pg_container_status()

        if status == "running":
            print(f"PostgreSQL is already running (container: {_PG_CONTAINER}).")
            return

        if status is not None:
            # Container exists but stopped — just start it
            print(f"Starting existing container {_PG_CONTAINER}...")
            r = subprocess.run(["docker", "start", _PG_CONTAINER], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"Error: {r.stderr.strip()}", file=sys.stderr)
                sys.exit(1)
        else:
            # Create new container
            print(f"Creating and starting {_PG_CONTAINER} (PostgreSQL 17, port {_PG_PORT})...")
            r = subprocess.run([
                "docker", "run", "-d",
                "--name", _PG_CONTAINER,
                "-e", f"POSTGRES_DB={_PG_DB}",
                "-e", f"POSTGRES_USER={_PG_USER}",
                "-e", f"POSTGRES_PASSWORD={_PG_PASSWORD}",
                "-p", f"{_PG_PORT}:5433",
                _PG_IMAGE,
            ], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"Error: {r.stderr.strip()}", file=sys.stderr)
                sys.exit(1)

        # Wait for PostgreSQL to be ready
        print("Waiting for PostgreSQL to be ready...", end="", flush=True)
        for _ in range(30):
            check = subprocess.run(
                ["docker", "exec", _PG_CONTAINER, "pg_isready", "-U", _PG_USER],
                capture_output=True,
            )
            if check.returncode == 0:
                print(" ready.")
                print(f"  Container: {_PG_CONTAINER}")
                print(f"  Port:      {_PG_PORT}")
                print(f"  DB URL:    postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@localhost:{_PG_PORT}/{_PG_DB}")
                return
            print(".", end="", flush=True)
            time.sleep(1)

        print("\nWarning: PostgreSQL did not become ready in 30s. Check with: docker logs ppbase-pg")
        return

    print(f"Unknown db action: {action}")
    sys.exit(1)


def _cmd_create_admin(args: argparse.Namespace) -> None:
    """Create an admin account interactively."""

    async def _create() -> None:
        from ppbase.config import Settings
        from ppbase.db.engine import init_engine, close_engine
        from ppbase.db.system_tables import create_system_tables
        from ppbase.services import admin_service
        from ppbase.db.engine import get_async_session

        settings = Settings()
        if args.db:
            settings = Settings(database_url=args.db)

        engine = await init_engine(settings.database_url)
        await create_system_tables(engine)

        email = args.email or input("Admin email: ")
        password = args.password or getpass.getpass("Admin password: ")

        if not email or not password:
            print("Error: email and password are required.", file=sys.stderr)
            await close_engine()
            sys.exit(1)

        async for session in get_async_session():
            existing = await admin_service.get_admin_by_email(session, email)
            if existing:
                print(f"Admin with email '{email}' already exists.")
                await close_engine()
                return

            admin = await admin_service.create_admin(session, email, password)
            await session.commit()
            print(f"Admin created successfully (id={admin.id}, email={admin.email})")
            break

        await close_engine()

    asyncio.run(_create())


def _read_bootstrap_dsn(args: argparse.Namespace) -> str:
    """Read the ephemeral cluster-admin DSN without accepting it on argv."""
    environment_name = (
        "PPBASE_POSTGRES_BOOTSTRAP_DATABASE_URL"
        if getattr(args, "command", None) == "init"
        else "PPBASE_BACKUP_BOOTSTRAP_DATABASE_URL"
    )
    value = str(os.environ.get(environment_name, "") or "").strip()
    path_value = str(getattr(args, "bootstrap_dsn_file", "") or "").strip()
    if value and path_value:
        raise ValueError(
            f"Use either {environment_name} or --bootstrap-dsn-file, not both."
        )
    if path_value:
        path = Path(path_value).expanduser().absolute()
        info = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValueError("Bootstrap DSN file must be a regular non-symlink file.")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("Bootstrap DSN file must have mode 0600.")
        value = path.read_text(encoding="utf-8").strip()
    if not value:
        value = getpass.getpass("Ephemeral PostgreSQL bootstrap DSN: ").strip()
    if not value:
        raise ValueError("A PostgreSQL bootstrap DSN is required.")
    return value


def _cmd_init(args: argparse.Namespace) -> None:
    """Initialize a complete PostgreSQL-backed PPBase project."""
    from ppbase.backup.provision import (
        BackupProvisionError,
        build_postgres_init_plan,
        execute_postgres_init,
    )
    from ppbase.config import Settings

    if args.resource != "postgres":
        print("Usage: ppbase init postgres --plan|--execute --name <project>")
        raise SystemExit(1)

    settings = Settings()
    updates = {
        field: value
        for field, value in (
            ("data_dir", args.data_dir),
            ("backup_root", args.backup_root),
            ("backup_control_dir", args.backup_control_dir),
            ("backup_staging_root", args.backup_staging_root),
            ("backup_target_root", args.backup_target_root),
        )
        if value is not None
    }
    if updates:
        settings = settings.model_copy(update=updates)

    async def run() -> None:
        bootstrap = _read_bootstrap_dsn(args)
        if args.plan:
            plan = await build_postgres_init_plan(
                settings,
                bootstrap_database_url=bootstrap,
                project_name=args.name,
                secret_sink=args.output_env,
            )
            print(json.dumps(plan, indent=2, sort_keys=True))
            if not plan["executable"]:
                raise SystemExit(2)
            return
        if not args.execute:
            raise BackupProvisionError("Select exactly one of --plan or --execute.")
        if not args.output_env:
            raise BackupProvisionError(
                "--output-env is required as the mode-0600 limited-credential sink"
            )
        result = await execute_postgres_init(
            settings,
            bootstrap_database_url=bootstrap,
            project_name=args.name,
            secret_sink=args.output_env,
        )
        print(json.dumps(result, indent=2, sort_keys=True))

    try:
        asyncio.run(run())
    except (BackupProvisionError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc


def _cmd_backup(args: argparse.Namespace) -> None:
    """Provision and diagnose native backup prerequisites."""
    from ppbase.backup.provision import (
        BackupProvisionError,
        backup_doctor,
        build_provision_plan,
        doctor_human,
        execute_provision,
    )
    from ppbase.config import Settings

    settings = Settings(
        **{
            key: value
            for key, value in (
                ("database_url", getattr(args, "db", None)),
                ("data_dir", getattr(args, "data_dir", None)),
            )
            if value is not None
        }
    )

    async def run() -> None:
        if args.action == "doctor":
            report = await backup_doctor(settings, server_url=args.server)
            print(json.dumps(report, sort_keys=True) if args.json else doctor_human(report))
            if not report["ready"]:
                raise SystemExit(int(report["exitCode"]))
            return
        if args.action != "provision":
            raise BackupProvisionError("Use ppbase backup provision or ppbase backup doctor.")
        if args.local:
            from ppbase.backup.postgres import sqlalchemy_url_to_libpq

            runtime = sqlalchemy_url_to_libpq(settings.database_url)
            if runtime.host not in {"localhost", "127.0.0.1", "::1", ""}:
                raise BackupProvisionError(
                    "--local is restricted to loopback TCP or Unix sockets"
                )
            print(
                "WARNING: local provisioning is for development only and is not a production default.",
                file=sys.stderr,
            )
        if args.plan:
            plan = await build_provision_plan(settings)
            print(json.dumps(plan, indent=2, sort_keys=True))
            if not plan["executable"]:
                raise SystemExit(2)
            return
        if not args.execute:
            raise BackupProvisionError("Select exactly one of --plan or --execute.")
        if not args.output_env:
            raise BackupProvisionError(
                "--output-env is required as the explicit 0600 limited-credential sink"
            )
        result = await execute_provision(
            settings,
            bootstrap_database_url=_read_bootstrap_dsn(args),
            secret_sink=args.output_env,
        )
        print(json.dumps(result, indent=2, sort_keys=True))

    try:
        asyncio.run(run())
    except (BackupProvisionError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(3) from exc


# ---------------------------------------------------------------------------
# Migrate commands
# ---------------------------------------------------------------------------


def _cmd_migrate(args: argparse.Namespace) -> None:
    """Dispatch migrate sub-subcommands."""
    action = args.action

    if action is None:
        print("Usage: ppbase migrate {up|down|status|create|snapshot}")
        print("Run 'ppbase migrate -h' for more information.")
        sys.exit(1)

    if action == "create":
        _cmd_migrate_create(args)
        return

    dispatch = {
        "up": _cmd_migrate_up_async,
        "down": _cmd_migrate_down_async,
        "status": _cmd_migrate_status_async,
        "snapshot": _cmd_migrate_snapshot_async,
    }
    handler = dispatch.get(action)
    if handler is None:
        print(f"Unknown migrate action: {action}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(handler(args))


def _cmd_migrate_create(args: argparse.Namespace) -> None:
    """Create a blank migration skeleton (no DB connection needed)."""
    from ppbase.config import Settings
    from ppbase.services.migration_generator import _safe_filename, _write_migration_file

    migrations_dir = args.dir or Settings().migrations_dir
    name = _safe_filename(args.name.strip())
    if not name:
        raise ValueError("Migration name must contain at least one letter or digit.")

    ts = int(time.time())
    filename = f"{ts}_{name}.py"
    filepath = _write_migration_file(
        migrations_dir,
        filename,
        '"""Auto-generated migration."""\n'
        "\n"
        "\n"
        "async def up(app):\n"
        '    """Apply migration."""\n'
        "    pass\n"
        "\n"
        "\n"
        "async def down(app):\n"
        '    """Revert migration."""\n'
        "    pass\n",
    )
    print(f"Created migration: {filepath}")


async def _cmd_migrate_up_async(args: argparse.Namespace) -> None:
    """Apply all pending migrations."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.services.database_preparation import prepare_database

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir or settings.migrations_dir

    engine = await init_engine(db_url)
    try:
        applied = await prepare_database(
            engine,
            migrations_dir,
            apply_migrations=True,
            lock_timeout_seconds=settings.migration_lock_timeout,
        )
        if not applied:
            print("No pending migrations.")
        else:
            for name in applied:
                print(f"  Applied: {name}")
            print(f"\n{len(applied)} migration(s) applied.")
    finally:
        await close_engine()


async def _cmd_migrate_down_async(args: argparse.Namespace) -> None:
    """Revert the last N migrations."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.services.database_preparation import prepare_database_and_revert

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir or settings.migrations_dir
    count = args.count

    engine = await init_engine(db_url)
    try:
        reverted = await prepare_database_and_revert(
            engine,
            migrations_dir,
            count=count,
            lock_timeout_seconds=settings.migration_lock_timeout,
        )
        if not reverted:
            print("No applied migrations to revert.")
            return
        for migration_file in reverted:
            print(f"  Reverted: {migration_file}")
        print(f"\n{len(reverted)} migration(s) reverted.")
    finally:
        await close_engine()


async def _cmd_migrate_status_async(args: argparse.Namespace) -> None:
    """Show migration status without bootstrapping or mutating the database."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.services.migration_runner import get_migration_status
    from sqlalchemy import text

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir or settings.migrations_dir

    engine = await init_engine(db_url)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                # Keep all status queries on one coherent, read-only snapshot.
                await connection.execute(
                    text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, "
                        "READ ONLY"
                    )
                )
                status = await get_migration_status(connection, migrations_dir)

        total = status.get("total", 0)
        applied_count = len(status.get("applied", []))
        pending_count = len(status.get("pending", []))

        print(f"Migration status ({migrations_dir}):")
        print(f"  Total:   {total}")
        print(f"  Applied: {applied_count}")
        print(f"  Pending: {pending_count}")
        print(f"  Orphaned: {status.get('orphaned_count', 0)}")

        if not status.get("initialized", True):
            print(
                "\nDatabase migration history is not initialized; "
                "status did not modify the database."
            )

        if status.get("applied"):
            print("\nApplied migrations:")
            for m in status["applied"]:
                print(f"  [x] {m}")

        if status.get("pending"):
            print("\nPending migrations:")
            for m in status["pending"]:
                print(f"  [ ] {m}")

        if status.get("orphaned"):
            print("\nApplied history rows without local files:")
            for m in status["orphaned"]:
                print(f"  [!] {m}")

        if total == 0:
            print(f"\nNo migration files found in {migrations_dir}")
    finally:
        await close_engine()


async def _cmd_migrate_snapshot_async(args: argparse.Namespace) -> None:
    """Generate migrations from the current database state."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.services.database_preparation import prepare_database
    from ppbase.services.migration_snapshot import create_migration_snapshot

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir or settings.migrations_dir

    engine = await init_engine(db_url)
    try:
        await prepare_database(
            engine,
            migrations_dir,
            apply_migrations=False,
            lock_timeout_seconds=settings.migration_lock_timeout,
        )
        filepath = await create_migration_snapshot(
            engine,
            migrations_dir,
            lock_timeout_seconds=settings.migration_lock_timeout,
        )
        print(f"  Generated: {filepath}")
        print("\n1 snapshot migration generated and recorded as applied.")
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command."""
    parser = argparse.ArgumentParser(
        prog="ppbase",
        description="PPBase - Python PocketBase reimplementation",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start the HTTP server")
    serve_parser.add_argument("--host", type=str, default=None)
    serve_parser.add_argument("--port", type=int, default=None)
    serve_parser.add_argument("--db", type=str, default=None, help="Database URL")
    serve_parser.add_argument(
        "--dir",
        dest="data_dir",
        type=str,
        default=None,
        help="Data directory (PocketBase compatible option name).",
    )
    serve_parser.add_argument(
        "--publicDir",
        "--public-dir",
        dest="public_dir",
        type=str,
        default=None,
        help="Public static directory served at /.",
    )
    serve_parser.add_argument(
        "--migrationsDir",
        "--migrations-dir",
        dest="migrations_dir",
        type=str,
        default=None,
        help="Consumer application migration directory.",
    )
    serve_parser.add_argument("-d", "--daemon", action="store_true", help="Run in background")
    serve_parser.add_argument(
        "--automigrate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Legacy switch for both startup application and file generation "
            "(default: from settings)"
        ),
    )
    serve_parser.add_argument(
        "--apply-migrations-on-start",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable applying pending migrations before serving traffic.",
    )
    serve_parser.add_argument(
        "--generate-migrations",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable migration generation after collection changes.",
    )
    serve_parser.add_argument(
        "--hooksDir",
        "--hooks-dir",
        dest="hooks_dir",
        type=str,
        default=None,
        help="Directory for file-based hooks (default: ./pb_hooks).",
    )
    serve_parser.add_argument(
        "--hooks",
        action="append",
        default=[],
        metavar="MODULE:FUNCTION",
        help="Load hook registration target (repeatable).",
    )

    # stop
    subparsers.add_parser("stop", help="Stop a running PPBase daemon")

    # restart
    restart_parser = subparsers.add_parser("restart", help="Restart PPBase daemon")
    restart_parser.add_argument("--host", type=str, default=None)
    restart_parser.add_argument("--port", type=int, default=None)
    restart_parser.add_argument("--db", type=str, default=None, help="Database URL")

    # status
    subparsers.add_parser("status", help="Check if PPBase is running")

    # db
    db_parser = subparsers.add_parser("db", help="Manage PostgreSQL Docker container")
    db_parser.add_argument("action", choices=["start", "stop", "restart", "status"])

    # create-admin
    admin_parser = subparsers.add_parser("create-admin", help="Create an admin account")
    admin_parser.add_argument("--email", type=str, default=None)
    admin_parser.add_argument("--password", type=str, default=None)
    admin_parser.add_argument("--db", type=str, default=None, help="Database URL")

    # autonomous PostgreSQL onboarding
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize a new PPBase project",
    )
    init_subs = init_parser.add_subparsers(dest="resource")
    init_postgres = init_subs.add_parser(
        "postgres",
        help="Create the application database and limited backup roles",
    )
    init_mode = init_postgres.add_mutually_exclusive_group(required=True)
    init_mode.add_argument("--plan", action="store_true", help="Read-only plan")
    init_mode.add_argument("--execute", action="store_true", help="Execute the plan")
    init_postgres.add_argument(
        "--name",
        required=True,
        help="Conservative PostgreSQL project/database name",
    )
    init_postgres.add_argument(
        "--output-env",
        default=None,
        help="Exclusive mode-0600 env file for all limited credentials",
    )
    init_postgres.add_argument(
        "--bootstrap-dsn-file",
        default=None,
        help="Mode-0600 file containing the ephemeral privileged PostgreSQL DSN",
    )
    init_paths = init_postgres.add_argument_group("advanced path overrides")
    init_paths.add_argument("--data-dir", default=None, help="Override ./pb_data")
    init_paths.add_argument(
        "--backup-root", default=None, help="Override ./pb_backups"
    )
    init_paths.add_argument(
        "--backup-control-dir",
        default=None,
        help="Override ./pb_backup_control",
    )
    init_paths.add_argument(
        "--backup-staging-root",
        default=None,
        help="Override ./pb_restore_staging",
    )
    init_paths.add_argument(
        "--backup-target-root",
        default=None,
        help="Override the sibling durable target root",
    )

    # native backup provisioning / doctor
    backup_parser = subparsers.add_parser(
        "backup",
        help="Provision and diagnose native backup/restore prerequisites",
    )
    backup_subs = backup_parser.add_subparsers(dest="action")
    backup_provision = backup_subs.add_parser(
        "provision",
        help="Plan or execute strict PostgreSQL backup role provisioning",
    )
    provision_mode = backup_provision.add_mutually_exclusive_group(required=True)
    provision_mode.add_argument("--plan", action="store_true", help="Read-only plan")
    provision_mode.add_argument("--execute", action="store_true", help="Execute the plan")
    backup_provision.add_argument(
        "--output-env",
        default=None,
        help="Exclusive mode-0600 env file for the limited runtime credentials",
    )
    backup_provision.add_argument(
        "--bootstrap-dsn-file",
        default=None,
        help="Mode-0600 file containing the ephemeral privileged PostgreSQL DSN",
    )
    backup_provision.add_argument(
        "--local",
        action="store_true",
        help="Explicit development-only loopback/socket mode",
    )
    backup_provision.add_argument("--db", type=str, default=None, help="Database URL")
    backup_provision.add_argument(
        "--dir",
        dest="data_dir",
        type=str,
        default=None,
        help="Data directory (PocketBase compatible option name).",
    )
    backup_doctor_parser = backup_subs.add_parser(
        "doctor",
        help="Check backup readiness without cluster-admin credentials",
    )
    backup_doctor_parser.add_argument("--json", action="store_true")
    backup_doctor_parser.add_argument(
        "--server",
        default=None,
        metavar="URL",
        help="Probe the running PPBase process for activation capability",
    )
    backup_doctor_parser.add_argument("--db", type=str, default=None, help="Database URL")
    backup_doctor_parser.add_argument(
        "--dir",
        dest="data_dir",
        type=str,
        default=None,
        help="Data directory (PocketBase compatible option name).",
    )

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="Manage database migrations")
    migrate_subs = migrate_parser.add_subparsers(dest="action")

    # migrate up
    migrate_up = migrate_subs.add_parser("up", help="Apply all pending migrations")
    migrate_up.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_up.add_argument(
        "--dir", type=str, default=None, help="Migrations directory (default: settings)",
    )

    # migrate down
    migrate_down = migrate_subs.add_parser("down", help="Revert the last N migrations")
    migrate_down.add_argument(
        "count", nargs="?", type=int, default=1,
        help="Number of migrations to revert (default: 1)",
    )
    migrate_down.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_down.add_argument(
        "--dir", type=str, default=None, help="Migrations directory (default: settings)",
    )

    # migrate status
    migrate_st = migrate_subs.add_parser("status", help="Show migration status")
    migrate_st.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_st.add_argument(
        "--dir", type=str, default=None, help="Migrations directory (default: settings)",
    )

    # migrate create
    migrate_cr = migrate_subs.add_parser("create", help="Create a blank migration file")
    migrate_cr.add_argument("name", type=str, help="Migration name (e.g. add_users_table)")
    migrate_cr.add_argument(
        "--dir", type=str, default=None, help="Migrations directory (default: settings)",
    )

    # migrate snapshot
    migrate_snap = migrate_subs.add_parser(
        "snapshot", help="Generate one migration from current DB state",
    )
    migrate_snap.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_snap.add_argument(
        "--dir", type=str, default=None, help="Migrations directory (default: settings)",
    )

    args = parser.parse_args()

    commands = {
        "serve": _cmd_serve,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "status": _cmd_status,
        "db": _cmd_db,
        "create-admin": _cmd_create_admin,
        "init": _cmd_init,
        "backup": _cmd_backup,
        "migrate": _cmd_migrate,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        if len(sys.argv) == 1:
            from ppbase import PPBase
            pb = PPBase()
            pb.start()
        else:
            parser.print_help()
            sys.exit(1)


if __name__ == "__main__":
    main()
