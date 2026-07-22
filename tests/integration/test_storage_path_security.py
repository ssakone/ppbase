from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from httpx import AsyncClient

from ppbase.config import Settings
from ppbase.services import file_storage


pytestmark = pytest.mark.asyncio


async def _create_public_file_collection(
    app_client: AsyncClient,
    admin_token: str,
) -> dict:
    collection_name = f"storage_safe_{uuid.uuid4().hex[:8]}"
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "name": collection_name,
            "type": "base",
            "schema": [
                {
                    "name": "title",
                    "type": "text",
                    "required": True,
                },
                {
                    "name": "document",
                    "type": "file",
                    "required": False,
                    "options": {"maxSelect": 1},
                },
            ],
            "listRule": "",
            "viewRule": "",
            "createRule": "",
            "updateRule": "",
            "deleteRule": "",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _create_record_with_file(
    app_client: AsyncClient,
    collection: dict,
    record_id: str,
) -> dict:
    response = await app_client.post(
        f"/api/collections/{collection['name']}/records",
        data={"id": record_id, "title": "original"},
        files={
            "document": (
                "original.txt",
                b"original payload",
                "text/plain",
            )
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_collection_create_rejects_unsafe_storage_id(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "id": "../escape",
            "name": f"unsafe_storage_id_{uuid.uuid4().hex[:8]}",
            "type": "base",
            "schema": [],
        },
    )

    assert response.status_code == 400, response.text


@pytest.mark.parametrize("invalid_id", [0, False, [], {}])
async def test_collection_create_does_not_generate_for_falsey_non_string_id(
    app_client: AsyncClient,
    admin_token: str,
    invalid_id: object,
) -> None:
    response = await app_client.post(
        "/api/collections",
        headers={"Authorization": admin_token},
        json={
            "id": invalid_id,
            "name": f"invalid_collection_id_{uuid.uuid4().hex[:8]}",
            "type": "base",
            "schema": [],
        },
    )

    assert response.status_code in {400, 422}, response.text


async def test_batch_upsert_does_not_trim_record_id(
    app_client: AsyncClient,
    admin_token: str,
) -> None:
    collection = await _create_public_file_collection(app_client, admin_token)
    try:
        response = await app_client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "PUT",
                        "url": f"/api/collections/{collection['name']}/records",
                        "body": {"id": " spacedid ", "title": "invalid"},
                    }
                ]
            },
        )
        assert response.status_code == 400, response.text
        nested = response.json()["data"]["requests"]["0"]["response"]
        assert nested["data"]["id"]["code"] == "validation_invalid_record_id"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


