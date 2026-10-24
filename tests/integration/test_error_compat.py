"""PostgreSQL integration tests of the opt-in PocketBase 0.22 error mode.

Default mode: PPBase's own ``{"status", "message", "data"}`` envelope and
messages are unchanged.  ``"0.22"`` mode: the same requests answer the
``{"code", "message", "data"}`` envelope of PocketBase 0.22.34
(apis/api_error.go l.11-15) with its messages; the expected 0.22 bodies are
the PocketBase 0.22.34 binary results of
``tests/pocketbase_parity/error_compat_probe.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from httpx import AsyncClient

from ppbase.middleware.error_compat import set_error_compat

pytestmark = pytest.mark.asyncio


def _app(client: AsyncClient) -> Any:
    return client._transport.app  # ASGITransport(app=...)


@pytest_asyncio.fixture(scope="module")
async def error_env(app_client: AsyncClient, admin_token: str) -> AsyncIterator[dict]:
    sfx = uuid.uuid4().hex[:8]
    locked = f"errc_locked_{sfx}"
    open_name = f"errc_open_{sfx}"
    for name, rule in ((locked, None), (open_name, "")):
        response = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": name,
                "type": "base",
                "schema": [
                    {
                        "name": "title",
                        "type": "text",
                        "required": True,
                        "options": {"min": None, "max": None, "pattern": ""},
                    }
                ],
                "listRule": rule,
                "viewRule": rule,
                "createRule": rule,
                "updateRule": rule,
                "deleteRule": rule,
            },
        )
        assert response.status_code == 200, response.text

    app = _app(app_client)

    async def boom() -> None:
        raise HTTPException(status_code=500, detail="boom")

    async def legacy_code() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"code": 401, "message": "Legacy body.", "data": {}},
        )

    async def legacy_message() -> JSONResponse:
        return JSONResponse(status_code=403, content={"message": "Montant non valide"})

    async def ok_with_status() -> JSONResponse:
        return JSONResponse(
            status_code=200, content={"status": 200, "message": "ok", "data": {}}
        )

    routes = {
        f"/api/errc-{sfx}/boom": boom,
        f"/api/errc-{sfx}/legacy-code": legacy_code,
        f"/api/errc-{sfx}/legacy-message": legacy_message,
        f"/api/errc-{sfx}/ok": ok_with_status,
    }
    added = []
    for path, endpoint in routes.items():
        app.add_api_route(path, endpoint, methods=["GET"])
        added.append(app.router.routes[-1])
    try:
        yield {"locked": locked, "open": open_name, "prefix": f"/api/errc-{sfx}"}
    finally:
        for route in added:
            app.router.routes.remove(route)
        set_error_compat(app, "")


@pytest_asyncio.fixture
async def compat_022(app_client: AsyncClient) -> AsyncIterator[None]:
    app = _app(app_client)
    set_error_compat(app, "0.22")
    try:
        yield
    finally:
        set_error_compat(app, "")


async def _cases(client: AsyncClient, env: dict) -> dict[str, Any]:
    locked, open_name, prefix = env["locked"], env["open"], env["prefix"]
    results: dict[str, Any] = {}

    async def record(key: str, method: str, url: str, **kwargs: Any) -> None:
        response = await client.request(method, url, **kwargs)
        assert response.headers["content-type"].startswith("application/json"), key
        assert int(response.headers["content-length"]) == len(response.content), key
        results[key] = (response.status_code, response.json())

    await record("list_admin_only", "GET", f"/api/collections/{locked}/records")
    await record("view_admin_only", "GET", f"/api/collections/{locked}/records/abc")
    await record(
        "create_admin_only", "POST", f"/api/collections/{locked}/records", json={}
    )
    await record("unknown_collection", "GET", "/api/collections/nope_errc/records")
    await record(
        "unknown_record", "GET", f"/api/collections/{open_name}/records/nope0000000000x"
    )
    await record(
        "create_invalid_json",
        "POST",
        f"/api/collections/{open_name}/records",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    await record(
        "create_validation", "POST", f"/api/collections/{open_name}/records", json={}
    )
    await record("refresh_without_token", "POST", "/api/collections/users/auth-refresh")
    await record("admin_route_without_token", "GET", "/api/collections")
    await record(
        "password_bad_credentials",
        "POST",
        "/api/collections/users/auth-with-password",
        json={"identity": "nobody@example.invalid", "password": "wrong-password"},
    )
    await record("unknown_route", "GET", "/api/errc-unknown-route")
    await record("internal_error", "GET", f"{prefix}/boom")
    await record("legacy_code_body", "GET", f"{prefix}/legacy-code")
    await record("legacy_message_body", "GET", f"{prefix}/legacy-message")
    await record("ok_with_status_key", "GET", f"{prefix}/ok")
    return results


async def test_default_mode_keeps_the_ppbase_envelope(
    app_client: AsyncClient, error_env: dict
) -> None:
    results = await _cases(app_client, error_env)
    for key in (
        "list_admin_only",
        "view_admin_only",
        "create_admin_only",
        "unknown_collection",
        "unknown_record",
        "create_invalid_json",
        "create_validation",
        "refresh_without_token",
        "admin_route_without_token",
        "password_bad_credentials",
        "internal_error",
    ):
        status, body = results[key]
        assert set(body) == {"status", "message", "data"}, (key, body)
        assert body["status"] == status, key
    assert results["list_admin_only"] == (
        403,
        {
            "status": 403,
            "message": "Only superusers can perform this action.",
            "data": {},
        },
    )
    assert results["unknown_collection"][1]["message"] == "Missing collection context."
    assert results["internal_error"] == (
        500,
        {"status": 500, "message": "boom", "data": {}},
    )
    # Starlette's own body for an unknown route is unchanged too.
    assert results["unknown_route"] == (404, {"detail": "Not Found"})
    assert results["create_validation"][1]["message"].startswith(
        "Failed to create record. Validation failed for: title"
    )


async def test_022_mode_answers_like_pocketbase_022(
    app_client: AsyncClient, error_env: dict, compat_022: None
) -> None:
    results = await _cases(app_client, error_env)
    expected = {
        "list_admin_only": (403, "Only admins can perform this action."),
        "view_admin_only": (403, "Only admins can perform this action."),
        "create_admin_only": (403, "Only admins can perform this action."),
        "unknown_collection": (404, "The requested resource wasn't found."),
        "unknown_record": (404, "The requested resource wasn't found."),
        "create_invalid_json": (
            400,
            "Failed to load the submitted data due to invalid formatting.",
        ),
        "create_validation": (400, "Failed to create record."),
        "refresh_without_token": (
            401,
            "The request requires valid record authorization token to be set.",
        ),
        "admin_route_without_token": (
            401,
            "The request requires valid admin authorization token to be set.",
        ),
        "password_bad_credentials": (400, "Failed to authenticate."),
        "unknown_route": (404, "Not Found."),
        "internal_error": (500, "Boom."),
    }
    for key, (status, message) in expected.items():
        got_status, body = results[key]
        assert got_status == status, (key, body)
        assert list(body) == ["code", "message", "data"], (key, body)
        assert body["code"] == status and body["message"] == message, (key, body)
        assert isinstance(body["data"], dict), key
    assert (
        results["create_validation"][1]["data"]["title"]["code"]
        == "validation_required"
    )

    # Host bodies that are not a PPBase envelope, and successes, are untouched.
    assert results["legacy_code_body"] == (
        401,
        {"code": 401, "message": "Legacy body.", "data": {}},
    )
    assert results["legacy_message_body"] == (403, {"message": "Montant non valide"})
    assert results["ok_with_status_key"] == (
        200,
        {"status": 200, "message": "ok", "data": {}},
    )


async def test_022_mode_keeps_successful_and_non_json_responses(
    app_client: AsyncClient, error_env: dict, compat_022: None
) -> None:
    response = await app_client.get(f"/api/collections/{error_env['open']}/records")
    assert response.status_code == 200
    assert set(response.json()) == {
        "page",
        "perPage",
        "totalItems",
        "totalPages",
        "items",
    }
    response = await app_client.get("/api/health")
    assert response.status_code == 200
    payload = json.loads(response.content)
    assert "code" in payload or "message" in payload
