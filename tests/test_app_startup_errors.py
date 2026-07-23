"""Startup error classification tests."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ppbase import app as app_module
from ppbase.backup import destructive as destructive_module
from ppbase.config import Settings
from ppbase.db import engine as engine_module
from ppbase.services import database_preparation


class _FakeConnection:
    """Minimal async connection: no interrupted destructive restore marker."""

    async def scalar(self, *args: object, **kwargs: object) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _ConnectContext:
    def __init__(
        self,
        error: BaseException | None = None,
        connection: object | None = None,
    ) -> None:
        self.error = error
        self.connection = connection

    async def __aenter__(self) -> object:
        if self.error is not None:
            raise self.error
        return self.connection if self.connection is not None else _FakeConnection()

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeEngine:
    def __init__(
        self,
        connect_error: BaseException | None = None,
        connection: object | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.connection = connection

    def connect(self) -> _ConnectContext:
        return _ConnectContext(self.connect_error, self.connection)


def _fake_app(tmp_path: Path) -> SimpleNamespace:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/test",
        jwt_secret="startup-test-secret",
        migrations_dir=str(tmp_path),
        apply_migrations_on_start=True,
        backup_control_dir=str(tmp_path / "missing-control"),
    )
    return SimpleNamespace(
        state=SimpleNamespace(settings=settings, extension_registry=None)
    )


@pytest.mark.asyncio
async def test_migration_oserror_keeps_its_type_and_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_engine = _FakeEngine()
    close_engine = AsyncMock()
    migration_error = FileNotFoundError("raised by migration up()")
    prepare_database = AsyncMock(side_effect=migration_error)

    monkeypatch.setattr(
        engine_module,
        "init_engine",
        AsyncMock(return_value=fake_engine),
    )
    monkeypatch.setattr(engine_module, "close_engine", close_engine)
    monkeypatch.setattr(
        database_preparation,
        "prepare_database",
        prepare_database,
    )

    def unexpected_database_handler(*args: object) -> None:
        raise AssertionError("migration OSError was misclassified as a DB outage")

    monkeypatch.setattr(
        app_module,
        "_handle_db_connection_error",
        unexpected_database_handler,
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        async with app_module._lifespan(_fake_app(tmp_path)):
            pytest.fail("lifespan must not start after a failed migration")

    assert exc_info.value is migration_error
    prepare_database.assert_awaited_once()
    close_engine.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_connection_oserror_still_uses_friendly_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_error = PermissionError("cannot open PostgreSQL socket")
    fake_engine = _FakeEngine(connection_error)
    close_engine = AsyncMock()
    prepare_database = AsyncMock()

    monkeypatch.setattr(
        engine_module,
        "init_engine",
        AsyncMock(return_value=fake_engine),
    )
    monkeypatch.setattr(engine_module, "close_engine", close_engine)
    monkeypatch.setattr(
        database_preparation,
        "prepare_database",
        prepare_database,
    )

    class FriendlyDatabaseExit(RuntimeError):
        pass

    def friendly_database_handler(database_url: str, exc: BaseException) -> None:
        assert database_url.endswith("@localhost/test")
        assert exc is connection_error
        raise FriendlyDatabaseExit

    monkeypatch.setattr(
        app_module,
        "_handle_db_connection_error",
        friendly_database_handler,
    )

    with pytest.raises(FriendlyDatabaseExit):
        async with app_module._lifespan(_fake_app(tmp_path)):
            pytest.fail("lifespan must not start without PostgreSQL")

    prepare_database.assert_not_awaited()
    close_engine.assert_awaited_once()


@pytest.mark.asyncio
async def test_restore_recovery_oserror_keeps_its_type_and_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_engine = _FakeEngine()
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    recovery_error = PermissionError("restore journal cannot be read")

    monkeypatch.setattr(
        engine_module,
        "init_engine",
        AsyncMock(return_value=fake_engine),
    )
    monkeypatch.setattr(engine_module, "close_engine", close_engine)
    monkeypatch.setattr(
        database_preparation,
        "prepare_database",
        prepare_database,
    )
    monkeypatch.setattr(
        destructive_module,
        "recover_interrupted_destructive_restore",
        AsyncMock(side_effect=recovery_error),
    )

    def unexpected_database_handler(*args: object) -> None:
        raise AssertionError("recovery OSError was misclassified as a DB outage")

    monkeypatch.setattr(
        app_module,
        "_handle_db_connection_error",
        unexpected_database_handler,
    )

    app = _fake_app(tmp_path)
    deferred_loader = AsyncMock()
    app.state.deferred_extension_loader = deferred_loader

    with pytest.raises(PermissionError) as exc_info:
        async with app_module._lifespan(app):
            pytest.fail("lifespan must not start after failed restore recovery")

    assert exc_info.value is recovery_error
    deferred_loader.assert_not_called()
    prepare_database.assert_not_awaited()
    close_engine.assert_awaited_once()


@pytest.mark.asyncio
async def test_deferred_hook_import_runs_only_after_restore_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_engine = _FakeEngine()
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    order: list[str] = []

    async def recover(_settings: object, _connection: object) -> None:
        order.append("recovery")
        return None

    class DeferredLoadStop(RuntimeError):
        pass

    def load_hooks() -> None:
        order.append("hook-import")
        raise DeferredLoadStop

    monkeypatch.setattr(
        engine_module,
        "init_engine",
        AsyncMock(return_value=fake_engine),
    )
    monkeypatch.setattr(engine_module, "close_engine", close_engine)
    monkeypatch.setattr(
        database_preparation,
        "prepare_database",
        prepare_database,
    )
    monkeypatch.setattr(
        destructive_module,
        "recover_interrupted_destructive_restore",
        recover,
    )
    app = _fake_app(tmp_path)
    app.state.deferred_extension_loader = load_hooks

    with pytest.raises(DeferredLoadStop):
        async with app_module._lifespan(app):
            pytest.fail("lifespan must stop on a deferred hook load error")

    assert order == ["recovery", "hook-import"]
    prepare_database.assert_not_awaited()
    close_engine.assert_awaited_once()
