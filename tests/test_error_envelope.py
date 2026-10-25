"""One error post-processing: current PocketBase envelope, or 0.22's on demand.

PPBase answers the exact error envelope of current PocketBase versions,
``{"status", "message", "data"}`` (v0.40.6 tools/router/error.go l.36-42).
PocketBase 0.22.34 answers ``{"code", "message", "data"}`` (apis/api_error.go
l.11-15, ``NewApiError`` l.67-75); clients written for it select the opt-in
0.22 mode (docs/error-compat.md), whose single outermost middleware rewrites
PPBase's envelope.  An earlier additive ``code`` next to ``status`` (a second
middleware) matched neither version and hid PPBase's envelope from the 0.22
mode; it is gone.
"""

from __future__ import annotations

import importlib.util

import pytest
from httpx import ASGITransport, AsyncClient

from ppbase.app import create_app
from ppbase.config import Settings
from ppbase.middleware.error_compat import ErrorCompatMiddleware

_MAINTENANCE = (
    "PPBase is temporarily read-only while a destructive backup restore is running."
)


async def _maintenance_error(mode: str) -> tuple[int, dict]:
    app = create_app(Settings(error_compat=mode))
    app.state.backup_maintenance = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/health")
    assert int(response.headers["content-length"]) == len(response.content)
    return response.status_code, response.json()


def test_a_single_error_middleware_is_installed() -> None:
    app = create_app(Settings())
    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes.count(ErrorCompatMiddleware) == 1
    # Outermost: every PPBase error body passes through it last.
    assert classes[0] is ErrorCompatMiddleware
    assert importlib.util.find_spec("ppbase.middleware.error_envelope") is None


@pytest.mark.asyncio
async def test_the_default_envelope_is_the_current_pocketbase_one() -> None:
    status, body = await _maintenance_error("")
    assert status == 503
    assert body == {"status": 503, "message": _MAINTENANCE, "data": {}}
    assert list(body) == ["status", "message", "data"]


@pytest.mark.asyncio
async def test_the_022_mode_envelope_is_the_pocketbase_022_one() -> None:
    status, body = await _maintenance_error("0.22")
    assert status == 503
    assert body == {"code": 503, "message": _MAINTENANCE, "data": {}}
    assert list(body) == ["code", "message", "data"]
