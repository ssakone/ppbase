"""Startup error classification tests."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ppbase import app as app_module
from ppbase.backup.activation import BackupActivationStore, apply_activation_runtime_overlay
from ppbase.backup.control import ControlPlaneRoot
from ppbase.config import Settings
from ppbase.db import engine as engine_module
from ppbase.services import database_preparation
from ppbase.services import process_control


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
        return self.connection if self.connection is not None else object()

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


class _IdentityResult:
    def __init__(self, identity: dict[str, object]) -> None:
        self.identity = identity

    def mappings(self) -> "_IdentityResult":
        return self

    def one(self) -> dict[str, object]:
        return self.identity


class _IdentityConnection:
    def __init__(self, identity: dict[str, object]) -> None:
        self.identity = identity

    async def execute(self, _statement: object) -> _IdentityResult:
        return _IdentityResult(self.identity)


class _SessionContext:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        return None


class _SessionFactory:
    def __call__(self) -> _SessionContext:
        return _SessionContext()


def _fake_app(tmp_path: Path) -> SimpleNamespace:
    settings = Settings(
        database_url="postgresql+asyncpg://user:password@localhost/test",
        jwt_secret="startup-test-secret",
        migrations_dir=str(tmp_path),
        apply_migrations_on_start=True,
    )
    return SimpleNamespace(
        state=SimpleNamespace(settings=settings, extension_registry=None)
    )


def _database_identity(
    database: str,
    *,
    database_oid: int = 16384,
    database_marker: str = "ppbase-staging:test",
) -> dict[str, object]:
    return {
        "role": "runtime",
        "database": database,
        "serverAddress": "127.0.0.1",
        "serverPort": 5432,
        "postmasterStartedAt": "1721260800.000000",
        "serverVersionNum": "160000",
        "databaseOid": database_oid,
        "databaseMarker": database_marker,
    }


def _database_identity_row(identity: dict[str, object]) -> dict[str, object]:
    return {
        "role": identity["role"],
        "database": identity["database"],
        "server_address": identity["serverAddress"],
        "server_port": identity["serverPort"],
        "postmaster_started_at": identity["postmasterStartedAt"],
        "server_version_num": identity["serverVersionNum"],
        "database_oid": identity["databaseOid"],
        "database_marker": identity.get("databaseMarker"),
    }


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
async def test_activation_startup_failure_persists_rollback_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    target_data = tmp_path / "target-data"
    target_data.mkdir(mode=0o700)
    (target_data / "storage").mkdir(mode=0o700)
    secret = target_data / ".jwt_secret"
    secret.write_text("target-secret\n", encoding="utf-8")
    secret.chmod(0o600)
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    prepared = store.prepare(
        activation_id="a" * 32,
        plan_id="b" * 32,
        backup_id="backup-startup-failure",
        plan_hash="c" * 64,
        manifest_sha256="d" * 64,
        signer_fingerprint_sha256="e" * 64,
        jwt_secret_mode="clone",
        previous_database_url="postgresql+asyncpg://runtime@localhost/old",
        previous_data_dir=str(tmp_path / "old-data"),
        previous_restart_command=["python", "-m", "ppbase", "serve", "--db", "old"],
        target_database_url="postgresql+asyncpg://runtime@localhost/target",
        target_data_dir=str(target_data),
        target_restart_command=["python", "-m", "ppbase", "serve", "--db", "target"],
        expected_jwt_sha256=hashlib.sha256(b"target-secret").hexdigest(),
        actor_id="admin-1",
    )
    store.close()
    root.close()

    settings = Settings(
        database_url="postgresql+asyncpg://runtime@localhost/old",
        data_dir=str(tmp_path / "old-data"),
        backup_control_dir=str(control),
        jwt_secret="explicit-test-secret",
        migrations_dir=str(tmp_path),
    )
    apply_activation_runtime_overlay(settings)
    app = SimpleNamespace(
        state=SimpleNamespace(settings=settings, extension_registry=None)
    )
    connection_error = PermissionError("target database unavailable")
    fake_engine = _FakeEngine(connection_error)
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    restarted: list[tuple[list[str], dict[str, str]]] = []

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
        app_module,
        "_handle_db_connection_error",
        lambda *_args: pytest.fail("activation failures must enter rollback"),
    )
    monkeypatch.setattr(
        process_control,
        "restart_process_now",
        lambda _reason, *, command, env_overrides, **_kwargs: restarted.append(
            (list(command), dict(env_overrides))
        ),
    )
    monkeypatch.setattr(
        process_control,
        "schedule_backup_activation_watchdog",
        lambda **_kwargs: None,
    )

    with pytest.raises(PermissionError) as exc_info:
        async with app_module._lifespan(app):
            pytest.fail("failed activation target must never open HTTP traffic")

    assert exc_info.value is connection_error
    assert restarted == [
        (
            [
                "python",
                "-m",
                "ppbase",
                "serve",
                "--dir",
                str(tmp_path / "old-data"),
            ],
            {
                "PPBASE_DATABASE_URL": (
                    "postgresql+asyncpg://runtime@localhost/old"
                )
            },
        )
    ]
    assert prepare_database.await_count == 0
    close_engine.assert_awaited_once()
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        state = store.inspect(prepared["activationId"])
        assert state["status"] == "rollback_pending"
        assert state["selectedTarget"] == "previous"
    finally:
        store.close()
        root.close()


@pytest.mark.asyncio
async def test_activation_identity_is_checked_before_hooks_or_database_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    target_data = tmp_path / "target-data"
    target_data.mkdir(mode=0o700)
    (target_data / "storage").mkdir(mode=0o700)
    target_secret = "target-secret"
    (target_data / ".jwt_secret").write_text(
        target_secret + "\n",
        encoding="utf-8",
    )
    (target_data / ".jwt_secret").chmod(0o600)
    target_info = target_data.stat()
    expected_identity = _database_identity("target")
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    activation_id = "2" * 32
    store.prepare(
        activation_id=activation_id,
        plan_id="3" * 32,
        backup_id="backup-identity-before-mutation",
        plan_hash="4" * 64,
        manifest_sha256="5" * 64,
        signer_fingerprint_sha256="6" * 64,
        jwt_secret_mode="clone",
        previous_database_url="postgresql+asyncpg://runtime@localhost/old",
        previous_data_dir=str(tmp_path / "old-data"),
        previous_restart_command=["serve"],
        target_database_url="postgresql+asyncpg://runtime@localhost/target",
        target_data_dir=str(target_data),
        target_restart_command=["serve"],
        expected_jwt_sha256=hashlib.sha256(
            target_secret.encode("utf-8")
        ).hexdigest(),
        actor_id=None,
        target_data_identity=(target_info.st_dev, target_info.st_ino),
        expected_database_identity=expected_identity,
    )
    store.close()
    root.close()

    settings = Settings(
        database_url="postgresql+asyncpg://runtime@localhost/old",
        data_dir=str(tmp_path / "old-data"),
        backup_control_dir=str(control),
        jwt_secret="deployment-secret",
        migrations_dir=str(tmp_path),
    )
    apply_activation_runtime_overlay(settings)
    bootstrap_trigger = AsyncMock()
    extensions = SimpleNamespace(
        hooks=SimpleNamespace(
            get=lambda _name: SimpleNamespace(trigger=bootstrap_trigger)
        )
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            extension_registry=extensions,
        )
    )
    changed_identity = {
        **expected_identity,
        "databaseOid": int(expected_identity["databaseOid"]) + 1,
    }
    fake_engine = _FakeEngine(
        connection=_IdentityConnection(_database_identity_row(changed_identity))
    )
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    get_pending_migrations = AsyncMock(return_value=[])
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
        migration_runner,
        "get_pending_migrations",
        get_pending_migrations,
    )
    monkeypatch.setattr(
        process_control,
        "schedule_backup_activation_watchdog",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        process_control,
        "restart_process_now",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        async with app_module._lifespan(app):
            pytest.fail("an unpinned activation target must never start")

    bootstrap_trigger.assert_not_awaited()
    prepare_database.assert_not_awaited()
    get_pending_migrations.assert_not_awaited()
    close_engine.assert_awaited_once()
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        state = store.inspect(activation_id)
        assert state["status"] == "rollback_pending"
        assert state["selectedTarget"] == "previous"
    finally:
        store.close()
        root.close()


@pytest.mark.asyncio
async def test_rollback_identity_is_checked_before_hooks_or_database_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    previous_data = tmp_path / "previous-data"
    previous_data.mkdir(mode=0o700)
    (previous_data / "storage").mkdir(mode=0o700)
    previous_info = previous_data.stat()
    deployment_secret = "deployment-secret"
    expected_identity = _database_identity("previous")
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    activation_id = "7" * 32
    store.prepare(
        activation_id=activation_id,
        plan_id="8" * 32,
        backup_id="backup-rollback-identity-before-mutation",
        plan_hash="9" * 64,
        manifest_sha256="a" * 64,
        signer_fingerprint_sha256="b" * 64,
        jwt_secret_mode="clone",
        previous_database_url="postgresql+asyncpg://runtime@localhost/previous",
        previous_data_dir=str(previous_data),
        previous_restart_command=["serve"],
        target_database_url="postgresql+asyncpg://runtime@localhost/target",
        target_data_dir=str(tmp_path / "target-data"),
        target_restart_command=["serve"],
        expected_jwt_sha256="c" * 64,
        actor_id=None,
        previous_data_identity=(previous_info.st_dev, previous_info.st_ino),
        expected_previous_jwt_sha256=hashlib.sha256(
            deployment_secret.encode("utf-8")
        ).hexdigest(),
        expected_previous_database_identity=expected_identity,
    )
    store.mark_starting(activation_id)
    store.mark_rollback_pending(activation_id, error_code="target_failed")
    store.close()
    root.close()

    settings = Settings(
        database_url="postgresql+asyncpg://runtime@localhost/target",
        data_dir=str(tmp_path / "target-data"),
        backup_control_dir=str(control),
        jwt_secret=deployment_secret,
        migrations_dir=str(tmp_path),
    )
    apply_activation_runtime_overlay(settings)
    bootstrap_trigger = AsyncMock()
    extensions = SimpleNamespace(
        hooks=SimpleNamespace(
            get=lambda _name: SimpleNamespace(trigger=bootstrap_trigger)
        )
    )
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            extension_registry=extensions,
        )
    )
    changed_identity = {
        **expected_identity,
        "databaseMarker": "foreign-database",
    }
    fake_engine = _FakeEngine(
        connection=_IdentityConnection(_database_identity_row(changed_identity))
    )
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    get_pending_migrations = AsyncMock(return_value=[])
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
        migration_runner,
        "get_pending_migrations",
        get_pending_migrations,
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        async with app_module._lifespan(app):
            pytest.fail("an unpinned rollback target must never start")

    bootstrap_trigger.assert_not_awaited()
    prepare_database.assert_not_awaited()
    get_pending_migrations.assert_not_awaited()
    close_engine.assert_awaited_once()
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        state = store.inspect(activation_id)
        assert state["status"] == "rollback_pending"
        assert state["selectedTarget"] == "previous"
    finally:
        store.close()
        root.close()


@pytest.mark.asyncio
async def test_rollback_with_pending_migrations_is_not_prepared_or_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner
    from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    previous_data = tmp_path / "previous-data"
    previous_data.mkdir(mode=0o700)
    (previous_data / "storage").mkdir(mode=0o700)
    previous_info = previous_data.stat()
    deployment_secret = "deployment-secret"
    expected_identity = _database_identity("previous")
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    activation_id = "d" * 32
    store.prepare(
        activation_id=activation_id,
        plan_id="e" * 32,
        backup_id="backup-rollback-pending-migration",
        plan_hash="f" * 64,
        manifest_sha256="0" * 64,
        signer_fingerprint_sha256="1" * 64,
        jwt_secret_mode="clone",
        previous_database_url="postgresql+asyncpg://runtime@localhost/previous",
        previous_data_dir=str(previous_data),
        previous_restart_command=["serve"],
        target_database_url="postgresql+asyncpg://runtime@localhost/target",
        target_data_dir=str(tmp_path / "target-data"),
        target_restart_command=["serve"],
        expected_jwt_sha256="2" * 64,
        actor_id=None,
        previous_data_identity=(previous_info.st_dev, previous_info.st_ino),
        expected_previous_jwt_sha256=hashlib.sha256(
            deployment_secret.encode("utf-8")
        ).hexdigest(),
        expected_previous_database_identity=expected_identity,
    )
    store.mark_starting(activation_id)
    store.mark_rollback_pending(activation_id, error_code="target_failed")
    store.close()
    root.close()

    settings = Settings(
        database_url="postgresql+asyncpg://runtime@localhost/target",
        data_dir=str(tmp_path / "target-data"),
        backup_control_dir=str(control),
        jwt_secret=deployment_secret,
        migrations_dir=str(tmp_path),
    )
    apply_activation_runtime_overlay(settings)
    bootstrap_trigger = AsyncMock()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            extension_registry=SimpleNamespace(
                hooks=SimpleNamespace(
                    get=lambda _name: SimpleNamespace(trigger=bootstrap_trigger)
                )
            ),
        )
    )
    fake_engine = _FakeEngine(
        connection=_IdentityConnection(_database_identity_row(expected_identity))
    )
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    get_pending_migrations = AsyncMock(return_value=["20260718_changed.py"])
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
        migration_runner,
        "get_pending_migrations",
        get_pending_migrations,
    )
    monkeypatch.setattr(
        sqlalchemy_asyncio,
        "async_sessionmaker",
        lambda **_kwargs: _SessionFactory(),
    )

    with pytest.raises(RuntimeError, match="migration files changed"):
        async with app_module._lifespan(app):
            pytest.fail("rollback with pending migrations must not become durable")

    bootstrap_trigger.assert_not_awaited()
    prepare_database.assert_not_awaited()
    get_pending_migrations.assert_awaited_once()
    close_engine.assert_awaited_once()
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        state = store.inspect(activation_id)
        assert state["status"] == "rollback_pending"
        assert state["selectedTarget"] == "previous"
    finally:
        store.close()
        root.close()


@pytest.mark.asyncio
async def test_activation_with_pending_migrations_is_not_prepared_or_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import migration_runner
    from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

    control = tmp_path / "control"
    control.mkdir(mode=0o700)
    target_data = tmp_path / "target-data"
    target_data.mkdir(mode=0o700)
    (target_data / "storage").mkdir(mode=0o700)
    target_secret = "target-secret"
    (target_data / ".jwt_secret").write_text(
        target_secret + "\n",
        encoding="utf-8",
    )
    (target_data / ".jwt_secret").chmod(0o600)
    target_info = target_data.stat()
    expected_identity = _database_identity("target")
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    activation_id = "3" * 32
    store.prepare(
        activation_id=activation_id,
        plan_id="4" * 32,
        backup_id="backup-target-pending-migration",
        plan_hash="5" * 64,
        manifest_sha256="6" * 64,
        signer_fingerprint_sha256="7" * 64,
        jwt_secret_mode="clone",
        previous_database_url="postgresql+asyncpg://runtime@localhost/previous",
        previous_data_dir=str(tmp_path / "previous-data"),
        previous_restart_command=["serve"],
        target_database_url="postgresql+asyncpg://runtime@localhost/target",
        target_data_dir=str(target_data),
        target_restart_command=["serve"],
        expected_jwt_sha256=hashlib.sha256(
            target_secret.encode("utf-8")
        ).hexdigest(),
        actor_id=None,
        target_data_identity=(target_info.st_dev, target_info.st_ino),
        expected_database_identity=expected_identity,
    )
    store.close()
    root.close()

    settings = Settings(
        database_url="postgresql+asyncpg://runtime@localhost/previous",
        data_dir=str(tmp_path / "previous-data"),
        backup_control_dir=str(control),
        jwt_secret="deployment-secret",
        migrations_dir=str(tmp_path),
    )
    apply_activation_runtime_overlay(settings)
    bootstrap_trigger = AsyncMock()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            extension_registry=SimpleNamespace(
                hooks=SimpleNamespace(
                    get=lambda _name: SimpleNamespace(trigger=bootstrap_trigger)
                )
            ),
        )
    )
    fake_engine = _FakeEngine(
        connection=_IdentityConnection(_database_identity_row(expected_identity))
    )
    close_engine = AsyncMock()
    prepare_database = AsyncMock()
    get_pending_migrations = AsyncMock(return_value=["20260718_changed.py"])
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
        migration_runner,
        "get_pending_migrations",
        get_pending_migrations,
    )
    monkeypatch.setattr(
        sqlalchemy_asyncio,
        "async_sessionmaker",
        lambda **_kwargs: _SessionFactory(),
    )
    monkeypatch.setattr(
        process_control,
        "schedule_backup_activation_watchdog",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        process_control,
        "restart_process_now",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="migration files changed"):
        async with app_module._lifespan(app):
            pytest.fail("activation with pending migrations must not become durable")

    bootstrap_trigger.assert_not_awaited()
    prepare_database.assert_not_awaited()
    get_pending_migrations.assert_awaited_once()
    close_engine.assert_awaited_once()
    root = ControlPlaneRoot.open(control, create_missing=False)
    store = BackupActivationStore(root)
    try:
        state = store.inspect(activation_id)
        assert state["status"] == "rollback_pending"
        assert state["selectedTarget"] == "previous"
    finally:
        store.close()
        root.close()