@pytest.mark.parametrize("invalid_id", [0, False, [], {}])
async def test_record_create_rejects_non_string_falsey_ids(
    app_client: AsyncClient,
    admin_token: str,
    invalid_id: object,
) -> None:
    collection = await _create_public_file_collection(app_client, admin_token)
    try:
        response = await app_client.post(
            f"/api/collections/{collection['name']}/records",
            json={"id": invalid_id, "title": "invalid id"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["data"]["id"]["code"] == (
            "validation_invalid_record_id"
        )
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_batch_rollback_with_traversal_id_preserves_outside_sentinel(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    sentinel = data_dir / "sentinel"
    sentinel.mkdir(parents=True)
    marker = sentinel / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    try:
        response = await app_client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "POST",
                        "url": f"/api/collections/{collection['name']}/records",
                        "body": {
                            "id": "../../sentinel",
                            "title": "must be rejected",
                        },
                    },
                    {
                        "method": "POST",
                        "url": f"/api/collections/{collection['name']}/records",
                        "body": {"id": "safeid000000001"},
                    },
                ]
            },
        )

        assert response.status_code == 400, response.text
        payload = response.json()
        first_error = payload["data"]["requests"]["0"]["response"]
        assert first_error["data"]["id"]["code"] == "validation_invalid_record_id"
        assert sentinel.is_dir()
        assert marker.read_text(encoding="utf-8") == "keep"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_failed_record_create_removes_only_new_uploads(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "failedfile00001"

    try:
        response = await app_client.post(
            f"/api/collections/{collection['name']}/records",
            headers={"Authorization": admin_token},
            data={"id": record_id},
            files={
                "document": (
                    "temporary.txt",
                    b"must be removed",
                    "text/plain",
                )
            },
        )

        assert response.status_code == 400, response.text
        record_storage = file_storage.get_storage_path(collection["id"], record_id)
        assert not record_storage.exists()
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_failed_batch_cleans_only_valid_record_storage(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "safeid000000001"
    sentinel = data_dir / "sentinel"
    sentinel.mkdir(parents=True)
    marker = sentinel / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    record_storage = file_storage.get_storage_path(collection["id"], record_id)
    record_storage.mkdir(parents=True)
    preexisting = record_storage / "preexisting.txt"
    preexisting.write_text("keep orphan", encoding="utf-8")

    payload = {
        "requests": [
            {
                "method": "POST",
                "url": f"/api/collections/{collection['name']}/records",
                "body": {"id": record_id, "title": "rollback file"},
            },
            {
                "method": "POST",
                "url": f"/api/collections/{collection['name']}/records",
                "body": {"id": "otherid00000001"},
            },
        ]
    }

    try:
        response = await app_client.post(
            "/api/batch",
            data={"@jsonPayload": json.dumps(payload)},
            files={
                "requests.0.document": (
                    "rollback.txt",
                    b"temporary payload",
                    "text/plain",
                )
            },
        )

        assert response.status_code == 400, response.text
        assert record_storage.is_dir()
        assert sorted(path.name for path in record_storage.iterdir()) == [
            "preexisting.txt"
        ]
        assert preexisting.read_text(encoding="utf-8") == "keep orphan"
        assert sentinel.is_dir()
        assert marker.read_text(encoding="utf-8") == "keep"

        record_response = await app_client.get(
            f"/api/collections/{collection['name']}/records/{record_id}"
        )
        assert record_response.status_code == 404
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_failed_batch_update_preserves_old_file_and_removes_new_upload(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "batchupdate0001"
    record = await _create_record_with_file(app_client, collection, record_id)
    old_filename = str(record["document"])
    payload = {
        "requests": [
            {
                "method": "PATCH",
                "url": (
                    f"/api/collections/{collection['name']}/records/{record_id}"
                ),
                "body": {"title": "rolled back"},
            },
            {
                "method": "POST",
                "url": f"/api/collections/{collection['name']}/records",
                "body": {"id": "invalid0000001"},
            },
        ]
    }

    try:
        response = await app_client.post(
            "/api/batch",
            data={"@jsonPayload": json.dumps(payload)},
            files={
                "requests.0.document": (
                    "replacement.txt",
                    b"replacement payload",
                    "text/plain",
                )
            },
        )

        assert response.status_code == 400, response.text
        fetched = await app_client.get(
            f"/api/collections/{collection['name']}/records/{record_id}"
        )
        assert fetched.status_code == 200, fetched.text
        fetched_record = fetched.json()
        assert fetched_record["title"] == "original"
        assert fetched_record["document"] == old_filename

        storage_dir = file_storage.get_storage_path(collection["id"], record_id)
        assert sorted(path.name for path in storage_dir.iterdir()) == [old_filename]
        assert (storage_dir / old_filename).read_bytes() == b"original payload"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_failed_batch_delete_preserves_record_files(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "batchdelete0001"
    record = await _create_record_with_file(app_client, collection, record_id)
    filename = str(record["document"])

    try:
        response = await app_client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "DELETE",
                        "url": (
                            f"/api/collections/{collection['name']}/records/"
                            f"{record_id}"
                        ),
                    },
                    {
                        "method": "POST",
                        "url": f"/api/collections/{collection['name']}/records",
                        "body": {"id": "invalid0000001"},
                    },
                ]
            },
        )

        assert response.status_code == 400, response.text
        fetched = await app_client.get(
            f"/api/collections/{collection['name']}/records/{record_id}"
        )
        assert fetched.status_code == 200, fetched.text
        storage_path = file_storage.get_storage_file_path(
            collection["id"],
            record_id,
            filename,
        )
        assert storage_path.read_bytes() == b"original payload"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_successful_batch_flushes_deferred_file_deletes(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "batchsuccess001"
    record = await _create_record_with_file(app_client, collection, record_id)
    old_filename = str(record["document"])

    try:
        update_payload = {
            "requests": [
                {
                    "method": "PATCH",
                    "url": (
                        f"/api/collections/{collection['name']}/records/{record_id}"
                    ),
                    "body": {"title": "updated"},
                }
            ]
        }
        update_response = await app_client.post(
            "/api/batch",
            data={"@jsonPayload": json.dumps(update_payload)},
            files={
                "requests.0.document": (
                    "replacement.txt",
                    b"replacement payload",
                    "text/plain",
                )
            },
        )
        assert update_response.status_code == 200, update_response.text
        updated_record = update_response.json()[0]["body"]
        new_filename = str(updated_record["document"])
        assert new_filename != old_filename

        storage_dir = file_storage.get_storage_path(collection["id"], record_id)
        assert sorted(path.name for path in storage_dir.iterdir()) == [new_filename]
        assert (storage_dir / new_filename).read_bytes() == b"replacement payload"

        delete_response = await app_client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "DELETE",
                        "url": (
                            f"/api/collections/{collection['name']}/records/"
                            f"{record_id}"
                        ),
                    }
                ]
            },
        )
        assert delete_response.status_code == 200, delete_response.text
        assert not storage_dir.exists()
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_batch_reconciliation_preserves_file_readded_before_commit(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "batchreadd00001"
    record = await _create_record_with_file(app_client, collection, record_id)
    filename = str(record["document"])

    try:
        response = await app_client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "PATCH",
                        "url": (
                            f"/api/collections/{collection['name']}/records/"
                            f"{record_id}"
                        ),
                        "body": {"document": ""},
                    },
                    {
                        "method": "PATCH",
                        "url": (
                            f"/api/collections/{collection['name']}/records/"
                            f"{record_id}"
                        ),
                        "body": {"document": filename},
                    },
                ]
            },
        )

        assert response.status_code == 200, response.text
        fetched = await app_client.get(
            f"/api/collections/{collection['name']}/records/{record_id}"
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["document"] == filename
        assert file_storage.read_file_bytes(
            collection["id"], record_id, filename
        ) == b"original payload"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_batch_create_then_delete_removes_captured_upload(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "batchcreatedel1"
    payload = {
        "requests": [
            {
                "method": "POST",
                "url": f"/api/collections/{collection['name']}/records",
                "body": {"id": record_id, "title": "temporary"},
            },
            {
                "method": "DELETE",
                "url": (
                    f"/api/collections/{collection['name']}/records/{record_id}"
                ),
            },
        ]
    }

    try:
        response = await app_client.post(
            "/api/batch",
            data={"@jsonPayload": json.dumps(payload)},
            files={
                "requests.0.document": (
                    "temporary.txt",
                    b"temporary payload",
                    "text/plain",
                )
            },
        )

        assert response.status_code == 200, response.text
        storage_dir = file_storage.get_storage_path(collection["id"], record_id)
        assert not storage_dir.exists()
        fetched = await app_client.get(
            f"/api/collections/{collection['name']}/records/{record_id}"
        )
        assert fetched.status_code == 404
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )


async def test_batch_delete_then_recreate_preserves_only_final_upload(
    app_client: AsyncClient,
    admin_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)
    collection = await _create_public_file_collection(app_client, admin_token)
    record_id = "batchrecreate01"
    old_record = await _create_record_with_file(app_client, collection, record_id)
    old_filename = str(old_record["document"])
    payload = {
        "requests": [
            {
                "method": "DELETE",
                "url": (
                    f"/api/collections/{collection['name']}/records/{record_id}"
                ),
            },
            {
                "method": "POST",
                "url": f"/api/collections/{collection['name']}/records",
                "body": {"id": record_id, "title": "recreated"},
            },
        ]
    }

    try:
        response = await app_client.post(
            "/api/batch",
            data={"@jsonPayload": json.dumps(payload)},
            files={
                "requests.1.document": (
                    "new.txt",
                    b"new payload",
                    "text/plain",
                )
            },
        )

        assert response.status_code == 200, response.text
        recreated = response.json()[1]["body"]
        new_filename = str(recreated["document"])
        assert new_filename != old_filename
        storage_dir = file_storage.get_storage_path(collection["id"], record_id)
        assert sorted(path.name for path in storage_dir.iterdir()) == [new_filename]
        assert (storage_dir / new_filename).read_bytes() == b"new payload"
    finally:
        await app_client.delete(
            f"/api/collections/{collection['id']}",
            headers={"Authorization": admin_token},
        )
