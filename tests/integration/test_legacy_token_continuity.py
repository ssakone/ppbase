"""Tokens issued by PocketBase 0.22.34 keep working after a migration to PPBase.

A migration copies each record ``tokenKey`` and the PocketBase 0.22 token
secrets into the auth collection options.  PocketBase 0.22.34 signs every
record token with ``type=authRecord`` and ``tokenKey + <purpose secret>``
(tokens/record.go:13-95, tools/security/jwt.go:45-58); the tokens below are
built exactly like that from the stored values.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from ppbase.api import realtime as realtime_api
from ppbase.services.realtime_service import SubscriptionManager

pytestmark = pytest.mark.asyncio

PASSWORD = "securepass123"
RECORD_MESSAGE = "The request requires valid record authorization token to be set."
ADMIN_MESSAGE = "The request requires valid admin authorization token to be set."


async def _one(sql: str, **params: Any) -> Any:
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params)).mappings().first()


async def _collection(name: str) -> Any:
    return await _one("SELECT id, options FROM _collections WHERE name = :name", name=name)


async def _record(collection_name: str, record_id: str) -> Any:
    return await _one(f'SELECT * FROM "{collection_name}" WHERE id = :id', id=record_id)


async def legacy_record_token(
    collection_name: str,
    record_id: str,
    token_option: str = "authToken",
    *,
    claims: dict[str, Any] | None = None,
    token_type: str = "authRecord",
    seconds: int = 3600,
) -> str:
    """A token as PocketBase 0.22.34 issues it (tokens/record.go:13-95)."""
    collection = await _collection(collection_name)
    record = await _record(collection_name, record_id)
    payload = {
        "exp": int(time.time()) + seconds,
        "id": record_id,
        "type": token_type,
        "collectionId": collection["id"],
        **(claims or {}),
    }
    key = record["token_key"] + collection["options"][token_option]["secret"]
    return pyjwt.encode(payload, key, algorithm="HS256")


async def _create_user(app_client: AsyncClient, collection: str = "users", **fields: Any) -> dict:
    email = f"legacy_{uuid.uuid4().hex[:10]}@example.com"
    resp = await app_client.post(
        f"/api/collections/{collection}/records",
        json={"email": email, "password": PASSWORD, "passwordConfirm": PASSWORD, **fields},
    )
    assert resp.status_code == 200, resp.text
    record = resp.json()
    # A guest signup response hides the new email unless emailVisibility, like
    # PocketBase 0.22.34 EnrichRecords (apis/record_crud.go l.289,
    # apis/record_helpers.go l.153-158): keep the address the test signed up.
    assert "email" not in record, record
    return {**record, "email": email}


async def _create_auth_collection(app_client: AsyncClient, admin_token: str) -> str:
    name = f"members_{uuid.uuid4().hex[:8]}"
    resp = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={"name": name, "type": "auth", "schema": [], "createRule": "", "viewRule": ""},
    )
    assert resp.status_code == 200, resp.text
    return name


def _realtime_app() -> tuple[FastAPI, SubscriptionManager]:
    """The realtime router with the real auth dependency and database."""
    from ppbase.config import Settings

    app = FastAPI()
    app.include_router(realtime_api.router)
    manager = SubscriptionManager()
    app.state.subscription_manager = manager
    app.state.settings = Settings(jwt_secret="test-secret-key-for-integration-tests")
    return app, manager


class TestLegacySessionTokens:
    async def test_legacy_token_refresh_and_realtime_identity(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        legacy = await legacy_record_token("users", user["id"])

        refresh = await app_client.post(
            "/api/collections/users/auth-refresh", headers={"Authorization": legacy}
        )
        assert refresh.status_code == 200, refresh.text
        refreshed = refresh.json()["token"]
        assert pyjwt.decode(refreshed, options={"verify_signature": False})["type"] == "auth"

        app, manager = _realtime_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as rt:
            client_id = manager.register_client()
            first = await rt.post(
                "/api/realtime",
                json={"clientId": client_id, "subscriptions": [f"users/{user['id']}"]},
                headers={"Authorization": legacy},
            )
            assert first.status_code == 204, first.text
            # New topic on the same SSE client with the refreshed token (0.22.34: 204).
            second = await rt.post(
                "/api/realtime",
                json={"clientId": client_id, "subscriptions": [f"users/{user['id']}", "users/*"]},
                headers={"Authorization": refreshed},
            )
            assert second.status_code == 204, second.text
            # Unsubscribe one of two topics, still with the refreshed token.
            third = await rt.post(
                "/api/realtime",
                json={"clientId": client_id, "subscriptions": ["users/*"]},
                headers={"Authorization": refreshed},
            )
            assert third.status_code == 204, third.text
            assert [s.topic for s in manager.get_session(client_id).subscriptions] == ["users/*"]

            other = await _create_user(app_client)
            other_token = await legacy_record_token("users", other["id"])
            refused = await rt.post(
                "/api/realtime",
                json={"clientId": client_id, "subscriptions": ["users/*"]},
                headers={"Authorization": other_token},
            )
            assert refused.status_code == 403

    async def test_guest_then_legacy_token_on_the_same_client(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        legacy = await legacy_record_token("users", user["id"])
        app, manager = _realtime_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as rt:
            client_id = manager.register_client()
            guest = await rt.post(
                "/api/realtime", json={"clientId": client_id, "subscriptions": ["users/*"]}
            )
            assert guest.status_code == 204, guest.text
            upgraded = await rt.post(
                "/api/realtime",
                json={"clientId": client_id, "subscriptions": ["users/*", f"users/{user['id']}"]},
                headers={"Authorization": legacy},
            )
            assert upgraded.status_code == 204, upgraded.text
            back_to_guest = await rt.post(
                "/api/realtime", json={"clientId": client_id, "subscriptions": ["users/*"]}
            )
            assert back_to_guest.status_code == 403

    async def test_deleted_admin_realtime_clients_become_guests(
        self, app_client: AsyncClient, admin_token: str
    ) -> None:
        email = f"rt_admin_{uuid.uuid4().hex[:8]}@example.com"
        created = await app_client.post(
            "/api/admins",
            headers={"Authorization": admin_token},
            json={"email": email, "password": "adminpass123"},
        )
        assert created.status_code == 200, created.text
        login = await app_client.post(
            "/api/admins/auth-with-password",
            json={"identity": email, "password": "adminpass123"},
        )
        assert login.status_code == 200, login.text

        app, manager = _realtime_app()
        # The admins router reads the manager from the application state.
        main_app = app_client._transport.app
        previous = getattr(main_app.state, "subscription_manager", None)
        main_app.state.subscription_manager = manager
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as rt:
                client_id = manager.register_client()
                subscribed = await rt.post(
                    "/api/realtime",
                    json={"clientId": client_id, "subscriptions": ["users/*"]},
                    headers={"Authorization": login.json()["token"]},
                )
                assert subscribed.status_code == 204, subscribed.text
                assert manager.get_session(client_id).auth_key is not None

                deleted = await app_client.delete(
                    f"/api/admins/{created.json()['id']}",
                    headers={"Authorization": admin_token},
                )
                assert deleted.status_code == 204, deleted.text
                assert manager.get_session(client_id).auth_key is None
                guest = await rt.post(
                    "/api/realtime", json={"clientId": client_id, "subscriptions": ["users/*"]}
                )
                assert guest.status_code == 204, guest.text
        finally:
            if previous is None:
                del main_app.state.subscription_manager
            else:
                main_app.state.subscription_manager = previous


class TestRefreshStatusesAndMessages:
    """Same statuses and messages as the PocketBase 0.22.34 binary (apis/middlewares.go)."""

    async def test_admin_token_on_record_refresh_is_401(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict
    ) -> None:
        resp = await app_client.post(
            "/api/collections/users/auth-refresh", headers={"Authorization": admin_token}
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["message"] == RECORD_MESSAGE

    async def test_anonymous_and_invalid_tokens_on_record_refresh(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        legacy = await legacy_record_token("users", user["id"])
        expired = await legacy_record_token("users", user["id"], seconds=-60)
        tampered = legacy[:-2] + ("AA" if legacy[-2:] != "AA" else "BB")
        for headers in ({}, {"Authorization": expired}, {"Authorization": tampered}):
            resp = await app_client.post("/api/collections/users/auth-refresh", headers=headers)
            assert resp.status_code == 401, resp.text
            assert resp.json()["message"] == RECORD_MESSAGE

    async def test_other_collection_token_names_the_token_collection(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict
    ) -> None:
        members = await _create_auth_collection(app_client, admin_token)
        user = await _create_user(app_client)
        token = await legacy_record_token("users", user["id"])
        refresh = await app_client.post(
            f"/api/collections/{members}/auth-refresh", headers={"Authorization": token}
        )
        assert refresh.status_code == 403, refresh.text
        assert (
            refresh.json()["message"] == "The request requires auth record from users collection."
        )
        email_change = await app_client.post(
            f"/api/collections/{members}/request-email-change",
            headers={"Authorization": token},
            json={"newEmail": f"new_{uuid.uuid4().hex[:8]}@example.com"},
        )
        assert email_change.status_code == 403, email_change.text
        assert email_change.json()["message"] == (
            "The request requires auth record from users collection."
        )

    async def test_admin_routes_use_the_admin_message(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        token = await legacy_record_token("users", user["id"])
        for method, path in (("GET", "/api/admins"), ("POST", "/api/admins/auth-refresh")):
            resp = await app_client.request(method, path, headers={"Authorization": token})
            assert resp.status_code == 401, resp.text
            assert resp.json()["message"] == ADMIN_MESSAGE


class TestForgedNonAuthCollectionToken:
    """A collectionId of a base collection is ignored (v0.22.34 daos/record.go:432-434)."""

    async def test_forged_base_collection_token_is_anonymous(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict
    ) -> None:
        name = f"forged_{uuid.uuid4().hex[:8]}"
        created = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": name,
                "type": "base",
                "schema": [{"name": "title", "type": "text"}],
                "listRule": '@request.auth.id != ""',
                "viewRule": '@request.auth.id != ""',
            },
        )
        assert created.status_code == 200, created.text
        record = await app_client.post(
            f"/api/collections/{name}/records",
            headers={"Authorization": admin_token},
            json={"title": "protected"},
        )
        assert record.status_code == 200, record.text

        user = await _create_user(app_client)
        claims = pyjwt.decode(
            await legacy_record_token("users", user["id"]), options={"verify_signature": False}
        )
        forged = pyjwt.encode(
            {**claims, "collectionId": created.json()["id"]},
            "forged-signature-key-long-enough-for-hs256",
            algorithm="HS256",
        )

        view = await app_client.get(
            f"/api/collections/{name}/records/{record.json()['id']}",
            headers={"Authorization": forged},
        )
        assert view.status_code == 404, view.text
        anonymous = await app_client.get(f"/api/collections/{name}/records/{record.json()['id']}")
        assert anonymous.status_code == 404
        refresh = await app_client.post(
            "/api/collections/users/auth-refresh", headers={"Authorization": forged}
        )
        assert refresh.status_code == 401, refresh.text
        assert refresh.json()["message"] == RECORD_MESSAGE


class TestLegacyPurposeTokens:
    """Verification, reset and email-change links e-mailed by PocketBase 0.22.34."""

    async def test_legacy_verification_token(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        assert (await _record("users", user["id"]))["verified"] is False
        token = await legacy_record_token(
            "users", user["id"], "verificationToken", claims={"email": user["email"]}
        )
        resp = await app_client.post(
            "/api/collections/users/confirm-verification", json={"token": token}
        )
        assert resp.status_code == 204, resp.text
        assert (await _record("users", user["id"]))["verified"] is True

    async def test_legacy_password_reset_token(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        before = await _record("users", user["id"])
        token = await legacy_record_token(
            "users", user["id"], "passwordResetToken", claims={"email": user["email"]}
        )
        resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={"token": token, "password": "brandnewpass1", "passwordConfirm": "brandnewpass1"},
        )
        assert resp.status_code == 204, resp.text
        after = await _record("users", user["id"])
        assert after["token_key"] != before["token_key"]
        # Same side effect as PocketBase 0.22.34 forms/record_password_reset_confirm.go:96-106.
        assert before["verified"] is False and after["verified"] is True
        login = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": user["email"], "password": "brandnewpass1"},
        )
        assert login.status_code == 200, login.text
        # A reset renews the tokenKey: the same link cannot be used twice.
        again = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={"token": token, "password": "anotherpass12", "passwordConfirm": "anotherpass12"},
        )
        assert again.status_code == 400, again.text

    async def test_legacy_email_change_token(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        before = await _record("users", user["id"])
        new_email = f"changed_{uuid.uuid4().hex[:8]}@example.com"
        token = await legacy_record_token(
            "users",
            user["id"],
            "emailChangeToken",
            claims={"email": user["email"], "newEmail": new_email},
        )
        resp = await app_client.post(
            "/api/collections/users/confirm-email-change",
            json={"token": token, "password": PASSWORD},
        )
        assert resp.status_code == 204, resp.text
        after = await _record("users", user["id"])
        assert after["email"] == new_email
        assert after["verified"] is True
        assert after["token_key"] != before["token_key"]

    async def test_ppbase_purpose_tokens_still_work(
        self, app_client: AsyncClient, auth_collection: dict
    ) -> None:
        user = await _create_user(app_client)
        token = await legacy_record_token(
            "users",
            user["id"],
            "verificationToken",
            claims={"email": user["email"]},
            token_type="verification",
        )
        resp = await app_client.post(
            "/api/collections/users/confirm-verification", json={"token": token}
        )
        assert resp.status_code == 204, resp.text

    @pytest.mark.parametrize(
        "variant",
        [
            "session_secret",
            "session_type",
            "admin_type",
            "other_collection",
            "expired",
            "other_email",
        ],
    )
    async def test_legacy_purpose_token_refusals(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict, variant: str
    ) -> None:
        user = await _create_user(app_client)
        claims = {"email": user["email"]}
        option, token_type, seconds = "verificationToken", "authRecord", 3600
        if variant == "session_secret":
            # A 0.22 session token: same type, signed with the session secret.
            option = "authToken"
        elif variant == "session_type":
            token_type = "auth"
        elif variant == "admin_type":
            token_type = "admin"
        elif variant == "expired":
            seconds = -60
        elif variant == "other_email":
            claims = {"email": "someone.else@example.com"}
        token = await legacy_record_token(
            "users", user["id"], option, claims=claims, token_type=token_type, seconds=seconds
        )
        if variant == "other_collection":
            members = await _create_auth_collection(app_client, admin_token)
            other = await _collection(members)
            payload = pyjwt.decode(token, options={"verify_signature": False})
            record = await _record("users", user["id"])
            secret = (await _collection("users"))["options"]["verificationToken"]["secret"]
            token = pyjwt.encode(
                {**payload, "collectionId": other["id"]},
                record["token_key"] + secret,
                algorithm="HS256",
            )

        resp = await app_client.post(
            "/api/collections/users/confirm-verification", json={"token": token}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["data"]["token"]["code"] == "validation_invalid_token"
        assert (await _record("users", user["id"]))["verified"] is False
