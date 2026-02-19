from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ppbase import PPBase
from ppbase.ext.events import RecordRequestEvent
from ppbase.ext import record_repository as repo_module


@pytest.mark.asyncio
async def test_record_request_event_crud_helpers_use_event_defaults(monkeypatch) -> None:
    fake_engine = object()
    fake_collection = SimpleNamespace(id="posts_id", name="posts")
    calls: dict[str, object] = {}

    async def _resolve_collection(engine, id_or_name):
        calls["resolve"] = (engine, id_or_name)
        return fake_collection

    async def _create_record(engine, collection, data, files=None):
        calls["create"] = (engine, collection.name, dict(data), files)
        return {"id": "rec_created", **data}

    async def _get_record(engine, collection, record_id, fields=None):
        calls["get"] = (engine, collection.name, record_id, fields)
        return {"id": record_id}

    async def _list_records(
        engine,
        collection,
        *,
        page,
        per_page,
        sort,
        filter_str,
        fields,
        skip_total,
        request_context,
    ):
        calls["list"] = (
            engine,
            collection.name,
            page,
            per_page,
            sort,
            filter_str,
            fields,
            skip_total,
            request_context,
        )
        return {"items": []}

    async def _update_record(engine, collection, record_id, data, files=None):
        calls["update"] = (engine, collection.name, record_id, dict(data), files)
        return {"id": record_id, **data}

    async def _get_all_collections(engine):
        calls["get_all_collections"] = engine
        return [fake_collection]

    async def _delete_record(engine, collection, record_id, *, all_collections):
        calls["delete"] = (engine, collection.name, record_id, list(all_collections))
        return True

    monkeypatch.setattr(repo_module, "resolve_collection", _resolve_collection)
    monkeypatch.setattr(repo_module, "create_record", _create_record)
    monkeypatch.setattr(repo_module, "get_record", _get_record)
    monkeypatch.setattr(repo_module, "list_records", _list_records)
    monkeypatch.setattr(repo_module, "update_record", _update_record)
    monkeypatch.setattr(repo_module, "get_all_collections", _get_all_collections)
    monkeypatch.setattr(repo_module, "delete_record", _delete_record)

    event = RecordRequestEvent(
        collection_id_or_name="posts",
        record_id="rec1",
        page=2,
        per_page=25,
        sort="-created",
        filter="title != ''",
        fields="id,title",
        skip_total=True,
        engine=fake_engine,
    )

    created = await event.create({"title": "hello"})
    fetched = await event.get()
    listed = await event.list()
    updated = await event.update({"title": "changed"})
    deleted = await event.delete()

    assert created["id"] == "rec_created"
    assert fetched == {"id": "rec1"}
    assert listed == {"items": []}
    assert updated["title"] == "changed"
    assert deleted is True

    assert calls["resolve"] == (fake_engine, "posts")
    assert calls["create"] == (fake_engine, "posts", {"title": "hello"}, None)
    assert calls["get"] == (fake_engine, "posts", "rec1", "id,title")
    assert calls["list"] == (
        fake_engine,
        "posts",
        2,
        25,
        "-created",
        "title != ''",
        "id,title",
        True,
        None,
    )
    assert calls["update"] == (fake_engine, "posts", "rec1", {"title": "changed"}, None)
    assert calls["get_all_collections"] == fake_engine
    assert calls["delete"][0:3] == (fake_engine, "posts", "rec1")


@pytest.mark.asyncio
async def test_hook_event_can_fetch_current_user_from_auth_token(monkeypatch) -> None:
    fake_engine = object()
    fake_collection = SimpleNamespace(id="users_id", name="users")

    async def _resolve_collection(_engine, _id_or_name):
        return fake_collection

    async def _get_record(_engine, _collection, record_id, fields=None):
        return {"id": record_id, "email": "dev@example.com", "fields": fields}

    monkeypatch.setattr(repo_module, "resolve_collection", _resolve_collection)
    monkeypatch.setattr(repo_module, "get_record", _get_record)

    event = RecordRequestEvent(
        auth={
            "type": "authRecord",
            "id": "user_123",
            "collectionName": "users",
            "collectionId": "users_id",
        },
        engine=fake_engine,
    )

    user = await event.get_current_user(fields="id,email")
    assert user is not None
    assert user["id"] == "user_123"
    assert event.current_user_id() == "user_123"


