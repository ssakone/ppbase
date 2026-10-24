"""End-to-end tests using the bootstrapped default users collection.

Verifies full user flows (register, login, refresh, verify, password reset)
work correctly with the bootstrapped users collection and its per-collection
token secrets.
"""

from __future__ import annotations

import uuid

import jwt as pyjwt
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestDefaultUsersE2E:
    """Full flows using the bootstrapped users collection."""

    async def test_register_login_access_flow(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """Register -> Login -> Access protected records."""
        email = f"e2e_default_{uuid.uuid4().hex[:8]}@example.com"
        # Register
        reg = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
                "name": "Default User",
            },
        )
        assert reg.status_code == 200
        registered = reg.json()
        assert registered["collectionId"] == "_pb_users_auth_"
        assert registered["collectionName"] == "users"
        assert len(registered["id"]) == 15
        assert "tokenKey" not in registered
        assert "token_key" not in registered

        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        collection = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, collection, registered["id"])
        assert raw is not None
        assert len(raw["token_key"]) == 50

        # Login
        login = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": email, "password": "securepass123"},
        )
        assert login.status_code == 200
        token = login.json()["token"]

        # Access records with token
        list_resp = await app_client.get(
            "/api/collections/users/records",
            headers={"Authorization": token},
        )
        assert list_resp.status_code == 200

    async def test_token_refresh_cycle(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """Register -> Login -> Refresh -> Use new token."""
        email = f"refresh_cycle_{uuid.uuid4().hex[:8]}@example.com"
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        login = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": email, "password": "securepass123"},
        )
        token1 = login.json()["token"]

        # Refresh
        refresh = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": token1},
        )
        assert refresh.status_code == 200
        token2 = refresh.json()["token"]
        assert token2  # non-empty

        # Use refreshed token
        resp = await app_client.get(
            "/api/collections/users/records",
            headers={"Authorization": token2},
        )
        assert resp.status_code == 200

    async def test_token_claims_contain_correct_collection_id(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """JWT claims should contain the correct collectionId."""
        email = f"claims_{uuid.uuid4().hex[:8]}@example.com"
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        login = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": email, "password": "securepass123"},
        )
        token = login.json()["token"]
        decoded = pyjwt.decode(token, options={"verify_signature": False})
        assert decoded["collectionId"] == "_pb_users_auth_"
        assert decoded["type"] == "auth"

    async def test_password_reset_invalidates_old_tokens(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """After password reset, old tokens should no longer work."""
        email = f"reset_inval_{uuid.uuid4().hex[:8]}@example.com"
        await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "oldpass12345",
                "passwordConfirm": "oldpass12345",
            },
        )
        login = await app_client.post(
            "/api/collections/users/auth-with-password",
            json={"identity": email, "password": "oldpass12345"},
        )
        old_token = login.json()["token"]

        # Generate a reset token directly
        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_field
        from ppbase.services.auth_service import (
            create_password_reset_token,
            get_collection_token_config,
        )
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_field(engine, coll, "email", email)
        reset_secret, reset_duration = get_collection_token_config(
            coll, "passwordResetToken"
        )
        reset_token = create_password_reset_token(
            raw["id"],
            coll.id,
            email,
            raw["token_key"] + reset_secret,
            reset_duration,
        )
        resp = await app_client.post(
            "/api/collections/users/confirm-password-reset",
            json={
                "token": reset_token,
                "password": "newpass12345",
                "passwordConfirm": "newpass12345",
            },
        )
        assert resp.status_code == 204

        # Old token should fail
        refresh = await app_client.post(
            "/api/collections/users/auth-refresh",
            headers={"Authorization": old_token},
        )
        assert refresh.status_code == 401

    async def test_verification_flow(
        self, app_client: AsyncClient, auth_collection: dict
    ):
        """Register -> Verify -> Check verified flag."""
        email = f"verify_e2e_{uuid.uuid4().hex[:8]}@example.com"
        reg = await app_client.post(
            "/api/collections/users/records",
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert reg.status_code == 200
        user = reg.json()
        assert user["verified"] is False

        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.auth_service import (
            create_verification_token,
            get_collection_token_config,
        )
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        coll = await resolve_collection(engine, "users")
        raw = await _get_raw_record_by_id(engine, coll, user["id"])
        ver_secret, ver_duration = get_collection_token_config(
            coll, "verificationToken"
        )
        token = create_verification_token(
            raw["id"],
            coll.id,
            email,
            raw["token_key"] + ver_secret,
            ver_duration,
        )

        confirm = await app_client.post(
            "/api/collections/users/confirm-verification",
            json={"token": token},
        )
        assert confirm.status_code == 204

        # Check verified
        view = await app_client.get(
            f"/api/collections/users/records/{user['id']}"
        )
        assert view.status_code == 200
        assert view.json()["verified"] is True

    async def test_superusers_records_create_requires_superuser(
        self, app_client: AsyncClient
    ):
        """Anonymous callers cannot create a superuser record."""
        resp = await app_client.post(
            "/api/collections/_superusers/records",
            json={
                "email": "hacker@evil.com",
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert resp.status_code == 403

    async def test_superusers_records_crud_matches_pocketbase(
        self, app_client: AsyncClient, admin_token: str
    ):
        """Superusers are managed through the standard auth-record API."""
        email = f"superuser_{uuid.uuid4().hex[:8]}@example.com"
        headers = {"Authorization": admin_token}
        created = await app_client.post(
            "/api/collections/_superusers/records",
            headers=headers,
            json={
                "email": email,
                "password": "securepass123",
                "passwordConfirm": "securepass123",
            },
        )
        assert created.status_code == 200, created.text
        record = created.json()
        assert record["collectionName"] == "_superusers"
        assert record["email"] == email
        assert record["avatar"] == 0
        assert len(record["id"]) == 15
        assert "tokenKey" not in record
        assert "token_key" not in record

        login = await app_client.post(
            "/api/collections/_superusers/auth-with-password",
            json={"identity": email, "password": "securepass123"},
        )
        assert login.status_code == 200, login.text
        assert login.json()["record"]["id"] == record["id"]

        from ppbase.db.engine import get_engine
        from ppbase.services.record_auth_service import _get_raw_record_by_id
        from ppbase.services.record_service import resolve_collection

        engine = get_engine()
        collection = await resolve_collection(engine, "_superusers")
        raw = await _get_raw_record_by_id(engine, collection, record["id"])
        assert raw is not None
        assert len(raw["token_key"]) == 50

        updated_email = f"updated_{email}"
        updated = await app_client.patch(
            f"/api/collections/_superusers/records/{record['id']}",
            headers=headers,
            json={"email": updated_email},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["email"] == updated_email

        deleted = await app_client.delete(
            f"/api/collections/_superusers/records/{record['id']}",
            headers=headers,
        )
        assert deleted.status_code == 204, deleted.text

    async def test_auth_methods_on_bootstrapped_users(
        self, app_client: AsyncClient
    ):
        """auth-methods should return correct config for bootstrapped users."""
        resp = await app_client.get("/api/collections/users/auth-methods")
        assert resp.status_code == 200
        data = resp.json()
        assert data["password"]["enabled"] is True
        assert "email" in data["password"]["identityFields"]

    async def test_auth_system_field_properties_are_reimposed_on_collection_update(
        self, app_client: AsyncClient, admin_token: str
    ):
        """Auth system fields cannot be made removable or publicly exposed."""
        headers = {"Authorization": admin_token}
        current = await app_client.get("/api/collections/users", headers=headers)
        assert current.status_code == 200, current.text
        collection = current.json()
        fields = collection["fields"]

        for field in fields:
            if field["name"] == "id":
                field.update(required=False, system=False, hidden=True, primaryKey=False)
            elif field["name"] == "password":
                field.update(required=False, system=False, hidden=False)
            elif field["name"] == "tokenKey":
                field.update(required=False, system=False, hidden=False)
            elif field["name"] == "email":
                field.update(system=False, hidden=True)

        updated = await app_client.patch(
            f"/api/collections/{collection['id']}",
            headers=headers,
            json={"fields": fields},
        )
        assert updated.status_code == 200, updated.text
        normalized = {field["name"]: field for field in updated.json()["fields"]}

        assert normalized["id"]["required"] is True
        assert normalized["id"]["system"] is True
        assert normalized["id"]["hidden"] is False
        assert normalized["id"]["primaryKey"] is True
        assert normalized["id"]["autogeneratePattern"] == "[a-z0-9]{15}"
        assert normalized["password"]["required"] is True
        assert normalized["password"]["system"] is True
        assert normalized["password"]["hidden"] is True
        assert normalized["tokenKey"]["required"] is True
        assert normalized["tokenKey"]["system"] is True
        assert normalized["tokenKey"]["hidden"] is True
        assert normalized["tokenKey"]["autogeneratePattern"] == "[a-zA-Z0-9]{50}"
        assert normalized["email"]["system"] is True
        assert normalized["email"]["hidden"] is False

    async def test_admin_auth_uses_superusers_collection_secret(
        self, app_client: AsyncClient, admin_token: str
    ):
        """Admin token should be verifiable (proves it uses correct collection secret)."""
        resp = await app_client.get(
            "/api/admins",
            headers={"Authorization": admin_token},
        )
        assert resp.status_code == 200
