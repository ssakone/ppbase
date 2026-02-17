"""Tests for system collections bootstrap.

Verifies that all 6 expected system collections exist after bootstrap,
and that the default users collection has the correct schema, rules,
and auth options.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestSystemCollectionExistence:
    """Verify all 6 system collections exist after bootstrap."""

    async def test_superusers_collection_exists(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/_superusers",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "_superusers"
        assert data["type"] == "auth"
        assert data["system"] is True

    async def test_external_auths_collection_exists(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/_externalAuths",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "_externalAuths"
        assert data["type"] == "base"
        assert data["system"] is True

    async def test_mfas_collection_exists(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/_mfas",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "_mfas"
        assert data["type"] == "base"
        assert data["system"] is True

    async def test_otps_collection_exists(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/_otps",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "_otps"
        assert data["type"] == "base"
        assert data["system"] is True

    async def test_auth_origins_collection_exists(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/_authOrigins",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "_authOrigins"
        assert data["type"] == "base"
        assert data["system"] is True

    async def test_users_collection_exists(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "users"
        assert data["type"] == "auth"
        assert data["system"] is False
        assert data["id"] == "_pb_users_auth_"


class TestDefaultUsersCollection:
    """Verify the default users collection has correct schema and rules."""

    async def test_users_has_name_field(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        data = resp.json()
        fields = data.get("fields") or data.get("schema") or []
        field_names = [f["name"] for f in fields]
        assert "name" in field_names

    async def test_users_has_avatar_field(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        data = resp.json()
        fields = data.get("fields") or data.get("schema") or []
        field_names = [f["name"] for f in fields]
        assert "avatar" in field_names

    async def test_users_has_auth_options(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        data = resp.json()
        opts = data.get("options", {})
        assert "authToken" in opts
        assert "secret" in opts["authToken"]
        assert "duration" in opts["authToken"]
        assert opts["authToken"]["duration"] == 604800  # 7 days for non-superusers

    async def test_users_has_password_auth_enabled(self, app_client: AsyncClient, admin_token: str):
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        data = resp.json()
        opts = data.get("options", {})
        assert opts.get("passwordAuth", {}).get("enabled") is True
        assert "email" in opts.get("passwordAuth", {}).get("identityFields", [])

    async def test_users_create_rule_is_public(self, app_client: AsyncClient, admin_token: str):
        """The bootstrapped users collection should allow public registration."""
        resp = await app_client.get(
            "/api/collections/users",
            headers={"Authorization": admin_token},
        )
        data = resp.json()
        assert data.get("createRule") is not None  # Not NULL (admin-only)

    async def test_total_collections_count(self, app_client: AsyncClient, admin_token: str):
        """At minimum 6 system collections should exist."""
        resp = await app_client.get(
            "/api/collections",
            headers={"Authorization": admin_token},
            params={"perPage": 100},
        )
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        names = {c["name"] for c in items}
        expected = {"_superusers", "_externalAuths", "_mfas", "_otps", "_authOrigins", "users"}
        assert expected.issubset(names), f"Missing: {expected - names}"

    async def test_collections_list_supports_skip_total(
        self,
        app_client: AsyncClient,
        admin_token: str,
    ):
        resp = await app_client.get(
            "/api/collections",
            headers={"Authorization": admin_token},
            params={"perPage": 2, "skipTotal": "true"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["totalItems"] == -1
        assert data["totalPages"] == -1
        assert data["page"] == 1
        assert data["perPage"] == 2
        assert isinstance(data["items"], list)

    async def test_collections_list_supports_fields_filter(
        self,
        app_client: AsyncClient,
        admin_token: str,
    ):
        resp = await app_client.get(
            "/api/collections",
            headers={"Authorization": admin_token},
            params={"perPage": 10, "fields": "id,name"},
        )
        assert resp.status_code == 200
        items = resp.json().get("items", [])
        assert items, "Expected at least one collection."
        for item in items:
            assert set(item.keys()) == {"id", "name"}
