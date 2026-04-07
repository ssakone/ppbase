from __future__ import annotations

from urllib.parse import quote
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_record_crud_accepts_custom_id_with_colon_and_quote(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"notify_{uuid.uuid4().hex[:8]}"
    record_id = "ab:cd'ef12"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "title",
                    "type": "text",
                    "required": False,
                }
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_collection_response.status_code == 200, create_collection_response.text
    collection = create_collection_response.json()

    record_path = (
        f"/api/collections/{collection_name}/records/{quote(record_id, safe='')}"
    )

    try:
        create_record_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            json={"id": record_id, "title": "created"},
        )
        assert create_record_response.status_code == 200, create_record_response.text
        assert create_record_response.json()["id"] == record_id

        update_record_response = await app_client.patch(
            record_path,
            headers={"Authorization": admin_token},
            json={"title": "updated"},
        )
        assert update_record_response.status_code == 200, update_record_response.text
        assert update_record_response.json()["title"] == "updated"

        delete_record_response = await app_client.delete(
            record_path,
            headers={"Authorization": admin_token},
        )
        assert delete_record_response.status_code == 204, delete_record_response.text
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )
