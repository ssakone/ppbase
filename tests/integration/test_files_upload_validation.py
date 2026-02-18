from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_file_upload_enforces_max_size(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"file_size_{uuid.uuid4().hex[:8]}"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "doc",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 1, "maxSize": 5},
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

    try:
        # 6 bytes > maxSize(5) -> fail
        too_big_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files={"doc": ("big.txt", b"123456", "text/plain")},
        )
        assert too_big_response.status_code == 400, too_big_response.text
        payload = too_big_response.json()
        assert payload["data"]["doc"]["code"] == "validation_max_size_constraint"

        # 5 bytes == maxSize(5) -> pass
        ok_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files={"doc": ("ok.txt", b"12345", "text/plain")},
        )
        assert ok_response.status_code == 200, ok_response.text
        record = ok_response.json()
        assert str(record.get("doc", "")).endswith(".txt")
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_file_upload_enforces_allowed_mime_types(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"file_mime_{uuid.uuid4().hex[:8]}"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "photo",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 1, "mimeTypes": ["image/png"]},
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

    try:
        wrong_mime_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files={"photo": ("not-allowed.txt", b"text", "text/plain")},
        )
        assert wrong_mime_response.status_code == 400, wrong_mime_response.text
        payload = wrong_mime_response.json()
        assert payload["data"]["photo"]["code"] == "validation_invalid_mime_type"

        allowed_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files={"photo": ("allowed.png", b"png-bytes", "image/png")},
        )
        assert allowed_response.status_code == 200, allowed_response.text
        record = allowed_response.json()
        assert str(record.get("photo", "")).endswith(".png")
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )
