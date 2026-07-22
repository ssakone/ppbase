from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase, __version__
from ppbase.api import health as health_module

pytestmark = pytest.mark.asyncio


async def test_health_includes_current_version() -> None:
    app = PPBase().get_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["code"] == 200
    assert payload["message"] == "API is healthy."
    assert payload["data"]["version"] == __version__


@pytest.mark.parametrize("configured", (True, False))
async def test_backup_activation_probe_reflects_live_process_capability(
    monkeypatch: pytest.MonkeyPatch,
    configured: bool,
) -> None:
    monkeypatch.setattr(health_module, "can_self_restart", lambda: configured)
    app = PPBase().get_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/health/backup-activation")

    assert response.status_code == 200
    assert response.json() == {
        "code": 200,
        "message": "Backup activation capability inspected.",
        "data": {"activation": {"configured": configured}},
    }
