from __future__ import annotations

import re
import uuid

import pytest
from httpx import AsyncClient

from ppbase.services.file_storage import get_storage_path

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
    assert create_collection_response.status_code == 200, (
        create_collection_response.text
    )
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
        etag = inline_response.headers.get("etag", "")
        last_modified = inline_response.headers.get("last-modified", "")
        assert etag.startswith('"') and etag.endswith('"')
        assert last_modified
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
        named_download_cd = named_download_response.headers.get(
            "content-disposition", ""
        )
        assert "attachment" in named_download_cd.lower()
        assert "custom-name.txt" in named_download_cd

        explicit_inline_response = await app_client.get(
            file_path, params={"download": "0"}
        )
        assert explicit_inline_response.status_code == 200, (
            explicit_inline_response.text
        )
        explicit_inline_cd = explicit_inline_response.headers.get(
            "content-disposition", ""
        )
        assert "attachment" not in explicit_inline_cd.lower()

        range_response = await app_client.get(
            file_path,
            headers={"Range": "bytes=1-4"},
        )
        assert range_response.status_code == 206, range_response.text
        assert range_response.content == b"ello"
        assert range_response.headers["content-range"] == "bytes 1-4/15"
        assert range_response.headers["accept-ranges"] == "bytes"

        matching_if_range_response = await app_client.get(
            file_path,
            headers={"Range": "bytes=1-4", "If-Range": etag},
        )
        assert matching_if_range_response.status_code == 206
        assert matching_if_range_response.content == b"ello"

        stale_if_range_response = await app_client.get(
            file_path,
            headers={"Range": "bytes=1-4", "If-Range": '"stale"'},
        )
        assert stale_if_range_response.status_code == 200
        assert stale_if_range_response.content == b"hello file body"

        suffix_range_response = await app_client.get(
            file_path,
            headers={"Range": "bytes=-4"},
        )
        assert suffix_range_response.status_code == 206
        assert suffix_range_response.content == b"body"

        invalid_range_response = await app_client.get(
            file_path,
            headers={"Range": "bytes=999-1000"},
        )
        assert invalid_range_response.status_code == 416
        assert invalid_range_response.headers["content-range"] == "bytes */15"

        thumb_fallback_response = await app_client.get(
            file_path, params={"thumb": "100x100"}
        )
        assert thumb_fallback_response.status_code == 200, thumb_fallback_response.text
        assert thumb_fallback_response.content == b"hello file body"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_single_file_append_modifier_stores_plain_filename(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"files_single_append_{uuid.uuid4().hex[:8]}"

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
    assert create_collection_response.status_code == 200, (
        create_collection_response.text
    )
    collection = create_collection_response.json()

    try:
        create_record_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files=[("doc", ("first.txt", b"first-file-body", "text/plain"))],
        )
        assert create_record_response.status_code == 200, create_record_response.text
        record = create_record_response.json()
        first_filename = str(record.get("doc", "") or "")
        assert re.match(r"^first_[A-Za-z0-9]{10}\.txt$", first_filename)

        update_response = await app_client.patch(
            f"/api/collections/{collection_name}/records/{record['id']}",
            headers={"Authorization": admin_token},
            data={},
            files=[("doc+", ("second.txt", b"second-file-body", "text/plain"))],
        )
        assert update_response.status_code == 200, update_response.text
        updated = update_response.json()

        updated_filename = updated.get("doc")
        assert isinstance(updated_filename, str)
        assert re.match(r"^second_[A-Za-z0-9]{10}\.txt$", updated_filename)

        stale_file_response = await app_client.get(
            f"/api/files/{collection['id']}/{record['id']}/{first_filename}"
        )
        assert stale_file_response.status_code == 404, stale_file_response.text

        updated_file_response = await app_client.get(
            f"/api/files/{collection['id']}/{record['id']}/{updated_filename}"
        )
        assert updated_file_response.status_code == 200, updated_file_response.text
        assert updated_file_response.content == b"second-file-body"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_serve_view_file_uses_original_record_storage(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    source_collection_name = f"files_source_{uuid.uuid4().hex[:8]}"
    view_collection_name = f"files_view_{uuid.uuid4().hex[:8]}"
    view_collection: dict[str, object] | None = None

    create_source_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": source_collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "images",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 5},
                }
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_source_response.status_code == 200, create_source_response.text
    source_collection = create_source_response.json()

    try:
        create_record_response = await app_client.post(
            f"/api/collections/{source_collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files=[("images", ("view-source.png", b"view-file-body", "image/png"))],
        )
        assert create_record_response.status_code == 200, create_record_response.text
        record = create_record_response.json()
        filenames = record.get("images")
        assert isinstance(filenames, list)
        stored_filename = str(filenames[0])

        create_view_response = await app_client.post(
            "/api/collections",
            headers={"Authorization": admin_token},
            json={
                "name": view_collection_name,
                "type": "view",
                "schema": [
                    {
                        "name": "id",
                        "type": "text",
                        "required": True,
                    },
                    {
                        "name": "projected_images",
                        "type": "file",
                        "required": False,
                        "options": {"maxSelect": 5},
                    },
                ],
                "options": {
                    "query": (
                        f'SELECT src.id, src.images AS projected_images '
                        f'FROM "{source_collection_name}" src'
                    ),
                },
                "listRule": "",
                "viewRule": "",
            },
        )
        assert create_view_response.status_code == 200, create_view_response.text
        view_collection = create_view_response.json()

        source_path = (
            get_storage_path(source_collection["id"], record["id"]) / stored_filename
        )
        view_path = get_storage_path(view_collection["id"], record["id"]) / stored_filename
        assert source_path.is_file()
        assert not view_path.exists()

        file_response = await app_client.get(
            f"/api/files/{view_collection['id']}/{record['id']}/{stored_filename}"
        )
        assert file_response.status_code == 200, file_response.text
        assert file_response.content == b"view-file-body"
    finally:
        if view_collection is not None:
            await app_client.delete(
                f"/api/collections/{view_collection['id']}",
                headers={"Authorization": admin_token},
            )
        await app_client.delete(
            f"/api/collections/{source_collection['id']}",
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
    assert create_collection_response.status_code == 200, (
        create_collection_response.text
    )
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

        with_token_response = await app_client.get(
            file_path, params={"token": file_token}
        )
        assert with_token_response.status_code == 200, with_token_response.text
        assert with_token_response.content == b"top secret"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_multipart_update_with_file_add_and_remove_keeps_db_and_storage_in_sync(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"files_mix_{uuid.uuid4().hex[:8]}"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "images",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 5},
                }
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_collection_response.status_code == 200, (
        create_collection_response.text
    )
    collection = create_collection_response.json()

    try:
        create_record_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files=[("images", ("old.png", b"old-image-body", "image/png"))],
        )
        assert create_record_response.status_code == 200, create_record_response.text
        record = create_record_response.json()

        old_images = record.get("images")
        assert isinstance(old_images, list)
        assert len(old_images) == 1
        old_filename = str(old_images[0])
        assert re.match(r"^old_[A-Za-z0-9]{10}\.png$", old_filename)

        update_response = await app_client.patch(
            f"/api/collections/{collection_name}/records/{record['id']}",
            headers={"Authorization": admin_token},
            data={"images-": old_filename},
            files=[("images", ("new.png", b"new-image-body", "image/png"))],
        )
        assert update_response.status_code == 200, update_response.text
        updated = update_response.json()

        updated_images = updated.get("images")
        assert isinstance(updated_images, list)
        assert len(updated_images) == 1

        new_filename = str(updated_images[0])
        assert new_filename != old_filename
        assert re.match(r"^new_[A-Za-z0-9]{10}\.png$", new_filename)
        assert old_filename not in updated_images

        storage_dir = get_storage_path(collection["id"], record["id"])
        old_path = storage_dir / old_filename
        new_path = storage_dir / new_filename

        assert not old_path.exists()
        assert new_path.is_file()

        fetch_updated_response = await app_client.get(
            f"/api/collections/{collection_name}/records/{record['id']}",
            headers={"Authorization": admin_token},
        )
        assert fetch_updated_response.status_code == 200, fetch_updated_response.text
        fetched = fetch_updated_response.json()
        fetched_images = fetched.get("images")
        assert isinstance(fetched_images, list)
        assert fetched_images == [new_filename]

        old_file_response = await app_client.get(
            f"/api/files/{collection['id']}/{record['id']}/{old_filename}"
        )
        assert old_file_response.status_code == 404, old_file_response.text

        new_file_response = await app_client.get(
            f"/api/files/{collection['id']}/{record['id']}/{new_filename}"
        )
        assert new_file_response.status_code == 200, new_file_response.text
        assert new_file_response.content == b"new-image-body"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_multipart_update_with_file_append_modifier_adds_without_removing_existing(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection_name = f"files_append_{uuid.uuid4().hex[:8]}"

    create_collection_response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "images",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 5},
                }
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert create_collection_response.status_code == 200, (
        create_collection_response.text
    )
    collection = create_collection_response.json()

    try:
        create_record_response = await app_client.post(
            f"/api/collections/{collection_name}/records",
            headers={"Authorization": admin_token},
            data={},
            files=[("images", ("first.png", b"first-image-body", "image/png"))],
        )
        assert create_record_response.status_code == 200, create_record_response.text
        record = create_record_response.json()

        initial_images = record.get("images")
        assert isinstance(initial_images, list)
        assert len(initial_images) == 1
        first_filename = str(initial_images[0])

        update_response = await app_client.patch(
            f"/api/collections/{collection_name}/records/{record['id']}",
            headers={"Authorization": admin_token},
            data={},
            files=[("images+", ("second.png", b"second-image-body", "image/png"))],
        )
        assert update_response.status_code == 200, update_response.text
        updated = update_response.json()

        updated_images = updated.get("images")
        assert isinstance(updated_images, list)
        assert len(updated_images) == 2
        assert first_filename in updated_images

        second_candidates = [
            str(name) for name in updated_images if str(name) != first_filename
        ]
        assert len(second_candidates) == 1
        second_filename = second_candidates[0]
        assert re.match(r"^second_[A-Za-z0-9]{10}\.png$", second_filename)

        storage_dir = get_storage_path(collection["id"], record["id"])
        assert (storage_dir / first_filename).is_file()
        assert (storage_dir / second_filename).is_file()

        first_file_response = await app_client.get(
            f"/api/files/{collection['id']}/{record['id']}/{first_filename}"
        )
        assert first_file_response.status_code == 200, first_file_response.text
        assert first_file_response.content == b"first-image-body"

        second_file_response = await app_client.get(
            f"/api/files/{collection['id']}/{record['id']}/{second_filename}"
        )
        assert second_file_response.status_code == 200, second_file_response.text
        assert second_file_response.content == b"second-image-body"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )
