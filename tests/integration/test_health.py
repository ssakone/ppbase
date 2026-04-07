from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase, __version__

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
