from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from ppbase.api import realtime as realtime_api
from ppbase.api.record_auth import _trigger_record_auth_hooks
from ppbase.ext.events import RecordAuthRequestEvent
from ppbase.ext.registry import (
    ExtensionRegistry,
    HOOK_REALTIME_CONNECT_REQUEST,
    HOOK_REALTIME_MESSAGE_SEND,
    HOOK_REALTIME_SUBSCRIBE_REQUEST,
    HOOK_RECORD_AUTH_REQUEST,
    HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
)
from ppbase.services.realtime_service import SubscriptionManager, broadcast_record_change

@pytest.mark.asyncio
async def test_record_auth_hooks_trigger_generic_and_specific() -> None:
    app = FastAPI()
    extensions = ExtensionRegistry()
    app.state.extension_registry = extensions

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth",
        "headers": [],
        "app": app,
    }
    request = Request(scope)
    event = RecordAuthRequestEvent(
        app=app,
        request=request,
        collection=SimpleNamespace(name="users", id="users_id"),
        collection_id_or_name="users",
        method="password",
        body={"identity": "u@example.com"},
    )

    calls: list[str] = []

    async def _generic(e):
        calls.append(f"generic:{e.method}")
        return await e.next()

    async def _specific(e):
        calls.append(f"specific:{e.collection.name}")
        return await e.next()

    async def _default(_: RecordAuthRequestEvent):
        calls.append("default")
        return "ok"

    extensions.hooks.get(HOOK_RECORD_AUTH_REQUEST).bind_func(_generic)
    extensions.hooks.get(HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST).bind_func(_specific)

    result = await _trigger_record_auth_hooks(
        request,
        HOOK_RECORD_AUTH_WITH_PASSWORD_REQUEST,
        event,
        _default,
    )

    assert result == "ok"
    assert calls == ["generic:password", "specific:users", "default"]


@pytest.mark.asyncio
async def test_realtime_connect_and_subscribe_hooks_are_triggered(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(realtime_api.router)
    extensions = ExtensionRegistry()
    manager = SubscriptionManager(extension_registry=extensions)
    app.state.extension_registry = extensions
    app.state.subscription_manager = manager
    app.dependency_overrides[realtime_api.get_optional_auth] = lambda: None

    connect_calls = 0
    subscribe_calls = 0

    async def _connect(e):
        nonlocal connect_calls
        connect_calls += 1
        return await e.next()

    async def _subscribe(e):
        nonlocal subscribe_calls
        subscribe_calls += 1
        return await e.next()

    extensions.hooks.get(HOOK_REALTIME_CONNECT_REQUEST).bind_func(_connect)
    extensions.hooks.get(HOOK_REALTIME_SUBSCRIBE_REQUEST).bind_func(_subscribe)

    async def _fake_resolve_collection(_engine, collection_name):
        return SimpleNamespace(id=f"{collection_name}_id", name=collection_name)

    monkeypatch.setattr(realtime_api, "get_engine", lambda: object())
    monkeypatch.setattr(realtime_api, "resolve_collection", _fake_resolve_collection)

    connect_request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/realtime",
            "headers": [],
            "app": app,
        }
    )
    connect_response = await realtime_api.realtime_connect(
        connect_request,
        subscription_manager=manager,
    )
    assert connect_response.status_code == 200
    assert connect_calls >= 1

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        client_id = manager.register_client()
        response = await client.post(
            "/api/realtime",
            json={
                "clientId": client_id,
                "subscriptions": ["posts/*"],
            },
        )
        assert response.status_code == 204, response.text

    assert subscribe_calls >= 1
    session = manager.get_session(client_id)
    assert session is not None
    assert len(session.subscriptions) == 1
    assert session.subscriptions[0].topic == "posts/*"


@pytest.mark.asyncio
async def test_realtime_message_send_hook_can_customize_payload() -> None:
    extensions = ExtensionRegistry()
    manager = SubscriptionManager(extension_registry=extensions)
    client_id = manager.register_client()
    await manager.add_subscription(client_id, "posts/*")
    session = manager.get_session(client_id)
    assert session is not None

    async def _decorate_message(e):
        e.data["hooked"] = True
        return await e.next()

    extensions.hooks.get(HOOK_REALTIME_MESSAGE_SEND).bind_func(_decorate_message)

    await broadcast_record_change(
        manager,
        "posts",
        "rec1",
        "create",
        {"id": "rec1", "title": "hello"},
    )

    event = await asyncio.wait_for(session.response_queue.get(), timeout=1.0)
    assert event["topic"] == "posts/*"
    assert event["data"]["hooked"] is True
