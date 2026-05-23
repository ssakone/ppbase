from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_json_field_parses_stringified_json_on_create_and_update(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"json_roles_{uuid.uuid4().hex[:8]}"
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {"name": "name", "type": "text", "required": False},
                {"name": "permissions", "type": "json", "required": False},
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert response.status_code == 200, response.text

    create_response = await app_client.post(
        f"/api/collections/{collection_name}/records",
        json={
            "name": "created from string",
            "permissions": '{"all":true,"article":{"read":true}}',
        },
    )
    assert create_response.status_code == 200, create_response.text
    created = create_response.json()
    assert created["permissions"] == {
        "all": True,
        "article": {"read": True},
    }

    update_response = await app_client.patch(
        f"/api/collections/{collection_name}/records/{created['id']}",
        json={
            "permissions": '{"all":false,"wallet":{"read":true}}',
        },
    )
    assert update_response.status_code == 200, update_response.text
    assert update_response.json()["permissions"] == {
        "all": False,
        "wallet": {"read": True},
    }


@pytest.mark.asyncio
async def test_json_field_keeps_plain_strings(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"json_labels_{uuid.uuid4().hex[:8]}"
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {"name": "value", "type": "json", "required": False},
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert response.status_code == 200, response.text

    create_response = await app_client.post(
        f"/api/collections/{collection_name}/records",
        json={"value": "hello"},
    )
    assert create_response.status_code == 200, create_response.text
    assert create_response.json()["value"] == "hello"
