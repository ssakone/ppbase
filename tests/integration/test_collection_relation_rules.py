from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient


async def _create_base_collection(
    app_client: AsyncClient,
    admin_token: str,
    *,
    name: str,
    schema: list[dict],
    list_rule: str = "",
) -> dict:
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": name,
            "type": "base",
            "schema": schema,
            "listRule": list_rule,
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create_user(
    app_client: AsyncClient,
    *,
    email: str,
    password: str,
) -> dict:
    response = await app_client.post(
        "/api/collections/users/records",
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
    *,
    email: str,
    password: str,
) -> str:
    response = await app_client.post(
        "/api/collections/users/auth-with-password",
        json={"identity": email, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _list_business_names(
    app_client: AsyncClient,
    *,
    collection_name: str,
    token: str,
) -> list[str]:
    response = await app_client.get(
        f"/api/collections/{collection_name}/records",
        headers={"Authorization": token},
    )
    assert response.status_code == 200, response.text
    return [item["name"] for item in response.json()["items"]]


@pytest.mark.asyncio
async def test_collection_relation_traversal_rule_allows_owner_and_access_user(
    app_client: AsyncClient,
    admin_token: str,
    auth_collection: dict,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    business_name = f"business_{suffix}"
    access_name = f"business_access_{suffix}"
    password = "secret12345"

    owner = await _create_user(
        app_client,
        email=f"owner_{suffix}@example.com",
        password=password,
    )
    access_user = await _create_user(
        app_client,
        email=f"access_{suffix}@example.com",
        password=password,
    )
    other_user = await _create_user(
        app_client,
        email=f"other_{suffix}@example.com",
        password=password,
    )

    owner_token = await _login_user(
        app_client,
        email=f"owner_{suffix}@example.com",
        password=password,
    )
    access_token = await _login_user(
        app_client,
        email=f"access_{suffix}@example.com",
        password=password,
    )
    other_token = await _login_user(
        app_client,
        email=f"other_{suffix}@example.com",
        password=password,
    )

    users_collection_id = auth_collection["id"]
    business_rule = (
        "@request.auth.id != '' && "
        "(@request.auth.id = owner.id || "
        f"(@collection.{access_name}.user ?= @request.auth.id && "
        f"@collection.{access_name}.business.id ?= id))"
    )

    business = await _create_base_collection(
        app_client,
        admin_token,
        name=business_name,
        list_rule=business_rule,
        schema=[
            {"name": "name", "type": "text", "required": True},
            {
                "name": "owner",
                "type": "relation",
                "required": True,
                "options": {"collectionId": users_collection_id, "maxSelect": 1},
            },
        ],
    )
    access = await _create_base_collection(
        app_client,
        admin_token,
        name=access_name,
        schema=[
            {
                "name": "user",
                "type": "relation",
                "required": True,
                "options": {"collectionId": users_collection_id, "maxSelect": 1},
            },
            {
                "name": "business",
                "type": "relation",
                "required": True,
                "options": {"collectionId": business["id"], "maxSelect": 1},
            },
        ],
    )

    create_business = await app_client.post(
        f"/api/collections/{business_name}/records",
        json={"name": "Owner Business", "owner": owner["id"]},
    )
    assert create_business.status_code == 200, create_business.text
    business_record = create_business.json()

    create_access = await app_client.post(
        f"/api/collections/{access_name}/records",
        json={"user": access_user["id"], "business": business_record["id"]},
    )
    assert create_access.status_code == 200, create_access.text

    assert await _list_business_names(
        app_client,
        collection_name=business_name,
        token=owner_token,
    ) == ["Owner Business"]
    assert await _list_business_names(
        app_client,
        collection_name=business_name,
        token=access_token,
    ) == ["Owner Business"]
    assert await _list_business_names(
        app_client,
        collection_name=business_name,
        token=other_token,
    ) == []

    relation_field_filter = await app_client.get(
        f"/api/collections/{business_name}/records",
        headers={"Authorization": admin_token},
        params={
            "filter": (
                f'@collection.{access["name"]}.business.owner ?= "{owner["id"]}"'
            ),
        },
    )
    assert relation_field_filter.status_code == 200, relation_field_filter.text
    assert [item["name"] for item in relation_field_filter.json()["items"]] == [
        "Owner Business"
    ]
