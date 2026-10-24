"""Tests for per-collection auth options.

Verifies that auth collections get unique token secrets and correct
durations, and that JWT tokens respect the configured durations.
"""

from __future__ import annotations

import uuid

import jwt as pyjwt
import pytest
from httpx import AsyncClient
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def _stored_token_secret(collection_name: str, token_option: str) -> str:
    """Read a token secret from ``_collections``: the API never serves it."""
    from ppbase.db.engine import get_engine

    async with get_engine().connect() as conn:
        result = await conn.execute(
            text(
                "SELECT options -> :option ->> 'secret' FROM _collections "
                "WHERE name = :name"
            ),
            {"option": token_option, "name": collection_name},
        )
        return result.scalar_one()


class TestAuthOptionsPopulated:
    """Verify auth options exist on auth collections."""

    async def test_superusers_has_auth_options(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/_superusers",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        opts = resp.json().get("options", {})
        assert "authToken" in opts
        assert "secret" not in opts["authToken"]
        assert len(await _stored_token_secret("_superusers", "authToken")) == 50
        # Superusers should have 1 day (86400s) auth token duration
        assert opts["authToken"]["duration"] == 86400

    async def test_users_has_auth_options(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        opts = resp.json().get("options", {})
        assert "authToken" in opts
        assert "verificationToken" in opts
        assert "passwordResetToken" in opts
        assert "fileToken" in opts
        # Users should have 7 days (604800s) auth token duration
        assert opts["authToken"]["duration"] == 604800

    async def test_users_oauth2_update_with_same_reserved_name(self, app_client: AsyncClient, admin_token: str):
        """Updating users OAuth2 settings should not fail on reserved-name validation."""
        resp = await app_client.patch(
            "/api/collections/users",
            headers={"Authorization": admin_token},
            json={
                "name": "users",
                "type": "auth",
                "options": {
                    "oauth2": {
                        "enabled": True,
                        "providers": [
                            {
                                "name": "google",
                                "clientId": "test-client-id",
                                "clientSecret": "test-client-secret",
                            }
                        ],
                        "mappedFields": {
                            "username": "name",
                            "avatarURL": "avatar",
                            "name": "Google",
                        },
                    }
                },
                "listRule": "id = @request.auth.id",
                "viewRule": "id = @request.auth.id",
                "createRule": "",
                "updateRule": "id = @request.auth.id",
                "deleteRule": "id = @request.auth.id",
            },
        )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        oauth2 = data["options"]["oauth2"]
        assert oauth2["enabled"] is True
        assert oauth2["providers"][0]["name"] == "google"
        assert "authToken" in data["options"]
        assert "passwordAuth" in data["options"]

    async def test_new_auth_collection_gets_created(self, app_client: AsyncClient, admin_token: str):
        """Creating a new auth collection should succeed."""
        coll_name = f"members_{uuid.uuid4().hex[:8]}"
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": coll_name,
                "type": "auth",
                "schema": [],
                "createRule": "",
                "listRule": "",
                "viewRule": "",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "auth"

    async def test_token_secrets_unique_per_collection(self, app_client: AsyncClient, admin_token: str):
        """Different collections should have different token secrets."""
        resp_su = await app_client.get(
            "/api/collections/_superusers",
            headers={"Authorization": admin_token},
        )
        resp_users = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        assert "secret" not in resp_su.json()["options"]["authToken"]
        assert "secret" not in resp_users.json()["options"]["authToken"]
        su_secret = await _stored_token_secret("_superusers", "authToken")
        user_secret = await _stored_token_secret("users", "authToken")
        assert su_secret and user_secret
        assert su_secret != user_secret


class TestTokenDurations:
    """Verify JWT tokens respect collection-configured durations."""

    async def test_admin_token_duration_matches_superusers_config(
        self, app_client: AsyncClient, admin_token: str
    ):
        """Admin token exp-iat should match _superusers authToken.duration."""
        decoded = pyjwt.decode(admin_token, options={"verify_signature": False})
        duration = decoded["exp"] - decoded["iat"]
        assert duration == 86400  # 1 day for superusers

    async def test_user_token_duration_matches_users_config(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """User auth token exp-iat should match users authToken.duration."""
        email = f"duration_test_{uuid.uuid4().hex[:8]}@example.com"
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        login_resp = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": email, "password": "securepass123"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]
        decoded = pyjwt.decode(token, options={"verify_signature": False})
        duration = decoded["exp"] - decoded["iat"]
        assert duration == 604800  # 7 days for users
