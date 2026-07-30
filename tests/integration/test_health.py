from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase, __version__
from ppbase.api import health as health_module

pytestmark = pytest.mark.asyncio


async def test_health_includes_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health_module,
        "backup_operation_available",
        lambda _data_dir: True,
    )
    app = PPBase().get_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 200
    assert payload["message"] == "API is healthy."
    assert payload["data"]["version"] == __version__
    assert payload["data"]["canBackup"] is True


@pytest.mark.parametrize("available", (True, False))
async def test_health_reflects_backup_operation_availability(
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
) -> None:
    monkeypatch.setattr(
        health_module,
        "backup_operation_available",
        lambda _data_dir: available,
    )
    app = PPBase().get_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["data"]["canBackup"] is available


@pytest.mark.parametrize("configured", (True, False))
async def test_backup_restart_probe_reflects_live_process_capability(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    monkeypatch.setattr(health_module, "can_self_restart", lambda: configured)
    app = PPBase().get_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health/backup-restart")

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "message": "Backup restart capability inspected.",
        "data": {"restart": {"configured": configured}},
    }


async def test_legacy_backup_activation_probe_is_hidden_from_openapi() -> None:
    app = PPBase().get_app()
    schema_paths = app.openapi()["paths"]

    assert "/api/health/backup-restart" in schema_paths
    assert "/api/health/backup-activation" not in schema_paths
