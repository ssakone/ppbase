"""Entry point for ``python -m ppbase``.

Supports the following commands:

    ppbase serve          -- Start the HTTP server (background with --daemon)
    ppbase stop           -- Stop a running daemon
    ppbase restart        -- Restart the daemon
    ppbase status         -- Check if PPBase is running
    ppbase db {start|stop|restart|status}  -- Manage PostgreSQL Docker container
    ppbase create-admin   -- Interactively create an admin account
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


def _start_daemon(host: str, port: int, db: str | None = None) -> None:
    """Start PPBase as a background daemon."""
    if _find_pid() is not None:
        print(f"PPBase is already running (PID {_find_pid()}).")
        return

    cmd = [sys.executable, "-m", "ppbase", "serve", "--host", host, "--port", str(port)]
    if db:
        cmd += ["--db", db]

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
    from ppbase import PPBase

    overrides: dict = {}
    if args.db:
        overrides["database_url"] = args.db

    pb = PPBase(**overrides)
    host = args.host or pb.settings.host
    port = args.port or pb.settings.port

    if getattr(args, "daemon", False):
        _start_daemon(host, port, args.db)
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
                "-p", f"{_PG_PORT}:5432",
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

    args = parser.parse_args()

    commands = {
        "serve": _cmd_serve,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "status": _cmd_status,
        "db": _cmd_db,
        "create-admin": _cmd_create_admin,
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
