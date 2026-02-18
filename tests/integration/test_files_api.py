from __future__ import annotations

import re
import uuid

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_serve_file_download_query_param_controls_content_disposition(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"files_{uuid.uuid4().hex[:8]}"

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
                    "options": {"maxSelect": 1},
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
        create_record_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files={"doc": ("sample.txt", b"hello file body", "text/plain")},
        )
        assert create_record_response.status_code == 200, create_record_response.text
        record = create_record_response.json()

        stored_filename = str(record.get("doc", "") or "")
        assert re.match(r"^sample_[A-Za-z0-9]{10}\.txt$", stored_filename)

        file_path = f"/api/files/{collection['id']}/{record['id']}/{stored_filename}"

        inline_response = await app_client.get(file_path)
        assert inline_response.status_code == 200, inline_response.text
        assert inline_response.content == b"hello file body"
        inline_cd = inline_response.headers.get("content-disposition", "")
        assert "attachment" not in inline_cd.lower()

        download_response = await app_client.get(file_path, params={"download": "1"})
        assert download_response.status_code == 200, download_response.text
        download_cd = download_response.headers.get("content-disposition", "")
        assert "attachment" in download_cd.lower()
        assert stored_filename in download_cd

        named_download_response = await app_client.get(
            file_path, params={"download": "custom-name.txt"}
        )
        assert named_download_response.status_code == 200, named_download_response.text
        named_download_cd = named_download_response.headers.get("content-disposition", "")
        assert "attachment" in named_download_cd.lower()
        assert "custom-name.txt" in named_download_cd

        explicit_inline_response = await app_client.get(file_path, params={"download": "0"})
        assert explicit_inline_response.status_code == 200, explicit_inline_response.text
        explicit_inline_cd = explicit_inline_response.headers.get("content-disposition", "")
        assert "attachment" not in explicit_inline_cd.lower()

        thumb_fallback_response = await app_client.get(file_path, params={"thumb": "100x100"})
        assert thumb_fallback_response.status_code == 200, thumb_fallback_response.text
        assert thumb_fallback_response.content == b"hello file body"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_protected_files_require_valid_file_token(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"files_protected_{uuid.uuid4().hex[:8]}"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "privateDoc",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 1, "protected": True},
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
        create_record_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files={"privateDoc": ("secret.txt", b"top secret", "text/plain")},
        )
        assert create_record_response.status_code == 200, create_record_response.text
        record = create_record_response.json()
        stored_filename = str(record.get("privateDoc", "") or "")
        assert re.match(r"^secret_[A-Za-z0-9]{10}\.txt$", stored_filename)

        file_path = f"/api/files/{collection['id']}/{record['id']}/{stored_filename}"

        no_token_response = await app_client.get(file_path)
        assert no_token_response.status_code == 404, no_token_response.text

        invalid_token_response = await app_client.get(
            file_path, params={"token": "invalid.token.value"}
        )
        assert invalid_token_response.status_code == 404, invalid_token_response.text

        no_auth_token_response = await app_client.post("/api/files/token")
        assert no_auth_token_response.status_code == 401, no_auth_token_response.text

        file_token_response = await app_client.post(
            "/api/files/token",
            headers={"Authorization": admin_token},
        )
        assert file_token_response.status_code == 200, file_token_response.text
        file_token = str(file_token_response.json().get("token", "") or "")
        assert file_token

        with_token_response = await app_client.get(file_path, params={"token": file_token})
        assert with_token_response.status_code == 200, with_token_response.text
        assert with_token_response.content == b"top secret"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )
