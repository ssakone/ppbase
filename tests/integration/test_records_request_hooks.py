from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from ppbase import PPBase
from ppbase.api import records as records_api
from ppbase.config import Settings
from ppbase.ext.registry import (
    ExtensionRegistry,
    HOOK_RECORD_CREATE_REQUEST,
    HOOK_RECORD_DELETE_REQUEST,
    HOOK_RECORD_UPDATE_REQUEST,
)
from ppbase.services import file_storage


class _FakeBeginContext:
    def __init__(self, engine: "_FakeEngine"):
        self._engine = engine

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        self._engine.last_exc_type = exc_type
        return False


class _FakeEngine:
    def __init__(self):
        self.last_exc_type = None

    def begin(self):
        return _FakeBeginContext(self)


def _build_records_app(extensions: ExtensionRegistry) -> FastAPI:
    app = FastAPI()
    app.include_router(records_api.router)
    app.state.extension_registry = extensions
    app.dependency_overrides[records_api.get_optional_auth] = lambda: None
    return app


@pytest.mark.asyncio
async def test_record_create_hook_can_mutate_payload(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    created_payloads: list[dict[str, object]] = []

    async def _set_default_title(e):
        e.data.setdefault("title", "set-by-hook")
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        created_payloads.append(dict(payload))
        return {
            "id": "rec1",
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
        }

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_set_default_title)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/api/collections/posts/records", json={})

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "set-by-hook"
    assert created_payloads and created_payloads[0]["title"] == "set-by-hook"


@pytest.mark.asyncio
async def test_record_create_route_decodes_multipart_json_payload(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    created_payloads: list[dict[str, object]] = []
    created_files: list[dict[str, list[tuple[str, bytes]]]] = []
    hook_payloads: list[dict[str, object]] = []

    async def _capture_hook(e):
        hook_payloads.append(dict(e.data))
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        created_payloads.append(dict(payload))
        created_files.append(dict(files or {}))
        return {
            "id": "rec_multipart",
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
            "count": payload.get("count"),
        }

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_capture_hook)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    payload = json.dumps({"title": "json-title", "count": 2})
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/collections/posts/records",
            files={
                "title": (None, "form-title"),
                "@jsonPayload": (None, payload),
                "attachment": ("hello.txt", b"hello", "text/plain"),
            },
        )

    assert response.status_code == 200, response.text
    assert hook_payloads == [{"title": "json-title", "count": 2}]
    assert created_payloads == [{"title": "json-title", "count": 2}]
    assert "@jsonPayload" not in created_payloads[0]
    assert created_files == [{"attachment": [("hello.txt", b"hello")]}]


@pytest.mark.asyncio
async def test_record_update_route_decodes_multipart_json_payload(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    updated_payloads: list[dict[str, object]] = []
    updated_files: list[dict[str, list[tuple[str, bytes]]]] = []
    hook_payloads: list[dict[str, object]] = []

    async def _capture_hook(e):
        hook_payloads.append(dict(e.data))
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            update_rule="",
            options={},
        )

    async def _fake_update_record(_engine, _collection, record_id, payload, files=None):
        updated_payloads.append(dict(payload))
        updated_files.append(dict(files or {}))
        return {
            "id": record_id,
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
            "enabled": payload.get("enabled"),
        }

    extensions.hooks.get(HOOK_RECORD_UPDATE_REQUEST).bind_func(_capture_hook)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "update_record", _fake_update_record)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    payload = json.dumps({"title": "json-title", "enabled": True})
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.patch(
            "/api/collections/posts/records/rec1",
            files={
                "title": (None, "form-title"),
                "@jsonPayload": (None, payload),
                "attachment": ("hello.txt", b"hello", "text/plain"),
            },
        )

    assert response.status_code == 200, response.text
    assert hook_payloads == [{"title": "json-title", "enabled": True}]
    assert updated_payloads == [{"title": "json-title", "enabled": True}]
    assert "@jsonPayload" not in updated_payloads[0]
    assert updated_files == [{"attachment": [("hello.txt", b"hello")]}]


@pytest.mark.asyncio
async def test_record_create_hook_exception_rolls_back(
    monkeypatch,
    tmp_path,
) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    writes = 0
    data_dir = tmp_path / "data"
    monkeypatch.setattr(
        file_storage,
        "_settings",
        Settings(data_dir=str(data_dir), storage_backend="local"),
    )
    monkeypatch.setattr(file_storage, "_runtime_storage_overrides", None)

    async def _explode_after_next(e):
        await e.next()
        raise RuntimeError("hook exploded")

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        nonlocal writes
        writes += 1
        saved = file_storage.save_files(
            "posts_id",
            "rec2",
            "attachment",
            (files or {}).get("attachment", []),
        )
        return {
            "id": "rec2",
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
            "attachment": saved[0] if saved else "",
        }

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_explode_after_next)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        with pytest.raises(RuntimeError, match="hook exploded"):
            await client.post(
                "/api/collections/posts/records",
                data={"title": "will-rollback"},
                files={
                    "attachment": (
                        "rollback.txt",
                        b"must be removed",
                        "text/plain",
                    )
                },
            )

    assert writes == 1
    assert fake_engine.last_exc_type is RuntimeError
    assert not file_storage.get_storage_path("posts_id", "rec2").exists()


@pytest.mark.asyncio
async def test_record_create_hook_can_apply_builtin_auth_middleware(
    monkeypatch,
) -> None:
    app_pb = PPBase()
    fake_engine = _FakeEngine()
    called = False

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="templates_id",
            name="templates",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        return {
            "id": "rec_auth",
            "collectionId": "templates_id",
            "collectionName": "templates",
            "title": payload.get("title"),
        }

    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)

    @app_pb.on_record_create_request(
        "templates",
        middleware=app_pb.apis.require_auth(),
    )
    async def _hook(event):
        nonlocal called
        called = True
        return await event.next()

    app = _build_records_app(app_pb._extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/collections/templates/records", json={"title": "blocked"}
        )

    assert response.status_code == 401
    assert (
        response.json()["detail"]["message"] == "The request requires authentication."
    )
    assert called is False


@pytest.mark.asyncio
async def test_record_create_hook_supports_multiple_middlewares(monkeypatch) -> None:
    app_pb = PPBase()
    fake_engine = _FakeEngine()
    calls: list[str] = []

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="templates_id",
            name="templates",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        calls.append("default")
        return {
            "id": "rec_multi",
            "collectionId": "templates_id",
            "collectionName": "templates",
            "title": payload.get("title"),
        }

    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)

    async def first(event):
        calls.append("mw:first")
        return await event.next()

    async def second(event):
        calls.append("mw:second")
        return await event.next()

    @app_pb.on_record_create_request(
        "templates",
        middleware=[first, second],
    )
    async def _hook(event):
        calls.append("hook")
        return await event.next()

    app = _build_records_app(app_pb._extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/collections/templates/records", json={"title": "ok"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["title"] == "ok"
    assert calls == ["mw:first", "mw:second", "hook", "default"]


@pytest.mark.asyncio
async def test_batch_create_triggers_record_create_hook(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    created_payloads: list[dict[str, object]] = []

    async def _set_batch_defaults(e):
        e.data.setdefault("title", "set-by-hook")
        e.data["status"] = True
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        created_payloads.append(dict(payload))
        return {
            "id": "rec_batch",
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
            "status": payload.get("status"),
        }

    async def _fake_get_all_collections(_engine):
        return []

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_set_batch_defaults)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)
    monkeypatch.setattr(records_api, "get_all_collections", _fake_get_all_collections)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "POST",
                        "url": "/api/collections/posts/records",
                        "body": {},
                    }
                ]
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "status": 200,
            "body": {
                "id": "rec_batch",
                "collectionId": "posts_id",
                "collectionName": "posts",
                "title": "set-by-hook",
                "status": True,
            },
        }
    ]
    assert created_payloads == [{"title": "set-by-hook", "status": True}]


