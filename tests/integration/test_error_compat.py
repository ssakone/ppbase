"""PostgreSQL integration tests of the opt-in PocketBase 0.22 error mode.

Default mode: PPBase's own ``{"status", "message", "data"}`` envelope and
messages are unchanged.  ``"0.22"`` mode: the same requests answer the
``{"code", "message", "data"}`` envelope of PocketBase 0.22.34
(apis/api_error.go l.11-15) with its messages; the expected 0.22 bodies are
the PocketBase 0.22.34 binary results of
``tests/pocketbase_parity/expand_rules_probe.py`` (errors group).
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

from ppbase.middleware.error_compat import ERROR_KIND_HEADER, set_error_compat

pytestmark = pytest.mark.asyncio


def _app(client: AsyncClient) -> Any:
    return client._transport.app  # ASGITransport(app=...)


@pytest_asyncio.fixture(scope="module")
async def error_env(app_client: AsyncClient, admin_token: str) -> AsyncIterator[dict]:
    sfx = uuid.uuid4().hex[:8]
    locked = f"errc_locked_{sfx}"
    open_name = f"errc_open_{sfx}"
    ids: dict[str, str] = {}
    for name, rule in ((locked, None), (open_name, "")):
        schema = [
            {
                "name": "title",
                "type": "text",
                "required": True,
                "options": {"min": None, "max": None, "pattern": ""},
            }
        ]
        if rule == "":
            schema.append(
                {
                    "name": "ref",
                    "type": "relation",
                    "required": False,
                    "options": {
                        "collectionId": ids[locked],
                        "cascadeDelete": False,
                        "minSelect": None,
                        "maxSelect": 1,
                        "displayFields": None,
                    },
                }
            )
        response = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": name,
                "type": "base",
                "schema": schema,
                "listRule": rule,
                "viewRule": rule,
                # A user's create goes through the createRule check.
                "createRule": "title != 'nope'" if rule == "" else rule,
                "updateRule": rule,
                "deleteRule": rule,
            },
        )
        assert response.status_code == 200, response.text
        ids[name] = response.json()["id"]
    response = await app_client.post(
        f"/api/collections/{open_name}/records",
        headers={"Authorization": admin_token},
        json={"title": "t1"},
    )
    assert response.status_code == 200, response.text
    ids["record"] = response.json()["id"]

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
        yield {
            "locked": locked,
            "open": open_name,
            "prefix": f"/api/errc-{sfx}",
            "record": ids["record"],
            "admin": admin_token,
        }
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


GENERIC = "Something went wrong while processing your request."
INVALID_FORMATTING = "Failed to load the submitted data due to invalid formatting."
LOAD_ERROR = "An error occurred while loading the submitted data."


async def _cases(client: AsyncClient, env: dict) -> dict[str, Any]:
    locked, open_name, prefix = env["locked"], env["open"], env["prefix"]
    results: dict[str, Any] = {}

    async def record(key: str, method: str, url: str, **kwargs: Any) -> None:
        response = await client.request(method, url, **kwargs)
        assert response.headers["content-type"].startswith("application/json"), key
        assert int(response.headers["content-length"]) == len(response.content), key
        # The private 0.22 error-kind tag never leaves PPBase.
        assert ERROR_KIND_HEADER not in response.headers, key
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
    admin = {"Authorization": env["admin"]}
    records_url = f"/api/collections/{open_name}/records"
    record_url = f"{records_url}/{env['record']}"
    missing_ref = {"title": "x", "ref": "zzzzzzzzzzzzzzz"}
    await record("create_validation_admin", "POST", records_url, json={}, headers=admin)
    await record(
        "create_missing_relation_admin",
        "POST",
        records_url,
        json=missing_ref,
        headers=admin,
    )
    await record("create_missing_relation", "POST", records_url, json=missing_ref)
    await record("create_rule_failed", "POST", records_url, json={"title": "nope"})
    await record("update_validation", "PATCH", record_url, json={"title": ""})
    await record(
        "update_validation_admin",
        "PATCH",
        record_url,
        json={"title": ""},
        headers=admin,
    )
    await record(
        "update_missing_relation", "PATCH", record_url, json={"ref": "zzzzzzzzzzzzzzz"}
    )
    await record(
        "password_missing_password",
        "POST",
        "/api/collections/users/auth-with-password",
        json={"identity": "nobody@example.invalid"},
    )
    await record(
        "password_empty_body",
        "POST",
        "/api/collections/users/auth-with-password",
        json={},
    )
    await record(
        "admin_password_empty_body", "POST", "/api/admins/auth-with-password", json={}
    )
    await record("list_bad_per_page", "GET", records_url, params={"perPage": "abc"})
    await record("refresh_without_token", "POST", "/api/collections/users/auth-refresh")
    await record("admin_route_without_token", "GET", "/api/collections")
    await record(
        "password_bad_credentials",
        "POST",
        "/api/collections/users/auth-with-password",
        json={"identity": "nobody@example.invalid", "password": "wrong-password"},
    )
    await record(
        "invalid_filter",
        "GET",
        f"/api/collections/{open_name}/records",
        params={"filter": "(("},
    )
    await record("unknown_route", "GET", "/api/errc-unknown-route")
    # c.Bind failures of the admin, settings and backup forms.
    raw_json = {**admin, "content-type": "application/json"}
    admins = await client.get("/api/admins", headers=admin)
    admin_id = admins.json()["items"][0]["id"]
    await record(
        "admin_create_invalid_json",
        "POST",
        "/api/admins",
        content=b"{bad",
        headers=raw_json,
    )
    await record(
        "admin_update_array_body",
        "PATCH",
        f"/api/admins/{admin_id}",
        content=b"[1]",
        headers=raw_json,
    )
    await record(
        "admin_password_invalid_json",
        "POST",
        "/api/admins/auth-with-password",
        content=b"{bad",
        headers={"content-type": "application/json"},
    )
    await record(
        "settings_invalid_json",
        "PATCH",
        "/api/settings",
        content=b"{bad",
        headers=raw_json,
    )
    await record(
        "test_email_invalid_json",
        "POST",
        "/api/settings/test/email",
        content=b"{bad",
        headers=raw_json,
    )
    await record(
        "backup_create_invalid_json",
        "POST",
        "/api/backups",
        content=b"{bad",
        headers=raw_json,
    )
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
        "admin_create_invalid_json",
        "admin_update_array_body",
        "admin_password_invalid_json",
        "settings_invalid_json",
        "test_email_invalid_json",
        "backup_create_invalid_json",
    ):
        status, body = results[key]
        assert set(body) == {"status", "message", "data"}, (key, body)
        assert body["status"] == status, key
    # An invalid settings JSON body is a 400, no longer a 500.
    assert results["settings_invalid_json"] == (
        400,
        {"status": 400, "message": "Invalid settings payload.", "data": {}},
    )
    assert results["test_email_invalid_json"] == (
        400,
        {
            "status": 400,
            "message": "Failed to send the test email.",
            "data": {
                "body": {
                    "code": "validation_invalid_value",
                    "message": "Invalid request body.",
                }
            },
        },
    )
    assert results["backup_create_invalid_json"] == (
        400,
        {"status": 400, "message": "Invalid UTF-8 JSON body.", "data": {}},
    )
    for key in ("admin_create_invalid_json", "admin_password_invalid_json"):
        assert results[key][1]["message"] == GENERIC, key
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
    # Every list filter/sort error is PocketBase's opaque 400 in both modes
    # (NewBadRequestError("", err), v0.22.34 apis/record_crud.go l.83-86).
    assert results["invalid_filter"] == (
        400,
        {
            "status": 400,
            "message": "Something went wrong while processing your request.",
            "data": {},
        },
    )
    # Starlette's own body for an unknown route is unchanged too.
    assert results["unknown_route"] == (404, {"detail": "Not Found"})
    assert results["create_validation"][1]["message"].startswith(
        "Failed to create record. Validation failed for: title"
    )
    # PPBase's own wording of the form errors, unchanged by default.
    blank = {"code": "validation_required", "message": "Cannot be blank."}
    assert results["create_validation_admin"] == (
        400,
        {
            "status": 400,
            "message": "Failed to create record. Validation failed for: title.",
            "data": {"title": blank},
        },
    )
    assert results["update_validation"] == (
        400,
        {
            "status": 400,
            "message": "Failed to update record. Validation failed for: title.",
            "data": {"title": blank},
        },
    )
    assert results["create_rule_failed"][1]["data"] == {
        "rule": {"code": "validation_rule_failed", "message": "Action not allowed."}
    }
    assert results["password_missing_password"] == (
        400,
        {
            "status": 400,
            "message": "Failed to authenticate.",
            "data": {"identity": {}, "password": blank},
        },
    )
    assert results["admin_password_empty_body"][1]["data"]["identity"] == {
        "code": "validation_required",
        "message": "Field required",
    }
    assert set(results["list_bad_per_page"][1]["data"]) == {"perPage"}


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
        "invalid_filter": (400, "Something went wrong while processing your request."),
        "unknown_route": (404, "Not Found."),
        "internal_error": (500, "Boom."),
        # 0.22.34 binary (probe errors group): an update form error and an
        # admin's create form error are unwrapped, a user's create wrapped.
        "create_validation_admin": (400, GENERIC),
        "create_missing_relation_admin": (400, GENERIC),
        "create_missing_relation": (400, "Failed to create record."),
        "create_rule_failed": (400, "Failed to create record."),
        "update_validation": (400, GENERIC),
        "update_validation_admin": (400, GENERIC),
        "update_missing_relation": (400, GENERIC),
        "password_missing_password": (400, GENERIC),
        "password_empty_body": (400, GENERIC),
        "admin_password_empty_body": (400, GENERIC),
        "list_bad_per_page": (400, GENERIC),
        # c.Bind failures (probe errors group): apis/admin.go l.247-249 and
        # l.293-295, l.83-85; apis/settings.go l.52-54, l.112-115;
        # apis/backup.go l.76-77.
        "admin_create_invalid_json": (400, INVALID_FORMATTING),
        "admin_update_array_body": (400, INVALID_FORMATTING),
        "admin_password_invalid_json": (400, LOAD_ERROR),
        "settings_invalid_json": (400, LOAD_ERROR),
        "test_email_invalid_json": (400, LOAD_ERROR),
        "backup_create_invalid_json": (400, LOAD_ERROR),
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
    # 0.22.34: "Invalid login credentials." is not a validation error.
    assert results["password_bad_credentials"][1]["data"] == {}
    required = {"code": "validation_required", "message": "Missing required value."}
    blank = {"code": "validation_required", "message": "Cannot be blank."}
    missing_rel = {
        "code": "validation_missing_rel_records",
        "message": "Failed to find all relation records with the provided ids.",
    }
    for key in ("create_validation", "create_validation_admin", "update_validation"):
        assert results[key][1]["data"] == {"title": required}, key
    for key in (
        "create_missing_relation",
        "create_missing_relation_admin",
        "update_missing_relation",
    ):
        assert results[key][1]["data"] == {"ref": missing_rel}, key
    assert results["create_rule_failed"][1]["data"] == {}
    assert results["password_missing_password"][1]["data"] == {"password": blank}
    assert results["password_empty_body"][1]["data"] == {
        "identity": blank,
        "password": blank,
    }
    assert results["admin_password_empty_body"][1]["data"] == {
        "identity": blank,
        "password": blank,
    }
    assert results["list_bad_per_page"][1]["data"] == {}
    for key, (_status, message) in expected.items():
        if message in {INVALID_FORMATTING, LOAD_ERROR}:
            assert results[key][1]["data"] == {}, key

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


async def test_022_mode_keeps_the_documented_data_differences(
    app_client: AsyncClient, error_env: dict, compat_022: None
) -> None:
    """Pinned PPBase ``data`` details the 0.22 mode does not rewrite.

    Listed in docs/error-compat.md ("Known differences"): a duplicate email
    (0.22.34 ``validation_invalid_email`` "The email is invalid or already
    in use.", forms/record_upsert.go l.584-598) and a confirm-password-reset
    without fields (0.22.34 also reports password and passwordConfirm,
    forms/record_password_reset_confirm.go l.47-49).
    """
    email = f"dup-{uuid.uuid4().hex[:8]}@example.invalid"
    body = {
        "email": email,
        "password": "dup-Password-1",
        "passwordConfirm": "dup-Password-1",
    }
    first = await app_client.post("/api/collections/users/records", json=body)
    assert first.status_code == 200, first.text
    duplicate = await app_client.post("/api/collections/users/records", json=body)
    assert duplicate.status_code == 400
    assert duplicate.json() == {
        "code": 400,
        "message": "Failed to create record.",
        "data": {
            "email": {
                "code": "validation_not_unique",
                "message": "The email is already in use.",
            }
        },
    }
    reset = await app_client.post(
        "/api/collections/users/confirm-password-reset", json={}
    )
    assert reset.status_code == 400
    assert list(reset.json()) == ["code", "message", "data"]
    assert set(reset.json()["data"]) == {"token"}
    await app_client.delete(
        f"/api/collections/users/records/{first.json()['id']}",
        headers={"Authorization": error_env["admin"]},
    )
