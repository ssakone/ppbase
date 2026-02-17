"""Tests for token isolation between auth collections.

Verifies that tokens signed with one auth collection's secret cannot
be used to authenticate against a different auth collection.
"""

from __future__ import annotations

import time
import uuid

import jwt as pyjwt
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestTokenIsolation:
    """Tokens from one auth collection should not work on another."""

    async def _create_auth_collection(
        self, app_client: AsyncClient, admin_token: str, name: str
    ) -> dict:
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": name,
                "type": "auth",
                "schema": [],
                "createRule": "",
                "listRule": "",
                "viewRule": "",
                "updateRule": "",
                "deleteRule": "",
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def _register_and_login(
        self,
        app_client: AsyncClient,
        coll_name: str,
        email: str,
        password: str = "securepass123",
    ) -> str:
        await app_client.post(
            f"/api/collections/{coll_name}/records",
            json={
                "email": email,
                "password": password,
                "passwordConfirm": password,
            },
        )
        resp = await app_client.post(
            f"/api/collections/{coll_name}/auth-with-password",
            json={"identity": email, "password": password},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["token"]

    async def test_token_from_collection_a_cannot_refresh_on_b(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict
    ):
        """Token from users collection should not refresh on a different collection."""
        coll_b_name = f"team_{uuid.uuid4().hex[:8]}"
        await self._create_auth_collection(app_client, admin_token, coll_b_name)

        email = f"iso_a_{uuid.uuid4().hex[:8]}@example.com"
        token_a = await self._register_and_login(app_client, "users", email)

        # Try to refresh on collection B
        resp = await app_client.post(
            f"/api/collections/{coll_b_name}/auth-refresh",
            headers={"Authorization": token_a},
        )
        assert resp.status_code == 401

    async def test_token_from_collection_b_cannot_refresh_on_a(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict
    ):
        """Token from collection B should not refresh on users."""
        coll_b_name = f"staff_{uuid.uuid4().hex[:8]}"
        await self._create_auth_collection(app_client, admin_token, coll_b_name)

        email = f"iso_b_{uuid.uuid4().hex[:8]}@example.com"
        token_b = await self._register_and_login(app_client, coll_b_name, email)

        resp = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": token_b},
        )
        assert resp.status_code == 401

    async def test_admin_token_cannot_refresh_on_user_collection(
        self, app_client: AsyncClient, admin_token: str, auth_collection: dict
    ):
        """Admin token should not work for user auth-refresh."""
        resp = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 401

    async def test_user_token_cannot_access_admin_endpoints(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """User auth token should not access admin API endpoints."""
        email = f"noadmin_{uuid.uuid4().hex[:8]}@example.com"
        token = await self._register_and_login(app_client, "users", email)

        resp = await app_client.get(
            "/api/admins",
            headers={"Authorization": token},
        )
        assert resp.status_code == 401

    async def test_forged_authrecord_with_unknown_collection_is_rejected(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """Forged authRecord token with unknown collectionId must not grant access."""
        email = f"forged_{uuid.uuid4().hex[:8]}@example.com"
        reg = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert reg.status_code == 200, reg.text
        user = reg.json()

        now = int(time.time())
        forged_payload = {
            "id": user["id"],
            "type": "authRecord",
            "collectionId": "missing_collection_id",
            "iat": now,
            "exp": now + 3600,
        }
        forged = pyjwt.encode(forged_payload, "attacker-secret", algorithm="HS256")

        resp = await app_client.get(
            f"/api/collections/users/records/{user['id']}",
            headers={"Authorization": forged},
        )
        assert resp.status_code == 404, resp.text