@pytest.mark.asyncio
async def test_batch_create_exposes_subrequest_headers_and_context(
    monkeypatch,
) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    seen_request: list[dict[str, object]] = []
    seen_rule_contexts: list[dict[str, object]] = []
    created_payloads: list[dict[str, object]] = []

    async def _capture_subrequest(e):
        seen_request.append(
            {
                "business_id": e.request.headers.get("business_id"),
                "authorization": e.request.headers.get("authorization"),
                "method": e.request.method,
                "query": dict(e.request.query_params),
                "path": e.request.url.path,
            }
        )
        e.data["business"] = e.request.headers.get("business_id")
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule='@request.context = "batch"',
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        created_payloads.append(dict(payload))
        return {
            "id": "rec_batch",
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
            "business": payload.get("business"),
        }

    async def _fake_check_record_rule(
        _engine,
        _collection,
        _record_id,
        _rule,
        request_context,
    ):
        seen_rule_contexts.append(dict(request_context))
        return True

    async def _fake_get_all_collections(_engine):
        return []

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_capture_subrequest)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)
    monkeypatch.setattr(records_api, "check_record_rule", _fake_check_record_rule)
    monkeypatch.setattr(records_api, "get_all_collections", _fake_get_all_collections)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/batch",
            headers={"Authorization": "Bearer outer", "business_id": "outer"},
            json={
                "requests": [
                    {
                        "method": "POST",
                        "url": "/api/collections/posts/records?source=batch",
                        "headers": {
                            "business_id": "biz-inner",
                            "Authorization": "Bearer inner",
                        },
                        "body": {"title": "from-batch"},
                    }
                ]
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()[0]["body"]["business"] == "biz-inner"
    assert seen_request == [
        {
            "business_id": "biz-inner",
            "authorization": "Bearer outer",
            "method": "POST",
            "query": {"source": "batch"},
            "path": "/api/collections/posts/records",
        }
    ]
    assert seen_rule_contexts
    assert seen_rule_contexts[0]["context"] == "batch"
    assert seen_rule_contexts[0]["headers"]["business_id"] == "biz-inner"
    assert seen_rule_contexts[0]["headers"]["authorization"] == "Bearer outer"
    assert seen_rule_contexts[0]["method"] == "POST"
    assert seen_rule_contexts[0]["query"] == {"source": "batch"}
    assert created_payloads == [{"title": "from-batch", "business": "biz-inner"}]


@pytest.mark.asyncio
async def test_batch_upsert_rewrites_subrequest_for_record_hooks(
    monkeypatch,
) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    calls: list[str] = []

    async def _create_hook(e):
        calls.append(f"create:{e.request.method}:{e.request.url.path}")
        return await e.next()

    async def _update_hook(e):
        calls.append(f"update:{e.request.method}:{e.request.url.path}:{e.record_id}")
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule="",
            update_rule="",
            options={},
        )

    async def _fake_get_record(_engine, _collection, record_id, **_kwargs):
        if record_id == "existing_rec":
            return {"id": record_id, "title": "existing"}
        return None

    async def _fake_create_record(_engine, _collection, payload, files=None):
        return {
            "id": payload.get("id"),
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
        }

    async def _fake_update_record(_engine, _collection, record_id, payload, files=None):
        return {
            "id": record_id,
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
        }

    async def _fake_get_all_collections(_engine):
        return []

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_create_hook)
    extensions.hooks.get(HOOK_RECORD_UPDATE_REQUEST).bind_func(_update_hook)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "get_record", _fake_get_record)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)
    monkeypatch.setattr(records_api, "update_record", _fake_update_record)
    monkeypatch.setattr(records_api, "get_all_collections", _fake_get_all_collections)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "PUT",
                        "url": "/api/collections/posts/records?source=upsert",
                        "body": {"id": "new_rec", "title": "created"},
                    },
                    {
                        "method": "PUT",
                        "url": "/api/collections/posts/records?source=upsert",
                        "body": {"id": "existing_rec", "title": "updated"},
                    },
                ]
            },
        )

    assert response.status_code == 200, response.text
    assert [item["status"] for item in response.json()] == [200, 200]
    assert calls == [
        "create:POST:/api/collections/posts/records",
        "update:PATCH:/api/collections/posts/records/existing_rec:existing_rec",
    ]


