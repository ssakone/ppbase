from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def _register_user(
    app_client: AsyncClient,
    collection_name: str,
    email: str,
    password: str = "securepass123",
) -> dict:
    response = await app_client.post(
        f"/api/collections/{collection_name}/records",
        json={
            "email": email,
            "password": password,
            "passwordConfirm": password,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _login_user(
    app_client: AsyncClient,
    collection_name: str,
    email: str,
    password: str = "securepass123",
) -> str:
    response = await app_client.post(
        f"/api/collections/{collection_name}/auth-with-password",
        json={"identity": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def test_auth_record_email_visibility_hides_email_from_other_non_admins(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"auth_visibility_{uuid.uuid4().hex[:8]}"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "auth",
            "schema": [],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_collection_response.status_code == 200, create_collection_response.text
    collection = create_collection_response.json()

    try:
        user1_email = f"user1_{uuid.uuid4().hex[:8]}@example.com"
        user2_email = f"user2_{uuid.uuid4().hex[:8]}@example.com"

        user1 = await _register_user(app_client, collection_name, user1_email)
        user2 = await _register_user(app_client, collection_name, user2_email)

        token_user2 = await _login_user(app_client, collection_name, user2_email)

        # Another non-admin auth record cannot see user1 email when emailVisibility=false.
        user1_as_user2 = await app_client.get(
            f"/api/collections/{collection_name}/records/{user1['id']}",
            headers={"Authorization": token_user2},
        )
        assert user1_as_user2.status_code == 200, user1_as_user2.text
        hidden_payload = user1_as_user2.json()
        assert "email" not in hidden_payload
        assert hidden_payload["emailVisibility"] is False

        # Owner can still see own email.
        user2_self = await app_client.get(
            f"/api/collections/{collection_name}/records/{user2['id']}",
            headers={"Authorization": token_user2},
        )
        assert user2_self.status_code == 200, user2_self.text
        self_payload = user2_self.json()
        assert self_payload["email"] == user2_email

        # Admin can see email.
        user1_as_admin = await app_client.get(
            f"/api/collections/{collection_name}/records/{user1['id']}",
            headers={"Authorization": admin_token},
        )
        assert user1_as_admin.status_code == 200, user1_as_admin.text
        admin_payload = user1_as_admin.json()
        assert admin_payload["email"] == user1_email
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )
