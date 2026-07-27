from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ppbase.api import backups as backups_api
from ppbase.app import create_app
from ppbase.config import Settings


class _FakeLease:
    scope = "global"
    mode = "exclusive"

    def verify_attached(self) -> None:
        return None


class _FakeOperationContext:
    def __init__(self) -> None:
        self.lease = _FakeLease()
        self.entries = 0
        self.exits = 0

    def __enter__(self) -> _FakeLease:
        self.entries += 1
        return self.lease

    def __exit__(self, *_args: object) -> None:
        self.exits += 1


class _FakeCutoverGuard:
    def __init__(self) -> None:
        self.restart_reservation = object()
        self.operation_context: _FakeOperationContext | None = None

    def retain_operation_context(self, context: _FakeOperationContext) -> None:
        self.operation_context = context

    async def close(self) -> None:
        context = self.operation_context
        self.operation_context = None
        if context is not None:
            context.__exit__(None, None, None)

    def close_from_restart_thread(self) -> None:
        return None

    def verify_from_restart_thread(self) -> None:
        return None


class _PreparedRestore(dict[str, Any]):
    def __init__(self, backup_id: str) -> None:
        super().__init__(
            backupId=backup_id,
            restoreId="d" * 32,
            status="restart_scheduled",
            destructive=True,
        )
        self.cutover_guard = _FakeCutoverGuard()


class _FakeBackupService:
    def __init__(self) -> None:
        self.operation_context = _FakeOperationContext()
        self.resolved_reference: str | None = None
        self.restored_backup_id: str | None = None
        self.prepared: _PreparedRestore | None = None
        self.close_calls = 0

    def mutation_operation(self) -> _FakeOperationContext:
        return self.operation_context

    async def resolve_restore_reference(
        self,
        backup_id: str,
        *,
        operation_lease: _FakeLease,
    ) -> str:
        assert operation_lease is self.operation_context.lease
        self.resolved_reference = backup_id
        return "backup_canonical_1"

    async def restore_local_backup(
        self,
        backup_id: str,
        *,
        actor_id: str | None,
        operation_lease: _FakeLease,
    ) -> _PreparedRestore:
        assert actor_id == "admin_1"
        assert operation_lease is self.operation_context.lease
        self.restored_backup_id = backup_id
        self.prepared = _PreparedRestore(backup_id)
        return self.prepared

    def close(self) -> None:
        self.close_calls += 1


def _api_app() -> FastAPI:
    app = FastAPI()
    app.state.backup_maintenance = False
    app.include_router(backups_api.router, prefix="/api")
    app.dependency_overrides[backups_api._require_backup_admin] = lambda: {
        "id": "admin_1"
    }
    return app


@pytest.mark.asyncio
async def test_restore_maintenance_uses_pocketbase_error_shape() -> None:
    app = create_app(Settings())
    app.state.backup_maintenance = True

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/health")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {
        "status": 503,
        "message": (
            "PPBase is temporarily read-only while a destructive backup "
            "restore is running."
        ),
        "data": {},
    }


def test_backup_readiness_reports_per_operation_blockers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backups_api, "can_self_restart", lambda: False)

    payload = backups_api._backup_readiness(
        SimpleNamespace(
            storage_backend="local",
            backup_root=str(tmp_path / "backups"),
            backup_control_dir=str(tmp_path / "control"),
        )
    )

    assert payload["create"] == {"configured": True, "missing": []}
    assert payload["restore"] == {
        "configured": False,
        "missing": ["PPBASE_RESTART_CMD"],
    }
    assert payload["restart"] == {
        "configured": False,
        "missing": ["PPBASE_RESTART_CMD"],
    }


@pytest.mark.asyncio
async def test_dashboard_restore_retains_operation_lease_until_cutover_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeBackupService()
    scheduled: list[dict[str, Any]] = []

    def schedule(*_args: object, **kwargs: Any) -> bool:
        scheduled.append(kwargs)
        return True

    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    monkeypatch.setattr(backups_api, "schedule_process_restart", schedule)
    monkeypatch.setattr(backups_api, "process_cutover_is_fenced", lambda: True)
    app = _api_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/backups/sdk-visible-name.zip/restore-destructive"
        )

    assert response.status_code == 202
    assert response.json()["backupId"] == "backup_canonical_1"
    assert service.resolved_reference == "sdk-visible-name.zip"
    assert service.restored_backup_id == "backup_canonical_1"
    assert service.operation_context.entries == 1
    assert service.operation_context.exits == 0
    assert service.close_calls == 1
    assert app.state.backup_maintenance is True
    assert len(scheduled) == 1
    assert service.prepared is not None

    await service.prepared.cutover_guard.close()
    assert service.operation_context.exits == 1


@pytest.mark.asyncio
async def test_dashboard_restore_stays_fenced_when_restart_scheduling_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    monkeypatch.setattr(
        backups_api,
        "schedule_process_restart",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(backups_api, "process_cutover_is_fenced", lambda: True)
    app = _api_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/backups/sdk-visible-name.zip/restore-destructive"
        )

    assert response.status_code == 503
    assert response.json()["detail"]["data"]["code"] == (
        "backup_restore_restart_rejected"
    )
    assert service.operation_context.entries == 1
    assert service.operation_context.exits == 0
    assert service.close_calls == 1
    assert app.state.backup_maintenance is True
    assert service.prepared is not None

    await service.prepared.cutover_guard.close()
    assert service.operation_context.exits == 1


@pytest.mark.asyncio
async def test_sdk_restore_returns_optimistic_204_and_runs_app_owned_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeBackupService()
    monkeypatch.setattr(backups_api, "_service", lambda _request: service)
    monkeypatch.setattr(
        backups_api,
        "schedule_process_restart",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(backups_api, "process_cutover_is_fenced", lambda: True)
    app = _api_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/backups/sdk-visible-name.zip/restore")
        await backups_api._restore_coordinator(app).wait_idle()

    assert response.status_code == 204
    assert service.restored_backup_id == "backup_canonical_1"
    assert service.prepared is not None
    assert service.operation_context.exits == 0

    await service.prepared.cutover_guard.close()
    assert service.operation_context.exits == 1