@pytest.mark.asyncio
async def test_hook_event_current_user_is_none_for_non_auth_record() -> None:
    event = RecordRequestEvent(auth={"type": "admin", "id": "admin_1"})
    assert event.current_user_id() is None
    assert await event.get_current_user() is None


def test_hook_event_auth_helpers_include_superuser_checks() -> None:
    admin_event = RecordRequestEvent(auth={"type": "admin", "id": "admin_1"})
    assert admin_event.has_auth() is True
    assert admin_event.auth_type() == "admin"
    assert admin_event.auth_id() == "admin_1"
    assert admin_event.has_superuser_auth() is True
    assert admin_event.is_superuser() is True
    assert admin_event.has_record_auth() is False

    user_event = RecordRequestEvent(
        auth={
            "type": "authRecord",
            "id": "user_1",
            "collectionId": "users_id",
            "collectionName": "users",
        }
    )
    assert user_event.has_auth() is True
    assert user_event.auth_type() == "authRecord"
    assert user_event.auth_collection_id() == "users_id"
    assert user_event.auth_collection_name() == "users"
    assert user_event.has_record_auth() is True
    assert user_event.has_superuser_auth() is False
    assert user_event.is_superuser() is False

    superusers_event = RecordRequestEvent(
        auth={
            "type": "authRecord",
            "id": "su_1",
            "collectionName": "_superusers",
        }
    )
    assert superusers_event.has_superuser_auth() is True
    assert superusers_event.is_superuser() is True


def test_hook_event_require_helpers_and_identity_matching() -> None:
    event = RecordRequestEvent(
        collection_id_or_name="users",
        record_id="user_1",
        auth={
            "type": "authRecord",
            "id": "user_1",
            "collectionId": "users_id",
            "collectionName": "users",
        },
    )

    assert event.require_auth_record()["id"] == "user_1"
    assert event.is_auth_collection("users") is True
    assert event.is_auth_collection("users_id") is True
    assert event.is_same_auth_record("user_1") is True
    assert event.is_same_auth_record("user_1", "users") is True
    assert event.is_same_auth_record("user_1", "users_id") is True
    assert event.is_same_auth_record("user_2") is False
    assert event.is_same_auth_record("user_1", "posts") is False
    assert event.require_same_auth_record("user_1")["id"] == "user_1"
    assert event.require_same_auth_record("user_1", "users")["id"] == "user_1"


def test_hook_event_require_auth_record_rejects_invalid_context() -> None:
    missing = RecordRequestEvent(auth=None)
    with pytest.raises(HTTPException) as missing_exc:
        missing.require_auth_record()
    assert missing_exc.value.status_code == 401

    admin = RecordRequestEvent(auth={"type": "admin", "id": "admin_1"})
    with pytest.raises(HTTPException) as admin_exc:
        admin.require_auth_record()
    assert admin_exc.value.status_code == 403


def test_hook_event_require_superuser_checks_both_admin_and_superusers_collection() -> None:
    admin = RecordRequestEvent(auth={"type": "admin", "id": "admin_1"})
    assert admin.require_superuser()["id"] == "admin_1"

    su_record = RecordRequestEvent(
        auth={
            "type": "authRecord",
            "id": "su_1",
            "collectionName": "_superusers",
        }
    )
    assert su_record.require_superuser()["id"] == "su_1"

    user = RecordRequestEvent(
        auth={
            "type": "authRecord",
            "id": "user_1",
            "collectionName": "users",
        }
    )
    with pytest.raises(HTTPException) as user_exc:
        user.require_superuser()
    assert user_exc.value.status_code == 403


def test_hook_event_require_same_auth_record_rejects_different_identity() -> None:
    event = RecordRequestEvent(
        auth={
            "type": "authRecord",
            "id": "user_1",
            "collectionName": "users",
        }
    )
    with pytest.raises(HTTPException) as exc:
        event.require_same_auth_record("user_2", "users")
    assert exc.value.status_code == 403


def test_pb_records_factory_exposes_repository_style_accessor() -> None:
    app_pb = PPBase()
    repo = app_pb.records("posts")
    assert repo.collection_id_or_name == "posts"
