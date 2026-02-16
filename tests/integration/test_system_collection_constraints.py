"""Tests for system collection protection constraints.

Verifies that system collections cannot be deleted, renamed, or modified,
and that creating collections with reserved names fails.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestSystemCollectionCannotBeDeleted:
    """System collections should reject deletion."""

    async def test_delete_superusers_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.delete(
            "/api/collections/_superusers",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 400

    async def test_delete_external_auths_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.delete(
            "/api/collections/_externalAuths",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 400

    async def test_delete_mfas_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.delete(
            "/api/collections/_mfas",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 400

    async def test_delete_otps_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.delete(
            "/api/collections/_otps",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 400

    async def test_delete_auth_origins_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.delete(
            "/api/collections/_authOrigins",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 400


class TestSystemCollectionCannotBeModified:
    """System collections should reject update/rename."""

    async def test_rename_superusers_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.patch(
            "/api/collections/_superusers",
            headers={"Authorization": admin_token},
            json={"name": "admins"},
        )
        assert resp.status_code == 400

    async def test_modify_mfas_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.patch(
            "/api/collections/_mfas",
            headers={"Authorization": admin_token},
            json={"schema": []},
        )
        assert resp.status_code == 400


class TestReservedNameProtection:
    """Creating collections with reserved names should fail."""

    async def test_create_collection_named_users_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={"name": "users", "type": "base", "schema": []},
        )
        assert resp.status_code == 400

    async def test_create_collection_named_mfas_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={"name": "_mfas", "type": "base", "schema": []},
        )
        assert resp.status_code == 400

    async def test_create_collection_named_superusers_rejected(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={"name": "_superusers", "type": "base", "schema": []},
        )
        assert resp.status_code == 400
