"""Ensure database exists before startup; prompt to create if missing."""

from __future__ import annotations

import asyncio
import sys

import asyncpg
from sqlalchemy.engine import make_url


def ensure_database_exists(database_url: str) -> None:
    """Check if the database exists; if not, prompt to create it and create on confirm.

    When running interactively (stdin is a TTY), prompts the user.
    When non-interactive (e.g. daemon), exits with a clear error.
    """

    url = make_url(database_url)
    db_name = url.database or "postgres"
    host = url.host or "localhost"
    port = url.port or 5432
    user = url.username
    password = url.password or ""

    async def _check_and_create() -> None:
        try:
            conn = await asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=db_name,
            )
            await conn.close()
            return
        except asyncpg.InvalidCatalogNameError:
            pass
        except Exception:
            raise

        # Database does not exist
        if sys.stdin.isatty():
            try:
                reply = input(
                    f'Database "{db_name}" does not exist. Create it? [y/N] '
                ).strip().lower()
            except EOFError:
                reply = "n"
            if reply not in ("y", "yes"):
                print("Aborted. Start with an existing database or confirm creation.")
                sys.exit(1)
        else:
            print(
                f'Database "{db_name}" does not exist. Create it first or run interactively to prompt.'
            )
            sys.exit(1)

        conn = await asyncpg.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            database="postgres",
        )
        try:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f'Created database "{db_name}".')
        finally:
            await conn.close()

    asyncio.run(_check_and_create())
