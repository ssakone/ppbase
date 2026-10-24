"""PPBase error bodies carry PocketBase 0.22's ``code`` next to ``status``.

PocketBase 0.22.34 serializes ``{"code", "message", "data"}``
(apis/api_error.go l.12-15), the current PocketBase ``{"status", ...}``.
See ppbase/middleware/error_envelope.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from httpx import ASGITransport, AsyncClient

from ppbase.app import create_app
from ppbase.config import Settings
from ppbase.middleware.error_envelope import ErrorEnvelopeCodeMiddleware


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/native")
    async def native() -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"status": 403, "message": "Only admins.", "data": {}},
        )

    @app.get("/legacy")
    async def legacy() -> JSONResponse:
        # BUMA's ported routes already answer in the 0.22 shape.
        return JSONResponse(
            status_code=401,
            content={"code": 401, "message": "Unauthorized.", "data": {}},
        )

    @app.get("/custom")
    async def custom() -> JSONResponse:
        return JSONResponse(status_code=400, content={"status": "error", "x": 1})

    @app.get("/mismatch")
    async def mismatch() -> JSONResponse:
        return JSONResponse(
            status_code=400, content={"status": 404, "message": "m", "data": {}}
        )

    @app.get("/ok")
    async def ok() -> JSONResponse:
        return JSONResponse(content={"status": 200, "message": "m", "data": {}})

    @app.get("/text")
    async def text() -> PlainTextResponse:
        return PlainTextResponse('{"status": 500}', status_code=500)

    @app.get("/raised")
    async def raised() -> None:
        raise HTTPException(status_code=404, detail="missing")

    @app.exception_handler(HTTPException)
    async def handler(_request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": exc.status_code, "message": exc.detail, "data": {}},
        )

    app.add_middleware(ErrorEnvelopeCodeMiddleware)
    return app


@pytest.mark.asyncio
async def test_native_error_bodies_gain_the_legacy_code_key() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://testserver"
    ) as client:
        native = await client.get("/native")
        assert native.status_code == 403
        assert native.json() == {
            "code": 403,
            "status": 403,
            "message": "Only admins.",
            "data": {},
        }
        assert int(native.headers["content-length"]) == len(native.content)
        raised = await client.get("/raised")
        assert raised.json() == {
            "code": 404,
            "status": 404,
            "message": "missing",
            "data": {},
        }
        # Anything else is passed through byte for byte.
        legacy = await client.get("/legacy")
        assert legacy.json() == {"code": 401, "message": "Unauthorized.", "data": {}}
        assert (await client.get("/custom")).json() == {"status": "error", "x": 1}
        assert "code" not in (await client.get("/mismatch")).json()
        assert "code" not in (await client.get("/ok")).json()
        assert (await client.get("/text")).text == '{"status": 500}'


@pytest.mark.asyncio
async def test_the_app_emits_code_and_status() -> None:
    app = create_app(Settings())
    app.state.backup_maintenance = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/api/health")
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == body["status"] == 503
    assert set(body) == {"code", "status", "message", "data"}
