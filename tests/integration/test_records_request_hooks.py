from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ppbase.api import records as records_api
from ppbase.ext.registry import ExtensionRegistry, HOOK_RECORD_CREATE_REQUEST


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
async def test_record_create_hook_exception_rolls_back(monkeypatch) -> None:
    extensions = ExtensionRegistry()
    fake_engine = _FakeEngine()
    writes = 0

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
        return {
            "id": "rec2",
            "collectionId": "posts_id",
            "collectionName": "posts",
            "title": payload.get("title"),
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
                json={"title": "will-rollback"},
            )

    assert writes == 1
    assert fake_engine.last_exc_type is RuntimeError
