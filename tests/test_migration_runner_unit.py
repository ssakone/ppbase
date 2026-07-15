"""Focused migration-runner compatibility tests."""

from unittest.mock import AsyncMock, create_autospec

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ppbase.services.migration_runner import (
    MigrationApp,
    _list_migration_files,
    _merge_auth_options,
    apply_all_pending,
)


def test_sanitized_auth_options_preserve_runtime_secrets_and_credentials() -> None:
    defaults = {
        "authToken": {"secret": "generated", "duration": 100},
        "oauth2": {"enabled": False, "providers": []},
    }
    existing = {
        "authToken": {"secret": "runtime-token", "duration": 200},
        "oauth2": {
            "enabled": True,
            "providers": [
                {
                    "name": "github",
                    "clientId": "runtime-id",
                    "clientSecret": "runtime-secret",
                }
            ],
        },
    }
    sanitized_snapshot = {
        "authToken": {"duration": 300},
        "oauth2": {
            "enabled": True,
            "providers": [{"name": "github", "displayName": "GitHub"}],
        },
    }

    merged = _merge_auth_options(defaults, existing, sanitized_snapshot)

    assert merged["authToken"] == {
        "secret": "runtime-token",
        "duration": 300,
    }
    assert merged["oauth2"]["providers"] == [
        {
            "name": "github",
            "clientId": "runtime-id",
            "clientSecret": "runtime-secret",
            "displayName": "GitHub",
        }
    ]


@pytest.mark.asyncio
async def test_compatibility_wrapper_rejects_active_session_clearly(tmp_path) -> None:
    session = create_autospec(AsyncSession, instance=True)
    session.in_transaction.return_value = True
    engine = create_autospec(AsyncEngine, instance=True)

    with pytest.raises(RuntimeError, match="requires an idle session"):
        await apply_all_pending(session, engine, tmp_path)


def test_migration_files_sort_by_numeric_timestamp(tmp_path) -> None:
    for filename in (
        "9999999999_late.py",
        "10000000000_later.py",
        "9999999998_early.py",
    ):
        (tmp_path / filename).write_text("", encoding="utf-8")

    assert _list_migration_files(tmp_path) == [
        "9999999998_early.py",
        "9999999999_late.py",
        "10000000000_later.py",
    ]


async def _execute_migration_sql_surface(
    app: MigrationApp,
    surface: str,
    sql: str,
) -> None:
    if surface == "execute_sql":
        await app.execute_sql(sql)
    elif surface == "session":
        await app.session.execute(text(sql))
    elif surface == "engine":
        async with app.engine.connect() as connection:
            await connection.execute(text(sql))
    else:  # pragma: no cover - protects the test helper itself
        raise AssertionError(f"Unknown migration SQL surface: {surface}")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["execute_sql", "session", "engine"])
@pytest.mark.parametrize(
    "sql",
    [
        "COMMIT",
        "-- keep the runner transaction\nROLLBACK WORK",
        "/* outer /* nested */ comment */ BEGIN",
        "END TRANSACTION",
        "START /* transaction comment */ TRANSACTION",
        "PREPARE /* two-phase comment */ TRANSACTION 'ppbase_test'",
        "SELECT 1; COMMIT",
        "ABORT",
    ],
)
async def test_public_migration_sql_surfaces_reject_transaction_control(
    surface: str,
    sql: str,
) -> None:
    session = create_autospec(AsyncSession, instance=True)
    session.execute = AsyncMock()
    engine = create_autospec(AsyncEngine, instance=True)
    app = MigrationApp(session, engine)

    with pytest.raises(RuntimeError, match="transaction-control command"):
        await _execute_migration_sql_surface(app, surface, sql)

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'COMMIT; ROLLBACK' AS value",
        "DO $body$ BEGIN RAISE NOTICE 'COMMIT'; END $body$",
        "PREPARE migration_query AS SELECT 1",
        r"SELECT E'escaped\'; COMMIT' AS value",
        (
            "CREATE FUNCTION migration_one() RETURNS integer LANGUAGE SQL "
            "BEGIN ATOMIC SELECT 1; END"
        ),
    ],
)
async def test_transaction_words_inside_regular_sql_are_not_rejected(sql: str) -> None:
    session = create_autospec(AsyncSession, instance=True)
    session.execute = AsyncMock()
    engine = create_autospec(AsyncEngine, instance=True)
    app = MigrationApp(session, engine)

    await app.execute_sql(sql)

    session.execute.assert_awaited_once()
