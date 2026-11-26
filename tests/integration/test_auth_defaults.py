"""Default limits on auth endpoints and OTP attempts."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from ppbase.middleware import rate_limit
from ppbase.middleware.rate_limit import (
    DEFAULT_AUTH_RATE_LIMIT,
    RateLimitMiddleware,
    is_auth_rate_limited_path,
)

pytestmark = pytest.mark.asyncio


def _limited_app() -> FastAPI:
    app = FastAPI()
    app.state.rate_limit_settings_version = 0
    app.add_middleware(RateLimitMiddleware)

    async def _ok() -> JSONResponse:
        return JSONResponse({"ok": True})

    for path in (
        "/api/collections/{coll}/auth-with-password",
        "/api/collections/{coll}/request-otp",
        "/api/collections/{coll}/auth-with-otp",
        "/api/collections/{coll}/request-password-reset",
        "/api/collections/{coll}/confirm-password-reset",
        "/api/collections/{coll}/auth-refresh",
    ):
        app.add_api_route(path, _ok, methods=["POST"])
    app.add_api_route("/api/collections/{coll}/records", _ok, methods=["GET"])
    return app


async def test_auth_rate_limit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A new install: no stored settings at all.
    def _no_settings():
        raise RuntimeError("no database")

    monkeypatch.setattr(rate_limit, "get_engine", _no_settings)

    from ppbase.api.settings import _DEFAULT_SETTINGS

    assert _DEFAULT_SETTINGS["rateLimiting"]["auth"] == DEFAULT_AUTH_RATE_LIMIT
    assert DEFAULT_AUTH_RATE_LIMIT["enabled"] is True
    assert _DEFAULT_SETTINGS["rateLimiting"]["enabled"] is False

    limit = DEFAULT_AUTH_RATE_LIMIT["maxRequests"]
    app = _limited_app()
    transport = ASGITransport(app=app, client=("203.0.113.5", 1234))
    other_transport = ASGITransport(app=app, client=("203.0.113.6", 1234))

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        for endpoint in (
            "auth-with-password",
            "request-otp",
            "auth-with-otp",
            "request-password-reset",
            "confirm-password-reset",
        ):
            url = f"/api/collections/users/{endpoint}"
            for _ in range(limit):
                resp = await client.post(url, json={})
                assert resp.status_code == 200, (endpoint, resp.text)
                assert resp.headers["x-ratelimit-limit"] == str(limit)
            blocked = await client.post(url, json={})
            assert blocked.status_code == 429, endpoint
            assert blocked.json() == {
                "status": 429,
                "message": "Too many requests.",
                "data": {},
            }
            assert blocked.headers.get("retry-after")

        # Other collections, other endpoints and other clients are separate.
        assert (await client.post("/api/collections/members/auth-with-password")).status_code == 200
        for _ in range(limit + 5):
            assert (await client.post("/api/collections/users/auth-refresh")).status_code == 200
            assert (await client.get("/api/collections/users/records")).status_code == 200

    async with AsyncClient(transport=other_transport, base_url="http://testserver") as other:
        assert (await other.post("/api/collections/users/auth-with-password")).status_code == 200

    # Only POSTs to the auth endpoints are matched.
    assert is_auth_rate_limited_path("POST", "/api/admins/auth-with-password")
    assert not is_auth_rate_limited_path("GET", "/api/collections/users/auth-with-password")
    assert not is_auth_rate_limited_path("POST", "/api/collections/users/auth-with-oauth2")

    # Settings saved by earlier versions (no ``rateLimiting.auth``) keep their
    # previous behaviour; explicit values are honoured.
    extract = rate_limit._extract_auth_rate_limit_config
    assert extract({}).enabled is True
    assert extract({"rateLimiting": {"enabled": False}}).enabled is False
    custom = extract({"rateLimiting": {"auth": {"enabled": True, "maxRequests": 3, "window": 9}}})
    assert (custom.enabled, custom.max_requests, custom.window_seconds) == (True, 3, 9)
    assert extract({"rateLimiting": {"auth": {"enabled": False}}}).enabled is False


async def test_auth_rate_limit_is_configurable(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    headers = {"Authorization": admin_token}
    current = await app_client.get("/api/settings", headers=headers)
    assert current.status_code == 200, current.text
    previous_auth = (current.json().get("rateLimiting") or {}).get(
        "auth", dict(DEFAULT_AUTH_RATE_LIMIT)
    )

    name = f"limited_{uuid.uuid4().hex[:8]}"
    created = await app_client.post(
        "/api/collections",
        headers=headers,
        json={"name": name, "type": "auth", "createRule": ""},
    )
    assert created.status_code == 200, created.text
    coll_id = created.json()["id"]

    enable = await app_client.patch(
        "/api/settings",
        headers=headers,
        json={"rateLimiting": {"auth": {"enabled": True, "maxRequests": 2, "window": 1}}},
    )
    assert enable.status_code == 200, enable.text
    assert enable.json()["rateLimiting"]["auth"]["maxRequests"] == 2

    try:
        url = f"/api/collections/{name}/auth-with-password"
        body = {"identity": "nobody@example.com", "password": "wrong-password"}
        first = await app_client.post(url, json=body)
        second = await app_client.post(url, json=body)
        blocked = await app_client.post(url, json=body)
        assert first.status_code == 400, first.text
        assert second.status_code == 400, second.text
        assert blocked.status_code == 429, blocked.text

        disable = await app_client.patch(
            "/api/settings",
            headers=headers,
            json={"rateLimiting": {"auth": {"enabled": False}}},
        )
        assert disable.status_code == 200, disable.text
        for _ in range(3):
            assert (await app_client.post(url, json=body)).status_code == 400
    finally:
        await asyncio.sleep(1.1)
        restore = await app_client.patch(
            "/api/settings",
            headers=headers,
            json={"rateLimiting": {"auth": previous_auth}},
        )
        assert restore.status_code == 200, restore.text
        await app_client.delete(f"/api/collections/{coll_id}", headers=headers)


async def test_otp_attempt_limit(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppbase.services import mail_service
    from ppbase.services.record_auth_service import DEFAULT_OTP_MAX_ATTEMPTS

    sent: dict[str, str] = {}

    async def _capture_otp(to, otp_id, otp_code, settings):
        sent[otp_id] = otp_code

    monkeypatch.setattr(mail_service, "send_otp_email", _capture_otp)

    headers = {"Authorization": admin_token}
    name = f"otp_{uuid.uuid4().hex[:8]}"
    created = await app_client.post(
        "/api/collections",
        headers=headers,
        json={
            "name": name,
            "type": "auth",
            "createRule": "",
            "options": {"otp": {"enabled": True, "duration": 300, "length": 6}},
        },
    )
    assert created.status_code == 200, created.text
    coll_id = created.json()["id"]

    async def _new_user(email: str) -> None:
        resp = await app_client.post(
            f"/api/collections/{name}/records",
            json={"email": email, "password": "pass123456", "passwordConfirm": "pass123456"},
        )
        assert resp.status_code == 200, resp.text

    async def _request_otp(email: str) -> tuple[str, str]:
        resp = await app_client.post(
            f"/api/collections/{name}/request-otp",
            json={"email": email},
        )
        assert resp.status_code == 200, resp.text
        otp_id = resp.json()["otpId"]
        return otp_id, sent[otp_id]

    def _wrong(code: str) -> str:
        return "".join(str((int(ch) + 1) % 10) for ch in code)

    url = f"/api/collections/{name}/auth-with-otp"
    try:
        assert DEFAULT_OTP_MAX_ATTEMPTS == 5

        # Exhausting the attempts invalidates the OTP, even for the right code.
        await _new_user("first@example.com")
        otp_id, code = await _request_otp("first@example.com")
        for _ in range(DEFAULT_OTP_MAX_ATTEMPTS):
            resp = await app_client.post(url, json={"otpId": otp_id, "password": _wrong(code)})
            assert resp.status_code == 400, resp.text
        resp = await app_client.post(url, json={"otpId": otp_id, "password": code})
        assert resp.status_code == 400, resp.text

        from sqlalchemy import text

        from ppbase.db.engine import get_engine

        async with get_engine().connect() as conn:
            remaining = (
                await conn.execute(
                    text('SELECT COUNT(*) FROM "_otps" WHERE "id" = :id'),
                    {"id": otp_id},
                )
            ).scalar_one()
        assert remaining == 0

        # Fewer failures than the limit still allow the right code, once.
        await _new_user("second@example.com")
        otp_id, code = await _request_otp("second@example.com")
        for _ in range(DEFAULT_OTP_MAX_ATTEMPTS - 1):
            resp = await app_client.post(url, json={"otpId": otp_id, "password": _wrong(code)})
            assert resp.status_code == 400, resp.text
        ok = await app_client.post(url, json={"otpId": otp_id, "password": code})
        assert ok.status_code == 200, ok.text
        assert ok.json()["token"]
        again = await app_client.post(url, json={"otpId": otp_id, "password": code})
        assert again.status_code == 400, again.text

        # The limit is configurable per collection.
        patched = await app_client.patch(
            f"/api/collections/{coll_id}",
            headers=headers,
            json={"options": {"otp": {"maxAttempts": 1}}},
        )
        assert patched.status_code == 200, patched.text
        await _new_user("third@example.com")
        otp_id, code = await _request_otp("third@example.com")
        resp = await app_client.post(url, json={"otpId": otp_id, "password": _wrong(code)})
        assert resp.status_code == 400, resp.text
        resp = await app_client.post(url, json={"otpId": otp_id, "password": code})
        assert resp.status_code == 400, resp.text
    finally:
        await app_client.delete(f"/api/collections/{coll_id}", headers=headers)
