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
import os
import signal
import shutil
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
    hooks: list[str] | None = None,
    automigrate: bool | None = None,
) -> None:
    """Start PPBase as a background daemon."""
    if _find_pid() is not None:
        print(f"PPBase is already running (PID {_find_pid()}).")
        return

    cmd = [sys.executable, "-m", "ppbase", "serve", "--host", host, "--port", str(port)]
    if db:
        cmd += ["--db", db]
    for target in hooks or []:
        cmd += ["--hooks", target]
    if automigrate is True:
        cmd += ["--automigrate"]
    elif automigrate is False:
        cmd += ["--no-automigrate"]

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

    overrides: dict = {}
    if args.db:
        overrides["database_url"] = args.db
    if args.automigrate is not None:
        overrides["auto_migrate"] = args.automigrate

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
            hooks=args.hooks,
            automigrate=args.automigrate,
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
    migrations_dir = args.dir
    name = args.name

    ts = int(time.time())
    filename = f"{ts}_{name}.py"
    dirpath = Path(migrations_dir)
    dirpath.mkdir(parents=True, exist_ok=True)
    filepath = dirpath / filename

    filepath.write_text(
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
        "    pass\n"
    )
    print(f"Created migration: {filepath}")


async def _cmd_migrate_up_async(args: argparse.Namespace) -> None:
    """Apply all pending migrations."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.db.system_tables import create_system_tables
    from ppbase.services.migration_runner import apply_all_pending
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir

    engine = await init_engine(db_url)
    try:
        await create_system_tables(engine)
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with session_factory() as session:
            async with session.begin():
                applied = await apply_all_pending(session, engine, migrations_dir)
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
    from ppbase.db.system_tables import create_system_tables
    from ppbase.services.migration_runner import (
        get_applied_migrations,
        revert_migration,
    )
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir
    count = args.count

    engine = await init_engine(db_url)
    try:
        await create_system_tables(engine)
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with session_factory() as session:
            async with session.begin():
                applied = await get_applied_migrations(session)
                if not applied:
                    print("No applied migrations to revert.")
                    return

                to_revert = list(reversed(applied[-count:]))
                for record in to_revert:
                    await revert_migration(
                        session, engine, record.file, migrations_dir,
                    )
                    print(f"  Reverted: {record.file}")

        print(f"\n{len(to_revert)} migration(s) reverted.")
    finally:
        await close_engine()


async def _cmd_migrate_status_async(args: argparse.Namespace) -> None:
    """Show migration status."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.db.system_tables import create_system_tables
    from ppbase.services.migration_runner import get_migration_status
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir

    engine = await init_engine(db_url)
    try:
        await create_system_tables(engine)
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with session_factory() as session:
            status = await get_migration_status(session, migrations_dir)

        total = status.get("total", 0)
        applied_count = len(status.get("applied", []))
        pending_count = len(status.get("pending", []))

        print(f"Migration status ({migrations_dir}):")
        print(f"  Total:   {total}")
        print(f"  Applied: {applied_count}")
        print(f"  Pending: {pending_count}")

        if status.get("applied"):
            print("\nApplied migrations:")
            for m in status["applied"]:
                print(f"  [x] {m}")

        if status.get("pending"):
            print("\nPending migrations:")
            for m in status["pending"]:
                print(f"  [ ] {m}")

        if total == 0:
            print(f"\nNo migration files found in {migrations_dir}")
    finally:
        await close_engine()


async def _cmd_migrate_snapshot_async(args: argparse.Namespace) -> None:
    """Generate migrations from the current database state."""
    from ppbase.config import Settings
    from ppbase.db.engine import init_engine, close_engine
    from ppbase.db.system_tables import (
        CollectionRecord,
        MigrationRecord,
        create_system_tables,
    )
    from ppbase.services.migration_generator import generate_create_migration
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    settings = Settings()
    db_url = args.db or settings.database_url
    migrations_dir = args.dir

    engine = await init_engine(db_url)
    try:
        await create_system_tables(engine)
        session_factory = async_sessionmaker(
            bind=engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with session_factory() as session:
            result = await session.execute(select(CollectionRecord))
            collections = result.scalars().all()

            if not collections:
                print("No collections found in the database.")
                return

            generated = []
            for coll in collections:
                filepath = generate_create_migration(coll, migrations_dir)
                filename = Path(filepath).name
                # Record the migration as applied so it doesn't re-run
                record = MigrationRecord(file=filename)
                session.add(record)
                generated.append(filepath)
                print(f"  Generated: {filepath}")
                # Small delay so timestamps differ between files
                time.sleep(1)

            await session.commit()

        print(f"\n{len(generated)} migration(s) generated and recorded as applied.")
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
    serve_parser.add_argument("-d", "--daemon", action="store_true", help="Run in background")
    serve_parser.add_argument(
        "--automigrate",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable/disable auto-migration (default: from settings)",
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

    # migrate
    migrate_parser = subparsers.add_parser("migrate", help="Manage database migrations")
    migrate_subs = migrate_parser.add_subparsers(dest="action")

    # migrate up
    migrate_up = migrate_subs.add_parser("up", help="Apply all pending migrations")
    migrate_up.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_up.add_argument(
        "--dir", type=str, default="./pb_migrations", help="Migrations directory",
    )

    # migrate down
    migrate_down = migrate_subs.add_parser("down", help="Revert the last N migrations")
    migrate_down.add_argument(
        "count", nargs="?", type=int, default=1,
        help="Number of migrations to revert (default: 1)",
    )
    migrate_down.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_down.add_argument(
        "--dir", type=str, default="./pb_migrations", help="Migrations directory",
    )

    # migrate status
    migrate_st = migrate_subs.add_parser("status", help="Show migration status")
    migrate_st.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_st.add_argument(
        "--dir", type=str, default="./pb_migrations", help="Migrations directory",
    )

    # migrate create
    migrate_cr = migrate_subs.add_parser("create", help="Create a blank migration file")
    migrate_cr.add_argument("name", type=str, help="Migration name (e.g. add_users_table)")
    migrate_cr.add_argument(
        "--dir", type=str, default="./pb_migrations", help="Migrations directory",
    )

    # migrate snapshot
    migrate_snap = migrate_subs.add_parser(
        "snapshot", help="Generate migrations from current DB state",
    )
    migrate_snap.add_argument("--db", type=str, default=None, help="Database URL")
    migrate_snap.add_argument(
        "--dir", type=str, default="./pb_migrations", help="Migrations directory",
    )

    args = parser.parse_args()

    commands = {
        "serve": _cmd_serve,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "status": _cmd_status,
        "db": _cmd_db,
        "create-admin": _cmd_create_admin,
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