@pytest.mark.asyncio
async def test_batch_create_hook_rejection_rolls_back_transaction(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    writes = 0

    async def _reject_batch_create(e):
        raise HTTPException(
            status_code=400,
            detail={
                "status": 400,
                "message": "Rejected by hook.",
                "data": {"title": {"code": "hook_rejected", "message": "Nope."}},
            },
        )

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            create_rule="",
            options={},
        )

    async def _fake_create_record(_engine, _collection, payload, files=None):
        nonlocal writes
        writes += 1
        return {"id": "should_not_commit"}

    async def _fake_get_all_collections(_engine):
        return []

    extensions.hooks.get(HOOK_RECORD_CREATE_REQUEST).bind_func(_reject_batch_create)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "create_record", _fake_create_record)
    monkeypatch.setattr(records_api, "get_all_collections", _fake_get_all_collections)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "POST",
                        "url": "/api/collections/posts/records",
                        "body": {"title": "blocked"},
                    }
                ]
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert body["message"] == "Batch transaction failed."
    assert body["data"]["requests"]["0"]["response"]["message"] == "Rejected by hook."
    assert writes == 0
    assert fake_engine.last_exc_type is records_api._BatchRequestFailed


@pytest.mark.asyncio
async def test_batch_update_and_delete_trigger_record_hooks(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    calls: list[str] = []
    updated_payloads: list[dict[str, object]] = []
    deleted_ids: list[str] = []

    async def _update_hook(e):
        calls.append(f"update:{e.record_id}")
        e.data["updatedByHook"] = True
        return await e.next()

    async def _delete_hook(e):
        calls.append(f"delete:{e.record_id}")
        return await e.next()

    async def _fake_resolve_collection(_engine, _collection):
        return SimpleNamespace(
            id="posts_id",
            name="posts",
            type="base",
            update_rule="",
            delete_rule="",
            options={},
        )

    async def _fake_update_record(_engine, _collection, record_id, payload, files=None):
        updated_payloads.append(dict(payload))
        return {
            "id": record_id,
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
            "updatedByHook": payload.get("updatedByHook"),
        }

    async def _fake_delete_record(
        _engine, _collection, record_id, all_collections=None
    ):
        deleted_ids.append(record_id)
        return True

    async def _fake_get_all_collections(_engine):
        return []

    extensions.hooks.get(HOOK_RECORD_UPDATE_REQUEST).bind_func(_update_hook)
    extensions.hooks.get(HOOK_RECORD_DELETE_REQUEST).bind_func(_delete_hook)
    monkeypatch.setattr(records_api, "get_engine", lambda: fake_engine)
    monkeypatch.setattr(records_api, "resolve_collection", _fake_resolve_collection)
    monkeypatch.setattr(records_api, "update_record", _fake_update_record)
    monkeypatch.setattr(records_api, "delete_record", _fake_delete_record)
    monkeypatch.setattr(records_api, "get_all_collections", _fake_get_all_collections)

    app = _build_records_app(extensions)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/batch",
            json={
                "requests": [
                    {
                        "method": "PATCH",
                        "url": "/api/collections/posts/records/rec1",
                        "body": {"title": "patched"},
                    },
                    {
                        "method": "DELETE",
                        "url": "/api/collections/posts/records/rec1",
                    },
                ]
            },
        )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "status": 200,
            "body": {
                "id": "rec1",
                "collectionId": "posts_id",
                "collectionName": "posts",
                "title": "patched",
                "updatedByHook": True,
            },
        },
        {"status": 204, "body": None},
    ]
    assert calls == ["update:rec1", "delete:rec1"]
    assert updated_payloads == [{"title": "patched", "updatedByHook": True}]
    assert deleted_ids == ["rec1"]
